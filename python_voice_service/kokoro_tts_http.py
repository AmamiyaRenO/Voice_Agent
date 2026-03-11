from __future__ import annotations

import asyncio
import base64
import io
import os
import re
import threading
import wave
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

app = FastAPI(title="Kokoro TTS HTTP Wrapper")

_DEFAULT_MAX_CONCURRENCY = 1
_DEFAULT_CACHE_SIZE = 32
_DEFAULT_SAMPLE_RATE = 24000
_DEFAULT_VOICE = "af_heart"
_DEFAULT_LANG_CODE = "a"
_DEFAULT_SPEED = 1.0
_DEFAULT_SEGMENT_SILENCE_MS = 60.0

_WHITESPACE_RE = re.compile(r"\s+")
_REPEATED_PUNCT_RE = re.compile(r"([!?.,;:])\1+")
_SUPPORTED_LANG_CODES: Dict[str, str] = {
    "a": "American English",
    "b": "British English",
    "e": "Spanish",
    "f": "French",
    "h": "Hindi",
    "i": "Italian",
    "j": "Japanese",
    "p": "Brazilian Portuguese",
    "z": "Mandarin Chinese",
}
_LANGUAGE_ALIASES: Dict[str, str] = {
    "a": "a",
    "american": "a",
    "american english": "a",
    "en": "a",
    "en-us": "a",
    "en_us": "a",
    "b": "b",
    "british": "b",
    "british english": "b",
    "en-gb": "b",
    "en_uk": "b",
    "en-gb": "b",
    "e": "e",
    "es": "e",
    "spanish": "e",
    "f": "f",
    "fr": "f",
    "french": "f",
    "h": "h",
    "hi": "h",
    "hindi": "h",
    "i": "i",
    "it": "i",
    "italian": "i",
    "j": "j",
    "ja": "j",
    "japanese": "j",
    "p": "p",
    "pt": "p",
    "pt-br": "p",
    "pt_br": "p",
    "brazilian portuguese": "p",
    "portuguese": "p",
    "z": "z",
    "zh": "z",
    "zh-cn": "z",
    "zh_cn": "z",
    "mandarin": "z",
    "mandarin chinese": "z",
    "chinese": "z",
}

_SYNTH_SEM = asyncio.Semaphore(max(1, int(os.getenv("KOKORO_TTS_MAX_CONCURRENCY", str(_DEFAULT_MAX_CONCURRENCY)) or "1")))
_CACHE_SIZE = max(0, int(os.getenv("KOKORO_TTS_CACHE_SIZE", str(_DEFAULT_CACHE_SIZE)) or "0"))
_CACHE: "OrderedDict[Tuple[str, str, str, float], Tuple[bytes, int]]" = OrderedDict()
_CACHE_LOCK = threading.Lock()
_PIPELINES: Dict[str, Any] = {}
_PIPELINES_LOCK = threading.Lock()


class TtsRequest(BaseModel):
    text: str = Field(..., min_length=1)
    speaker: Optional[str] = None
    voice: Optional[str] = None
    instruct: Optional[str] = None
    language: Optional[str] = None
    lang_code: Optional[str] = None
    speed: Optional[float] = None
    model: Optional[str] = None
    config: Optional[str] = None


class TtsResponse(BaseModel):
    audio_wav_base64: str
    sample_rate: int


def _env(key: str, default: str = "") -> str:
    value = os.getenv(key)
    return value.strip() if value else default


def _env_int(key: str, default: int, floor: int = 0) -> int:
    value = os.getenv(key)
    if value is None:
        return max(floor, default)
    try:
        return max(floor, int(value))
    except Exception:
        return max(floor, default)


def _env_float(key: str, default: float, floor: float = 0.0) -> float:
    value = os.getenv(key)
    if value is None:
        return max(floor, default)
    try:
        return max(floor, float(value))
    except Exception:
        return max(floor, default)


