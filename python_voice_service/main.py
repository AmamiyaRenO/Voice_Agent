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
import asyncio
import logging
import time
from collections import deque
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Tuple

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
DEFAULT_OLLAMA_MODEL = "gemma3:4b"
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

def _resolve_whisper_model_path(raw: str) -> str:
    """Resolve WHISPER_MODEL_PATH for faster-whisper.

    Notes:
    - This service uses `faster-whisper` (CTranslate2) models.
    - HuggingFace repos like `openai/whisper-large-v3-turbo` are Transformers checkpoints
      and are NOT directly loadable by faster-whisper.
    - For "large-v3-turbo", use a faster-whisper / CTranslate2 converted repo instead.
      A common choice is `Systran/faster-whisper-large-v3-turbo`.
    """
    value = (raw or "").strip()
    if not value:
        return "large-v3"

    lower = value.lower()
    # User pasted HF URL
    if lower.startswith("https://huggingface.co/"):
        value = value[len("https://huggingface.co/") :].strip().strip("/")
        lower = value.lower()

    # Map the Transformers repo to a faster-whisper compatible repo.
    if lower == "openai/whisper-large-v3-turbo":
        try:
            logger.warning(
                "WHISPER_MODEL_PATH=%r is a Transformers checkpoint; "
                "mapping to faster-whisper repo %r",
                raw,
                "Systran/faster-whisper-large-v3-turbo",
            )
        except Exception:
            pass
        return "Systran/faster-whisper-large-v3-turbo"

    # Allow shorthand for turbo if user provides it.
    if lower in {"large-v3-turbo", "whisper-large-v3-turbo"}:
        return "Systran/faster-whisper-large-v3-turbo"

    return value


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


logger = logging.getLogger("coach_voice_service")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


app = FastAPI(title=APP_TITLE)


def _environment(key: str, default: str) -> str:
    value = os.getenv(key)
    return value.strip() if value is not None else default


logger.setLevel(
    getattr(logging, _environment("VOICE_SERVICE_LOG_LEVEL", "INFO").upper(), logging.INFO)
)


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
        "rachel, rachael, richel, richelle, rachal, raychel, ra chel, rach el",
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
_KNOWN_HALLUCINATION_PHRASES = {
    "thanks for watching",
    "thank you for watching",
}


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


def _environment_bool(key: str, default: bool) -> bool:
    value = os.getenv(key)
    if value is None:
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "f", "no", "n", "off"}:
        return False
    return default


RESPOND_METRICS_SIZE = max(10, _environment_int("RESPOND_METRICS_SIZE", 200))
RESPOND_METRICS = deque(maxlen=RESPOND_METRICS_SIZE)
RESPOND_METRICS_LOCK = asyncio.Lock()
RESPOND_METRICS_STARTED_AT = time.perf_counter()
RESPOND_METRICS_TOTAL = 0
RESPOND_METRICS_ERRORS = 0


WHISPER_NO_REPEAT_NGRAM_SIZE = _positive_or_zero(
    _environment_int("WHISPER_NO_REPEAT_NGRAM_SIZE", 4)
)
WHISPER_REPETITION_PENALTY = max(1.0, _environment_float("WHISPER_REPETITION_PENALTY", 1.15))
WHISPER_LENGTH_PENALTY = _non_negative_float(
    _environment_float("WHISPER_LENGTH_PENALTY", 1.0)
)
WHISPER_CPU_THREADS = max(1, _environment_int("WHISPER_CPU_THREADS", os.cpu_count() or 4))
WHISPER_MAX_AUDIO_SECONDS = _non_negative_float(
    _environment_float("WHISPER_MAX_AUDIO_SECONDS", 12.0)
)
WHISPER_VAD_SILENCE_MS = _positive_or_zero(
    _environment_int("WHISPER_VAD_SILENCE_MS", 300)
)
WHISPER_VAD_MIN_SPEECH_MS = _positive_or_zero(
    _environment_int("WHISPER_VAD_MIN_SPEECH_MS", 150)
)
WHISPER_RECENT_WINDOW_PAD_MS = _positive_or_zero(
    _environment_int("WHISPER_RECENT_WINDOW_PAD_MS", 200)
)
WHISPER_RECENT_WINDOW_MAX_GAP_MS = _positive_or_zero(
    _environment_int("WHISPER_RECENT_WINDOW_MAX_GAP_MS", 1200)
)
WHISPER_CONDITION_ON_PREVIOUS_TEXT = _environment_bool(
    "WHISPER_CONDITION_ON_PREVIOUS_TEXT", False
)


def _positive_or_zero(value: int) -> int:
    return value if value >= 0 else 0


def _non_negative_float(value: float) -> float:
    return value if value >= 0.0 else 0.0


WHISPER_NO_REPEAT_NGRAM_SIZE = _positive_or_zero(
    _environment_int("WHISPER_NO_REPEAT_NGRAM_SIZE", 4)
)
WHISPER_REPETITION_PENALTY = max(1.0, _environment_float("WHISPER_REPETITION_PENALTY", 1.15))
WHISPER_LENGTH_PENALTY = _non_negative_float(
    _environment_float("WHISPER_LENGTH_PENALTY", 1.0)
)

# Optional hotwords/bias terms (comma or whitespace separated). Applied on top of initial_prompt.
WHISPER_HOTWORDS = os.getenv("WHISPER_HOTWORDS", "").strip()

