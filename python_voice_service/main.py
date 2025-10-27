"""Python voice service using Faster-Whisper for speech recognition.

This module exposes a FastAPI application that accepts raw PCM audio
from the Unity client, performs transcription with Faster-Whisper and
returns a Vosk-compatible JSON payload so the rest of the Unity project
can reuse the existing message hub pipeline.
"""

from __future__ import annotations

import math
import os
import re
from functools import lru_cache
from typing import Dict, Iterable, List, Optional

import numpy as np
import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from faster_whisper import WhisperModel
from pydantic import BaseModel, Field

try:  # Optional dependency used to improve resampling quality when available.
    from scipy.signal import resample_poly
except Exception:  # pragma: no cover - SciPy is optional at runtime.
    resample_poly = None

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

DEFAULT_HTTP_TIMEOUT = httpx.Timeout(30.0)


class _AsyncHttpClient:
    """Singleton-style manager for a shared httpx.AsyncClient instance.

    Creating a new AsyncClient for every request is relatively expensive because
    it has to establish a fresh connection pool and negotiate TLS each time.
    For chat-style interactions where the client repeatedly talks to the same
    Ollama and Piper services, that overhead can add a noticeable delay before
    the model even starts generating tokens.  Reusing a single client keeps the
    connection pool warm while still allowing FastAPI to handle requests
    concurrently.
    """

    _client: Optional[httpx.AsyncClient] = None

    @classmethod
    def get(cls) -> httpx.AsyncClient:
        if cls._client is None:
            cls._client = httpx.AsyncClient(timeout=DEFAULT_HTTP_TIMEOUT)
        return cls._client

    @classmethod
    async def aclose(cls) -> None:
        if cls._client is not None:
            await cls._client.aclose()
            cls._client = None


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
WAKE_WORD_PREFIXES = [
    s.strip().lower()
    for s in os.getenv("WAKE_WORD_PREFIXES", "hey, hi").split(",")
    if s.strip()
]


def _build_wake_word_pattern() -> re.Pattern[str]:
    terms = []
    for term in {WAKE_WORD, *WAKE_WORD_ALIASES}:
        stripped = term.strip()
        if not stripped:
            continue
        pieces = [re.escape(piece) for piece in stripped.split() if piece]
        if not pieces:
            continue
        if len(pieces) == 1:
            pattern = pieces[0]
        else:
            # Allow variable whitespace between the pieces so variants like "ra chel"
            # collapse to the canonical wake word as well.
            pattern = r"\s*".join(pieces)
        terms.append(pattern)

    if not terms:
        terms.append(re.escape(WAKE_WORD))

    combined = "|".join(sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{combined})(?!\w)", re.IGNORECASE)


_WAKE_WORD_REGEX = _build_wake_word_pattern()


_REPETITION_TOKEN_PATTERN = re.compile(r"[\w']+")


def _build_wake_word_prefix_pattern() -> Optional[re.Pattern[str]]:
    prefixes = [prefix for prefix in WAKE_WORD_PREFIXES if prefix]
    if not prefixes:
        return None

    prefix_pattern = "|".join(sorted({re.escape(p) for p in prefixes}, key=len, reverse=True))

    wake_terms: list[str] = []
    for term in {WAKE_WORD, *WAKE_WORD_ALIASES}:
        stripped = term.strip()
        if not stripped:
            continue
        pieces = [re.escape(piece) for piece in stripped.split() if piece]
        if not pieces:
            continue
        if len(pieces) == 1:
            wake_terms.append(pieces[0])
        else:
            wake_terms.append(r"\s*".join(pieces))

    if not wake_terms:
        wake_terms.append(re.escape(WAKE_WORD))

    wake_pattern = "|".join(sorted(set(wake_terms), key=len, reverse=True))
    separator = r"(?:\s|[,;:!\-])+"
    return re.compile(
        rf"(?<!\w)(?P<prefix>{prefix_pattern})(?:{separator})(?P<wake>{wake_pattern})(?!\w)",
        re.IGNORECASE,
    )


_WAKE_WORD_PREFIX_REGEX = _build_wake_word_prefix_pattern()


