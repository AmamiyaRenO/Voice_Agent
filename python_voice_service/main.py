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
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from faster_whisper import WhisperModel

APP_TITLE = "Coach Voice Agent - Python Voice Service"
DEFAULT_SAMPLE_RATE = 16000

app = FastAPI(title=APP_TITLE)


def _environment(key: str, default: str) -> str:
    value = os.getenv(key)
    return value.strip() if value is not None else default


@lru_cache(maxsize=1)
def _load_model() -> WhisperModel:
    model_path = _environment("WHISPER_MODEL_PATH", "small.en")
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
        "avg_logprob": info.avg_logprob,
        "language_probability": info.language_probability,
        "translation": False,
    }

    return JSONResponse(response)


if __name__ == "__main__":
    import uvicorn

    port = int(_environment("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
