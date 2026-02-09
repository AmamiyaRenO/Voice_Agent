from __future__ import annotations

import asyncio
import base64
import os
import subprocess
import tempfile
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Piper TTS Wrapper")


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


def _build_command(
    out_path: Path | None,
    model_override: str | None = None,
    config_override: str | None = None,
    speaker_override: str | None = None,
    raw_output: bool = False,
) -> list[str]:
    exe = _env("PIPER_EXECUTABLE", "piper")
    model = (model_override or "").strip()
    if not model:
        model = _env("PIPER_MODEL_PATH")
    if not model:
        raise HTTPException(status_code=500, detail="PIPER_MODEL_PATH is not configured")
    model_path = str(Path(os.path.expandvars(model)).expanduser())
    if not Path(model_path).exists():
        raise HTTPException(status_code=500, detail=f"Piper model not found: {model_path}")
    cmd = [exe, "--model", model_path]
    if raw_output:
        cmd += ["--output_raw"]
    else:
        if out_path is None:
            raise HTTPException(status_code=500, detail="Output path is required for non-streaming mode")
        cmd += ["--output_file", str(out_path)]
    cfg = (config_override or "").strip()
    if not cfg:
        cfg = _env("PIPER_CONFIG_PATH")
    if not cfg and model_override:
        inferred = _infer_config_for_model(model_path)
        if inferred:
            cfg = inferred
    if cfg:
        cmd += ["--config", cfg]
    speaker = (speaker_override or "").strip()
    if not speaker:
        speaker = _env("PIPER_SPEAKER")
    if speaker:
        cmd += ["--speaker", speaker]
    return cmd


def _run_piper_subprocess(text: str, model_override: str | None = None, config_override: str | None = None) -> bytes:
    with tempfile.TemporaryDirectory(prefix="piper-http-") as tmp:
        out_path = Path(tmp) / "out.wav"
        cmd = _build_command(
            out_path,
            model_override=model_override,
            config_override=config_override,
            speaker_override=None,
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
            raise RuntimeError(detail)
    finally:
        try:
            if process.returncode is None:
                process.kill()
        except Exception:
            pass


# Gate synthesis concurrency to avoid spawning multiple heavy Piper processes simultaneously.
_SYNTH_SEM = asyncio.Semaphore(max(1, int(os.getenv("PIPER_MAX_CONCURRENCY", "1") or "1")))


async def _synthesize_audio(text: str, model_override: str | None = None, config_override: str | None = None) -> tuple[bytes, int]:
    sample_rate = int(_env("PIPER_SAMPLE_RATE", "22050"))
    async with _SYNTH_SEM:
        audio_bytes = await asyncio.to_thread(_run_piper_subprocess, text, model_override, config_override)
    return audio_bytes, sample_rate


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
        async with _SYNTH_SEM:
            async for chunk in _stream_piper_raw(
                text=text,
                model_override=model_override,
                config_override=config_override,
                speaker_override=speaker_override,
            ):
                yield chunk

    return StreamingResponse(
        _iter(),
        media_type=f"audio/L16;rate={sample_rate};channels=1",
        headers=headers,
    )