def _canonicalize_wake_words(text: str) -> str:
    if not text:
        return text

    if _WAKE_WORD_PREFIX_REGEX is not None:
        def _replace_prefix(match: re.Match[str]) -> str:
            prefix_text = match.group("prefix")
            normalized_prefix = re.sub(r"\s+", " ", prefix_text).strip().lower()
            return f"{normalized_prefix} {WAKE_WORD}"

        text = _WAKE_WORD_PREFIX_REGEX.sub(_replace_prefix, text)

    return _WAKE_WORD_REGEX.sub(WAKE_WORD, text)


def _limit_repeated_sequence_indices(
    tokens: List[str],
    *,
    max_repetitions: int = 3,
    max_sequence_length: int = 4,
) -> List[int]:
    """Return indices that keep short repeated phrases under control.

    Some Whisper models, especially the smaller variants, occasionally produce
    a very short phrase over and over ("hi rachael" repeated dozens of times).
    Downstream Unity logic then interprets the transcription as an excessively
    long command.  To keep that in check while preserving genuine repetitions,
    we detect short repeated sequences and keep only the first few occurrences.
    """

    if not tokens:
        return []

    n = len(tokens)
    normalized = [
        token.strip().lower() if isinstance(token, str) else ""
        for token in tokens
    ]
    keep: List[int] = []
    i = 0

    while i < n:
        best_repeat = 1
        best_length = 1

        max_span = min(max_sequence_length, n - i)
        for span in range(1, max_span + 1):
            sequence = normalized[i : i + span]
            repeats = 1
            j = i + span

            while j + span <= n and normalized[j : j + span] == sequence:
                repeats += 1
                j += span

            if repeats > best_repeat or (repeats == best_repeat and span > best_length):
                best_repeat = repeats
                best_length = span

        allowed_repeats = min(best_repeat, max_repetitions)
        for repeat_index in range(allowed_repeats):
            for offset in range(best_length):
                keep.append(i + repeat_index * best_length + offset)

        i += best_length * best_repeat

    keep.sort()
    return keep


def _build_wake_word_pattern() -> re.Pattern[str]:
    terms = []
    for term in {WAKE_WORD, *WAKE_WORD_ALIASES}:
        stripped = term.strip()
        if not stripped:
            continue
        pieces = [re.escape(piece) for piece in stripped.split() if piece]
        if not pieces:
            continue
        if len(pieces) == 1:
            pattern = pieces[0]
        else:
            # Allow variable whitespace between the pieces so variants like "ra chel"
            # collapse to the canonical wake word as well.
            pattern = r"\s*".join(pieces)
        terms.append(pattern)

    if not terms:
        terms.append(re.escape(WAKE_WORD))

    combined = "|".join(sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{combined})(?!\w)", re.IGNORECASE)


_WAKE_WORD_REGEX = _build_wake_word_pattern()


def _canonicalize_wake_words(text: str) -> str:
    if not text:
        return text
    return _WAKE_WORD_REGEX.sub(WAKE_WORD, text)


