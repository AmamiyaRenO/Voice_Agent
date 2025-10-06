"""Python voice service with wake word gating.

This module exposes a FastAPI application with two main endpoints:

* ``POST /wake`` accepts raw PCM audio and runs a Vosk recogniser that
  is limited to the wake-word pronunciation variants ("richel", "richelle",
  etc.). Only when Vosk confirms a match does the service emit a short-lived
  wake token.
* ``POST /transcribe`` accepts follow-up audio together with a valid wake
  token and performs full transcription with Faster-Whisper. The JSON
  response stays compatible with the Vosk payload Unity already expects so
  downstream processing keeps working.

This keeps the heavy Whisper model idle most of the time, dramatically
reducing GPU usage because it only runs when the wake word has been spoken.
"""

from __future__ import annotations

import math
import os
import difflib
import json
import logging
import threading
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import numpy as np
import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from faster_whisper import WhisperModel
from vosk import KaldiRecognizer, Model, SetLogLevel
from pydantic import BaseModel, Field

APP_TITLE = "Coach Voice Agent - Python Voice Service"
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_WAKE_TOKEN_TTL = 30.0
DEFAULT_WAKE_RECORD_SECONDS = 5.0
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "llama3.1:8b"
DEFAULT_SYSTEM_PROMPT = (
    "You are the Coach Voice Agent inside a rehabilitation and exercise game system.\n"
    "Your role is to:\n"
    "- Greet the user politely when they start interacting.\n"
    "- Provide short, clear spoken feedback after the user finishes an exercise or command.\n"
    "- Encourage the user with motivational phrases (\"Great job!\", \"Keep going!\", \"You are improving!\").\n"
    "- Confirm user intents from speech recognition (e.g., start game, stop game, switch activity).\n"
    "- Answer simple questions from the user about the game or their progress.\n"
    "- Keep responses short (1–2 sentences) so they sound natural when spoken.\n"
    "- Use a friendly, supportive tone, like a personal trainer or companion.\n"
    "- If the user asks something outside your knowledge, politely say you don’t know and redirect them back to the exercise context."
)

logger = logging.getLogger(__name__)

app = FastAPI(title=APP_TITLE)

SetLogLevel(-1)


def _service_root() -> Path:
    return Path(__file__).resolve().parent


def _environment(key: str, default: str) -> str:
    value = os.getenv(key)
    return value.strip() if value is not None else default


