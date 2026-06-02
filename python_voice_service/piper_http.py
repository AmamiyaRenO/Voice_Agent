from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import subprocess
import tempfile
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("piper_http")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    try:
        yield
    finally:
        await _shutdown_persistent_workers()


app = FastAPI(title="Piper TTS Wrapper", lifespan=_lifespan)


class TtsRequest(BaseModel):
    text: str = Field(..., min_length=1)
    model: str | None = None
    config: str | None = None


class TtsResponse(BaseModel):
    audio_wav_base64: str
    sample_rate: int


def _env(key: str, default: str = "") -> str:
    v = os.getenv(key)
    return v.strip() if v else default


def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    n = v.strip().lower()
    if n in {"1", "true", "yes", "on"}:
        return True
    if n in {"0", "false", "no", "off"}:
        return False
    return default


def _infer_config_for_model(model_path: str) -> str | None:
    path = Path(model_path)
    candidates = []
    if path.suffix:
        candidates.append(path.with_suffix(".onnx.json"))
        candidates.append(path.with_suffix(".json"))
    candidates.append(Path(str(model_path) + ".json"))
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _resolve_synthesis_settings(
    model_override: str | None = None,
    config_override: str | None = None,
    speaker_override: str | None = None,
) -> tuple[str, str, str | None, str | None]:
    exe = _env("PIPER_EXECUTABLE", "piper")
    model = (model_override or "").strip()
    if not model:
        model = _env("PIPER_MODEL_PATH")
    if not model:
        raise HTTPException(status_code=500, detail="PIPER_MODEL_PATH is not configured")
    model_path = str(Path(os.path.expandvars(model)).expanduser())
    if not Path(model_path).exists():
        raise HTTPException(status_code=500, detail=f"Piper model not found: {model_path}")
    cfg = (config_override or "").strip()
    if not cfg:
        cfg = _env("PIPER_CONFIG_PATH")
    if not cfg and model_override:
        inferred = _infer_config_for_model(model_path)
        if inferred:
            cfg = inferred
    speaker = _normalize_speaker_arg(speaker_override)
    if not speaker:
        speaker = _normalize_speaker_arg(_env("PIPER_SPEAKER"))
    return exe, model_path, cfg or None, speaker


def _normalize_speaker_arg(speaker_raw: str | None) -> str | None:
    speaker = (speaker_raw or "").strip()
    if not speaker:
        return None
    if speaker.isdigit():
        return speaker
    # Most Piper builds expect numeric speaker ids. Ignore string labels by default.
    if _env_bool("PIPER_ALLOW_STRING_SPEAKER", False):
        return speaker
    logger.info("Ignoring non-numeric Piper speaker override: %r", speaker)
    return None


def _build_command(
    out_path: Path | None,
    model_override: str | None = None,
    config_override: str | None = None,
    speaker_override: str | None = None,
    raw_output: bool = False,
) -> list[str]:
    exe, model_path, cfg, speaker = _resolve_synthesis_settings(
        model_override=model_override,
        config_override=config_override,
        speaker_override=speaker_override,
    )
    cmd = [exe, "--model", model_path]
    if raw_output:
        cmd += ["--output_raw"]
    else:
        if out_path is None:
            raise HTTPException(status_code=500, detail="Output path is required for non-streaming mode")
        cmd += ["--output_file", str(out_path)]
    if cfg:
        cmd += ["--config", cfg]
    if speaker:
        cmd += ["--speaker", speaker]
    return cmd