def _environment_int(key: str, default: int) -> int:
    value = os.getenv(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _positive_or_zero(value: int) -> int:
    return value if value >= 0 else 0


def _non_negative_float(value: float) -> float:
    return value if value >= 0.0 else 0.0


WHISPER_NO_REPEAT_NGRAM_SIZE = _positive_or_zero(
    _environment_int("WHISPER_NO_REPEAT_NGRAM_SIZE", 3)
)
WHISPER_REPETITION_PENALTY = max(1.0, _environment_float("WHISPER_REPETITION_PENALTY", 1.05))
WHISPER_LENGTH_PENALTY = _non_negative_float(
    _environment_float("WHISPER_LENGTH_PENALTY", 1.0)
)


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


def _piper_http_base_url() -> str:
    # Base URL for the Piper HTTP wrapper (piper_http.py)
    # Defaults to local instance started by scripts/start_local_services.py
    return _environment("PIPER_HTTP_URL", "http://127.0.0.1:5005").rstrip("/")


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
        client = _AsyncHttpClient.get()
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
    # Warm the shared HTTP client so the first request can reuse an existing connection.
    _AsyncHttpClient.get()


@app.on_event("shutdown")
async def _shutdown_event() -> None:
    await _AsyncHttpClient.aclose()


@app.get("/healthz")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


def _wake_word_prompt() -> str:
    # Include aliases to help Whisper bias toward the expected wake word variants.
    unique_terms = sorted({WAKE_WORD, *WAKE_WORD_ALIASES})
    prompt_terms: list[str] = []
    prompt_terms.extend(unique_terms)

    for prefix in WAKE_WORD_PREFIXES:
        if not prefix:
            continue
        for term in unique_terms:
            prompt_terms.append(f"{prefix} {term}")

    prompt_terms.extend(["open", "play", "back", "quit", "close", "shut", "down"])

    # Deduplicate while preserving the first occurrence order so the prompt stays predictable.
    ordered_terms = list(dict.fromkeys(prompt_terms))
    return " ".join(ordered_terms)


def _normalize_language(language: Optional[str]) -> Optional[str]:
    if language is None:
        return None

    normalized = language.strip()
    if not normalized:
        return None

    if normalized.lower() in {"auto", "detect"}:
        return None

    return normalized


def _collect_avg_logprobs(segments: Iterable) -> List[float]:
    values: List[float] = []
    for segment in segments:
        if segment.avg_logprob is None:
            continue
        try:
            values.append(float(segment.avg_logprob))
        except (TypeError, ValueError):
            continue
    return values


def _collect_segment_texts(segments: Iterable) -> List[str]:
    texts: List[str] = []
    for segment in segments:
        text = getattr(segment, "text", "")
        if not isinstance(text, str):
            continue
        stripped = text.strip()
        if stripped:
            texts.append(stripped)
    return texts


def _mean(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _looks_like_meaningful_text(text: str) -> bool:
    if not text:
        return False

    stripped = text.strip()
    if not stripped:
        return False

    if stripped.lower() in {WAKE_WORD, *WAKE_WORD_ALIASES}:
        return True

    alpha_numeric = sum(ch.isalnum() for ch in stripped)
    if alpha_numeric >= 3:
        return True

    return len(stripped.split()) >= 2


def _tokenize_for_repetition(text: str) -> List[str]:
    if not text:
        return []
    return [match.group(0) for match in _REPETITION_TOKEN_PATTERN.finditer(text.lower())]


def _has_consecutive_repetition(tokens: List[str], window: int) -> bool:
    if window <= 0:
        return False
    limit = len(tokens) - (2 * window) + 1
    for index in range(max(0, limit)):
        first = tokens[index : index + window]
        second = tokens[index + window : index + (2 * window)]
        if first == second:
            return True
    return False


def _should_retry_for_repetition(text: str, compression_ratio: Optional[float]) -> bool:
    tokens = _tokenize_for_repetition(text)
    if len(tokens) < 4:
        return False

    for window in (1, 2, 3):
        if _has_consecutive_repetition(tokens, window):
            return True

    if compression_ratio is not None and compression_ratio >= 2.4 and len(tokens) >= 6:
        return True

    unique_count = len(set(tokens))
    if unique_count <= len(tokens) // 3 and len(tokens) >= 6:
        return True

    return False


def _audio_energy_metrics(audio: np.ndarray) -> tuple[float, float]:
    if audio.size == 0:
        return 0.0, 0.0

    # Ensure calculations happen in float64 to avoid precision loss for tiny signals.
    squared = np.square(audio, dtype=np.float64)
    rms = float(np.sqrt(np.mean(squared))) if squared.size else 0.0
    max_amplitude = float(np.max(np.abs(audio)))
    return rms, max_amplitude


LOW_CONFIDENCE_THRESHOLD = -0.6


def _resample_audio(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate or samples.size == 0:
        return samples

    if resample_poly is not None:
        # Use polyphase filtering with a Kaiser window to minimize aliasing and ringing artefacts.
        gcd = math.gcd(source_rate, target_rate)
        up = target_rate // gcd
        down = source_rate // gcd
        resampled = resample_poly(samples, up, down, window=("kaiser", 8.0))
        return np.asarray(resampled, dtype=np.float32)

    # Fallback to linear interpolation if SciPy is unavailable.
    duration_seconds = samples.shape[0] / float(source_rate)
    target_length = max(1, int(math.ceil(duration_seconds * target_rate)))
    source_indices = np.linspace(0, samples.shape[0] - 1, num=samples.shape[0], dtype=np.float64)
    target_indices = np.linspace(0, samples.shape[0] - 1, num=target_length, dtype=np.float64)
    resampled = np.interp(target_indices, source_indices, samples)
    return resampled.astype(np.float32, copy=False)


def _build_vosk_result(words: Iterable[dict]) -> List[dict]:
    # Vosk uses "result" for word-level entries. Unity expects "word" and timing fields.
    return list(words)


def _run_transcription(
    model: WhisperModel,
    audio: np.ndarray,
    beam_size: int,
    language: Optional[str],
    temperature_schedule: tuple[float, ...],
    overrides: Optional[Dict[str, object]] = None,
):
    best_of = 1 if all(temp <= 0.0 for temp in temperature_schedule) else max(beam_size, 5)
    transcription_kwargs = {
        "beam_size": beam_size,
        "language": language,
        "task": "transcribe",
        "word_timestamps": True,
        "vad_filter": True,
        "vad_parameters": {"min_silence_duration_ms": 300, "speech_pad_ms": 200},
        "initial_prompt": _wake_word_prompt(),
        "temperature": temperature_schedule,
        "best_of": best_of,
        "length_penalty": WHISPER_LENGTH_PENALTY,
        "repetition_penalty": WHISPER_REPETITION_PENALTY,
    }

    if WHISPER_NO_REPEAT_NGRAM_SIZE > 0:
        transcription_kwargs["no_repeat_ngram_size"] = WHISPER_NO_REPEAT_NGRAM_SIZE

    if overrides:
        for key, value in overrides.items():
            transcription_kwargs[key] = value

    segments_generator, info = model.transcribe(
        audio,
        **transcription_kwargs,
    )
    return list(segments_generator), info


def _should_retry_transcription(
    avg_logprob: Optional[float],
    language_probability: Optional[float],
    has_speech: bool,
) -> bool:
    if not has_speech:
        return False

    if avg_logprob is not None and avg_logprob < LOW_CONFIDENCE_THRESHOLD:
        return True

    if language_probability is not None and language_probability < 0.45:
        return True

    return False


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

    rms, max_amplitude = _audio_energy_metrics(audio)

    model = _load_model()

    normalized_language = _normalize_language(language)
    effective_beam_size = max(1, min(beam_size, 10))
    primary_beam_size = max(5, effective_beam_size)

    primary_temperatures: tuple[float, ...] = (0.0,)
    segments, info = _run_transcription(
        model,
        audio,
        beam_size=primary_beam_size,
        language=normalized_language,
        temperature_schedule=primary_temperatures,
    )

    avg_logprob_values = _collect_avg_logprobs(segments)
    avg_logprob = _mean(avg_logprob_values)

    if _should_retry_transcription(avg_logprob, getattr(info, "language_probability", None), bool(segments)):
        retry_beam_size = max(primary_beam_size, 8)
        retry_temperatures = (0.0, 0.3, 0.6)
        retry_segments, retry_info = _run_transcription(
            model,
            audio,
            beam_size=retry_beam_size,
            language=normalized_language,
            temperature_schedule=retry_temperatures,
        )
        retry_logprob_values = _collect_avg_logprobs(retry_segments)
        retry_avg_logprob = _mean(retry_logprob_values)

        # Prefer whichever run yields the higher confidence while keeping the richer transcript.
        if (retry_avg_logprob or float("-inf")) >= (avg_logprob or float("-inf")):
            segments = retry_segments
            info = retry_info
            avg_logprob_values = retry_logprob_values
            avg_logprob = retry_avg_logprob

    segment_texts = _collect_segment_texts(segments)
    full_raw_text = " ".join(segment_texts).strip()

    repetition_overrides = {
        "no_repeat_ngram_size": max(2, WHISPER_NO_REPEAT_NGRAM_SIZE),
        "repetition_penalty": max(WHISPER_REPETITION_PENALTY, 1.15),
        "length_penalty": min(WHISPER_LENGTH_PENALTY, 0.85),
        "condition_on_previous_text": False,
        "compression_ratio_threshold": 2.3,
    }

    for _ in range(2):
        if not _should_retry_for_repetition(full_raw_text, getattr(info, "compression_ratio", None)):
            break

        repetition_segments, repetition_info = _run_transcription(
            model,
            audio,
            beam_size=max(primary_beam_size, 8),
            language=normalized_language,
            temperature_schedule=(0.0, 0.2, 0.4),
            overrides=repetition_overrides,
        )

        if not repetition_segments:
            break

        segments = repetition_segments
        info = repetition_info
        segment_texts = _collect_segment_texts(segments)
        full_raw_text = " ".join(segment_texts).strip()
        avg_logprob_values = _collect_avg_logprobs(segments)
        avg_logprob = _mean(avg_logprob_values)

    words: List[dict] = []
    combined_text_parts: List[str] = []
    raw_text_parts: List[str] = []

    for segment in segments:
        text = segment.text.strip()
        if text:
            combined_text_parts.append(text)
            raw_text_parts.append(text)

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

    # Wake word biasing: normalise recognised variants to the configured wake word so Unity
    # only needs to reason about a single spelling. This mirrors the alias configuration
    # exposed by the Python service.
    full_text = " ".join(part for part in combined_text_parts if part).strip()
    if not full_text and words:
        full_text = " ".join(word["word"] for word in words).strip()

    full_text = _canonicalize_wake_words(full_text)

    for word in words:
        original = word.get("word")
        if not isinstance(original, str):
            continue
        canonical = _canonicalize_wake_words(original)
        if canonical != original:
            word["word"] = canonical

    if words:
        token_strings = [word.get("word", "") for word in words]
        keep_indices = _limit_repeated_sequence_indices(token_strings)

        if keep_indices and len(keep_indices) < len(words):
            words = [words[index] for index in keep_indices]
            deduped_tokens = [token_strings[index] for index in keep_indices if token_strings[index]]
            if deduped_tokens:
                full_text = _canonicalize_wake_words(" ".join(deduped_tokens).strip()) or full_text

    response = {
        "text": full_text,
        "result": _build_vosk_result(words),
        "language": getattr(info, "language", normalized_language),
        "duration": getattr(info, "duration", None),
        "language_probability": getattr(info, "language_probability", None),
        "translation": False,
        "rms": rms,
        "max_amplitude": max_amplitude,
    }

    if avg_logprob is not None:
        if avg_logprob >= LOW_CONFIDENCE_THRESHOLD or not _looks_like_meaningful_text(full_text):
            response["avg_logprob"] = float(round(avg_logprob, 4))
        else:
            response["avg_logprob_raw"] = float(round(avg_logprob, 4))

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


# --- Compatibility TTS endpoint -------------------------------------------------
# Some clients call POST /tts expecting a TTS service. Provide a thin proxy to the
# Piper HTTP wrapper so existing integrations keep working without changes.


class TtsRequest(BaseModel):
    text: str = Field(..., min_length=1)


@app.post("/tts")
async def tts(payload: TtsRequest) -> JSONResponse:
    url = f"{_piper_http_base_url()}/speak"
    try:
        client = _AsyncHttpClient.get()
        resp = await client.post(url, json={"text": payload.text})
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to contact Piper at {url}: {exc}") from exc

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text.strip())

    # Proxy Piper's JSON (audio_wav_base64 + sample_rate)
    return JSONResponse(resp.json())


from fastapi.responses import Response  # existing import above includes JSONResponse only


@app.get("/tts")
async def tts_get(text: str) -> Response:
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty text")

    url = f"{_piper_http_base_url()}/speak"
    try:
        client = _AsyncHttpClient.get()
        resp = await client.get(url, params={"text": text})
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to contact Piper at {url}: {exc}") from exc

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text.strip())

    return Response(content=resp.content, media_type="audio/wav")


if __name__ == "__main__":
    import uvicorn

    port = int(_environment("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