# Retry thresholds (layered decoding)
WHISPER_LOW_CONFIDENCE_THRESHOLD = _environment_float("WHISPER_LOW_CONFIDENCE_THRESHOLD", -0.6)
WHISPER_RETRY_BEAM_BONUS = max(0, _environment_int("WHISPER_RETRY_BEAM_BONUS", 3))
WHISPER_RETRY_MAX_BEAM = max(1, _environment_int("WHISPER_RETRY_MAX_BEAM", 10))
WHISPER_RETRY_TEMPERATURES = tuple(
    float(x)
    for x in (
        os.getenv("WHISPER_RETRY_TEMPERATURES", "0.0,0.2,0.4").split(",")
        if os.getenv("WHISPER_RETRY_TEMPERATURES") is not None
        else ["0.0", "0.2", "0.4"]
    )
    if str(x).strip() != ""
)

# Streaming/session glue: allow overlap and carry over some context across adjacent chunks.
WHISPER_STREAM_OVERLAP_SECONDS = _non_negative_float(
    _environment_float("WHISPER_STREAM_OVERLAP_SECONDS", 0.8)
)
WHISPER_STREAM_SESSION_TTL_SECONDS = _non_negative_float(
    _environment_float("WHISPER_STREAM_SESSION_TTL_SECONDS", 12.0)
)
WHISPER_STREAM_MAX_SESSIONS = max(4, _environment_int("WHISPER_STREAM_MAX_SESSIONS", 32))
WHISPER_STREAM_CONTEXT_CHARS = max(0, _environment_int("WHISPER_STREAM_CONTEXT_CHARS", 220))
WHISPER_SUPPRESS_KNOWN_HALLUCINATIONS = _environment_bool(
    "WHISPER_SUPPRESS_KNOWN_HALLUCINATIONS", True
)
WHISPER_HALLUCINATION_MAX_SPEECH_FRACTION = _non_negative_float(
    _environment_float("WHISPER_HALLUCINATION_MAX_SPEECH_FRACTION", 0.2)
)
WHISPER_HALLUCINATION_MAX_AMPLITUDE = _non_negative_float(
    _environment_float("WHISPER_HALLUCINATION_MAX_AMPLITUDE", 0.08)
)
WHISPER_HALLUCINATION_MAX_RMS = _non_negative_float(
    _environment_float("WHISPER_HALLUCINATION_MAX_RMS", 0.015)
)
WHISPER_HALLUCINATION_MAX_EXTRA_TOKENS = max(
    0, _environment_int("WHISPER_HALLUCINATION_MAX_EXTRA_TOKENS", 3)
)


def _parse_hotwords(raw: str) -> Optional[List[str]]:
    value = (raw or "").strip()
    if not value:
        return None
    # allow commas or whitespace as separators
    parts = re.split(r"[,\s]+", value)
    hotwords = [p.strip() for p in parts if p and p.strip()]
    return hotwords or None


class _SessionState:
    __slots__ = ("last_ts", "last_text", "audio_tail")

    def __init__(self) -> None:
        self.last_ts: float = 0.0
        self.last_text: str = ""
        self.audio_tail: Optional[np.ndarray] = None


_SESSIONS: Dict[str, _SessionState] = {}


def _get_session(session_id: Optional[str]) -> _SessionState:
    key = (session_id or "default").strip()[:64] or "default"
    state = _SESSIONS.get(key)
    if state is None:
        state = _SessionState()
        _SESSIONS[key] = state
    return state


def _prune_sessions(now: float) -> None:
    if not _SESSIONS:
        return
    ttl = WHISPER_STREAM_SESSION_TTL_SECONDS
    if ttl <= 0:
        return
    expired = [k for k, v in _SESSIONS.items() if now - (v.last_ts or 0.0) > ttl]
    for k in expired:
        _SESSIONS.pop(k, None)
    # cap size (simple LRU by last_ts)
    if len(_SESSIONS) > WHISPER_STREAM_MAX_SESSIONS:
        ordered = sorted(_SESSIONS.items(), key=lambda kv: kv[1].last_ts)
        for k, _ in ordered[: max(0, len(_SESSIONS) - WHISPER_STREAM_MAX_SESSIONS)]:
            _SESSIONS.pop(k, None)


@lru_cache(maxsize=1)
def _load_model() -> WhisperModel:
    model_path = _resolve_whisper_model_path(_environment("WHISPER_MODEL_PATH", "large-v3"))
    compute_type = _environment("WHISPER_COMPUTE_TYPE", "int8_float16")
    device_pref = _environment("WHISPER_DEVICE", "auto").lower()

    def _cpu_compute(ct: str) -> str:
        # If compute_type is tuned for GPU (e.g., float16), pick a CPU-friendly default
        return "int8" if "float16" in ct.lower() else ct

    def _raise_load_error(exc: Exception) -> "WhisperModel":
        msg = (
            "Failed to load Faster-Whisper model.\n"
            f"- WHISPER_MODEL_PATH={model_path}\n"
            "If your machine is offline / outgoing traffic is disabled, you must pre-download the model.\n"
            "Run:\n"
            "  python scripts\\download_whisper_model.py --repo Systran/faster-distil-whisper-large-v3\n"
            "Then set:\n"
            "  $env:WHISPER_MODEL_PATH=\"<downloaded_folder>\"\n"
            "and restart the service."
        )
        raise RuntimeError(msg) from exc

    # Explicit CPU request
    if device_pref == "cpu":
        try:
            model = WhisperModel(
                model_path,
                device="cpu",
                compute_type=_cpu_compute(compute_type),
                cpu_threads=WHISPER_CPU_THREADS,
            )
        except Exception as exc:
            _raise_load_error(exc)
        try:
            print(
                "[VoiceService] Loaded Faster-Whisper "
                f"model={model_path} device=cpu compute_type={_cpu_compute(compute_type)} "
                f"cpu_threads={WHISPER_CPU_THREADS}"
            )
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
        try:
            model = WhisperModel(
                model_path,
                device="cpu",
                compute_type=_cpu_compute(compute_type),
                cpu_threads=WHISPER_CPU_THREADS,
            )
        except Exception as exc2:
            _raise_load_error(exc2)
        try:
            print(
                "[VoiceService] Loaded Faster-Whisper "
                f"model={model_path} device=cpu compute_type={_cpu_compute(compute_type)} "
                f"cpu_threads={WHISPER_CPU_THREADS} (fallback from CUDA: {exc})"
            )
        except Exception:
            pass
        return model