def _run_piper_subprocess(
    text: str,
    model_override: str | None = None,
    config_override: str | None = None,
    speaker_override: str | None = None,
) -> bytes:
    with tempfile.TemporaryDirectory(prefix="piper-http-") as tmp:
        out_path = Path(tmp) / "out.wav"
        cmd = _build_command(
            out_path,
            model_override=model_override,
            config_override=config_override,
            speaker_override=speaker_override,
            raw_output=False,
        )
        try:
            completed = subprocess.run(
                cmd,
                input=f"{text}\n",
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to launch Piper: {exc}") from exc

        if completed.returncode != 0:
            raise HTTPException(status_code=500, detail=completed.stderr.strip())
        if not out_path.exists():
            raise HTTPException(status_code=500, detail="Piper did not produce output")

        return out_path.read_bytes()


async def _stream_piper_raw(
    text: str,
    model_override: str | None = None,
    config_override: str | None = None,
    speaker_override: str | None = None,
    chunk_bytes: int = 4096,
) -> AsyncIterator[bytes]:
    cmd = _build_command(
        out_path=None,
        model_override=model_override,
        config_override=config_override,
        speaker_override=speaker_override,
        raw_output=True,
    )
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    assert process.stdin is not None
    assert process.stdout is not None

    try:
        process.stdin.write((text + "\n").encode("utf-8", errors="replace"))
        await process.stdin.drain()
        process.stdin.close()

        while True:
            chunk = await process.stdout.read(max(512, int(chunk_bytes)))
            if not chunk:
                break
            yield chunk

        stderr_bytes = b""
        if process.stderr is not None:
            stderr_bytes = await process.stderr.read()
        return_code = await process.wait()
        if return_code != 0:
            detail = stderr_bytes.decode("utf-8", errors="replace").strip() or f"piper exited with code {return_code}"
            if return_code in {3221226505, -1073740791}:
                detail = (
                    f"{detail} (Windows STATUS_STACK_BUFFER_OVERRUN). "
                    "Try non-streaming fallback and avoid invalid --speaker values."
                )
            raise RuntimeError(detail)
    finally:
        try:
            if process.returncode is None:
                process.kill()
        except Exception:
            pass


class _PersistentPiperWorker:
    def __init__(self, *, exe: str, model_path: str, config_path: str | None, speaker: str | None) -> None:
        self.exe = exe
        self.model_path = model_path
        self.config_path = config_path
        self.speaker = speaker
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._stderr_buffer: list[str] = []
        self._stderr_task: asyncio.Task[None] | None = None

    async def _ensure_process(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return
        cmd = [self.exe, "--model", self.model_path, "--json-input"]
        if self.config_path:
            cmd += ["--config", self.config_path]
        if self.speaker:
            cmd += ["--speaker", self.speaker]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._process = process
        self._stderr_buffer = []
        self._stderr_task = asyncio.create_task(self._drain_stderr(process))

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        if process.stderr is None:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    self._stderr_buffer.append(text)
                    while len(self._stderr_buffer) > 24:
                        del self._stderr_buffer[0]
        except Exception:
            return

    def _stderr_summary(self) -> str:
        return " | ".join(self._stderr_buffer[-6:]).strip()

    async def _close_process(self) -> None:
        process = self._process
        self._process = None
        task = self._stderr_task
        self._stderr_task = None
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except Exception:
                pass
            try:
                await process.wait()
            except Exception:
                pass
        if task is not None:
            task.cancel()
            try:
                await task
            except Exception:
                pass

    async def synthesize_wav(self, text: str) -> bytes:
        async with self._lock:
            await self._ensure_process()
            process = self._process
            if process is None or process.stdin is None:
                raise RuntimeError("persistent Piper worker is not available")
            with tempfile.TemporaryDirectory(prefix="piper-http-worker-") as tmp:
                out_path = Path(tmp) / "out.wav"
                payload = {"text": text, "output_file": str(out_path)}
                try:
                    process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8", errors="replace"))
                    await process.stdin.drain()
                except Exception:
                    await self._close_process()
                    raise RuntimeError("failed to write to persistent Piper worker")

                deadline = asyncio.get_running_loop().time() + max(15.0, min(90.0, 4.0 + len(text) * 0.12))
                last_size = -1
                stable_passes = 0
                while asyncio.get_running_loop().time() < deadline:
                    if process.returncode is not None:
                        detail = self._stderr_summary() or f"piper exited with code {process.returncode}"
                        await self._close_process()
                        raise RuntimeError(detail)
                    if out_path.exists():
                        try:
                            size = out_path.stat().st_size
                        except OSError:
                            size = -1
                        if size > 44 and size == last_size:
                            stable_passes += 1
                        else:
                            stable_passes = 0
                        last_size = size
                        if stable_passes >= 2:
                            try:
                                wav_bytes = out_path.read_bytes()
                                with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                                    if wf.getnframes() > 0:
                                        return wav_bytes
                            except Exception:
                                pass
                    await asyncio.sleep(0.05)
                detail = self._stderr_summary() or "persistent Piper worker timed out"
                await self._close_process()
                raise RuntimeError(detail)


_PERSISTENT_WORKERS: dict[tuple[str, str, str | None, str | None], _PersistentPiperWorker] = {}
_PERSISTENT_WORKERS_LOCK = asyncio.Lock()


def _persistent_enabled() -> bool:
    return _env_bool("PIPER_PERSISTENT_WORKER", True)


async def _persistent_worker_for(
    model_override: str | None = None,
    config_override: str | None = None,
    speaker_override: str | None = None,
) -> _PersistentPiperWorker:
    exe, model_path, config_path, speaker = _resolve_synthesis_settings(
        model_override=model_override,
        config_override=config_override,
        speaker_override=speaker_override,
    )
    key = (exe, model_path, config_path, speaker)
    async with _PERSISTENT_WORKERS_LOCK:
        worker = _PERSISTENT_WORKERS.get(key)
        if worker is None:
            worker = _PersistentPiperWorker(
                exe=exe,
                model_path=model_path,
                config_path=config_path,
                speaker=speaker,
            )
            _PERSISTENT_WORKERS[key] = worker
        return worker


async def _shutdown_persistent_workers() -> None:
    async with _PERSISTENT_WORKERS_LOCK:
        workers = list(_PERSISTENT_WORKERS.values())
        _PERSISTENT_WORKERS.clear()
    for worker in workers:
        try:
            await worker._close_process()
        except Exception:
            pass


# Gate synthesis concurrency to avoid spawning multiple heavy Piper processes simultaneously.
_SYNTH_SEM = asyncio.Semaphore(max(1, int(os.getenv("PIPER_MAX_CONCURRENCY", "1") or "1")))


def _wav_to_pcm16_mono(wav_bytes: bytes) -> bytes:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        if channels != 1 or sample_width != 2:
            raise RuntimeError(
                f"Expected mono s16 WAV from Piper fallback, got channels={channels} sample_width={sample_width}"
            )
        return wf.readframes(wf.getnframes())


def _wav_sample_rate(wav_bytes: bytes, fallback: int) -> int:
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            return int(wf.getframerate())
    except Exception:
        return int(fallback)


async def _synthesize_audio(
    text: str,
    model_override: str | None = None,
    config_override: str | None = None,
    speaker_override: str | None = None,
) -> tuple[bytes, int]:
    fallback_sample_rate = int(_env("PIPER_SAMPLE_RATE", "22050"))
    async with _SYNTH_SEM:
        if _persistent_enabled():
            worker = await _persistent_worker_for(
                model_override=model_override,
                config_override=config_override,
                speaker_override=speaker_override,
            )
            audio_bytes = await worker.synthesize_wav(text)
        else:
            audio_bytes = await asyncio.to_thread(
                _run_piper_subprocess,
                text,
                model_override,
                config_override,
                speaker_override,
            )
    return audio_bytes, _wav_sample_rate(audio_bytes, fallback_sample_rate)


@app.post("/speak", response_model=TtsResponse)
async def speak(payload: TtsRequest) -> TtsResponse:
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty text")

    model_override = payload.model.strip() if payload.model else None
    config_override = payload.config.strip() if payload.config else None
    audio_bytes, sample_rate = await _synthesize_audio(text, model_override, config_override)
    audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
    return TtsResponse(audio_wav_base64=audio_b64, sample_rate=sample_rate)


@app.get("/speak")
async def speak_get(text: str, model: str | None = None, config: str | None = None) -> Response:
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty text")

    model_override = model.strip() if model else None
    config_override = config.strip() if config else None
    audio_bytes, _ = await _synthesize_audio(text, model_override, config_override)
    return Response(content=audio_bytes, media_type="audio/wav")


@app.get("/speak_stream")
async def speak_stream(
    text: str,
    model: str | None = None,
    config: str | None = None,
    speaker: str | None = None,
    voice: str | None = None,
    instruct: str | None = None,
) -> StreamingResponse:
    _ = instruct  # not used by Piper, kept for compatibility with callers
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty text")

    model_override = model.strip() if model else None
    config_override = config.strip() if config else None
    speaker_override = (speaker or "").strip() or (voice or "").strip() or None
    sample_rate = int(_env("PIPER_SAMPLE_RATE", "22050"))
    headers = {
        "Cache-Control": "no-store",
        "X-Audio-Format": "s16le",
        "X-Audio-Sample-Rate": str(sample_rate),
        "X-Audio-Channels": "1",
    }

    async def _iter() -> AsyncIterator[bytes]:
        if _persistent_enabled() and not _env_bool("PIPER_STREAM_FORCE_DIRECT", False):
            try:
                wav_bytes, _ = await _synthesize_audio(
                    text=text,
                    model_override=model_override,
                    config_override=config_override,
                    speaker_override=speaker_override,
                )
                pcm_bytes = _wav_to_pcm16_mono(wav_bytes)
                chunk_size = 4096
                for offset in range(0, len(pcm_bytes), chunk_size):
                    yield pcm_bytes[offset : offset + chunk_size]
                return
            except Exception as exc:
                logger.warning("Persistent Piper stream synth failed (%s); trying direct raw stream", exc)

        stream_started = False
        try:
            async with _SYNTH_SEM:
                async for chunk in _stream_piper_raw(
                    text=text,
                    model_override=model_override,
                    config_override=config_override,
                    speaker_override=speaker_override,
                ):
                    stream_started = True
                    yield chunk
            return
        except Exception as exc:
            # Graceful downgrade: if real streaming fails, synthesize WAV and stream PCM bytes.
            # This avoids ASGI exception groups and keeps caller behavior stable.
            logger.warning("Piper /speak_stream failed (%s); falling back to non-stream synth", exc)
            if stream_started:
                # Stream already emitted bytes; cannot safely switch protocol mid-flight.
                return

        wav_bytes, _ = await _synthesize_audio(
            text=text,
            model_override=model_override,
            config_override=config_override,
            speaker_override=speaker_override,
        )
        pcm_bytes = _wav_to_pcm16_mono(wav_bytes)
        chunk_size = 4096
        for offset in range(0, len(pcm_bytes), chunk_size):
            yield pcm_bytes[offset : offset + chunk_size]

    return StreamingResponse(
        _iter(),
        media_type=f"audio/L16;rate={sample_rate};channels=1",
        headers=headers,
    )