def _environment_float(key: str, default: float) -> float:
    value = os.getenv(key)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _environment_int(key: str, default: int) -> int:
    value = os.getenv(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _wake_keywords() -> Tuple[str, ...]:
    custom = _environment("VOICE_AGENT_WAKE_KEYWORDS", "")
    if custom:
        keywords = [part.strip().lower() for part in custom.split(",") if part.strip()]
        if keywords:
            return tuple(dict.fromkeys(keywords))
    # Default pronunciation variants of "Richel".
    return tuple(
        dict.fromkeys(
            [
                "richel",
                "richelle",
                "rachelle",
                "rachel",
                "richell",
            ]
        )
    )


@lru_cache(maxsize=1)
def _wake_keyword_set() -> set[str]:
    return set(_wake_keywords())


@lru_cache(maxsize=1)
def _wake_grammar() -> str:
    keywords = [f'"{keyword}"' for keyword in _wake_keywords()]
    keywords.append('"[unk]"')
    return "[" + ", ".join(keywords) + "]"


def _match_wake_word(text: str) -> Optional[str]:
    text = text.strip().lower()
    if not text:
        return None
    if text in _wake_keyword_set():
        return text
    matches = difflib.get_close_matches(text, _wake_keywords(), n=1, cutoff=0.6)
    return matches[0] if matches else None


def _helper_audio_path() -> Path:
    value = _environment("VOICE_AGENT_HELPER_AUDIO", "helper.mp3")
    helper_path = Path(value)
    if not helper_path.is_absolute():
        helper_path = _service_root() / helper_path
    return helper_path


def _wake_record_seconds() -> float:
    return max(0.0, _environment_float("VOICE_AGENT_RECORD_SECONDS", DEFAULT_WAKE_RECORD_SECONDS))


def _prepare_vosk_payload(payload: bytes, sample_rate: int) -> Tuple[bytes, int]:
    if sample_rate == DEFAULT_SAMPLE_RATE:
        return payload, sample_rate
    audio = np.frombuffer(payload, dtype=np.int16).astype(np.float32) / 32768.0
    resampled = _resample_audio(audio, sample_rate, DEFAULT_SAMPLE_RATE)
    resampled = np.clip(resampled * 32768.0, -32768.0, 32767.0)
    return resampled.astype(np.int16).tobytes(), DEFAULT_SAMPLE_RATE


@lru_cache(maxsize=1)
def _load_vosk_model() -> Model:
    model_path = _environment("VOSK_MODEL_PATH", "vosk-model-small-en-us-0.15")
    resolved = Path(model_path).expanduser()
    if not resolved.is_absolute():
        resolved = _service_root() / resolved
    if not resolved.exists():
        raise RuntimeError(
            f"Vosk model path not found: {resolved}. Set VOSK_MODEL_PATH to a valid directory."
        )
    return Model(str(resolved))


def _detect_wake_word(payload: bytes, sample_rate: int) -> tuple[bool, Optional[str], Optional[float], str]:
    model = _load_vosk_model()
    audio_bytes, effective_rate = _prepare_vosk_payload(payload, sample_rate)
    recognizer = KaldiRecognizer(model, effective_rate, _wake_grammar())
    recognizer.SetWords(True)

    if not recognizer.AcceptWaveform(audio_bytes):
        result_json = recognizer.FinalResult()
    else:
        result_json = recognizer.Result()

    if not result_json:
        return False, None, None, ""

    try:
        result = json.loads(result_json)
    except json.JSONDecodeError:
        logger.exception("Failed to decode Vosk result: %s", result_json)
        return False, None, None, ""

    text = (result.get("text") or "").strip().lower()
    matched = _match_wake_word(text)

    confidence: Optional[float] = None
    words = result.get("result")
    if isinstance(words, list) and words:
        confidences = [float(entry.get("conf", 0.0)) for entry in words if isinstance(entry, dict)]
        if confidences:
            confidence = max(confidences)

    return matched is not None, matched, confidence, text


class WakeSessionManager:
    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = max(1.0, ttl_seconds)
        self._lock = threading.Lock()
        self._tokens: dict[str, float] = {}

    def issue(self) -> str:
        token = uuid.uuid4().hex
        with self._lock:
            self._cleanup_locked()
            self._tokens[token] = time.monotonic()
        return token

    def consume(self, token: str) -> bool:
        with self._lock:
            self._cleanup_locked()
            return self._tokens.pop(token, None) is not None

    def _cleanup_locked(self) -> None:
        now = time.monotonic()
        expired = [tok for tok, created in self._tokens.items() if now - created > self._ttl]
        for token in expired:
            self._tokens.pop(token, None)


_wake_sessions = WakeSessionManager(_environment_float("VOICE_AGENT_WAKE_TOKEN_TTL", DEFAULT_WAKE_TOKEN_TTL))


@lru_cache(maxsize=1)
def _load_model() -> WhisperModel:
    model_path = _environment("WHISPER_MODEL_PATH", "medium.en")
    device = _environment("WHISPER_DEVICE", "cpu").lower()

    if device == "cuda":
        compute_type = _environment("WHISPER_COMPUTE_TYPE", "float16")
        return WhisperModel(model_path, device=device, compute_type=compute_type)

    compute_type = _environment("WHISPER_COMPUTE_TYPE", "int8")
    try:
        cpu_threads = int(_environment("WHISPER_CPU_THREADS", "8"))
    except ValueError:
        cpu_threads = 8

    return WhisperModel(
        model_path,
        device=device,
        compute_type=compute_type,
        cpu_threads=cpu_threads,
    )


class RespondRequest(BaseModel):
    text: str = Field(..., min_length=1, description="User transcript to send to the coach agent")


class RespondResponse(BaseModel):
    text: str


class OllamaError(RuntimeError):
    pass


def _ollama_base_url() -> str:
    return _environment("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL).rstrip("/")


def _ollama_model() -> str:
    return _environment("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)


def _ollama_system_prompt() -> str:
    return _environment("OLLAMA_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)


async def _generate_coach_reply(user_text: str) -> str:
    payload = {
        "model": _ollama_model(),
        "system": _ollama_system_prompt(),
        "prompt": f"User: {user_text}\nCoach:",
        "stream": False,
        "options": {
            "temperature": _environment_float("OLLAMA_TEMPERATURE", 0.6),
            "top_p": _environment_float("OLLAMA_TOP_P", 0.9),
            "top_k": _environment_int("OLLAMA_TOP_K", 40),
            "num_predict": _environment_int("OLLAMA_MAX_TOKENS", 128),
            "repeat_penalty": _environment_float("OLLAMA_REPEAT_PENALTY", 1.1),
        },
    }

    url = f"{_ollama_base_url()}/api/generate"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            response = await client.post(url, json=payload)
    except httpx.HTTPError as exc:
        raise OllamaError(f"Failed to contact Ollama at {url}: {exc}") from exc

    if response.status_code != 200:
        raise OllamaError(
            f"Ollama returned status {response.status_code}: {response.text.strip()}"
        )

    data = response.json()
    reply_text = (data.get("response") or "").strip()

    if not reply_text:
        raise OllamaError("Ollama response was empty")

    return reply_text


@app.on_event("startup")
async def _startup_event() -> None:
    # Trigger model loading during startup so the first request does not pay the cost.
    try:
        _load_vosk_model()
    except Exception as exc:  # pragma: no cover - best effort logging
        logger.warning("Vosk model could not be preloaded: %s", exc)
    _load_model()


@app.get("/healthz")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


def _resample_audio(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate or samples.size == 0:
        return samples

    duration_seconds = samples.shape[0] / float(source_rate)
    target_length = max(1, int(math.ceil(duration_seconds * target_rate)))

    source_indices = np.linspace(0, samples.shape[0] - 1, num=samples.shape[0], dtype=np.float64)
    target_indices = np.linspace(0, samples.shape[0] - 1, num=target_length, dtype=np.float64)

    resampled = np.interp(target_indices, source_indices, samples)
    return resampled.astype(np.float32, copy=False)


def _build_vosk_result(words: Iterable[dict]) -> List[dict]:
    # Vosk uses "result" for word-level entries. Unity expects "word" and timing fields.
    return list(words)


@app.post("/wake")
async def wake(
    request: Request,
    sample_rate: int = Query(DEFAULT_SAMPLE_RATE, ge=8000, le=48000),
) -> JSONResponse:
    payload = await request.body()
    if not payload:
        raise HTTPException(status_code=400, detail="Empty audio payload")

    detected, keyword, confidence, raw_text = _detect_wake_word(payload, sample_rate)

    response: dict[str, object | None] = {
        "wake_word_detected": detected,
        "wake_word": keyword,
        "confidence": confidence,
        "raw_text": raw_text,
    }

    if detected:
        helper_path = _helper_audio_path()
        helper_audio: str | None
        if helper_path.exists():
            helper_audio = str(helper_path)
        else:
            helper_audio = None
            logger.warning("Helper audio file not found: %s", helper_path)
        response.update(
            {
                "wake_token": _wake_sessions.issue(),
                "helper_audio": helper_audio,
                "record_seconds": _wake_record_seconds(),
            }
        )

    return JSONResponse(response)


@app.post("/transcribe")
async def transcribe(
    request: Request,
    sample_rate: int = Query(DEFAULT_SAMPLE_RATE, ge=8000, le=48000),
    language: Optional[str] = Query("en", min_length=1, max_length=8),
    wake_token: Optional[str] = Query(
        None,
        description="Wake token issued by /wake. Required so Whisper only runs when the wake word was detected.",
    ),
) -> JSONResponse:
    if not wake_token:
        raise HTTPException(status_code=400, detail="Missing wake_token. Call /wake first.")
    if not _wake_sessions.consume(wake_token):
        raise HTTPException(status_code=403, detail="Wake token is invalid or expired.")

    payload = await request.body()
    if not payload:
        raise HTTPException(status_code=400, detail="Empty audio payload")

    audio = np.frombuffer(payload, dtype=np.int16)
    if audio.size == 0:
        raise HTTPException(status_code=400, detail="Invalid audio payload")

    audio = audio.astype(np.float32) / 32768.0
    if sample_rate != DEFAULT_SAMPLE_RATE:
        audio = _resample_audio(audio, sample_rate, DEFAULT_SAMPLE_RATE)

    model = _load_model()

    segments_generator, info = model.transcribe(
        audio,
        beam_size=1,
        language=language or "en",
        task="transcribe",
        word_timestamps=False,
    )

    segments = list(segments_generator)

    words: List[dict] = []
    combined_text_parts: List[str] = []

    for segment in segments:
        text = segment.text.strip()
        if text:
            combined_text_parts.append(text)

        for word in segment.words or []:
            word_text = word.word.strip()
            if not word_text:
                continue

            words.append(
                {
                    "word": word_text,
                    "start": max(0.0, float(word.start) if word.start is not None else 0.0),
                    "end": max(0.0, float(word.end) if word.end is not None else 0.0),
                    "confidence": round(float(word.probability), 4) if word.probability is not None else None,
                }
            )

    full_text = " ".join(part for part in combined_text_parts if part).strip()
    if not full_text and words:
        full_text = " ".join(word["word"] for word in words).strip()

    response = {
        "text": full_text,
        "result": _build_vosk_result(words),
        "language": info.language,
        "duration": info.duration,
        "language_probability": info.language_probability,
        "translation": False,
    }

    return JSONResponse(response)


@app.post("/respond", response_model=RespondResponse)
async def respond(payload: RespondRequest) -> RespondResponse:
    user_text = payload.text.strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="Empty text payload")

    try:
        reply = await _generate_coach_reply(user_text)
    except OllamaError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return RespondResponse(text=reply)


if __name__ == "__main__":
    import uvicorn

    port = int(_environment("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