class RespondRequest(BaseModel):
    text: str = Field(..., min_length=1, description="User transcript to send to the coach agent")
    system: Optional[str] = Field(default=None, description="Optional system prompt override for the LLM.")


class RespondResponse(BaseModel):
    text: str
    generation_seconds: Optional[float] = Field(default=None, ge=0.0)


class RespondConfigRequest(BaseModel):
    system_prompt: Optional[str] = Field(default=None, description="Runtime system prompt override.")
    prompt: Optional[str] = Field(default=None, description="Alias of system_prompt.")
    reset: bool = Field(default=False, description="Clear runtime override and use env/default prompt.")


class RespondConfigResponse(BaseModel):
    status: str = "ok"
    system_prompt: str
    runtime_override_active: bool
    source: str


class OllamaError(RuntimeError):
    pass


def _ollama_base_url() -> str:
    return _environment("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL).rstrip("/")


def _ollama_model() -> str:
    return _environment("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)


def _ollama_system_prompt() -> str:
    return _environment("OLLAMA_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)


_RUNTIME_OLLAMA_SYSTEM_PROMPT: Optional[str] = None
_RUNTIME_OLLAMA_SYSTEM_PROMPT_LOCK = asyncio.Lock()


async def _set_runtime_ollama_system_prompt(value: Optional[str]) -> None:
    global _RUNTIME_OLLAMA_SYSTEM_PROMPT
    async with _RUNTIME_OLLAMA_SYSTEM_PROMPT_LOCK:
        _RUNTIME_OLLAMA_SYSTEM_PROMPT = value


async def _get_runtime_ollama_system_prompt() -> Optional[str]:
    async with _RUNTIME_OLLAMA_SYSTEM_PROMPT_LOCK:
        return _RUNTIME_OLLAMA_SYSTEM_PROMPT


async def _get_effective_ollama_system_prompt() -> tuple[str, bool, str]:
    runtime_value = await _get_runtime_ollama_system_prompt()
    if runtime_value is not None:
        normalized = runtime_value.strip()
        if normalized:
            return normalized, True, "runtime"
    return _ollama_system_prompt(), False, "env_or_default"


def _piper_http_base_url() -> str:
    # Base URL for the Piper HTTP wrapper (piper_http.py)
    # Defaults to local instance started by scripts/start_local_services.py
    return _environment("PIPER_HTTP_URL", "http://127.0.0.1:5005").rstrip("/")


async def _generate_coach_reply(user_text: str, system_override: Optional[str] = None) -> str:
    keep_alive = _environment("OLLAMA_KEEP_ALIVE", "30m")
    effective_system_prompt, _, _ = await _get_effective_ollama_system_prompt()
    payload = {
        "model": _ollama_model(),
        "system": (system_override or "").strip() or effective_system_prompt,
        "prompt": f"User: {user_text}\nCoach:",
        "stream": False,
        "options": {
            "temperature": _environment_float("OLLAMA_TEMPERATURE", 0.6),
            "top_p": _environment_float("OLLAMA_TOP_P", 0.9),
            "top_k": _environment_int("OLLAMA_TOP_K", 40),
            "num_predict": _environment_int("OLLAMA_MAX_TOKENS", 120),
            "repeat_penalty": _environment_float("OLLAMA_REPEAT_PENALTY", 1.1),
        },
    }
    if keep_alive:
        payload["keep_alive"] = keep_alive

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

    prompt_terms.extend(["open", "back", "cornhole", "disc golf", "disc",  "golf"])

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


def _normalize_phrase_for_match(text: str) -> str:
    if not text:
        return ""
    normalized = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    return " ".join(normalized.split())


def _is_known_hallucination_phrase(text: str) -> bool:
    normalized = _normalize_phrase_for_match(text)
    if not normalized:
        return False
    return normalized in _KNOWN_HALLUCINATION_PHRASES


def _matches_known_hallucination_fragment(text: str) -> tuple[bool, str]:
    normalized = _normalize_phrase_for_match(text)
    if not normalized:
        return False, ""

    for phrase in _KNOWN_HALLUCINATION_PHRASES:
        if normalized == phrase:
            return True, phrase

    normalized_tokens = normalized.split()
    normalized_token_count = len(normalized_tokens)
    if normalized_token_count == 0:
        return False, ""

    for phrase in _KNOWN_HALLUCINATION_PHRASES:
        if phrase not in normalized:
            continue
        phrase_token_count = len(phrase.split())
        # Allow a few extra words around a known hallucination phrase.
        if normalized_token_count <= phrase_token_count + WHISPER_HALLUCINATION_MAX_EXTRA_TOKENS:
            return True, phrase
    return False, ""


def _tokenize_for_repetition(text: str) -> List[str]:
    if not text:
        return []
    return [match.group(0) for match in _REPETITION_TOKEN_PATTERN.finditer(text.lower())]


def _dominant_repetition_window(tokens: List[str]) -> Optional[int]:
    if len(tokens) < 4:
        return None

    max_window = min(4, len(tokens) // 2)
    for window in range(1, max_window + 1):
        index = window
        repeats = 0
        while index + window <= len(tokens) and tokens[index : index + window] == tokens[index - window : index]:
            repeats += 1
            index += window

        if repeats == 0:
            continue

        coverage = window * (repeats + 1)
        trailing = len(tokens) - coverage
        if coverage >= int(len(tokens) * 0.6) and trailing <= max(1, window // 2):
            return window

    return None


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


def _collapse_repetitive_output(text: str, words: List[dict]) -> tuple[str, List[dict]]:
    if not text:
        return text, words

    word_tokens: List[str] = []
    for word in words:
        raw_word = word.get("word")
        if isinstance(raw_word, str) and raw_word.strip():
            word_tokens.append(raw_word.strip().lower())

    window = _dominant_repetition_window(word_tokens)
    if window is not None and window > 0:
        trimmed_words = words[:window]
        collapsed_text = " ".join(
            word["word"].strip() for word in trimmed_words if isinstance(word.get("word"), str)
        ).strip()
        if collapsed_text:
            return collapsed_text, trimmed_words

    tokens = _tokenize_for_repetition(text)
    window = _dominant_repetition_window(tokens)
    if window is None or window <= 0:
        return text, words

    collapsed_tokens = tokens[:window]
    collapsed_text = " ".join(collapsed_tokens).strip()
    return collapsed_text if collapsed_text else text, words


def _limit_repeated_sequence_indices(tokens: List[str]) -> List[int]:
    """Return indices that keep only the first occurrence of repeated sequences.

    Whisper can occasionally emit the same 1–4 token sequence multiple times in
    a row.  When that happens we keep the first instance of the repeated block
    and discard the subsequent duplicates so the Unity client does not surface
    "echoed" words to the player.
    """

    total = len(tokens)
    if total == 0:
        return []

    keep_mask = [True] * total
    max_window = min(4, total // 2) or 1

    for window in range(1, max_window + 1):
        index = 0
        while index + (2 * window) <= total:
            first = tokens[index : index + window]
            second = tokens[index + window : index + (2 * window)]
            if first != second:
                index += 1
                continue

            # Mark all subsequent consecutive repetitions of the same window
            # for removal, keeping only the first occurrence.
            repeat_index = index + window
            while repeat_index + window <= total and tokens[repeat_index : repeat_index + window] == first:
                for drop in range(repeat_index, repeat_index + window):
                    keep_mask[drop] = False
                repeat_index += window
            index = repeat_index

    return [idx for idx, keep in enumerate(keep_mask) if keep]


def _audio_energy_metrics(audio: np.ndarray) -> tuple[float, float]:
    if audio.size == 0:
        return 0.0, 0.0

    # Ensure calculations happen in float64 to avoid precision loss for tiny signals.
    squared = np.square(audio, dtype=np.float64)
    rms = float(np.sqrt(np.mean(squared))) if squared.size else 0.0
    max_amplitude = float(np.max(np.abs(audio)))
    return rms, max_amplitude


LOW_CONFIDENCE_THRESHOLD = float(WHISPER_LOW_CONFIDENCE_THRESHOLD)


def _energy_speech_fraction(audio: np.ndarray, sample_rate: int) -> float:
    """Estimate how much of the clip looks like speech based on short-time energy.

    Returns a value in [0,1]. This is intentionally simple and fast; it's only used
    to decide whether we should retry transcription when VAD returns empty output.
    """
    if audio.size == 0:
        return 0.0

    frame_ms = 30
    hop_ms = 15
    frame_length = max(1, int(sample_rate * frame_ms / 1000))
    hop_length = max(1, int(sample_rate * hop_ms / 1000))

    if audio.size <= frame_length * 2:
        return 0.0

    starts = list(range(0, audio.size - frame_length + 1, hop_length))
    if not starts:
        return 0.0

    energies = np.empty(len(starts), dtype=np.float32)
    for idx, start in enumerate(starts):
        frame = audio[start : start + frame_length]
        energies[idx] = float(np.sqrt(np.mean(np.square(frame)))) if frame.size else 0.0

    high_energy = float(np.percentile(energies, 90)) if energies.size else 0.0
    # If the whole clip is extremely quiet, treat it as non-speech.
    if high_energy <= 0.0008:
        return 0.0

    threshold = max(0.0008, high_energy * 0.35)
    speech_mask = energies >= threshold
    if not speech_mask.any():
        return 0.0

    return float(np.mean(speech_mask))


def _extract_recent_speech_window(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    if audio.size == 0:
        return audio

    trimmed = audio

    if WHISPER_MAX_AUDIO_SECONDS > 0.0:
        max_samples = int(WHISPER_MAX_AUDIO_SECONDS * sample_rate)
        if max_samples > 0 and audio.size > max_samples:
            trimmed = audio[-max_samples:]
    else:
        max_samples = trimmed.size

    if trimmed.size == 0:
        return trimmed

    frame_ms = 30
    hop_ms = 15
    frame_length = max(1, int(sample_rate * frame_ms / 1000))
    hop_length = max(1, int(sample_rate * hop_ms / 1000))

    if trimmed.size <= frame_length * 2:
        return trimmed

    starts = list(range(0, trimmed.size - frame_length + 1, hop_length))
    if not starts:
        return trimmed

    energies = np.empty(len(starts), dtype=np.float32)
    for index, start in enumerate(starts):
        frame = trimmed[start : start + frame_length]
        energies[index] = float(np.sqrt(np.mean(np.square(frame)))) if frame.size else 0.0

    high_energy = float(np.percentile(energies, 90)) if energies.size else 0.0
    if high_energy <= 0.0005:
        return trimmed

    threshold = max(0.0005, high_energy * 0.35)

    speech_mask = energies >= threshold
    if not speech_mask.any():
        return trimmed

    min_silence_frames = max(1, int(max(WHISPER_VAD_SILENCE_MS, hop_ms) / hop_ms))
    min_speech_frames = max(1, int(max(WHISPER_VAD_MIN_SPEECH_MS, hop_ms) / hop_ms))

    segments: List[Tuple[int, int]] = []
    start_frame: Optional[int] = None
    silence_run = 0

    for idx, is_speech in enumerate(speech_mask):
        if is_speech:
            if start_frame is None:
                start_frame = idx
            silence_run = 0
            continue

        if start_frame is None:
            continue

        silence_run += 1
        if silence_run < min_silence_frames:
            continue

        end_frame = idx - silence_run
        if end_frame < start_frame:
            start_frame = None
            silence_run = 0
            continue

        frame_count = end_frame - start_frame + 1
        if frame_count >= min_speech_frames:
            start_sample = starts[start_frame]
            end_sample = starts[end_frame] + frame_length
            segments.append((start_sample, min(trimmed.size, end_sample)))

        start_frame = None
        silence_run = 0

    if start_frame is not None:
        end_frame = len(starts) - 1 - (silence_run if silence_run else 0)
        end_frame = max(end_frame, start_frame)
        frame_count = end_frame - start_frame + 1
        if frame_count >= min_speech_frames:
            start_sample = starts[start_frame]
            end_sample = starts[end_frame] + frame_length
            segments.append((start_sample, min(trimmed.size, end_sample)))

    if not segments:
        return trimmed

    last_start, last_end = segments[-1]

    max_gap_samples = int((WHISPER_RECENT_WINDOW_MAX_GAP_MS / 1000.0) * sample_rate)
    if max_gap_samples > 0:
        for start_sample, end_sample in reversed(segments[:-1]):
            if last_start - end_sample > max_gap_samples:
                break
            last_start = start_sample
            last_end = max(last_end, end_sample)

    if WHISPER_MAX_AUDIO_SECONDS > 0.0:
        max_samples = int(WHISPER_MAX_AUDIO_SECONDS * sample_rate)
        if max_samples > 0 and last_end - last_start > max_samples:
            last_start = max(0, last_end - max_samples)

    pad_samples = int((WHISPER_RECENT_WINDOW_PAD_MS / 1000.0) * sample_rate)
    if pad_samples > 0:
        last_start = max(0, last_start - pad_samples)
        last_end = min(trimmed.size, last_end + pad_samples)

    window_start = last_start
    window_end = last_end

    if WHISPER_MAX_AUDIO_SECONDS > 0.0:
        max_samples = int(WHISPER_MAX_AUDIO_SECONDS * sample_rate)
        if max_samples > 0 and window_end - window_start > max_samples:
            window_start = max(0, window_end - max_samples)

    if window_end - window_start < frame_length:
        return trimmed

    return trimmed[window_start:window_end]


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


def _remove_dc(audio: np.ndarray) -> np.ndarray:
    if audio.size == 0:
        return audio
    # DC offset hurts VAD and decoding a bit; safe to remove.
    mean = float(np.mean(audio, dtype=np.float64))
    if abs(mean) < 1e-6:
        return audio
    return (audio - mean).astype(np.float32, copy=False)


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
    best_of = 1 if all(temp <= 0.0 for temp in temperature_schedule) else max(beam_size, 1)
    transcription_kwargs: Dict[str, object] = {
        "beam_size": beam_size,
        "language": language,
        "task": "transcribe",
        "word_timestamps": True,
        "vad_filter": True,
        "vad_parameters": {
            "min_silence_duration_ms": int(max(0, WHISPER_VAD_SILENCE_MS)),
            "speech_pad_ms": int(max(0, WHISPER_RECENT_WINDOW_PAD_MS)),
        },
        "initial_prompt": _wake_word_prompt(),
        "temperature": temperature_schedule,
        "best_of": best_of,
        "length_penalty": WHISPER_LENGTH_PENALTY,
        "repetition_penalty": WHISPER_REPETITION_PENALTY,
        "condition_on_previous_text": WHISPER_CONDITION_ON_PREVIOUS_TEXT,
    }

    if WHISPER_NO_REPEAT_NGRAM_SIZE > 0:
        transcription_kwargs["no_repeat_ngram_size"] = WHISPER_NO_REPEAT_NGRAM_SIZE

    if overrides:
        for key, value in overrides.items():
            transcription_kwargs[key] = value

    # hotwords is supported by recent faster-whisper; include only if available.
    hotwords = _parse_hotwords(WHISPER_HOTWORDS)
    if hotwords:
        transcription_kwargs["hotwords"] = hotwords

    try:
        segments_generator, info = model.transcribe(
            audio,
            **transcription_kwargs,
        )
    except TypeError:
        # Compatibility fallback if the installed faster-whisper doesn't support some kwargs (e.g., hotwords).
        transcription_kwargs.pop("hotwords", None)
        segments_generator, info = model.transcribe(audio, **transcription_kwargs)
    return list(segments_generator), info


@app.post("/transcribe")
async def transcribe(
    request: Request,
    sample_rate: int = Query(DEFAULT_SAMPLE_RATE, ge=8000, le=48000),
    language: Optional[str] = Query("en", min_length=1, max_length=8),
    beam_size: int = Query(5, ge=1, le=10),
    vad: bool = Query(True, description="Whether to enable faster-whisper VAD filter (silence trimming)."),
    vad_fallback_retry: bool = Query(True, description="If VAD removes everything and text is empty, retry once with VAD disabled."),
    session_id: Optional[str] = Query(None, description="Client/session id for streaming context and overlap."),
    hotwords: Optional[str] = Query(None, description="Optional per-request hotwords (comma/space separated)."),
) -> JSONResponse:
    start_time = time.perf_counter()
    now = time.time()
    payload = await request.body()
    if not payload:
        raise HTTPException(status_code=400, detail="Empty audio payload")

    audio = np.frombuffer(payload, dtype=np.int16)
    if audio.size == 0:
        raise HTTPException(status_code=400, detail="Invalid audio payload")

    audio = audio.astype(np.float32) / 32768.0
    if sample_rate != DEFAULT_SAMPLE_RATE:
        audio = _resample_audio(audio, sample_rate, DEFAULT_SAMPLE_RATE)

    audio = _remove_dc(audio)

    # Streaming overlap: prepend a small tail from the previous request so boundary words aren't chopped.
    state = _get_session(session_id)
    _prune_sessions(now)
    if (
        WHISPER_STREAM_OVERLAP_SECONDS > 0.0
        and state.audio_tail is not None
        and state.audio_tail.size > 0
        and now - (state.last_ts or 0.0) <= 2.0
    ):
        try:
            audio = np.concatenate([state.audio_tail, audio]).astype(np.float32, copy=False)
        except Exception:
            pass

    audio = _extract_recent_speech_window(audio, DEFAULT_SAMPLE_RATE)

    rms, max_amplitude = _audio_energy_metrics(audio)
    speech_fraction = _energy_speech_fraction(audio, DEFAULT_SAMPLE_RATE)

    model = _load_model()

    normalized_language = _normalize_language(language)
    effective_beam_size = max(1, min(beam_size, 10))
    primary_beam_size = effective_beam_size

    primary_temperatures: tuple[float, ...] = (0.0,)
    segments, info = await asyncio.to_thread(
        _run_transcription,
        model,
        audio,
        primary_beam_size,
        normalized_language,
        primary_temperatures,
        {
            "vad_filter": bool(vad),
            # Allow per-request hotwords override (if provided).
            **({"hotwords": _parse_hotwords(hotwords)} if _parse_hotwords(hotwords) else {}),
            # If we have session context, we can optionally condition on it (helps streaming continuity).
            "initial_prompt": (
                (_wake_word_prompt() + " " + state.last_text[-WHISPER_STREAM_CONTEXT_CHARS :]).strip()
                if WHISPER_STREAM_CONTEXT_CHARS > 0 and state.last_text and now - (state.last_ts or 0.0) <= WHISPER_STREAM_SESSION_TTL_SECONDS
                else _wake_word_prompt()
            ),
        },
    )

    avg_logprob_values = _collect_avg_logprobs(segments)
    avg_logprob = _mean(avg_logprob_values)

    segment_texts = _collect_segment_texts(segments)
    full_raw_text = " ".join(segment_texts).strip()

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

    # If VAD was enabled and we got nothing back, retry once without VAD.
    # This helps when the upstream audio is valid but the built-in VAD is too aggressive.
    if (
        vad
        and vad_fallback_retry
        and not full_text
        and not words
        # Don't retry just because there's "some" volume; require the clip to look speech-like.
        and speech_fraction >= 0.25
        and max_amplitude >= 0.03
        and rms >= 0.006
    ):
        segments2, info2 = await asyncio.to_thread(
            _run_transcription,
            model,
            audio,
            primary_beam_size,
            normalized_language,
            primary_temperatures,
            {"vad_filter": False},
        )

        words2: List[dict] = []
        combined_text_parts2: List[str] = []
        for segment in segments2:
            text2 = segment.text.strip()
            if text2:
                combined_text_parts2.append(text2)
            for word in segment.words or []:
                word_text2 = word.word.strip()
                if not word_text2:
                    continue
                words2.append(
                    {
                        "word": word_text2,
                        "start": max(0.0, float(word.start) if word.start is not None else 0.0),
                        "end": max(0.0, float(word.end) if word.end is not None else 0.0),
                        "confidence": round(float(word.probability), 4) if word.probability is not None else None,
                    }
                )
        full_text2 = " ".join(part for part in combined_text_parts2 if part).strip()
        if not full_text2 and words2:
            full_text2 = " ".join(word["word"] for word in words2).strip()

        if full_text2 or words2:
            segments = segments2
            info = info2
            words = words2
            combined_text_parts = combined_text_parts2
            full_text = full_text2

    if _should_retry_for_repetition(full_raw_text, getattr(info, "compression_ratio", None)):
        collapsed_text, collapsed_words = _collapse_repetitive_output(full_text or full_raw_text, words)
        if collapsed_text and collapsed_text != full_text:
            full_text = collapsed_text
            words = collapsed_words

    # Layered decoding: if confidence is low / output is suspicious, do one stronger retry.
    avg_logprob_for_retry = avg_logprob if avg_logprob is not None else -999.0
    needs_retry = False
    if full_text and avg_logprob_for_retry < LOW_CONFIDENCE_THRESHOLD:
        needs_retry = True
    if not full_text and speech_fraction >= 0.25 and max_amplitude >= 0.03 and rms >= 0.006:
        needs_retry = True
    if _should_retry_for_repetition(full_raw_text, getattr(info, "compression_ratio", None)):
        needs_retry = True

    if needs_retry:
        retry_beam = min(WHISPER_RETRY_MAX_BEAM, max(1, primary_beam_size + WHISPER_RETRY_BEAM_BONUS))
        retry_temps = WHISPER_RETRY_TEMPERATURES if WHISPER_RETRY_TEMPERATURES else (0.0, 0.2, 0.4)
        try:
            segments_r, info_r = await asyncio.to_thread(
                _run_transcription,
                model,
                audio,
                retry_beam,
                normalized_language,
                tuple(retry_temps),
                {
                    "vad_filter": bool(vad),
                    **({"hotwords": _parse_hotwords(hotwords)} if _parse_hotwords(hotwords) else {}),
                    "initial_prompt": (
                        (_wake_word_prompt() + " " + state.last_text[-WHISPER_STREAM_CONTEXT_CHARS :]).strip()
                        if WHISPER_STREAM_CONTEXT_CHARS > 0 and state.last_text and now - (state.last_ts or 0.0) <= WHISPER_STREAM_SESSION_TTL_SECONDS
                        else _wake_word_prompt()
                    ),
                },
            )
            # adopt retry result if it yields more meaningful text or better logprob
            texts_r = _collect_segment_texts(segments_r)
            full_text_r = _canonicalize_wake_words(" ".join(texts_r).strip())
            avg_r_values = _collect_avg_logprobs(segments_r)
            avg_r = _mean(avg_r_values)
            if _looks_like_meaningful_text(full_text_r) and (
                not full_text
                or (avg_r is not None and avg_logprob is not None and avg_r > avg_logprob)
                or len(full_text_r) > len(full_text)
            ):
                segments = segments_r
                info = info_r
                combined_text_parts = [seg.text.strip() for seg in segments if getattr(seg, "text", "").strip()]
                words = []
                for seg in segments:
                    for w in seg.words or []:
                        wtxt = w.word.strip()
                        if not wtxt:
                            continue
                        words.append(
                            {
                                "word": wtxt,
                                "start": max(0.0, float(w.start) if w.start is not None else 0.0),
                                "end": max(0.0, float(w.end) if w.end is not None else 0.0),
                                "confidence": round(float(w.probability), 4) if w.probability is not None else None,
                            }
                        )
                full_text = full_text_r
                full_raw_text = " ".join(texts_r).strip()
                avg_logprob = avg_r
        except Exception:
            # If retry fails, keep primary result.
            pass

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

    if WHISPER_SUPPRESS_KNOWN_HALLUCINATIONS:
        matched_hallucination, matched_phrase = _matches_known_hallucination_fragment(full_text)
        if matched_hallucination:
            normalized_text = _normalize_phrase_for_match(full_text)
            token_count = len(normalized_text.split()) if normalized_text else 0
            phrase_token_count = len(matched_phrase.split()) if matched_phrase else 0
            short_phrase_guard = (
                phrase_token_count > 0
                and token_count <= phrase_token_count + WHISPER_HALLUCINATION_MAX_EXTRA_TOKENS
            )
            low_signal_guard = (
                speech_fraction <= WHISPER_HALLUCINATION_MAX_SPEECH_FRACTION
                and max_amplitude <= WHISPER_HALLUCINATION_MAX_AMPLITUDE
                and rms <= WHISPER_HALLUCINATION_MAX_RMS
            )

            if short_phrase_guard or low_signal_guard:
                logger.info(
                    "Suppressed known hallucination phrase: %r (matched=%r short=%s low_signal=%s speech_fraction=%.3f rms=%.4f max_amp=%.4f)",
                    full_text,
                    matched_phrase,
                    short_phrase_guard,
                    low_signal_guard,
                    speech_fraction,
                    rms,
                    max_amplitude,
                )
                full_text = ""
                words = []

    response = {
        "text": full_text,
        "result": _build_vosk_result(words),
        "language": getattr(info, "language", normalized_language),
        "duration": getattr(info, "duration", None),
        "language_probability": getattr(info, "language_probability", None),
        "translation": False,
        "rms": rms,
        "max_amplitude": max_amplitude,
        "speech_fraction": speech_fraction,
    }

    if avg_logprob is not None:
        # Always include avg_logprob so the Unity-side noise filter can work reliably.
        # (Unity historically only reads "avg_logprob", not "avg_logprob_raw".)
        response["avg_logprob"] = float(round(avg_logprob, 4))

    processing_seconds = round(time.perf_counter() - start_time, 4)
    response["processing_seconds"] = processing_seconds

    logger.info(
        "Transcription finished in %.3fs (language=%s, tokens=%d)",
        processing_seconds,
        response.get("language"),
        len(words),
    )

    # Update session state for streaming continuity
    state.last_ts = now
    state.last_text = full_text or state.last_text
    if WHISPER_STREAM_OVERLAP_SECONDS > 0.0 and audio.size > 0:
        tail_samples = int(DEFAULT_SAMPLE_RATE * WHISPER_STREAM_OVERLAP_SECONDS)
        if tail_samples > 0 and audio.size >= tail_samples:
            state.audio_tail = np.asarray(audio[-tail_samples:], dtype=np.float32)
        elif tail_samples > 0:
            state.audio_tail = np.asarray(audio, dtype=np.float32)

    return JSONResponse(response)


async def _record_respond_metric(
    *,
    user_chars: int,
    reply_chars: int,
    elapsed_ms: float,
    ok: bool,
) -> None:
    global RESPOND_METRICS_TOTAL, RESPOND_METRICS_ERRORS
    async with RESPOND_METRICS_LOCK:
        RESPOND_METRICS_TOTAL += 1
        if not ok:
            RESPOND_METRICS_ERRORS += 1
        RESPOND_METRICS.append(
            {
                "ts": time.time(),
                "user_chars": int(user_chars),
                "reply_chars": int(reply_chars),
                "elapsed_ms": round(float(elapsed_ms), 2),
                "ok": bool(ok),
            }
        )


@app.post("/respond", response_model=RespondResponse)
async def respond(payload: RespondRequest) -> RespondResponse:
    start_time = time.perf_counter()
    user_text = payload.text.strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="Empty text payload")

    try:
        reply = await _generate_coach_reply(user_text, system_override=(payload.system or None))
    except OllamaError as exc:
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        await _record_respond_metric(
            user_chars=len(user_text),
            reply_chars=0,
            elapsed_ms=elapsed_ms,
            ok=False,
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    generation_seconds = round(time.perf_counter() - start_time, 4)
    logger.info("LLM response generated in %.3fs", generation_seconds)
    await _record_respond_metric(
        user_chars=len(user_text),
        reply_chars=len(reply),
        elapsed_ms=generation_seconds * 1000.0,
        ok=True,
    )

    return RespondResponse(text=reply, generation_seconds=generation_seconds)


@app.get("/respond/config", response_model=RespondConfigResponse)
async def get_respond_config() -> RespondConfigResponse:
    system_prompt, runtime_override_active, source = await _get_effective_ollama_system_prompt()
    return RespondConfigResponse(
        status="ok",
        system_prompt=system_prompt,
        runtime_override_active=runtime_override_active,
        source=source,
    )


@app.post("/respond/config", response_model=RespondConfigResponse)
async def set_respond_config(payload: RespondConfigRequest) -> RespondConfigResponse:
    if payload.reset:
        await _set_runtime_ollama_system_prompt(None)
        system_prompt, runtime_override_active, source = await _get_effective_ollama_system_prompt()
        return RespondConfigResponse(
            status="ok",
            system_prompt=system_prompt,
            runtime_override_active=runtime_override_active,
            source=source,
        )

    candidate = (payload.system_prompt or payload.prompt or "").strip()
    if not candidate:
        raise HTTPException(status_code=400, detail="system_prompt (or prompt) is required unless reset=true")

    await _set_runtime_ollama_system_prompt(candidate)
    system_prompt, runtime_override_active, source = await _get_effective_ollama_system_prompt()
    return RespondConfigResponse(
        status="ok",
        system_prompt=system_prompt,
        runtime_override_active=runtime_override_active,
        source=source,
    )


@app.get("/respond/metrics")
async def respond_metrics() -> dict:
    async with RESPOND_METRICS_LOCK:
        entries = list(RESPOND_METRICS)
        total = RESPOND_METRICS_TOTAL
        errors = RESPOND_METRICS_ERRORS

    elapsed_values = [float(e.get("elapsed_ms", 0.0)) for e in entries]
    elapsed_sorted = sorted(elapsed_values)
    count = len(elapsed_sorted)

    def _percentile(p: float) -> float:
        if count == 0:
            return 0.0
        idx = int(round((count - 1) * p))
        idx = max(0, min(count - 1, idx))
        return float(elapsed_sorted[idx])

    avg_ms = (sum(elapsed_values) / len(elapsed_values)) if elapsed_values else 0.0
    uptime = time.perf_counter() - RESPOND_METRICS_STARTED_AT
    return {
        "status": "ok",
        "uptime_seconds": round(float(uptime), 2),
        "totals": {
            "requests": int(total),
            "errors": int(errors),
            "error_ratio": round((errors / total), 4) if total > 0 else 0.0,
        },
        "recent": {
            "window_size": int(count),
            "avg_ms": round(float(avg_ms), 2),
            "p50_ms": round(_percentile(0.50), 2),
            "p95_ms": round(_percentile(0.95), 2),
            "last": entries[-20:],
        },
    }





if __name__ == "__main__":
    import uvicorn

    port = int(_environment("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
