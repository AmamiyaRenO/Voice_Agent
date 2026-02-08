from __future__ import annotations

import asyncio
import base64
import io
import os
import wave
from collections import OrderedDict
from typing import Optional, Tuple

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

app = FastAPI(title="Qwen3-TTS HTTP Wrapper")


class TtsRequest(BaseModel):
    text: str = Field(..., min_length=1)
    speaker: Optional[str] = None
    voice: Optional[str] = None  # compatibility alias
    instruct: Optional[str] = None
    language: Optional[str] = None


class TtsResponse(BaseModel):
    audio_wav_base64: str
    sample_rate: int


def _env(key: str, default: str = "") -> str:
    v = os.getenv(key)
    return v.strip() if v else default


def _pick_speaker(speaker: Optional[str], voice: Optional[str]) -> str:
    # Prefer explicit speaker, then voice alias, then env default.
    s = (speaker or "").strip() or (voice or "").strip()
    if s:
        return s
    return _env("QWEN_TTS_SPEAKER", "").strip()


def _pick_instruct(instruct: Optional[str]) -> str:
    s = (instruct or "").strip()
    if s:
        return s
    return _env("QWEN_TTS_INSTRUCT", "").strip()


def _pick_language(language: Optional[str]) -> str:
    s = (language or "").strip()
    if s:
        return s
    return _env("QWEN_TTS_LANGUAGE", "English").strip()


def _to_wav_bytes(mono_float: np.ndarray, sample_rate: int) -> bytes:
    if mono_float.ndim != 1:
        mono_float = mono_float.reshape(-1)
    if mono_float.size == 0:
        raise HTTPException(status_code=500, detail="TTS returned empty audio")

    x = np.asarray(mono_float, dtype=np.float32)
    x = np.clip(x, -1.0, 1.0)
    pcm16 = (x * 32767.0).astype(np.int16)

    with io.BytesIO() as bio:
        with wave.open(bio, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(int(sample_rate))
            wf.writeframes(pcm16.tobytes())
        return bio.getvalue()


# Gate synthesis concurrency: CPU-only mini PCs will saturate easily.
_SYNTH_SEM = asyncio.Semaphore(max(1, int(os.getenv("QWEN_TTS_MAX_CONCURRENCY", "1") or "1")))
_CACHE_SIZE = max(0, int(os.getenv("QWEN_TTS_CACHE_SIZE", "0") or "0"))
_CACHE: "OrderedDict[Tuple[str, str, str, str], Tuple[bytes, int]]" = OrderedDict()
_CACHE_LOCK = asyncio.Lock()
_TORCH_CONFIGURED = False


def _configure_torch() -> None:
    global _TORCH_CONFIGURED
    if _TORCH_CONFIGURED:
        return
    try:
        import torch  # type: ignore
    except Exception:
        _TORCH_CONFIGURED = True
        return

    threads = os.getenv("QWEN_TTS_NUM_THREADS")
    if threads:
        try:
            torch.set_num_threads(int(threads))
        except ValueError:
            pass

    interop = os.getenv("QWEN_TTS_NUM_INTEROP")
    if interop:
        try:
            torch.set_num_interop_threads(int(interop))
        except ValueError:
            pass

    precision = os.getenv("QWEN_TTS_MATMUL_PRECISION")
    if precision:
        try:
            torch.set_float32_matmul_precision(precision)
        except Exception:
            pass

    tf32 = os.getenv("QWEN_TTS_TF32", "").strip().lower()
    if tf32 in {"1", "true", "yes", "on"}:
        try:
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True
        except Exception:
            pass

    _TORCH_CONFIGURED = True


def _load_model():
    _configure_torch()
    try:
        from qwen_tts import Qwen3TTSModel  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "qwen-tts is not installed. Install it in python_voice_service venv:\n"
            "  pip install -U qwen-tts\n"
            "Note: CPU inference can be slow; start with the 0.6B model."
        ) from exc

    model_id = _env("QWEN_TTS_MODEL", "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice")
    # CPU-only: use float32 for compatibility/stability.
    # qwen-tts uses a HuggingFace-style loader with device_map support.
    model = Qwen3TTSModel.from_pretrained(
        model_id,
        device_map=_env("QWEN_TTS_DEVICE_MAP", "cpu"),
        dtype=_env("QWEN_TTS_DTYPE", "float32"),
        attn_implementation=_env("QWEN_TTS_ATTN", "eager"),
    )
    return model


