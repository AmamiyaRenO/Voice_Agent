"""Python voice service using Faster-Whisper for speech recognition.

This module exposes a FastAPI application that accepts raw PCM audio
from the Unity client, performs transcription with Faster-Whisper and
returns a Vosk-compatible JSON payload so the rest of the Unity project
can reuse the existing message hub pipeline.
"""

from __future__ import annotations

import asyncio
import base64
import math
import os
import subprocess
import tempfile
from functools import lru_cache, partial
from pathlib import Path
from typing import Dict, Iterable, List, Optional

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

# Wake-word biasing/normalization via environment variables
WAKE_WORD = os.getenv("WAKE_WORD", "rachel").strip().lower()
WAKE_WORD_ALIASES = [
    s.strip().lower()
    for s in os.getenv(
        "WAKE_WORD_ALIASES",
        "rachel, richel, richelle, rachal, raychel, ra chel, rach el",
    ).split(",")
    if s.strip()
]


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
    device_pref = _environment("WHISPER_DEVICE", "auto").lower()

    def _cpu_compute(ct: str) -> str:
        # If compute_type is tuned for GPU (e.g., float16), pick a CPU-friendly default
        return "int8" if "float16" in ct.lower() else ct

    # Explicit CPU request
    if device_pref == "cpu":
        model = WhisperModel(model_path, device="cpu", compute_type=_cpu_compute(compute_type))
        try:
            print(f"[VoiceService] Loaded Faster-Whisper model={model_path} device=cpu compute_type={_cpu_compute(compute_type)}")
        except Exception:
            pass
        return model

    # Prefer CUDA; fall back to CPU if unavailable or fails
    try:
        model = WhisperModel(model_path, device="cuda", compute_type=compute_type)
        try:
            print(f"[VoiceService] Loaded Faster-Whisper model={model_path} device=cuda compute_type={compute_type}")
        except Exception:
            pass
        return model
    except Exception as exc:
        model = WhisperModel(model_path, device="cpu", compute_type=_cpu_compute(compute_type))
        try:
            print(f"[VoiceService] Loaded Faster-Whisper model={model_path} device=cpu compute_type={_cpu_compute(compute_type)} (fallback from CUDA: {exc})")
        except Exception:
            pass
        return model


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


class TtsRequest(BaseModel):
    text: str = Field(..., min_length=1, description="Text to synthesize")
    voice: Optional[str] = Field(None, description="Optional voice or speaker hint")
    speed: float = Field(1.0, ge=0.1, le=4.0, description="Playback speed multiplier")
    volume: float = Field(1.0, ge=0.0, le=2.0, description="Volume hint (handled client-side)")
    play: bool = Field(True, description="Whether the Unity client should auto-play")


class TtsResponse(BaseModel):
    audio_wav_base64: str
    sample_rate: int


_DEFAULT_TTS_SAMPLE_RATE = 22050


@lru_cache(maxsize=1)
def _piper_speaker_map() -> Dict[str, str]:
    raw = _environment("PIPER_SPEAKER_MAP", "")
    mapping: Dict[str, str] = {}
    if not raw:
        return mapping
    for item in raw.split(","):
        if not item:
            continue
        if ":" not in item:
            continue
        voice_key, speaker_value = item.split(":", 1)
        voice_key = voice_key.strip().lower()
        speaker_value = speaker_value.strip()
        if voice_key and speaker_value:
            mapping[voice_key] = speaker_value
    return mapping


def _resolve_speaker(requested_voice: Optional[str]) -> Optional[str]:
    if requested_voice:
        speaker = _piper_speaker_map().get(requested_voice.strip().lower())
        if speaker:
            return speaker
    env_default = _environment("PIPER_SPEAKER", "")
    return env_default or None


def _build_piper_command(out_path: Path, requested_voice: Optional[str], speed: float) -> List[str]:
    exe = _environment("PIPER_EXECUTABLE", "piper")
    model = _environment("PIPER_MODEL_PATH", "")
    if not model:
        raise RuntimeError("PIPER_MODEL_PATH environment variable is not configured")

    cmd: List[str] = [exe, "--model", model, "--output_file", str(out_path)]

    cfg = _environment("PIPER_CONFIG_PATH", "")
    if cfg:
        cmd += ["--config", cfg]

    speaker = _resolve_speaker(requested_voice)
    if speaker:
        cmd += ["--speaker", speaker]

    clamped_speed = max(0.1, min(4.0, speed if speed and speed > 0 else 1.0))
    if abs(clamped_speed - 1.0) > 1e-3:
        # Piper uses length_scale (inverse of speed): lower values => faster speech
        length_scale = round(1.0 / clamped_speed, 3)
        cmd += ["--length_scale", f"{length_scale}"]

    noise_scale = _environment("PIPER_NOISE_SCALE", "")
    if noise_scale:
        cmd += ["--noise_scale", noise_scale]

    noise_w = _environment("PIPER_NOISE_W", "")
    if noise_w:
        cmd += ["--noise_w", noise_w]

    if _environment("PIPER_SPEAKER_LATENCY", ""):
        cmd += ["--speaker_latency", _environment("PIPER_SPEAKER_LATENCY", "")]

    return cmd


def _invoke_piper(text: str, requested_voice: Optional[str], speed: float) -> bytes:
    with tempfile.TemporaryDirectory(prefix="voice-agent-tts-") as tmp_dir:
        out_path = Path(tmp_dir) / "out.wav"
        cmd = _build_piper_command(out_path, requested_voice, speed)
        try:
            completed = subprocess.run(
                cmd,
                input=text.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except FileNotFoundError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise RuntimeError(f"Failed to launch Piper: {exc}") from exc

        if completed.returncode != 0:
            stderr_text = completed.stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(stderr_text or "Piper returned a non-zero exit code")

        if not out_path.exists():
            raise RuntimeError("Piper did not generate an output file")

        return out_path.read_bytes()


def _tts_sample_rate() -> int:
    return _environment_int("PIPER_SAMPLE_RATE", _DEFAULT_TTS_SAMPLE_RATE)


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
        beam_size=max(5, min(10, effective_beam_size)),
        language="en",
        task="transcribe",
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300, "speech_pad_ms": 200},
        initial_prompt=(WAKE_WORD + " open play back quit close shut down"),
        temperature=(0.0,),
        best_of=1,
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

    return RespondResponse(text=reply)


@app.post("/tts", response_model=TtsResponse)
async def synthesize(payload: TtsRequest) -> TtsResponse:
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty text payload")

    loop = asyncio.get_running_loop()

    try:
        audio_bytes = await loop.run_in_executor(
            None,
            partial(_invoke_piper, text, payload.voice, payload.speed),
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=500,
            detail=(
                "Piper executable was not found. Configure PIPER_EXECUTABLE or install Piper in PATH."
            ),
        ) from None
    except RuntimeError as exc:
        message = str(exc).strip() or "Piper synthesis failed"
        if "PIPER_MODEL_PATH" in message:
            raise HTTPException(status_code=503, detail=message) from exc
        raise HTTPException(status_code=500, detail=message) from exc

    audio_b64 = base64.b64encode(audio_bytes).decode("ascii")

    return TtsResponse(audio_wav_base64=audio_b64, sample_rate=_tts_sample_rate())


if __name__ == "__main__":
    import uvicorn

    port = int(_environment("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