def _normalize_text_for_tts(text: str) -> str:
    normalized = _WHITESPACE_RE.sub(" ", (text or "").strip())
    normalized = _REPEATED_PUNCT_RE.sub(r"\1", normalized)
    max_chars = _env_int("KOKORO_TTS_MAX_TEXT_CHARS", 0, floor=0)
    if max_chars > 0 and len(normalized) > max_chars:
        clipped = normalized[:max_chars]
        boundary = max(
            clipped.rfind(" "),
            clipped.rfind("."),
            clipped.rfind("!"),
            clipped.rfind("?"),
            clipped.rfind(","),
            clipped.rfind(";"),
            clipped.rfind(":"),
        )
        if boundary >= max(12, int(max_chars * 0.65)):
            clipped = clipped[:boundary]
        normalized = clipped.strip()
    return normalized


def _pick_voice(speaker: Optional[str], voice: Optional[str]) -> str:
    selected = (speaker or "").strip() or (voice or "").strip() or _env("KOKORO_TTS_VOICE", _DEFAULT_VOICE)
    return selected or _DEFAULT_VOICE


def _voice_to_lang_code(voice: str) -> str:
    prefix = str(voice or "").strip().lower().split("_", 1)[0]
    if prefix in _SUPPORTED_LANG_CODES:
        return prefix
    return ""


def _language_alias_to_code(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return ""
    return _LANGUAGE_ALIASES.get(normalized, "")


def _pick_lang_code(language: Optional[str], lang_code: Optional[str], voice: str) -> str:
    selected = _language_alias_to_code(lang_code or "")
    if not selected:
        selected = _language_alias_to_code(language or "")
    if not selected:
        selected = _voice_to_lang_code(voice)
    if not selected:
        selected = _language_alias_to_code(_env("KOKORO_TTS_LANG_CODE", _DEFAULT_LANG_CODE))
    return selected if selected in _SUPPORTED_LANG_CODES else _DEFAULT_LANG_CODE


def _pick_speed(speed: Optional[float]) -> float:
    if speed is not None:
        try:
            return max(0.1, float(speed))
        except Exception:
            return max(0.1, _env_float("KOKORO_TTS_SPEED", _DEFAULT_SPEED, floor=0.1))
    return max(0.1, _env_float("KOKORO_TTS_SPEED", _DEFAULT_SPEED, floor=0.1))


def _to_wav_bytes(mono_float: np.ndarray, sample_rate: int) -> bytes:
    samples = np.asarray(mono_float, dtype=np.float32).reshape(-1)
    if samples.size == 0:
        raise HTTPException(status_code=500, detail="Kokoro returned empty audio")
    samples = np.clip(samples, -1.0, 1.0)
    pcm16 = (samples * 32767.0).astype(np.int16)
    with io.BytesIO() as buffer:
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(int(sample_rate))
            wav_file.writeframes(pcm16.tobytes())
        return buffer.getvalue()


def _load_pipeline(lang_code: str) -> Any:
    with _PIPELINES_LOCK:
        cached = _PIPELINES.get(lang_code)
        if cached is not None:
            return cached
    try:
        from kokoro import KPipeline  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "kokoro is not installed. Install it in the TTS venv:\n"
            "  pip install -U kokoro>=0.9.4\n"
            "For better English fallback on Windows, also install espeak-ng."
        ) from exc

    pipeline = KPipeline(lang_code=lang_code)
    with _PIPELINES_LOCK:
        existing = _PIPELINES.get(lang_code)
        if existing is not None:
            return existing
        _PIPELINES[lang_code] = pipeline
    return pipeline


def _cache_get(key: Tuple[str, str, str, float]) -> Optional[Tuple[bytes, int]]:
    if _CACHE_SIZE <= 0:
        return None
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is None:
            return None
        _CACHE.move_to_end(key)
        return cached


def _cache_set(key: Tuple[str, str, str, float], value: Tuple[bytes, int]) -> None:
    if _CACHE_SIZE <= 0:
        return
    with _CACHE_LOCK:
        _CACHE[key] = value
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)