_MODEL = None


@app.on_event("startup")
async def _startup() -> None:
    # Load once during startup to avoid first-request latency spikes.
    global _MODEL
    if _MODEL is None:
        _MODEL = await asyncio.to_thread(_load_model)


async def _synthesize(text: str, speaker: str, instruct: str, language: str) -> Tuple[bytes, int]:
    global _MODEL
    if _MODEL is None:
        _MODEL = await asyncio.to_thread(_load_model)

    key = (text, speaker or "", instruct or "", language or "")
    if _CACHE_SIZE > 0:
        async with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if cached:
                _CACHE.move_to_end(key)
                return cached

    async with _SYNTH_SEM:
        def _run():
            try:
                import torch  # type: ignore
            except Exception:
                torch = None  # type: ignore[assignment]

            if torch:
                with torch.inference_mode():
                    wavs, sr = _MODEL.generate_custom_voice(
                        text=text,
                        language=language,
                        speaker=speaker if speaker else None,
                        instruct=instruct if instruct else None,
                    )
            else:
                wavs, sr = _MODEL.generate_custom_voice(
                    text=text,
                    language=language,
                    speaker=speaker if speaker else None,
                    instruct=instruct if instruct else None,
                )
            if not wavs:
                raise RuntimeError("Qwen3-TTS returned no waveforms")
            wav0 = wavs[0]
            return _to_wav_bytes(np.asarray(wav0), int(sr)), int(sr)

        try:
            result = await asyncio.to_thread(_run)
            if _CACHE_SIZE > 0:
                async with _CACHE_LOCK:
                    _CACHE[key] = result
                    _CACHE.move_to_end(key)
                    while len(_CACHE) > _CACHE_SIZE:
                        _CACHE.popitem(last=False)
            return result
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Qwen3-TTS failed: {exc}") from exc


@app.get("/speak")
async def speak_get(
    text: str = Query(..., min_length=1),
    speaker: Optional[str] = Query(None),
    voice: Optional[str] = Query(None),  # compatibility alias
    instruct: Optional[str] = Query(None),
    language: Optional[str] = Query(None),
    # Compatibility no-ops (Unity currently sends these for Piper).
    model: Optional[str] = Query(None),
    config: Optional[str] = Query(None),
) -> Response:
    _ = (model, config)  # ignored
    t = (text or "").strip()
    if not t:
        raise HTTPException(status_code=400, detail="Empty text")

    spk = _pick_speaker(speaker, voice)
    ins = _pick_instruct(instruct)
    lang = _pick_language(language)

    wav_bytes, _sr = await _synthesize(t, spk, ins, lang)
    return Response(content=wav_bytes, media_type="audio/wav")


@app.post("/speak", response_model=TtsResponse)
async def speak_post(payload: TtsRequest) -> TtsResponse:
    t = (payload.text or "").strip()
    if not t:
        raise HTTPException(status_code=400, detail="Empty text")

    spk = _pick_speaker(payload.speaker, payload.voice)
    ins = _pick_instruct(payload.instruct)
    lang = _pick_language(payload.language)

    wav_bytes, sr = await _synthesize(t, spk, ins, lang)
    audio_b64 = base64.b64encode(wav_bytes).decode("ascii")
    return TtsResponse(audio_wav_base64=audio_b64, sample_rate=sr)
