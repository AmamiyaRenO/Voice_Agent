"""Python voice service using Faster-Whisper for speech recognition.

This module exposes a FastAPI application that accepts raw PCM audio
from the Unity client, performs transcription with Faster-Whisper and
returns a Vosk-compatible JSON payload so the rest of the Unity project
can reuse the existing message hub pipeline.
"""

from __future__ import annotations

import math
import os
from functools import lru_cache
from typing import Iterable, List, Optional

import numpy as np
import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from faster_whisper import WhisperModel
from pydantic import BaseModel, Field

APP_TITLE = "Coach Voice Agent - Python Voice Service"
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "llama3.1:8b-instruct"
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

app = FastAPI(title=APP_TITLE)


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


@app.post("/transcribe")
async def transcribe(
    request: Request,
    sample_rate: int = Query(DEFAULT_SAMPLE_RATE, ge=8000, le=48000),
    language: Optional[str] = Query("en", min_length=1, max_length=8),
) -> JSONResponse:
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