def _run_synthesis(text: str, voice: str, lang_code: str, speed: float) -> Tuple[bytes, int]:
    pipeline = _load_pipeline(lang_code)
    sample_rate = int(_env_int("KOKORO_TTS_SAMPLE_RATE", _DEFAULT_SAMPLE_RATE, floor=1))
    silence_ms = _env_float("KOKORO_TTS_SEGMENT_SILENCE_MS", _DEFAULT_SEGMENT_SILENCE_MS, floor=0.0)
    split_pattern = _env("KOKORO_TTS_SPLIT_PATTERN", r"\n+")
    chunks: list[np.ndarray] = []
    generator = pipeline(text, voice=voice, speed=speed, split_pattern=split_pattern)
    for item in generator:
        try:
            _graphemes, _phonemes, audio_raw = item
        except Exception:
            audio_raw = getattr(item, "audio", None)
            if audio_raw is None:
                continue
        audio = np.asarray(audio_raw, dtype=np.float32).reshape(-1)
        if audio.size > 0:
            chunks.append(audio)
    if not chunks:
        raise RuntimeError("Kokoro returned no waveform segments")
    if len(chunks) == 1:
        combined = chunks[0]
    else:
        parts: list[np.ndarray] = []
        silence = np.zeros(int(round((silence_ms / 1000.0) * sample_rate)), dtype=np.float32)
        for index, chunk in enumerate(chunks):
            if index > 0 and silence.size > 0:
                parts.append(silence)
            parts.append(chunk)
        combined = np.concatenate(parts)
    return _to_wav_bytes(combined, sample_rate), sample_rate


async def _synthesize(text: str, voice: str, lang_code: str, speed: float) -> Tuple[bytes, int]:
    key = (text, voice, lang_code, round(float(speed), 3))
    cached = _cache_get(key)
    if cached is not None:
        return cached
    async with _SYNTH_SEM:
        cached = _cache_get(key)
        if cached is not None:
            return cached
        try:
            result = await asyncio.to_thread(_run_synthesis, text, voice, lang_code, speed)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Kokoro TTS failed: {exc}") from exc
        _cache_set(key, result)
        return result


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/options")
async def options() -> Dict[str, Any]:
    voice = _pick_voice(None, None)
    lang_code = _pick_lang_code(None, None, voice)
    return {
        "voice": voice,
        "lang_code": lang_code,
        "speed": _pick_speed(None),
        "supported_lang_codes": _SUPPORTED_LANG_CODES,
    }


@app.get("/speak")
async def speak_get(
    text: str = Query(..., min_length=1),
    speaker: Optional[str] = Query(None),
    voice: Optional[str] = Query(None),
    instruct: Optional[str] = Query(None),
    language: Optional[str] = Query(None),
    lang_code: Optional[str] = Query(None),
    speed: Optional[float] = Query(None),
    model: Optional[str] = Query(None),
    config: Optional[str] = Query(None),
) -> Response:
    _ = (instruct, model, config)
    normalized_text = _normalize_text_for_tts(text)
    if not normalized_text:
        raise HTTPException(status_code=400, detail="Empty text")
    selected_voice = _pick_voice(speaker, voice)
    selected_lang_code = _pick_lang_code(language, lang_code, selected_voice)
    selected_speed = _pick_speed(speed)
    wav_bytes, _sample_rate = await _synthesize(normalized_text, selected_voice, selected_lang_code, selected_speed)
    return Response(content=wav_bytes, media_type="audio/wav")


@app.post("/speak", response_model=TtsResponse)
async def speak_post(payload: TtsRequest) -> TtsResponse:
    normalized_text = _normalize_text_for_tts(payload.text)
    if not normalized_text:
        raise HTTPException(status_code=400, detail="Empty text")
    selected_voice = _pick_voice(payload.speaker, payload.voice)
    selected_lang_code = _pick_lang_code(payload.language, payload.lang_code, selected_voice)
    selected_speed = _pick_speed(payload.speed)
    wav_bytes, sample_rate = await _synthesize(normalized_text, selected_voice, selected_lang_code, selected_speed)
    return TtsResponse(
        audio_wav_base64=base64.b64encode(wav_bytes).decode("ascii"),
        sample_rate=sample_rate,
    )
