"""Python voice service using Faster-Whisper for speech recognition.

This module exposes a FastAPI application that accepts raw PCM audio
from the Unity client, performs transcription with Faster-Whisper and
returns a Vosk-compatible JSON payload so the rest of the Unity project
can reuse the existing message hub pipeline.
"""

from __future__ import annotations

import math
import os
import asyncio
import base64
import contextlib
import io
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import numpy as np
import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from faster_whisper import WhisperModel
from pydantic import BaseModel, Field

APP_TITLE = "Coach Voice Agent - Python Voice Service"
DEFAULT_SAMPLE_RATE = 16000
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
    model_path = _environment("WHISPER_MODEL_PATH", "large-v3")
    compute_type = _environment("WHISPER_COMPUTE_TYPE", "int8_float16")

    model = WhisperModel(model_path, device="cuda", compute_type=compute_type)
    try:
        print(f"[VoiceService] Loaded Faster-Whisper model={model_path} device=cuda compute_type={compute_type}")
    except Exception:
        pass
    return model


class RespondRequest(BaseModel):
    text: str = Field(..., min_length=1, description="User transcript to send to the coach agent")


class RespondResponse(BaseModel):
    text: str
    audio_wav_base64: Optional[str] = Field(
        None,
        description=(
            "Base64-encoded WAV containing the Piper TTS output for the reply. "
            "Present only when Piper is configured."
        ),
    )
    audio_sample_rate: Optional[int] = Field(
        None,
        description="Sample rate of the generated audio in Hz when available.",
    )


class OllamaError(RuntimeError):
    pass


class PiperError(RuntimeError):
    pass


class PiperNotConfigured(RuntimeError):
    pass


@dataclass(frozen=True)
class PiperSettings:
    executable: str
    model_path: Path
    config_path: Optional[Path]
    speaker_id: Optional[str]


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


def _piper_settings() -> Optional[PiperSettings]:
    model_path_value = os.getenv("PIPER_MODEL_PATH")
    if not model_path_value:
        return None

    model_path = Path(model_path_value).expanduser().resolve()
    config_override = os.getenv("PIPER_CONFIG_PATH")
    config_path = Path(config_override).expanduser().resolve() if config_override else None

    if config_path is None and model_path.suffix:
        # Piper models usually ship with a matching .onnx.json metadata file.
        candidate = Path(str(model_path) + ".json")
        if candidate.exists():
            config_path = candidate

    speaker_value = os.getenv("PIPER_SPEAKER", "").strip()

    return PiperSettings(
        executable=_environment("PIPER_EXECUTABLE", "piper"),
        model_path=model_path,
        config_path=config_path,
        speaker_id=speaker_value or None,
    )


def _run_piper_sync(text: str, settings: PiperSettings) -> Tuple[bytes, int]:
    if not settings.model_path.exists():
        raise PiperError(f"Piper model not found at {settings.model_path}")
    if settings.config_path is not None and not settings.config_path.exists():
        raise PiperError(f"Piper config not found at {settings.config_path}")

    with tempfile.TemporaryDirectory(prefix="voice-agent-piper-") as tmpdir:
        output_path = Path(tmpdir) / "response.wav"
        command = [
            settings.executable,
            "--model",
            str(settings.model_path),
            "--output_file",
            str(output_path),
            "--text",
            text,
        ]

        if settings.config_path is not None:
            command.extend(["--config", str(settings.config_path)])
        if settings.speaker_id is not None:
            command.extend(["--speaker", settings.speaker_id])

        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        if completed.returncode != 0:
            stderr_text = completed.stderr.decode("utf-8", errors="ignore").strip()
            raise PiperError(stderr_text or "Piper synthesis failed")

        if not output_path.exists():
            raise PiperError("Piper did not produce an output file")

        audio_bytes = output_path.read_bytes()

    try:
        with contextlib.closing(wave.open(io.BytesIO(audio_bytes), "rb")) as wav_file:
            sample_rate = wav_file.getframerate()
    except wave.Error as exc:
        raise PiperError(f"Unable to read Piper WAV output: {exc}") from exc

    return audio_bytes, sample_rate


async def _synthesise_reply_audio(reply_text: str) -> Tuple[bytes, int]:
    settings = _piper_settings()
    if settings is None:
        raise PiperNotConfigured("Piper is not configured")

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run_piper_sync, reply_text, settings)


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
    beam_size: int = Query(5, ge=1, le=10),
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

    effective_beam_size = max(1, min(beam_size, 10))

    segments_generator, info = model.transcribe(
        audio,
        beam_size=effective_beam_size,
        language="en",
        task="transcribe",
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300},
        initial_prompt="rachel",
        temperature=(0.0, 0.2),
        best_of=5,
    )

    segments = list(segments_generator)

    words: List[dict] = []
    combined_text_parts: List[str] = []
    raw_text_parts: List[str] = []

    avg_logprob_values: List[float] = []

    for segment in segments:
        text = segment.text.strip()
        if text:
            combined_text_parts.append(text)
            raw_text_parts.append(text)

        if segment.avg_logprob is not None:
            try:
                avg_logprob_values.append(float(segment.avg_logprob))
            except (TypeError, ValueError):
                pass

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

    # Wake word biasing: if the entire utterance is a short single token close to "rachel",
    # normalize it to the exact form to stabilize wake-word detection on Whisper side.
    full_text = " ".join(part for part in combined_text_parts if part).strip()
    normalized = full_text.lower()
    if normalized in ("rachel", "ra chel", "rachal", "richel", "richelle", "rach el"):
        full_text = "rachel"
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

    if avg_logprob_values:
        response["avg_logprob"] = float(round(sum(avg_logprob_values) / len(avg_logprob_values), 4))

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

    audio_b64: Optional[str] = None
    audio_rate: Optional[int] = None

    try:
        audio_bytes, audio_rate = await _synthesise_reply_audio(reply)
        audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
    except PiperNotConfigured:
        # Piper is optional; if it isn't configured we simply return the text response.
        pass
    except PiperError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return RespondResponse(text=reply, audio_wav_base64=audio_b64, audio_sample_rate=audio_rate)


if __name__ == "__main__":
    import uvicorn

    port = int(_environment("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
