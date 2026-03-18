"""Python voice service using Faster-Whisper for speech recognition.

This module exposes a FastAPI application that accepts raw PCM audio
from the Unity client, performs transcription with Faster-Whisper and
returns a speech JSON payload with legacy compatibility fields.
"""

from __future__ import annotations

import math
import os
import re
import difflib
import asyncio
import logging
import time
import io
import json
import sys
import wave
import threading
from collections import deque
from functools import lru_cache
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional, Tuple

import numpy as np
import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from faster_whisper import WhisperModel

try:
    from .game_grounding import GameCatalog
except Exception:
    from game_grounding import GameCatalog

try:
    from .local_docs_rag import LocalDocsRAG
except Exception:
    from local_docs_rag import LocalDocsRAG

try:
    from .session_context import SessionContextStore
except Exception:
    from session_context import SessionContextStore

try:
    from .service_models import (
        CapabilityRouteDecision,
        ConversationConfigRequest,
        ConversationConfigResponse,
        ConversationTurnRequest,
        OllamaError,
        OpenAIResponseError,
        RespondConfigRequest,
        RespondConfigResponse,
        RespondRequest,
        RespondResponse,
        TranscribeConfigRequest,
        TranscribeConfigResponse,
    )
except Exception:
    from service_models import (
        CapabilityRouteDecision,
        ConversationConfigRequest,
        ConversationConfigResponse,
        ConversationTurnRequest,
        OllamaError,
        OpenAIResponseError,
        RespondConfigRequest,
        RespondConfigResponse,
        RespondRequest,
        RespondResponse,
        TranscribeConfigRequest,
        TranscribeConfigResponse,
    )

try:
    import paho.mqtt.publish as mqtt_publish
except Exception:  # pragma: no cover - MQTT publish is optional at runtime.
    mqtt_publish = None

try:  # Optional dependency used to improve resampling quality when available.
    from scipy.signal import resample_poly
except Exception:  # pragma: no cover - SciPy is optional at runtime.
    resample_poly = None

try:  # Optional dependency when using OpenAI API transcription.
    from openai import AsyncOpenAI
except Exception:  # pragma: no cover - openai is optional at runtime.
    AsyncOpenAI = None

try:  # Optional dependency when using Moonshine offline transcription.
    from moonshine_voice import (
        Transcriber as MoonshineTranscriber,
        get_model_for_language as moonshine_get_model_for_language,
    )
    from moonshine_voice.moonshine_api import (
        ModelArch as MoonshineModelArch,
        model_arch_to_string as moonshine_model_arch_to_string,
    )
except Exception:  # pragma: no cover - moonshine is optional at runtime.
    MoonshineTranscriber = None
    moonshine_get_model_for_language = None
    MoonshineModelArch = None
    moonshine_model_arch_to_string = None

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = REPO_ROOT / "scripts"
INTENT_SERVICE_DIR = SCRIPT_ROOT / "intent_service"
DIALOG_SERVICE_DIR = SCRIPT_ROOT / "dialog_service"
for _module_dir in (SCRIPT_ROOT, INTENT_SERVICE_DIR, DIALOG_SERVICE_DIR):
    _module_dir_str = str(_module_dir)
    if _module_dir_str not in sys.path:
        sys.path.insert(0, _module_dir_str)

try:
    from intent_config import load_config as load_intent_config
    from intent_routing import IntentRouterEngine, ManifestAliasResolver
    from dialog_config import load_config as load_dialog_config
    from dialog_service_impl import DialogService
    from text_utils import (
        compress_reply_by_words,
        compress_reply_for_latency,
        sanitize_tts_text,
        trim_trailing_connectors,
    )
    from user_memory import speaker_identity_key
except Exception:  # pragma: no cover - dialog/intent helpers are optional at runtime.
    load_intent_config = None
    IntentRouterEngine = None
    ManifestAliasResolver = None
    load_dialog_config = None
    DialogService = None
    compress_reply_by_words = None
    compress_reply_for_latency = None
    sanitize_tts_text = None
    trim_trailing_connectors = None
    speaker_identity_key = None

APP_TITLE = "Coach Voice Agent - Python Voice Service"
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "qwen3.5:0.8b"
DEFAULT_OPENAI_RESPONSE_MODEL = "gpt-4o-mini"
DEFAULT_OLLAMA_THINK = False
PIPELINE_MODE_DIRECT_UNIFIED = "direct_unified"
PIPELINE_MODE_LEGACY_MQTT = "legacy_mqtt"
CONVERSATION_PROFILE_LOCAL = "local"
CONVERSATION_PROFILE_CLOUD = "cloud"
DEFAULT_SYSTEM_PROMPT = (
    "You are Rachel, a warm conversational assistant.\n"
    "Priorities:\n"
    "- Understand the user's intent and answer naturally.\n"
    "- Follow the user's lead instead of steering the topic.\n"
    "- Keep spoken replies concise and clear.\n"
    "Behavior rules:\n"
    "- Treat each user turn as part of an ongoing conversation, not an isolated request.\n"
    "- Answer the latest user message on its own terms.\n"
    "- Do not steer back to exercise, rehab, games, setup, or prior recommendations unless the user clearly asks about them.\n"
    "- If the user asks a casual or general question, answer it directly first.\n"
    "- Do not turn vague questions into workout planning, stretching advice, or walking suggestions unless the user explicitly asked for that.\n"
    "- Do not act like a coach, planner, or therapist unless the user clearly asked for coaching or planning help.\n"
    "- If the user asks for help with an exercise plan or workout plan, treat that as a normal planning request, not as a game question.\n"
    "- If the user switches away from games or rejects games, do not bring old game suggestions back unless the user explicitly reopens game talk.\n"
    "- Confirm explicit action intents clearly (start/stop/switch game, back home).\n"
    "- If intent is unclear, ask one short clarification question instead of guessing.\n"
    "- If the user asks for options without saying what kind, ask a short clarification instead of assuming games or exercise.\n"
    "- If the user asks you to introduce yourself, answer about yourself rather than about a game.\n"
    "- Do not append a generic follow-up question after a complete answer unless it is genuinely needed.\n"
    "- Do not repeat or restate the user's question unless you are asking for clarification.\n"
    "- Do not address the user by name unless they explicitly gave it and using it is clearly helpful.\n"
    "- If the user shares a personal event, setback, or feeling, acknowledge that content briefly instead of turning it into generic coaching.\n"
    "- If the user asks about live real-world information you cannot verify here, say that limitation plainly instead of guessing.\n"
    "- For local game availability, recommendations, and game descriptions, rely on the local game catalog context instead of claiming you cannot see the environment.\n"
    "- When answering from memory or structured local data, sound like a person speaking, not a system notice.\n"
    "- Avoid stiff lead-ins such as 'From what I have saved' or 'I don't have access to your personal history'.\n"
    "- Do not claim you can see, check, track, monitor, or verify real-world conditions unless tool or vision context actually provides that information.\n"
    "- When explaining a game or activity, describe what it actually is; do not invent benefits or training claims.\n"
    "- Avoid repetitive motivational slogans.\n"
    "- Default length is 1-2 sentences.\n"
    "- Reply in English only."
)
STRUCTURED_RENDER_SYSTEM_PROMPT = (
    "You rewrite structured assistant results into natural spoken replies.\n"
    "Rules:\n"
    "- Keep every fact exactly true.\n"
    "- Keep every required name, option, status, and limitation intact.\n"
    "- Do not add new facts, medical claims, environment observations, or user-state guesses.\n"
    "- Do not change a game from available into already opening unless the payload explicitly says it is opening.\n"
    "- Use warm, natural spoken language.\n"
    "- Reply in English only.\n"
    "- Use 1 or 2 short sentences unless the payload explicitly allows 3.\n"
    "- No bullet lists, no labels, no JSON, no meta commentary.\n"
    "- Output only the final spoken reply."
)
STRUCTURED_RENDER_DISALLOWED_MARKERS = (
    "i don't have access to your personal history",
    "i do not have access to your personal history",
    "from what i have saved",
    "i can see your environment",
    "i can check your environment",
    "i can track your environment",
    "i know your complete history",
    "i know your full history",
)
STRUCTURED_RENDER_SENTENCE_SPLIT_RE = re.compile(r"[.!?。！？]+")
ANONYMOUS_SESSION_MAX_TURNS = 6
ANONYMOUS_SESSION_MAX_AGE_SEC = 600.0
_SYSTEM_PROMPT_LEAK_MARKERS = (
    "understand the user's intent and answer naturally",
    "keep interactions supportive and safe during exercise",
    "conversation protocol",
    "treat each turn as part of an ongoing dialogue",
    "do not force every topic back to exercise",
    "keep spoken replies concise and clear",
)

DEFAULT_HTTP_TIMEOUT = httpx.Timeout(30.0)
TRANSCRIBE_MODE_API = "api"
TRANSCRIBE_MODE_WHISPER_LARGE_V3 = "whisper-large-v3"
TRANSCRIBE_MODE_MOONSHINE_SMALL = "moonshine-small"
TRANSCRIBE_MODE_MOONSHINE_MEDIUM = "moonshine-medium"
TRANSCRIBE_AVAILABLE_MODES = [TRANSCRIBE_MODE_WHISPER_LARGE_V3]
if MoonshineTranscriber is not None:
    TRANSCRIBE_AVAILABLE_MODES.extend([TRANSCRIBE_MODE_MOONSHINE_SMALL, TRANSCRIBE_MODE_MOONSHINE_MEDIUM])
TRANSCRIBE_AVAILABLE_MODES.append(TRANSCRIBE_MODE_API)


def _normalize_transcribe_mode_bootstrap(mode: Optional[str]) -> Optional[str]:
    normalized = (mode or "").strip().lower()
    if not normalized:
        return None
    if normalized in {
        TRANSCRIBE_MODE_WHISPER_LARGE_V3,
        "offline",
        "local",
        "whisper",
        "faster-whisper",
        "large-v3",
    }:
        return TRANSCRIBE_MODE_WHISPER_LARGE_V3
    if normalized in {TRANSCRIBE_MODE_API, "openai", "online"}:
        return TRANSCRIBE_MODE_API
    if normalized in {
        TRANSCRIBE_MODE_MOONSHINE_SMALL,
        "moonshine-small-streaming",
        "moonshine_small",
        "small",
    }:
        return TRANSCRIBE_MODE_MOONSHINE_SMALL
    if normalized in {
        TRANSCRIBE_MODE_MOONSHINE_MEDIUM,
        "moonshine-medium-streaming",
        "moonshine_medium",
        "moonshine",
        "moonshine-voice",
        "medium",
    }:
        return TRANSCRIBE_MODE_MOONSHINE_MEDIUM
    return None


TRANSCRIBE_MODE_REQUESTED_RAW = (
    os.getenv("TRANSCRIBE_MODE", TRANSCRIBE_MODE_MOONSHINE_MEDIUM).strip().lower() or TRANSCRIBE_MODE_MOONSHINE_MEDIUM
)
TRANSCRIBE_MODE_REQUESTED = _normalize_transcribe_mode_bootstrap(TRANSCRIBE_MODE_REQUESTED_RAW)
TRANSCRIBE_MODE_DEFAULT = TRANSCRIBE_MODE_REQUESTED or TRANSCRIBE_MODE_MOONSHINE_MEDIUM
if TRANSCRIBE_MODE_DEFAULT not in TRANSCRIBE_AVAILABLE_MODES:
    if TRANSCRIBE_MODE_WHISPER_LARGE_V3 in TRANSCRIBE_AVAILABLE_MODES:
        TRANSCRIBE_MODE_DEFAULT = TRANSCRIBE_MODE_WHISPER_LARGE_V3
    else:
        TRANSCRIBE_MODE_DEFAULT = TRANSCRIBE_AVAILABLE_MODES[0]


def _openai_transcribe_model() -> str:
    return (os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe") or "gpt-4o-mini-transcribe").strip()


def _openai_transcribe_prompt() -> str:
    # Important: for OpenAI ASR, only send prompt when user explicitly configured it.
    # Auto-injecting default prompt can bias decoding and cause prompt leakage/hallucinations.
    configured = (os.getenv("OPENAI_TRANSCRIBE_PROMPT", "") or "").strip()
    if not configured:
        return ""

    # Backward compatibility: ignore legacy auto-generated prompt text that used to be
    # injected by default from launch triggers/game names.
    lowered = configured.lower()
    if (
        lowered.startswith("english only. voice commands include launch words:")
        and "game names include:" in lowered
    ):
        return ""
    return configured


def _openai_api_key() -> str:
    return (os.getenv("OPENAI_API_KEY", "") or "").strip()


def _openai_configured() -> bool:
    return bool(_openai_api_key()) and AsyncOpenAI is not None


def _moonshine_configured() -> bool:
    return (
        MoonshineTranscriber is not None
        and moonshine_get_model_for_language is not None
        and MoonshineModelArch is not None
    )


def _moonshine_model_arch_from_value(raw: Optional[str]) -> Optional["MoonshineModelArch"]:
    if MoonshineModelArch is None:
        return None

    value = (raw or "").strip().lower()
    if not value:
        return None

    by_name = {
        "tiny": MoonshineModelArch.TINY,
        "base": MoonshineModelArch.BASE,
        "tiny-streaming": MoonshineModelArch.TINY_STREAMING,
        "base-streaming": MoonshineModelArch.BASE_STREAMING,
        "small-streaming": MoonshineModelArch.SMALL_STREAMING,
        "medium-streaming": MoonshineModelArch.MEDIUM_STREAMING,
    }
    if value in by_name:
        return by_name[value]

    try:
        numeric = int(value)
        return MoonshineModelArch(numeric)
    except Exception:
        return None


def _moonshine_model_arch_name(value: object) -> str:
    if moonshine_model_arch_to_string is not None:
        try:
            return str(moonshine_model_arch_to_string(value))
        except Exception:
            pass
    return str(value)


def _moonshine_install_hint() -> str:
    return (
        "Moonshine modes require dependency 'moonshine-voice'. "
        "Install with: pip install moonshine-voice==0.0.49"
    )


def _is_moonshine_mode(mode: Optional[str]) -> bool:
    return mode in {TRANSCRIBE_MODE_MOONSHINE_SMALL, TRANSCRIBE_MODE_MOONSHINE_MEDIUM}


def _moonshine_profile_for_mode(mode: str) -> str:
    if mode == TRANSCRIBE_MODE_MOONSHINE_SMALL:
        return "small"
    return "medium"


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


class _AsyncOpenAIClient:
    """Singleton-style manager for a shared AsyncOpenAI client."""

    _client: Optional["AsyncOpenAI"] = None

    @classmethod
    def get(cls) -> "AsyncOpenAI":
        if AsyncOpenAI is None:
            raise RuntimeError(
                "OpenAI SDK is not installed. Install dependency: pip install openai"
            )
        api_key = _openai_api_key()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        if cls._client is None:
            base_url = (os.getenv("OPENAI_BASE_URL", "") or "").strip()
            timeout_seconds = max(5.0, _environment_float("OPENAI_HTTP_TIMEOUT_SECONDS", 30.0))
            kwargs: Dict[str, object] = {"api_key": api_key, "timeout": timeout_seconds}
            if base_url:
                kwargs["base_url"] = base_url
            cls._client = AsyncOpenAI(**kwargs)
        return cls._client

    @classmethod
    async def aclose(cls) -> None:
        if cls._client is not None:
            try:
                await cls._client.close()
            except Exception:
                pass
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

if TRANSCRIBE_MODE_REQUESTED is None or TRANSCRIBE_MODE_DEFAULT != TRANSCRIBE_MODE_REQUESTED:
    logger.warning(
        "Unsupported TRANSCRIBE_MODE=%r; using %r (available=%s).",
        TRANSCRIBE_MODE_REQUESTED_RAW,
        TRANSCRIBE_MODE_DEFAULT,
        ",".join(TRANSCRIBE_AVAILABLE_MODES),
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
        "rachel, rachael, richel, richelle, rachal, raychel, ra chel, rach el, rita, ritu",
    ).split(",")
    if s.strip()
]
WAKE_WORD_PREFIXES = [
    s.strip().lower()
    for s in os.getenv("WAKE_WORD_PREFIXES", "hey, hi").split(",")
    if s.strip()
]
WHISPER_OFFLINE_COMMAND_HINTS = [
    s.strip()
    for s in re.split(
        r"[,\n;]+",
        os.getenv(
            "WHISPER_OFFLINE_COMMAND_HINTS",
            os.getenv("WHISPER_COMMAND_HINTS", "open,back,cornhole,disc golf,disc,golf"),
        ),
    )
    if s.strip()
]
ASR_DEFAULT_LANGUAGE = ((os.getenv("ASR_DEFAULT_LANGUAGE", "en") or "").strip() or "en")
ASR_FORCE_LANGUAGE = (os.getenv("ASR_FORCE_LANGUAGE", "") or "").strip()
ASR_ENGLISH_ONLY = (
    (os.getenv("ASR_ENGLISH_ONLY", "") or "").strip().lower()
    in {"1", "true", "t", "yes", "y", "on"}
)
ASR_API_LANGUAGE = ((os.getenv("ASR_API_LANGUAGE", "en") or "").strip() or "en")
ASR_API_FORCE_LANGUAGE = (
    (os.getenv("ASR_API_FORCE_LANGUAGE", "0") or "0").strip().lower()
    in {"1", "true", "t", "yes", "y", "on"}
)


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
_ASCII_TEXT_FILTER = re.compile(r"[^A-Za-z0-9\s\.,!?:;'\-_/\\\"()\[\]{}]+")


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


_GAME_TERM_PATTERNS = [
    (re.compile(r"(?<!\w)corn[\s\-]*hole(?!\w)", re.IGNORECASE), "cornhole"),
    (re.compile(r"(?<!\w)kong[\s\-]*ho(?:u)?(?!\w)", re.IGNORECASE), "cornhole"),
    (re.compile(r"(?<!\w)disc[\s\-]*golf(?!\w)", re.IGNORECASE), "disc golf"),
    (re.compile(r"(?<!\w)pickle[\s\-]*ball(?!\w)", re.IGNORECASE), "pickleball"),
]


def _canonicalize_game_terms(text: str) -> str:
    if not text:
        return text
    normalized = text
    for pattern, replacement in _GAME_TERM_PATTERNS:
        normalized = pattern.sub(replacement, normalized)
    return normalized


_COMMON_AGENT_PHRASE_PATTERNS = [
    (re.compile(r"(?<!\w)holstein\s+screen(?!\w)", re.IGNORECASE), "how's things going"),
    (re.compile(r"(?<!\w)recommend(?:ed)?\s+game\s+to\s+me(?!\w)", re.IGNORECASE), "recommend a game to me"),
]


def _canonicalize_common_agent_phrases(text: str) -> str:
    if not text:
        return text
    normalized = text
    for pattern, replacement in _COMMON_AGENT_PHRASE_PATTERNS:
        normalized = pattern.sub(replacement, normalized)
    return normalized


def _canonicalize_asr_text(text: str) -> str:
    return _canonicalize_common_agent_phrases(_canonicalize_game_terms(_canonicalize_wake_words(text)))


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


@lru_cache(maxsize=2)
def _load_moonshine_transcriber(profile: str) -> tuple["MoonshineTranscriber", str, object]:
    if not _moonshine_configured():
        raise RuntimeError(_moonshine_install_hint())

    profile_key = (profile or "").strip().lower()
    if profile_key not in {"small", "medium"}:
        raise RuntimeError(f"Unsupported moonshine profile: {profile}")

    model_path_raw = (
        os.getenv(f"MOONSHINE_{profile_key.upper()}_MODEL_PATH", "")
        or os.getenv("MOONSHINE_MODEL_PATH", "")
        or ""
    ).strip()
    language = (os.getenv("MOONSHINE_LANGUAGE", "en") or "en").strip().lower() or "en"
    model_arch_raw = (
        os.getenv(f"MOONSHINE_{profile_key.upper()}_MODEL_ARCH", "")
        or os.getenv("MOONSHINE_MODEL_ARCH", "")
        or ""
    )
    model_arch = _moonshine_model_arch_from_value(model_arch_raw)
    default_arch = (
        MoonshineModelArch.SMALL_STREAMING
        if profile_key == "small"
        else MoonshineModelArch.MEDIUM_STREAMING
    )

    try:
        if model_path_raw:
            resolved_model_path = model_path_raw
            resolved_model_arch = model_arch or default_arch
        else:
            resolved_model_path, resolved_model_arch = moonshine_get_model_for_language(
                wanted_language=language,
                wanted_model_arch=(model_arch or default_arch),
            )
    except Exception as exc:
        raise RuntimeError(
            "Failed to resolve/load Moonshine model. "
            "Set MOONSHINE_<PROFILE>_MODEL_PATH (or MOONSHINE_MODEL_PATH) to an existing model folder "
            "or verify internet access "
            "for automatic model download."
        ) from exc

    try:
        identify_speakers = (
            (os.getenv("MOONSHINE_IDENTIFY_SPEAKERS", "1") or "1").strip().lower()
            in {"1", "true", "t", "yes", "y", "on"}
        )
        options: Optional[Dict[str, object]] = None
        if identify_speakers:
            options = {"identify_speakers": "true"}
        try:
            transcriber = MoonshineTranscriber(
                model_path=resolved_model_path,
                model_arch=resolved_model_arch,
                options=options,
            )
        except Exception:
            # Compatibility fallback for older moonshine builds / options parsing.
            transcriber = MoonshineTranscriber(
                model_path=resolved_model_path,
                model_arch=resolved_model_arch,
            )
            if identify_speakers:
                logger.warning(
                    "Moonshine transcriber initialized without identify_speakers option. "
                    "Speaker IDs may be unavailable on this build."
                )
    except Exception as exc:
        raise RuntimeError(
            "Failed to initialize Moonshine transcriber. "
            "Verify MOONSHINE_MODEL_PATH and MOONSHINE_MODEL_ARCH."
        ) from exc

    try:
        logger.info(
            "Loaded Moonshine model profile=%s path=%s arch=%s language=%s",
            profile_key,
            resolved_model_path,
            _moonshine_model_arch_name(resolved_model_arch),
            language,
        )
    except Exception:
        pass

    return transcriber, resolved_model_path, resolved_model_arch


def _normalize_identity_resolution(value: Optional[str]) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"none", "anonymous", "anon", "skip", "off"}:
        return "none"
    return "auto"


def _ollama_base_url() -> str:
    return _environment("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL).rstrip("/")


def _ollama_model() -> str:
    return _environment("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)


def _ollama_think_enabled() -> bool:
    return _environment_bool("OLLAMA_THINK", DEFAULT_OLLAMA_THINK)


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


_RUNTIME_TRANSCRIBE_MODE: Optional[str] = None
_RUNTIME_TRANSCRIBE_MODE_LOCK = asyncio.Lock()


def _normalize_transcribe_mode(mode: Optional[str]) -> Optional[str]:
    return _normalize_transcribe_mode_bootstrap(mode)


async def _set_runtime_transcribe_mode(mode: Optional[str]) -> None:
    global _RUNTIME_TRANSCRIBE_MODE
    async with _RUNTIME_TRANSCRIBE_MODE_LOCK:
        _RUNTIME_TRANSCRIBE_MODE = mode


async def _get_runtime_transcribe_mode() -> Optional[str]:
    async with _RUNTIME_TRANSCRIBE_MODE_LOCK:
        return _RUNTIME_TRANSCRIBE_MODE


async def _get_effective_transcribe_mode() -> tuple[str, str]:
    runtime_mode = await _get_runtime_transcribe_mode()
    if runtime_mode:
        return runtime_mode, "runtime"
    return TRANSCRIBE_MODE_DEFAULT, "env_or_default"


def _normalize_pipeline_mode(mode: Optional[str]) -> str:
    normalized = (mode or "").strip().lower()
    if normalized in {"direct", PIPELINE_MODE_DIRECT_UNIFIED, "unified", "best"}:
        return PIPELINE_MODE_DIRECT_UNIFIED
    if normalized in {"legacy", PIPELINE_MODE_LEGACY_MQTT, "mqtt"}:
        return PIPELINE_MODE_LEGACY_MQTT
    return PIPELINE_MODE_DIRECT_UNIFIED


def _normalize_conversation_profile(profile: Optional[str]) -> str:
    normalized = (profile or "").strip().lower()
    if normalized in {CONVERSATION_PROFILE_CLOUD, "online", "openai"}:
        return CONVERSATION_PROFILE_CLOUD
    return CONVERSATION_PROFILE_LOCAL


def _normalize_cloud_response_provider(provider: Optional[str]) -> str:
    normalized = (provider or "").strip().lower()
    if normalized in {"openai", "api", "cloud"}:
        return "openai"
    return "openai"


def _conversation_pipeline_mode() -> str:
    return _normalize_pipeline_mode(os.getenv("VOICE_PIPELINE_MODE", PIPELINE_MODE_DIRECT_UNIFIED))


def _conversation_profile() -> str:
    return _normalize_conversation_profile(os.getenv("VOICE_CONVERSATION_PROFILE", CONVERSATION_PROFILE_LOCAL))


def _conversation_local_asr_mode() -> str:
    return _normalize_transcribe_mode(os.getenv("VOICE_LOCAL_ASR_MODE")) or TRANSCRIBE_MODE_MOONSHINE_MEDIUM


def _conversation_cloud_asr_mode() -> str:
    return _normalize_transcribe_mode(os.getenv("VOICE_CLOUD_ASR_MODE")) or TRANSCRIBE_MODE_API


def _conversation_preferred_asr_mode(profile: Optional[str] = None) -> str:
    effective_profile = _normalize_conversation_profile(profile or _conversation_profile())
    if effective_profile == CONVERSATION_PROFILE_CLOUD:
        return _conversation_cloud_asr_mode()
    return _conversation_local_asr_mode()


def _conversation_cloud_response_provider() -> str:
    return _normalize_cloud_response_provider(os.getenv("VOICE_CLOUD_RESPONSE_PROVIDER", "openai"))


def _openai_response_model() -> str:
    return (os.getenv("OPENAI_RESPONSE_MODEL", DEFAULT_OPENAI_RESPONSE_MODEL) or DEFAULT_OPENAI_RESPONSE_MODEL).strip()


def _conversation_local_response_model() -> str:
    return _ollama_model()


def _conversation_effective_response_provider(profile: Optional[str] = None) -> str:
    effective_profile = _normalize_conversation_profile(profile or _conversation_profile())
    if effective_profile == CONVERSATION_PROFILE_CLOUD and _openai_configured():
        return _conversation_cloud_response_provider()
    return "ollama"


_CONVERSATION_ENV_DEFAULTS: Dict[str, Optional[str]] = {
    "VOICE_PIPELINE_MODE": os.getenv("VOICE_PIPELINE_MODE"),
    "VOICE_CONVERSATION_PROFILE": os.getenv("VOICE_CONVERSATION_PROFILE"),
    "VOICE_LOCAL_ASR_MODE": os.getenv("VOICE_LOCAL_ASR_MODE"),
    "VOICE_CLOUD_ASR_MODE": os.getenv("VOICE_CLOUD_ASR_MODE"),
    "VOICE_CLOUD_RESPONSE_PROVIDER": os.getenv("VOICE_CLOUD_RESPONSE_PROVIDER"),
    "OPENAI_RESPONSE_MODEL": os.getenv("OPENAI_RESPONSE_MODEL"),
    "OLLAMA_MODEL": os.getenv("OLLAMA_MODEL"),
}


def _restore_conversation_env_defaults() -> None:
    for key, value in _CONVERSATION_ENV_DEFAULTS.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _conversation_config_snapshot() -> Dict[str, Any]:
    profile = _conversation_profile()
    effective_provider = _conversation_effective_response_provider(profile)
    return {
        "pipeline_mode": _conversation_pipeline_mode(),
        "profile": profile,
        "local_asr_mode": _conversation_local_asr_mode(),
        "cloud_asr_mode": _conversation_cloud_asr_mode(),
        "preferred_asr_mode": _conversation_preferred_asr_mode(profile),
        "cloud_response_provider": _conversation_cloud_response_provider(),
        "openai_response_model": _openai_response_model(),
        "local_response_model": _conversation_local_response_model(),
        "openai_configured": _openai_configured(),
        "cloud_ready": _openai_configured(),
        "effective_response_provider": effective_provider,
    }



class _AnonymousSessionStore:
    def __init__(self, *, max_turns: int = ANONYMOUS_SESSION_MAX_TURNS, max_age_sec: float = ANONYMOUS_SESSION_MAX_AGE_SEC) -> None:
        self.max_turns = max(2, int(max_turns))
        self.max_age_sec = float(max_age_sec)
        self._lock = threading.Lock()
        self._turns: deque[Dict[str, Any]] = deque(maxlen=self.max_turns)

    def _prune_unlocked(self, *, now_ts: Optional[float] = None) -> None:
        cutoff = float(now_ts if now_ts is not None else time.time()) - self.max_age_sec
        while self._turns and float(self._turns[0].get("ts") or 0.0) < cutoff:
            self._turns.popleft()

    def remember_turn(self, role: str, text: str) -> None:
        clean = " ".join((text or "").strip().split())
        if not clean:
            return
        now_ts = time.time()
        with self._lock:
            self._prune_unlocked(now_ts=now_ts)
            self._turns.append({"role": str(role or "user").strip().lower() or "user", "text": clean[:240], "ts": now_ts})

    def build_memory_payload(self, query_text: str) -> Dict[str, Any]:
        normalized_query = _normalize_compare_text(query_text)
        now_ts = time.time()
        with self._lock:
            self._prune_unlocked(now_ts=now_ts)
            turns = list(self._turns)
        user_points: List[str] = []
        for item in turns:
            if str(item.get("role") or "user").strip().lower() != "user":
                continue
            text = " ".join(str(item.get("text") or "").strip().split())
            if not text:
                continue
            if _normalize_compare_text(text) == normalized_query:
                continue
            user_points.append(text)
        user_points = user_points[-2:]
        fallback = "So far I only know what we have talked about in this conversation, and I do not have a linked long-term profile yet."
        if user_points:
            if len(user_points) == 1:
                fallback = (
                    f"So far in this conversation, I know you mentioned {user_points[0]}. "
                    "I just do not have your long-term profile linked yet."
                )
            else:
                fallback = (
                    f"So far in this conversation, I know you mentioned {user_points[0]} and {user_points[1]}. "
                    "I just do not have your long-term profile linked yet."
                )
        return {
            "type": "memory_query",
            "query_kind": "summary",
            "result_kind": "session_only_summary",
            "binding_state": "session_only",
            "facts": [],
            "notes": user_points,
            "required_terms": [],
            "text": fallback,
            "max_sentences": 2,
        }


_ANONYMOUS_SESSION_STORE = _AnonymousSessionStore()
_SESSION_CONTEXT_STORE = SessionContextStore(max_age_sec=ANONYMOUS_SESSION_MAX_AGE_SEC)


def _structured_memory_field_clause(field: str, value: str) -> str:
    clean_field = str(field or "").strip()
    clean_value = " ".join(str(value or "").strip().split())
    if not clean_value:
        return ""
    if clean_field == "goal":
        return f"your goal is {clean_value}"
    if clean_field == "name":
        return f"your name is {clean_value}"
    if clean_field == "like":
        return f"you like {clean_value}"
    if clean_field == "dislike":
        return f"you dislike {clean_value}"
    if clean_field == "favorite_game":
        return f"your favorite game is {clean_value}"
    if clean_field == "origin":
        return f"you are from {clean_value}"
    if clean_field == "preferred_training_day":
        return f"you prefer training on {clean_value}"
    if clean_field == "preferred_training_time":
        return f"you prefer training in the {clean_value}"
    return clean_value


def _pick_structured_variant(options: List[str], seed_text: str) -> str:
    if not options:
        return ""
    seed = sum(ord(ch) for ch in (seed_text or ""))
    return options[seed % len(options)]


def _structured_template_reply(payload: Dict[str, Any], user_text: str) -> str:
    reply_type = str(payload.get("type") or "").strip().lower()
    result_kind = str(payload.get("result_kind") or "").strip().lower()
    primary_game_name = str(payload.get("primary_game_name") or payload.get("game_name") or "").strip()
    reference_game_name = str(payload.get("reference_game_name") or "").strip()
    candidate_games = [str(item).strip() for item in payload.get("candidate_games", []) or [] if str(item).strip()]
    facts = [item for item in payload.get("facts", []) or [] if isinstance(item, dict)]
    notes = [" ".join(str(item).strip().split()) for item in payload.get("notes", []) or [] if str(item).strip()]
    reason_text = " ".join(str(payload.get("reason_text") or payload.get("recommendation_reason") or "").strip().split())
    summary_text = " ".join(str(payload.get("summary_text") or "").strip().split())
    fallback_text = summary_text or " ".join(str(payload.get("text") or "").strip().split())
    primary_entity = str(payload.get("primary_entity") or primary_game_name or "").strip()
    candidate_entities = [str(item).strip() for item in payload.get("candidate_entities", []) or [] if str(item).strip()]
    allowed_entities = [str(item).strip() for item in payload.get("allowed_entities", []) or [] if str(item).strip()]
    answer_mode = str(payload.get("answer_mode") or "").strip().lower()
    domain = str(payload.get("domain") or "").strip().lower()
    clarify_kind = str(payload.get("clarify_kind") or "").strip().lower()
    seed = f"{reply_type}|{result_kind}|{primary_game_name}|{user_text}"

    if reply_type == "game_recommend":
        if not primary_game_name:
            return fallback_text
        opener = _pick_structured_variant(
            [
                f"I would go with {primary_game_name}.",
                f"{primary_game_name} looks like the best fit right now.",
                f"A good one to try next is {primary_game_name}.",
            ],
            seed,
        )
        if reason_text:
            return f"{opener} {reason_text[:1].upper() + reason_text[1:].rstrip('.')}."
        return opener

    if reply_type == "game_alternative":
        listed = ""
        if candidate_games:
            if len(candidate_games) == 1:
                listed = candidate_games[0]
            elif len(candidate_games) == 2:
                listed = f"{candidate_games[0]} and {candidate_games[1]}"
            else:
                listed = ", ".join(candidate_games[:-1]) + f", and {candidate_games[-1]}"
        if reference_game_name and listed:
            opener = f"Other good options besides {reference_game_name} are {listed}."
        elif listed:
            opener = f"Other good options are {listed}."
        else:
            opener = fallback_text
        if primary_game_name and reason_text:
            return f"{opener} I would lean toward {primary_game_name} because {reason_text[:1].lower() + reason_text[1:].rstrip('.')}."
        if primary_game_name and primary_game_name not in opener:
            return f"{opener} I would start with {primary_game_name}."
        return opener

    if reply_type == "game_list":
        if candidate_games:
            if len(candidate_games) == 1:
                return f"Right now I have {candidate_games[0]} available."
            if len(candidate_games) == 2:
                return f"Right now I have {candidate_games[0]} and {candidate_games[1]} available."
            return "Right now I have " + ", ".join(candidate_games[:-1]) + f", and {candidate_games[-1]} available."
        return fallback_text

    if reply_type == "game_explain":
        if fallback_text:
            return fallback_text
        if primary_game_name and reason_text:
            return f"{primary_game_name} is {reason_text.rstrip('.') }."

    if reply_type == "memory_write_ack":
        clauses = [str(item.get("spoken_text") or "").strip() for item in facts if str(item.get("spoken_text") or "").strip()]
        if clauses:
            if len(clauses) == 1:
                return f"Okay, I will remember that {clauses[0]}."
            return "Okay, I will remember that " + "; ".join(clauses[:3]) + "."
        return fallback_text or "Okay, I will remember that."

    if reply_type == "memory_query":
        if result_kind == "session_only_summary":
            return fallback_text
        if result_kind == "known_specific_fact":
            if facts:
                spoken = str(facts[0].get("spoken_text") or "").strip()
                if spoken:
                    return f"I remember that {spoken}."
        if result_kind == "known_recent_notes":
            if len(notes) == 1:
                return f"I remember you mentioned {notes[0]}."
            if len(notes) >= 2:
                return f"I remember you mentioned {notes[0]} and {notes[1]}."
        if result_kind == "known_profile_summary":
            clauses = [str(item.get("spoken_text") or "").strip() for item in facts if str(item.get("spoken_text") or "").strip()]
            if len(clauses) == 1:
                return f"So far I remember that {clauses[0]}."
            if len(clauses) == 2:
                return f"So far I remember that {clauses[0]}, and {clauses[1]}."
            if len(clauses) >= 3:
                return f"So far I remember that {clauses[0]}, {clauses[1]}, and {clauses[2]}."
        return fallback_text

    if reply_type == "doc_clarify":
        if fallback_text:
            return fallback_text
        if clarify_kind == "clarify_missing_entity" and answer_mode == "compare":
            return "Which two games do you want me to compare?"
        if clarify_kind == "clarify_ambiguous_intent" and len(candidate_entities) >= 2:
            return f"Do you want me to compare {candidate_entities[0]} and {candidate_entities[1]}, or recommend one?"
        return "Could you clarify which document or item you mean?"

    if reply_type == "doc_answer":
        if fallback_text:
            return fallback_text
        if domain == "game" and answer_mode == "availability" and candidate_entities:
            if len(candidate_entities) == 1:
                return f"Right now I have {candidate_entities[0]} available."
            if len(candidate_entities) == 2:
                return f"Right now I have {candidate_entities[0]} and {candidate_entities[1]} available."
            return "Right now I have " + ", ".join(candidate_entities[:-1]) + f", and {candidate_entities[-1]} available."
        if domain == "game" and answer_mode == "recommend" and primary_entity:
            if reason_text:
                return f"I recommend {primary_entity} because {reason_text.rstrip('.')}."
            return f"I recommend {primary_entity}."
        if domain == "game" and answer_mode == "compare" and len(candidate_entities) >= 2:
            if reason_text:
                return f"{candidate_entities[0]} and {candidate_entities[1]} are current options. {reason_text.rstrip('.') }."
            return f"{candidate_entities[0]} and {candidate_entities[1]} are current options."
        if primary_entity and answer_mode in {"introduce", "factual", "how_to"} and reason_text:
            return f"{primary_entity} is {reason_text.rstrip('.') }."
        if allowed_entities and answer_mode == "recommend":
            return f"I recommend {allowed_entities[0]}."

    return fallback_text


def _count_sentences(text: str) -> int:
    clean = " ".join((text or "").strip().split())
    if not clean:
        return 0
    parts = [segment.strip() for segment in STRUCTURED_RENDER_SENTENCE_SPLIT_RE.split(clean) if segment.strip()]
    return max(1, len(parts))


def _validate_structured_reply(
    reply_text: str,
    payload: Dict[str, Any],
    *,
    all_game_names: Optional[List[str]] = None,
) -> Tuple[bool, str]:
    clean = _normalize_final_reply_text(reply_text)
    if not clean:
        return False, "empty"
    if _looks_like_system_prompt_leak(clean):
        return False, "prompt_leak"
    normalized = _normalize_compare_text(clean)
    if any(_normalize_compare_text(marker) in normalized for marker in STRUCTURED_RENDER_DISALLOWED_MARKERS):
        return False, "disallowed_marker"
    max_sentences = int(payload.get("max_sentences") or (3 if str(payload.get("type") or "") == "game_explain" else 2))
    if _count_sentences(clean) > max(1, max_sentences):
        return False, "too_many_sentences"

    allowed_entities = {
        _normalize_compare_text(str(item))
        for item in payload.get("allowed_entities", []) or []
        if _normalize_compare_text(str(item))
    }
    allowed_games = {
        _normalize_compare_text(str(item))
        for item in payload.get("allowed_game_names", []) or []
        if _normalize_compare_text(str(item))
    }
    allowed_games.update(allowed_entities)
    if all_game_names:
        for game_name in all_game_names:
            normalized_name = _normalize_compare_text(game_name)
            if not normalized_name or normalized_name in allowed_games:
                continue
            if normalized_name in normalized:
                return False, f"unexpected_entity:{game_name}"

    answer_mode = str(payload.get("answer_mode") or "").strip().lower()
    candidate_entities = [
        str(item).strip()
        for item in payload.get("candidate_entities", []) or []
        if str(item).strip()
    ]
    primary_entity = str(payload.get("primary_entity") or "").strip()
    if answer_mode == "compare" and len(candidate_entities) >= 2:
        covered = 0
        for entity_name in candidate_entities[:2]:
            if _normalize_compare_text(entity_name) in normalized:
                covered += 1
        if covered < 2:
            return False, "compare_entities_missing"
    if answer_mode == "recommend" and primary_entity:
        if _normalize_compare_text(primary_entity) not in normalized:
            for game_name in all_game_names or []:
                normalized_name = _normalize_compare_text(game_name)
                if normalized_name and normalized_name in normalized and normalized_name != _normalize_compare_text(primary_entity):
                    return False, "recommend_invalid_entity"

    required_terms = [str(item).strip() for item in payload.get("required_terms", []) or [] if str(item).strip()]
    for term in required_terms:
        if _normalize_compare_text(term) not in normalized:
            return False, f"missing_required:{term}"

    binding_state = str(payload.get("binding_state") or "").strip().lower()
    if binding_state in {"session_only", "no_saved_profile"} and any(
        _normalize_compare_text(marker) in normalized
        for marker in (
            "complete history",
            "full history",
            "your whole history",
            "your long term profile says",
      )
    ):
        return False, "binding_reversal"
    return True, ""


def _doc_rag_summary_enabled() -> bool:
    return _environment_bool("DOC_RAG_SUMMARY_ENABLE", True)


def _doc_rag_summary_model() -> str:
    return _environment("DOC_RAG_SUMMARY_MODEL", _conversation_local_response_model())


def _doc_rag_summary_max_snippets() -> int:
    return max(1, _environment_int("DOC_RAG_SUMMARY_MAX_SNIPPETS", 3))


def _doc_rag_summary_max_chars_per_snippet() -> int:
    return max(80, _environment_int("DOC_RAG_SUMMARY_MAX_CHARS_PER_SNIPPET", 220))


def _build_doc_summary_prompt(user_text: str, payload: Dict[str, Any]) -> Tuple[str, str]:
    compact_payload = {
        "domain": str(payload.get("domain") or "").strip(),
        "answer_mode": str(payload.get("answer_mode") or "").strip(),
        "general_focus": str(payload.get("general_focus") or "").strip(),
        "primary_entity": str(payload.get("primary_entity") or "").strip(),
        "candidate_entities": [str(item).strip() for item in payload.get("candidate_entities", []) or [] if str(item).strip()],
        "allowed_entities": [str(item).strip() for item in payload.get("allowed_entities", []) or [] if str(item).strip()],
        "required_terms": [str(item).strip() for item in payload.get("required_terms", []) or [] if str(item).strip()],
        "related_entities": payload.get("related_entities", {}) or {},
        "summary_text": str(payload.get("summary_text") or payload.get("text") or "").strip(),
        "summary_points": [str(item).strip() for item in payload.get("summary_points", []) or [] if str(item).strip()],
        "doc_snippets": [
            " ".join(str(item).strip().split())[: _doc_rag_summary_max_chars_per_snippet()]
            for item in (payload.get("doc_snippets", []) or [])[: _doc_rag_summary_max_snippets()]
            if str(item).strip()
        ],
        "doc_source_ids": [str(item).strip() for item in payload.get("doc_source_ids", []) or [] if str(item).strip()],
        "max_sentences": int(payload.get("max_sentences") or 2),
    }
    system_prompt = (
        "You summarize grounded local document evidence into a concise spoken answer.\n"
        "Rules:\n"
        "- Use only the supplied evidence.\n"
        "- Keep every required term.\n"
        "- Do not mention any entity outside allowed_entities.\n"
        "- Keep it to one or two complete sentences.\n"
        "- Do not use labels, bullets, or JSON.\n"
        "- Output only the final summary in English."
    )
    prompt = (
        "Summarize the grounded local document evidence for speech.\n\n"
        f"Latest user message:\n{user_text}\n\n"
        "Grounded evidence JSON:\n"
        + json.dumps(compact_payload, ensure_ascii=False, indent=2)
        + "\n\nFinal spoken summary:"
    )
    return system_prompt, prompt


async def _generate_local_doc_summary(user_text: str, payload: Dict[str, Any]) -> str:
    system_prompt, prompt = _build_doc_summary_prompt(user_text, payload)
    payload_json: Dict[str, Any] = {
        "model": _doc_rag_summary_model(),
        "think": False,
        "system": system_prompt,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.1,
            "top_p": 0.7,
            "top_k": 20,
            "num_predict": 96,
            "repeat_penalty": 1.05,
        },
    }
    keep_alive = _environment("OLLAMA_KEEP_ALIVE", "30m")
    if keep_alive:
        payload_json["keep_alive"] = keep_alive
    url = f"{_ollama_base_url()}/api/generate"
    try:
        client = _AsyncHttpClient.get()
        response = await client.post(url, json=payload_json)
    except httpx.HTTPError as exc:
        raise OllamaError(f"Failed to contact Ollama at {url}: {exc}") from exc
    if response.status_code != 200:
        raise OllamaError(f"Ollama returned status {response.status_code}: {response.text.strip()}")
    data = response.json()
    summary = _normalize_final_reply_text(str(data.get("response") or ""))
    if not summary:
        raise OllamaError("Ollama doc summary response was empty")
    return summary


async def _prepare_structured_payload_for_render(user_text: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    effective_payload = dict(payload)
    reply_type = str(effective_payload.get("type") or "").strip().lower()
    domain = str(effective_payload.get("domain") or "").strip().lower()
    default_summary = _normalize_final_reply_text(
        str(effective_payload.get("summary_text") or effective_payload.get("text") or "")
    )
    effective_payload["summary_used"] = False
    effective_payload["summary_model"] = ""
    effective_payload["summary_fallback_reason"] = ""
    if reply_type != "doc_answer" or domain != "general":
        if default_summary:
            effective_payload["summary_text"] = default_summary
        return effective_payload
    if default_summary:
        effective_payload["summary_text"] = default_summary
    if not _doc_rag_summary_enabled():
        if default_summary:
            effective_payload["summary_used"] = True
            effective_payload["summary_model"] = "deterministic"
            effective_payload["summary_fallback_reason"] = "disabled"
        return effective_payload
    try:
        summary_text = await _generate_local_doc_summary(user_text, effective_payload)
    except (OllamaError, OpenAIResponseError, RuntimeError) as exc:
        logger.info("doc summary fallback: %s", exc)
        if default_summary:
            effective_payload["summary_used"] = True
            effective_payload["summary_model"] = "deterministic"
            effective_payload["summary_fallback_reason"] = f"summary_error:{type(exc).__name__}"
        else:
            effective_payload["summary_fallback_reason"] = f"summary_error:{type(exc).__name__}"
        return effective_payload
    valid, reason = _validate_structured_reply(summary_text, effective_payload)
    if not valid:
        logger.info("doc summary validation fallback: %s", reason)
        if default_summary:
            effective_payload["summary_used"] = True
            effective_payload["summary_model"] = "deterministic"
            effective_payload["summary_fallback_reason"] = reason or "summary_invalid"
        else:
            effective_payload["summary_fallback_reason"] = reason or "summary_invalid"
        return effective_payload
    effective_payload["summary_text"] = summary_text
    effective_payload["summary_used"] = True
    effective_payload["summary_model"] = _doc_rag_summary_model()
    effective_payload["summary_fallback_reason"] = ""
    return effective_payload


_GAME_CAPABILITY_LABELS = {
    "game_availability",
    "game_recommend",
    "game_alternative",
    "game_introduce",
    "game_compare",
}
_STRUCTURED_CAPABILITY_LABELS = _GAME_CAPABILITY_LABELS | {"memory_query", "vision_query", "clarify", "doc_query"}


class _UnifiedConversationRuntime:
    def __init__(self) -> None:
        self.intent_cfg = None
        self.intent_resolver = None
        self.intent_router = None
        self.game_catalog = None
        self.local_docs_rag = None
        self.dialog_cfg = None
        self.dialog_helper = None
        self.session_store = _SESSION_CONTEXT_STORE
        self._capability_classifier_cache: Dict[str, Tuple[float, str]] = {}
        self.ready = False
        self.error = ""
        self.reload_from_env()

    def close(self) -> None:
        self._capability_classifier_cache = {}
        if self.intent_router is not None:
            try:
                self.intent_router.close()
            except Exception:
                pass
        self.intent_router = None
        self.intent_resolver = None
        self.game_catalog = None
        if self.local_docs_rag is not None:
            try:
                self.local_docs_rag.close()
            except Exception:
                pass
        self.local_docs_rag = None
        if self.dialog_helper is not None:
            try:
                self.dialog_helper.http.close()
            except Exception:
                pass
        self.dialog_helper = None
        self.ready = False

    def reload_from_env(self) -> None:
        self.close()
        if load_intent_config is None or IntentRouterEngine is None or ManifestAliasResolver is None:
            self.error = "intent routing helpers are unavailable"
            return
        if load_dialog_config is None or DialogService is None:
            self.error = "dialog helpers are unavailable"
            return

        try:
            self.intent_cfg = load_intent_config()
            self.intent_resolver = ManifestAliasResolver(self.intent_cfg.manifest_path)
            self.intent_router = IntentRouterEngine(self.intent_cfg, self.intent_resolver)
            self.game_catalog = GameCatalog(Path(self.intent_cfg.manifest_path))
        except Exception as exc:
            self.error = f"intent router init failed: {exc}"
            self.intent_router = None
            self.game_catalog = None
            return

        try:
            self.dialog_cfg = load_dialog_config()
            self.dialog_helper = DialogService(self.dialog_cfg)
        except Exception as exc:
            self.error = f"dialog helper init failed: {exc}"
            if self.intent_router is not None:
                try:
                    self.intent_router.close()
                except Exception:
                    pass
            self.intent_router = None
            self.dialog_helper = None
            return

        try:
            self.local_docs_rag = LocalDocsRAG(
                manifest_path=Path(self.intent_cfg.manifest_path),
                game_catalog=self.game_catalog,
            )
        except Exception as exc:
            logger.warning("local docs rag init failed: %s", exc)
            self.local_docs_rag = None
        if self.local_docs_rag is not None:
            try:
                doc_rag_diag = self.local_docs_rag.diagnostics()
            except Exception as exc:
                logger.warning("local docs rag diagnostics failed: %s", exc)
            else:
                logger.info(
                    "local docs rag startup root=%s ready=%s dense_ready=%s general_files=%s general_chunks=%s game_chunks=%s entities=%s error=%s",
                    doc_rag_diag.get("doc_root", ""),
                    doc_rag_diag.get("ready", False),
                    doc_rag_diag.get("dense_ready", False),
                    doc_rag_diag.get("general_source_files", 0),
                    doc_rag_diag.get("general_chunk_count", 0),
                    doc_rag_diag.get("game_chunk_count", 0),
                    doc_rag_diag.get("entity_registry_count", 0),
                    doc_rag_diag.get("error", ""),
                )
                if not bool(doc_rag_diag.get("docs_dir_exists", False)):
                    logger.warning("local docs rag docs directory missing: %s", doc_rag_diag.get("docs_dir", ""))
                elif int(doc_rag_diag.get("general_source_files", 0) or 0) <= 0:
                    logger.warning("local docs rag docs directory is present but has no general doc files: %s", doc_rag_diag.get("docs_dir", ""))
                elif int(doc_rag_diag.get("general_chunk_count", 0) or 0) <= 0:
                    logger.warning("local docs rag loaded no general chunks from docs directory: %s", doc_rag_diag.get("docs_dir", ""))
                if not bool(doc_rag_diag.get("ready", False)) and str(doc_rag_diag.get("error", "")).strip():
                    logger.warning("local docs rag unavailable: %s", doc_rag_diag.get("error", ""))

        self.error = ""
        self.ready = True

    def ensure_ready(self) -> None:
        if not self.ready or self.intent_router is None or self.dialog_helper is None:
            raise RuntimeError(self.error or "unified conversation runtime is not ready")

    def resolve_user_id(
        self,
        *,
        payload: Dict[str, Any],
        user_id: Optional[str],
        identity_resolution: Optional[str] = None,
    ) -> Optional[str]:
        self.ensure_ready()
        helper = self.dialog_helper
        assert helper is not None
        resolved_user_id = (user_id or "").strip() or None
        if resolved_user_id:
            return resolved_user_id
        if helper.user_memory is None:
            return resolved_user_id
        if _normalize_identity_resolution(identity_resolution or payload.get("identity_resolution")) == "none":
            return None
        try:
            identity_payload = dict(payload)
            if speaker_identity_key is None:
                identity_key = "source:default"
            else:
                identity_key = speaker_identity_key(identity_payload)
            return helper.user_memory.resolve_user(identity_key)
        except Exception as exc:
            logger.warning("user identity resolve failed: %s", exc)
            return resolved_user_id

    def route_text(
        self,
        text: str,
        corr_id: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
        identity_resolution: Optional[str] = None,
    ):
        self.ensure_ready()
        helper = self.dialog_helper
        assert helper is not None
        resolved_user_id = self.resolve_user_id(
            payload=payload or {},
            user_id=user_id,
            identity_resolution=identity_resolution,
        )
        context_game_name = self.session_store.context_game_name(resolved_user_id, text)
        if (
            not context_game_name
            and resolved_user_id
            and helper.user_memory is not None
            and not self.session_store.is_game_suppressed(resolved_user_id)
        ):
            try:
                context_game_name = helper.user_memory.get_game_reference(resolved_user_id)
            except Exception as exc:
                logger.warning("game reference resolve failed: %s", exc)
        return self.intent_router.route(text, corr_id, context_game_name=context_game_name)

    def build_turn_context(
        self,
        *,
        payload: Dict[str, Any],
        text: str,
        user_id: Optional[str],
        resolved_user_id: Optional[str] = None,
        identity_resolution: Optional[str] = None,
    ) -> Tuple[Optional[str], str, Dict[str, str], Dict[str, Any]]:
        self.ensure_ready()
        helper = self.dialog_helper
        assert helper is not None

        resolved_user_id = (resolved_user_id or user_id or "").strip() or None
        memory_context = ""
        dialog_request_ctx: Dict[str, str] = {
            "dialog_context": "",
            "current_topic": "",
            "open_question": "",
        }
        memory_update: Dict[str, Any] = {
            "explicit_memory": False,
            "facts_written": [],
            "facts_removed": [],
            "ack_text": "",
            "utterance": "",
        }
        if helper.user_memory is None:
            return resolved_user_id, memory_context, dialog_request_ctx, memory_update
        if _normalize_identity_resolution(identity_resolution or payload.get("identity_resolution")) == "none":
            return None, memory_context, dialog_request_ctx, memory_update

        try:
            resolved_user_id = self.resolve_user_id(
                payload=payload,
                user_id=resolved_user_id,
                identity_resolution=identity_resolution,
            )
            if not resolved_user_id:
                return None, memory_context, dialog_request_ctx, memory_update
            memory_update = helper.user_memory.remember_utterance(resolved_user_id, text)
            memory_context = helper.user_memory.build_memory_context(resolved_user_id, query_text=text)
            dialog_request_ctx = helper._build_dialog_request_context(user_id=resolved_user_id, user_text=text)
        except Exception as exc:
            logger.warning("dialog context build failed: %s", exc)
            resolved_user_id = (user_id or "").strip() or None
            memory_context = ""
            dialog_request_ctx = {
                "dialog_context": "",
                "current_topic": "",
                "open_question": "",
            }
            memory_update = {
                "explicit_memory": False,
                "facts_written": [],
                "facts_removed": [],
                "ack_text": "",
                "utterance": "",
            }
        return resolved_user_id, memory_context, dialog_request_ctx, memory_update

    def remember_user_turn(
        self,
        *,
        user_id: Optional[str],
        text: str,
    ) -> None:
        self.session_store.remember_turn(user_id=user_id, role="user", text=text)
        reset_reason = self._hard_reset_reason(text=text)
        if reset_reason:
            self.session_store.activate_game_suppression(user_id=user_id, reason=reset_reason)
            return
        if self.game_catalog is None:
            return
        mentions = self.game_catalog.extract_game_mentions(text, limit=3)
        if len(mentions) == 1:
            self.session_store.update_game_state(
                user_id=user_id,
                focused_game=mentions[0],
            )

    def remember_unbound_turn(self, *, role: str, text: str) -> None:
        self.session_store.remember_turn(user_id=None, role=role, text=text)

    def build_general_session_context(
        self,
        *,
        user_id: Optional[str],
        dialog_request_ctx: Dict[str, str],
        current_user_text: str,
    ) -> str:
        return self.session_store.build_general_session_context(
            user_id=user_id,
            exclude_user_text=current_user_text,
        )

    def _profile_snapshot(self, *, user_id: Optional[str]) -> Dict[str, Any]:
        helper = self.dialog_helper
        if helper is None or not user_id or helper.user_memory is None:
            return {}
        try:
            return helper.user_memory.profile_snapshot(user_id)
        except Exception as exc:
            logger.warning("profile snapshot failed: %s", exc)
            return {}

    @staticmethod
    def _log_doc_probe(telemetry: Dict[str, Any]) -> None:
        try:
            logger.info("doc_rag probe %s", json.dumps(telemetry, ensure_ascii=False, sort_keys=True))
        except Exception:
            logger.info("doc_rag probe %s", telemetry)

    @staticmethod
    def _normalize_capability_label(label: str) -> str:
        normalized = str(label or "").strip().lower().replace("-", "_").replace(" ", "_")
        return normalized

    @staticmethod
    def _normalized_query_text(text: str) -> str:
        lowered = str(text or "").strip().lower()
        lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
        return " ".join(lowered.split())

    def _capability_cache_key(self, *, text: str, focus_state: Any, clarification_hint: str = "") -> str:
        candidates = [
            str(item).strip()
            for item in getattr(focus_state, "candidate_entities", []) or []
            if str(item).strip()
        ]
        return "|".join(
            [
                self._normalized_query_text(text),
                str(getattr(focus_state, "active_capability", "") or "").strip().lower(),
                str(getattr(focus_state, "focused_entity", "") or "").strip().lower(),
                ",".join(item.lower() for item in candidates[:3]),
                str(int(getattr(focus_state, "consecutive_general_turns", 0) or 0)),
                str(clarification_hint or "").strip().lower(),
            ]
        )

    def _capability_cache_get(self, cache_key: str) -> str:
        item = self._capability_classifier_cache.get(cache_key)
        if item is None:
            return ""
        ts, label = item
        if (time.time() - ts) > 120.0:
            self._capability_classifier_cache.pop(cache_key, None)
            return ""
        return label

    def _capability_cache_put(self, cache_key: str, label: str) -> None:
        self._capability_classifier_cache[cache_key] = (time.time(), label)
        if len(self._capability_classifier_cache) > 256:
            oldest_key = min(self._capability_classifier_cache.items(), key=lambda kv: kv[1][0])[0]
            self._capability_classifier_cache.pop(oldest_key, None)

    def _hard_reset_reason(self, *, text: str) -> str:
        normalized = f" {self._normalized_query_text(text)} "
        if not normalized.strip():
            return ""
        if self._has_game_scope_signal(text=text):
            return ""

        if any(
            marker in normalized
            for marker in (
                " do not want to play the game ",
                " don t want to play the game ",
                " do not want the game ",
                " don t want the game ",
                " no game ",
                " not the game ",
            )
        ):
            return "game_rejection"

        if any(marker in normalized for marker in (" normal talk ", " normal talking ", " normally talking ", " normal conversation ", " regular conversation ", " talk normally ", " just talk ")):
            return "normal_talk"

        if any(marker in normalized for marker in (" switch topic ", " switch topics ", " go back to ", " back to ", " return to ")):
            if any(marker in normalized for marker in (" exercise ", " workout ", " plan ", " conversation ", " talking ", " talk ", " chat ")):
                return "topic_switch"

        if any(marker in normalized for marker in (" stop talking about ", " stop talking games ", " stop talking about games ")):
            return "topic_switch"

        return ""

    def _has_explicit_game_reference(self, *, text: str, session_state: Any = None) -> bool:
        if self.game_catalog is None:
            return False
        if session_state is None:
            return False
        return self.game_catalog.looks_like_game_followup(text, session_state=session_state)

    def _has_game_scope_signal(self, *, text: str, session_state: Any = None) -> bool:
        if self.game_catalog is None:
            return False
        if self.game_catalog.extract_game_mentions(text, limit=1):
            return True
        normalized = f" {self._normalized_query_text(text)} "
        if any(
            marker in normalized
            for marker in (
                " game ",
                " games ",
                " game option ",
                " game options ",
                " game choice ",
                " game choices ",
                " exercise game ",
                " exercise games ",
                " local game ",
                " local games ",
            )
        ):
            return True
        return False

    def _looks_like_general_planning_request(self, *, text: str, session_state: Any = None) -> bool:
        normalized = f" {self._normalized_query_text(text)} "
        if not normalized.strip():
            return False
        if self._has_game_scope_signal(text=text, session_state=session_state):
            return False
        if any(
            marker in normalized
            for marker in (
                " exercise plan ",
                " my exercise plan ",
                " workout plan ",
                " activity plan ",
                " training plan ",
                " exercise planning ",
                " workout planning ",
            )
        ):
            return True
        if " help me with my " in normalized and any(
            marker in normalized
            for marker in (" exercise ", " workout ", " plan ", " schedule ", " activity ", " training ")
        ):
            return True
        if " plan " in normalized and any(
            marker in normalized
            for marker in (" exercise ", " workout ", " activity ", " training ")
        ):
            return True
        return False

    def _is_ambiguous_help_request(self, *, text: str, session_state: Any = None) -> bool:
        normalized = f" {self._normalized_query_text(text)} "
        if not normalized.strip():
            return False
        if self._has_game_scope_signal(text=text, session_state=session_state):
            return False
        if self._looks_like_general_planning_request(text=text, session_state=session_state):
            return False
        if normalized in {" help ", " help me ", " help me with ", " help with "}:
            return True
        if " help me with " in normalized:
            if any(marker in normalized for marker in (" this ", " that ", " it ", " them ")):
                return True
            trailing = normalized.split(" help me with ", 1)[1].strip()
            if not trailing:
                return True
            if len(trailing.split()) <= 1 and trailing in {"this", "that", "it", "them"}:
                return True
        return False

    @staticmethod
    def _clarify_options_reply() -> str:
        return "What kind of options do you mean?"

    @staticmethod
    def _clarify_help_reply() -> str:
        return "What kind of help do you want?"

    def _clarification_reply_for_text(self, text: str, *, session_state: Any = None) -> Tuple[str, str]:
        if self._is_ambiguous_options_query(text=text, session_state=session_state):
            return "options", self._clarify_options_reply()
        return "help", self._clarify_help_reply()

    def _should_attempt_clarification_merge(self, clarification: Any, followup_text: str) -> bool:
        if clarification is None:
            return False
        if not str(getattr(clarification, "kind", "") or "").strip():
            return False
        if not str(getattr(clarification, "source_user_text", "") or "").strip():
            return False
        normalized = self._normalized_query_text(followup_text)
        if not normalized:
            return False
        if self._is_referential_doc_followup(followup_text) or self._is_short_confirmation_reply(followup_text):
            return True
        if len(normalized.split()) <= 4:
            return True
        if any(
            marker in f" {normalized} "
            for marker in (
                " games ",
                " game ",
                " exercise ",
                " workout ",
                " plan ",
                " options ",
                " option ",
            )
        ):
            return True
        return False

    def _merged_clarification_text(self, clarification: Any, followup_text: str) -> str:
        source = str(getattr(clarification, "source_user_text", "") or "").strip()
        followup = str(followup_text or "").strip()
        if not source:
            return followup
        if not followup:
            return source
        return f"{source} {followup}".strip()

    def _is_short_confirmation_reply(self, text: str) -> bool:
        normalized = self._normalized_query_text(text)
        return normalized in {
            "yes",
            "yeah",
            "yep",
            "sure",
            "okay",
            "ok",
            "that one",
            "the first one",
            "first one",
            "the second one",
            "second one",
            "it",
            "them",
            "the lab",
            "the research",
            "the team",
        }

    def _is_referential_doc_followup(self, text: str) -> bool:
        normalized = f" {self._normalized_query_text(text)} "
        if not normalized.strip():
            return False
        markers = (
            " his ",
            " her ",
            " their ",
            " its ",
            " it ",
            " them ",
            " they ",
            " the lab ",
            " his lab ",
            " her lab ",
            " the research ",
            " their research ",
            " the team ",
            " their team ",
            " who works there ",
        )
        return any(marker in normalized for marker in markers)

    def _resume_query_from_target(
        self,
        *,
        target_domain: str,
        target_answer_mode: str,
        target_general_focus: str,
        target_entity: str,
        related_entities: Dict[str, List[str]],
    ) -> str:
        target = str(target_entity or "").strip()
        if target_domain == "game":
            if target_answer_mode == "compare":
                compare_entities = [str(item).strip() for item in related_entities.get("compare", []) or [] if str(item).strip()]
                if len(compare_entities) >= 2:
                    return f"Compare {compare_entities[0]} and {compare_entities[1]}."
            if target_answer_mode == "availability":
                return "What games do you have?"
            if target:
                if target_answer_mode == "how_to":
                    return f"How do I play {target}?"
                if target_answer_mode == "factual":
                    return f"Tell me about {target}."
                return f"Tell me about {target}."
            return ""

        if target_general_focus == "people":
            return f"Tell me about {target}." if target else ""
        if target_general_focus == "research":
            return f"What research does {target} work on?" if target else ""
        if target_general_focus == "equipment":
            return f"What equipment does {target} have?" if target else ""
        if target_general_focus == "location_contact":
            return f"Where is {target}?" if target else ""
        if target_general_focus == "news":
            return f"What are the latest updates about {target}?" if target else ""
        if target:
            return f"What is {target}?"
        return ""

    def _structured_clarification_followup_text(self, clarification: Any, followup_text: str) -> str:
        normalized = self._normalized_query_text(followup_text)
        if not normalized:
            return ""
        target_domain = str(getattr(clarification, "target_domain", "") or "").strip().lower()
        target_answer_mode = str(getattr(clarification, "target_answer_mode", "") or "").strip().lower()
        target_general_focus = str(getattr(clarification, "target_general_focus", "") or "").strip().lower()
        target_entities = [
            str(item).strip()
            for item in getattr(clarification, "target_entities", []) or []
            if str(item).strip()
        ]
        related_entities = getattr(clarification, "related_entities", {}) or {}
        primary_target = target_entities[0] if target_entities else ""
        if self._is_short_confirmation_reply(followup_text):
            resumed = self._resume_query_from_target(
                target_domain=target_domain,
                target_answer_mode=target_answer_mode,
                target_general_focus=target_general_focus,
                target_entity=primary_target,
                related_entities=related_entities,
            )
            if resumed:
                return resumed
        if self._is_referential_doc_followup(followup_text):
            normalized_padded = f" {normalized} "
            lab_target = ""
            for key in ("lab", "labs"):
                values = [str(item).strip() for item in related_entities.get(key, []) or [] if str(item).strip()]
                if values:
                    lab_target = values[0]
                    break
            if any(marker in normalized_padded for marker in (" the research ", " their research ", " its research ")):
                target = lab_target or primary_target
                return f"What research does {target} work on?" if target else ""
            if any(marker in normalized_padded for marker in (" work on ", " works on ", " social robotics ", " serious games ", " virtual reality ", " hri ")):
                target = lab_target or primary_target
                return f"What research does {target} work on?" if target else ""
            if any(marker in normalized_padded for marker in (" equipment ", " tools ", " devices ", " sensors ")):
                target = lab_target or primary_target
                return f"What equipment does {target} have?" if target else ""
            if any(marker in normalized_padded for marker in (" the team ", " their team ", " who works there ", " researchers ")):
                target = lab_target or primary_target
                return f"What researchers does {target} have?" if target else ""
            if any(marker in normalized_padded for marker in (" contact ", " email ", " phone ", " where is ")):
                target = lab_target or primary_target
                return f"Where is {target}?" if target else ""
            if any(marker in normalized_padded for marker in (" his lab ", " her lab ", " the lab ")):
                return f"What is {lab_target}?" if lab_target else ""
            if any(marker in normalized_padded for marker in (" it ", " them ", " they ", " their ")):
                resumed = self._resume_query_from_target(
                    target_domain=target_domain,
                    target_answer_mode=target_answer_mode,
                    target_general_focus=target_general_focus,
                    target_entity=primary_target,
                    related_entities=related_entities,
                )
                if resumed:
                    return resumed
        return self._merged_clarification_text(clarification, followup_text)

    def _confirmed_doc_followup_text(self, *, text: str, focus_state: Any) -> str:
        active_capability = str(getattr(focus_state, "active_capability", "") or "").strip().lower()
        focus_source = str(getattr(focus_state, "focus_source", "") or "").strip().lower()
        focus_domain = str(getattr(focus_state, "focus_domain", "") or "").strip().lower()
        focus_general_focus = str(getattr(focus_state, "focus_general_focus", "") or "").strip().lower()
        focused_entity = str(getattr(focus_state, "focused_entity", "") or "").strip()
        related_entities = getattr(focus_state, "related_entities", {}) or {}
        if active_capability != "doc_query" or focus_source not in {"answer", "launch_command"}:
            return ""
        normalized = f" {self._normalized_query_text(text)} "
        if not normalized.strip():
            return ""
        lab_target = ""
        for key in ("lab", "labs"):
            values = [str(item).strip() for item in related_entities.get(key, []) or [] if str(item).strip()]
            if values:
                lab_target = values[0]
                break
        if focus_domain == "general":
            if any(marker in normalized for marker in (" the research ", " their research ", " its research ", " about the research ")):
                target = lab_target or focused_entity
                return f"What research does {target} work on?" if target else ""
            if any(marker in normalized for marker in (" work on ", " works on ", " social robotics ", " serious games ", " virtual reality ", " hri ")):
                target = lab_target or focused_entity
                return f"What research does {target} work on?" if target else ""
            if any(marker in normalized for marker in (" equipment ", " tools ", " devices ", " sensors ")):
                target = lab_target or focused_entity
                return f"What equipment does {target} have?" if target else ""
            if any(marker in normalized for marker in (" researchers ", " team ", " members ", " collaborators ", " who works there ")):
                target = lab_target or focused_entity
                return f"What researchers does {target} have?" if target else ""
            if any(marker in normalized for marker in (" contact ", " email ", " phone ", " where is ")):
                target = lab_target or focused_entity
                return f"Where is {target}?" if target else ""
            if any(marker in normalized for marker in (" his lab ", " her lab ", " the lab ")):
                target = lab_target or focused_entity
                return f"What is {target}?" if target else ""
            if any(marker in normalized for marker in (" do you know about it ", " tell me about it ", " know about it ", " do you know about them ")):
                if focus_general_focus == "people" and lab_target:
                    return f"What is {lab_target}?"
                if focus_general_focus == "research":
                    target = lab_target or focused_entity
                    return f"What research does {target} work on?" if target else ""
                if focused_entity:
                    return f"Tell me about {focused_entity}."
        if focus_domain == "game":
            if any(marker in normalized for marker in (" option ", " options ", " all the game ", " all the games ", " describe all the game ", " describe all the games ")):
                return "What games do you have?"
            if any(marker in normalized for marker in (" do you know about it ", " tell me about it ", " describe it ")):
                return f"Tell me about {focused_entity}." if focused_entity else ""
        return ""

    @staticmethod
    def _has_explicit_vision_scope(text: str) -> bool:
        normalized = f" {' '.join(re.sub(r'[^a-z0-9]+', ' ', str(text or '').lower()).split())} "
        return any(
            marker in normalized
            for marker in (
                " camera ",
                " see ",
                " frame ",
                " image ",
                " scene ",
                " preview ",
                " what do you see ",
                " can you see ",
                " look at ",
            )
        )

    @staticmethod
    def _decision_from_doc_probe(
        *,
        doc_probe: Any,
        explicit_reference: bool,
        routed_text: str,
    ) -> CapabilityRouteDecision:
        probe_telemetry = doc_probe.telemetry()
        probe_fallback_reason = str(getattr(doc_probe, "fallback_reason", "") or "").strip()
        reply_text = str(getattr(doc_probe, "response_text", "") or "").strip()
        payload = dict(getattr(doc_probe, "payload", {}) or {})
        if payload and "doc_confidence" not in payload:
            payload["doc_confidence"] = round(float(getattr(doc_probe, "doc_confidence", 0.0) or 0.0), 4)
        confidence = float(getattr(doc_probe, "doc_confidence", 0.0) or getattr(doc_probe, "routing_confidence", 0.0) or 0.0)
        return CapabilityRouteDecision(
            label="doc_query",
            confidence=confidence,
            clarification_text=reply_text,
            clarification_kind=str(getattr(doc_probe, "clarify_kind", "") or "").strip(),
            explicit_reference=explicit_reference,
            routed_text=routed_text,
            structured_payload=payload,
            probe_telemetry=probe_telemetry,
            fallback_reason=probe_fallback_reason,
        )

    def _is_ambiguous_options_query(self, *, text: str, session_state: Any = None) -> bool:
        normalized = f" {self._normalized_query_text(text)} "
        if not any(marker in normalized for marker in (" option ", " options ", " choice ", " choices ", " available ", " availability ")):
            return False
        if session_state is not None:
            focused_game = str(
                getattr(session_state, "focused_game", "")
                or getattr(session_state, "primary_recommendation", "")
                or ""
            ).strip()
            candidate_games = [
                str(item).strip()
                for item in getattr(session_state, "candidate_games", []) or []
                if str(item).strip()
            ]
            if focused_game or candidate_games:
                return False
        if self._has_game_scope_signal(text=text, session_state=session_state):
            return False
        if any(
            marker in normalized
            for marker in (
                " remember ",
                " know about me ",
                " know me ",
                " vision ",
                " camera ",
                " look at ",
                " see ",
                " introduce yourself ",
                " who are you ",
            )
        ):
            return False
        return True

    async def _semantic_capability_label(
        self,
        *,
        user_id: Optional[str],
        text: str,
        focus_state: Any,
        session_state: Any = None,
        clarification_hint: str = "",
        game_suppressed: bool = False,
    ) -> str:
        if not text.strip():
            return ""
        cache_key = self._capability_cache_key(text=text, focus_state=focus_state, clarification_hint=clarification_hint)
        cached = self._capability_cache_get(cache_key)
        if cached:
            return cached
        provider = _conversation_effective_response_provider(_conversation_profile())
        candidate_entities = [
            str(item).strip()
            for item in getattr(focus_state, "candidate_entities", []) or []
            if str(item).strip()
        ]
        prompt = (
            "Classify the latest user message into exactly one label:\n"
            "- memory_query\n"
            "- vision_query\n"
            "- general_chat\n"
            "- clarify\n\n"
            "Rules:\n"
            "- Doc-grounded game and document queries are handled elsewhere; only use these labels for memory, vision, ordinary chat, or a general clarification.\n"
            "- Exercise planning, workout planning, and general help requests default to general_chat.\n"
            "- If the message asks about vague options or choices without saying what kind and without an explicit game reference, choose clarify.\n"
            "- Choose general_chat for ordinary chat, self-introduction, or exercise planning that is not specifically about local games.\n"
            "- If the user is answering a recent clarification, interpret the combined request before falling back to a raw fragment.\n"
            "- Return only the label.\n\n"
            f"Active capability: {str(getattr(focus_state, 'active_capability', '') or '').strip() or '(none)'}\n"
            f"Focused entity: {str(getattr(focus_state, 'focused_entity', '') or '').strip() or '(none)'}\n"
            f"Candidate entities: {', '.join(candidate_entities) if candidate_entities else '(none)'}\n"
            f"Consecutive unrelated general turns: {int(getattr(focus_state, 'consecutive_general_turns', 0) or 0)}\n"
            f"Game reuse suppressed: {'yes' if game_suppressed else 'no'}\n"
            f"Clarification context: {clarification_hint or '(none)'}\n"
            f"Explicit game reference in latest message: {'yes' if self._has_explicit_game_reference(text=text, session_state=session_state) else 'no'}\n"
            f"Latest user message: {text}\n\n"
            "Label:"
        )
        system_prompt = (
            "You are a tiny classifier for voice assistant capabilities. "
            "Reply with exactly one label and no explanation."
        )
        raw = ""
        try:
            if provider == "openai":
                client = _AsyncOpenAIClient.get()
                response = await client.chat.completions.create(
                    model=_openai_response_model(),
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.0,
                    top_p=0.1,
                    max_tokens=16,
                    stream=False,
                )
                raw = response.choices[0].message.content if response.choices else ""
            else:
                client = _AsyncHttpClient.get()
                response = await client.post(
                    f"{_ollama_base_url()}/api/generate",
                    json={
                        "model": _conversation_local_response_model(),
                        "think": False,
                        "system": system_prompt,
                        "prompt": prompt,
                        "stream": False,
                        "options": {
                            "temperature": 0.0,
                            "top_p": 0.1,
                            "top_k": 8,
                            "num_predict": 16,
                            "repeat_penalty": 1.0,
                        },
                    },
                )
                raw = (response.json().get("response") or "") if response.status_code == 200 else ""
        except Exception:
            return ""
        normalized = self._normalize_capability_label(raw)
        if normalized not in {"memory_query", "vision_query", "general_chat", "clarify"}:
            return ""
        self._capability_cache_put(cache_key, normalized)
        return normalized

    async def route_query_capability(
        self,
        *,
        user_id: Optional[str],
        text: str,
    ) -> CapabilityRouteDecision:
        clarification = self.session_store.clarification_state(user_id)
        if str(getattr(clarification, "kind", "") or "").strip():
            clarification_hint = f"{clarification.kind}:{clarification.source_user_text}"
            if self._should_attempt_clarification_merge(clarification, text):
                merged_text = self._structured_clarification_followup_text(clarification, text)
                if merged_text:
                    merged_decision = await self._route_query_capability_once(
                        user_id=user_id,
                        text=merged_text,
                        clarification_hint=clarification_hint,
                    )
                    if merged_decision.label != "clarify":
                        self.session_store.clear_clarification(user_id)
                        merged_decision.routed_text = merged_text
                        merged_decision.merged_from_clarification = True
                        return merged_decision
            self.session_store.clear_clarification(user_id)
        return await self._route_query_capability_once(user_id=user_id, text=text)

    async def _route_query_capability_once(
        self,
        *,
        user_id: Optional[str],
        text: str,
        clarification_hint: str = "",
    ) -> CapabilityRouteDecision:
        self.ensure_ready()
        helper = self.dialog_helper
        assert helper is not None
        focus_state = self.session_store.capability_state(user_id)
        game_suppressed = self.session_store.is_game_suppressed(user_id)
        raw_session_state = None if game_suppressed else self.session_store.game_state(user_id)
        explicit_game_scope = self._has_game_scope_signal(text=text)
        explicit_reference = False if game_suppressed else self._has_explicit_game_reference(text=text, session_state=raw_session_state)
        recent_general_turns = int(getattr(focus_state, "consecutive_general_turns", 0) or 0)
        effective_session_state = raw_session_state if recent_general_turns <= 0 or explicit_reference else None
        probe_telemetry: Optional[Dict[str, Any]] = None
        probe_fallback_reason = ""
        local_docs_rag = getattr(self, "local_docs_rag", None)

        confirmed_followup_text = self._confirmed_doc_followup_text(text=text, focus_state=focus_state)
        if confirmed_followup_text and local_docs_rag is not None and getattr(local_docs_rag, "ready", False):
            try:
                doc_probe = local_docs_rag.probe(
                    confirmed_followup_text,
                    focus_state=focus_state,
                    session_state=effective_session_state,
                    user_profile=self._profile_snapshot(user_id=user_id),
                )
            except Exception as exc:
                logger.warning("doc_rag follow-up probe failed: %s", exc)
                doc_probe = None
            if doc_probe is not None:
                probe_telemetry = doc_probe.telemetry()
                probe_fallback_reason = str(getattr(doc_probe, "fallback_reason", "") or "").strip()
                self._log_doc_probe(probe_telemetry)
                if doc_probe.stage1_result == "doc_candidate" or bool(getattr(doc_probe, "open_world_fallback_blocked", False)):
                    return self._decision_from_doc_probe(
                        doc_probe=doc_probe,
                        explicit_reference=True,
                        routed_text=confirmed_followup_text,
                    )

        if helper._is_memory_query(text):
            return CapabilityRouteDecision(label="memory_query", confidence=1.0, routed_text=text)
        if helper.cfg.enable_vision_query and helper._is_vision_query(text):
            return CapabilityRouteDecision(label="vision_query", confidence=1.0, routed_text=text)

        normalized = f" {self._normalized_query_text(text)} "
        if any(marker in normalized for marker in (" introduce yourself ", " who are you ", " tell me about yourself ")):
            return CapabilityRouteDecision(label="general_chat", confidence=1.0, routed_text=text)

        if self._looks_like_general_planning_request(text=text, session_state=effective_session_state):
            return CapabilityRouteDecision(
                label="general_chat",
                confidence=1.0,
                explicit_reference=explicit_reference,
                routed_text=text,
            )

        if self._is_ambiguous_options_query(text=text, session_state=effective_session_state) or self._is_ambiguous_help_request(text=text, session_state=effective_session_state):
            clarify_kind, clarify_text = self._clarification_reply_for_text(text, session_state=effective_session_state)
            return CapabilityRouteDecision(
                label="clarify",
                confidence=1.0,
                clarification_text=clarify_text,
                clarification_kind=clarify_kind,
                explicit_reference=explicit_reference,
                routed_text=text,
            )

        if local_docs_rag is not None and getattr(local_docs_rag, "ready", False):
            try:
                doc_probe = local_docs_rag.probe(
                    text,
                    focus_state=focus_state,
                    session_state=effective_session_state,
                    user_profile=self._profile_snapshot(user_id=user_id),
                )
            except Exception as exc:
                logger.warning("doc_rag probe failed: %s", exc)
                doc_probe = None
            if doc_probe is not None:
                probe_telemetry = doc_probe.telemetry()
                probe_fallback_reason = doc_probe.fallback_reason
                self._log_doc_probe(probe_telemetry)
                if doc_probe.stage1_result == "doc_candidate" or bool(getattr(doc_probe, "open_world_fallback_blocked", False)):
                    return self._decision_from_doc_probe(
                        doc_probe=doc_probe,
                        explicit_reference=explicit_reference,
                        routed_text=text,
                    )

        semantic_label = await self._semantic_capability_label(
            user_id=user_id,
            text=text,
            focus_state=focus_state,
            session_state=effective_session_state,
            clarification_hint=clarification_hint,
            game_suppressed=game_suppressed,
        )
        if semantic_label == "memory_query" and not helper._is_memory_query(text):
            semantic_label = "general_chat"
        if semantic_label == "vision_query" and not self._has_explicit_vision_scope(text):
            semantic_label = "general_chat"
        if semantic_label == "clarify":
            if self._is_ambiguous_options_query(text=text, session_state=effective_session_state) or self._is_ambiguous_help_request(text=text, session_state=effective_session_state):
                clarify_kind, clarify_text = self._clarification_reply_for_text(text, session_state=effective_session_state)
                return CapabilityRouteDecision(
                    label="clarify",
                    confidence=0.7,
                    clarification_text=clarify_text,
                    clarification_kind=clarify_kind,
                    explicit_reference=explicit_reference,
                    routed_text=text,
                    probe_telemetry=probe_telemetry,
                    fallback_reason=probe_fallback_reason,
                )
            return CapabilityRouteDecision(
                label="general_chat",
                confidence=0.55,
                explicit_reference=explicit_reference,
                routed_text=text,
                probe_telemetry=probe_telemetry,
                fallback_reason=probe_fallback_reason,
            )
        if semantic_label in {"memory_query", "vision_query"}:
            return CapabilityRouteDecision(
                label=semantic_label,
                confidence=0.7,
                explicit_reference=explicit_reference,
                routed_text=text,
                probe_telemetry=probe_telemetry,
                fallback_reason=probe_fallback_reason,
            )
        return CapabilityRouteDecision(
            label="general_chat",
            confidence=0.5,
            explicit_reference=explicit_reference,
            routed_text=text,
            probe_telemetry=probe_telemetry,
            fallback_reason=probe_fallback_reason,
        )

    def _all_game_names(self) -> List[str]:
        cards = getattr(self.game_catalog, "cards", None)
        if not isinstance(cards, list):
            return []
        return [str(getattr(card, "name", "")).strip() for card in cards if str(getattr(card, "name", "")).strip()]

    async def _render_structured_reply(self, *, user_text: str, payload: Dict[str, Any]) -> str:
        effective_payload = await _prepare_structured_payload_for_render(user_text, payload)
        payload.clear()
        payload.update(effective_payload)
        if str(effective_payload.get("type") or "").strip().startswith("game_"):
            allowed = [
                str(item).strip()
                for item in (
                    effective_payload.get("allowed_game_names")
                    or effective_payload.get("candidate_games")
                    or []
                )
                if str(item).strip()
            ]
            primary = str(effective_payload.get("primary_game_name") or effective_payload.get("game_name") or "").strip()
            reference = str(effective_payload.get("reference_game_name") or "").strip()
            for name in (primary, reference):
                if name and name not in allowed:
                    allowed.append(name)
            effective_payload["allowed_game_names"] = allowed
            return await _spoken_reply_from_payload(user_text, effective_payload, all_game_names=self._all_game_names())
        if str(effective_payload.get("type") or "").strip() in {"doc_answer", "doc_clarify"} and str(effective_payload.get("domain") or "").strip() == "game":
            allowed = [
                str(item).strip()
                for item in (
                    effective_payload.get("allowed_entities")
                    or effective_payload.get("candidate_entities")
                    or effective_payload.get("game_names")
                    or []
                )
                if str(item).strip()
            ]
            primary = str(effective_payload.get("primary_entity") or "").strip()
            if primary and primary not in allowed:
                allowed.append(primary)
            effective_payload["allowed_entities"] = allowed
            effective_payload["allowed_game_names"] = allowed[:]
            return await _spoken_reply_from_payload(user_text, effective_payload, all_game_names=self._all_game_names())
        return await _spoken_reply_from_payload(user_text, effective_payload)

    def finalize_assistant_turn(
        self,
        *,
        user_id: Optional[str],
        user_text: str,
        answer_text: str,
        dialog_request_ctx: Dict[str, str],
        track_game_mentions: bool = False,
    ) -> None:
        self.ensure_ready()
        helper = self.dialog_helper
        assert helper is not None
        self.session_store.remember_turn(user_id=user_id, role="assistant", text=answer_text)
        if track_game_mentions and self.game_catalog is not None:
            mentions = self.game_catalog.extract_game_mentions(answer_text, limit=3)
            if len(mentions) == 1:
                self.session_store.update_game_state(
                    user_id=user_id,
                    focused_game=mentions[0],
                )
        if not user_id or helper.user_memory is None:
            return
        if track_game_mentions:
            try:
                helper._remember_game_context(
                    user_id=user_id,
                    text=answer_text,
                    reference_kind="mentioned",
                    source="assistant",
                )
            except Exception as exc:
                logger.warning("game context update failed: %s", exc)
        if not helper.cfg.enable_dialog_context:
            return

        try:
            helper.user_memory.remember_dialog_turn(
                user_id,
                "assistant",
                answer_text,
                max_turns=helper.cfg.dialog_history_turns,
                summary_max_chars=helper.cfg.dialog_summary_max_chars,
            )
            helper._update_dialog_slots_after_reply(
                user_id=user_id,
                user_text=user_text,
                answer_text=answer_text,
                previous_topic=dialog_request_ctx.get("current_topic", ""),
            )
        except Exception as exc:
            logger.warning("dialog slot update failed: %s", exc)

    async def try_memory_reply(
        self,
        *,
        user_id: Optional[str],
        text: str,
        dialog_request_ctx: Dict[str, str],
    ) -> str:
        self.ensure_ready()
        helper = self.dialog_helper
        assert helper is not None
        if not helper._is_memory_query(text):
            return ""
        payload: Dict[str, Any] = {}
        if user_id and helper.user_memory is not None:
            try:
                payload = helper.user_memory.answer_memory_query_payload(user_id, text)
            except Exception as exc:
                logger.warning("user memory reply failed: %s", exc)
                return ""
        else:
            payload = self.session_store.build_memory_payload(user_id=user_id, query_text=text)
        reply = await self._render_structured_reply(user_text=text, payload=payload)
        reply = _finalize_static_tts_reply(helper, reply)
        if reply:
            self.session_store.record_general_turn(user_id=user_id, capability="memory_query")
            self.finalize_assistant_turn(
                user_id=user_id,
                user_text=text,
                answer_text=reply,
                dialog_request_ctx=dialog_request_ctx,
            )
        return reply

    async def try_memory_write_reply(
        self,
        *,
        user_id: Optional[str],
        text: str,
        memory_update: Dict[str, Any],
        dialog_request_ctx: Dict[str, str],
    ) -> str:
        self.ensure_ready()
        helper = self.dialog_helper
        assert helper is not None
        if not user_id or helper.user_memory is None:
            return ""
        if not str(memory_update.get("ack_text") or "").strip():
            return ""
        facts_written = [
            {
                "field": str(item.get("field") or "").strip(),
                "value": str(item.get("value") or "").strip(),
                "spoken_text": _structured_memory_field_clause(str(item.get("field") or ""), str(item.get("value") or "")),
            }
            for item in memory_update.get("facts_written", []) or []
            if str(item.get("value") or "").strip()
        ]
        payload = {
            "type": "memory_write_ack",
            "result_kind": "memory_write_ack",
            "binding_state": "bound_profile",
            "facts": facts_written,
            "notes": [],
            "required_terms": [str(item.get("value") or "").strip() for item in facts_written[:2] if str(item.get("value") or "").strip()],
            "text": str(memory_update.get("ack_text") or "").strip(),
            "max_sentences": 2,
        }
        reply = await self._render_structured_reply(user_text=text, payload=payload)
        reply = _finalize_static_tts_reply(helper, reply)
        if reply:
            self.session_store.record_general_turn(user_id=user_id, capability="memory_write")
            self.finalize_assistant_turn(
                user_id=user_id,
                user_text=text,
                answer_text=reply,
                dialog_request_ctx=dialog_request_ctx,
            )
        return reply

    def record_game_event(
        self,
        *,
        user_id: Optional[str],
        game_name: str,
        action: str = "launch",
        source: str = "conversation",
    ) -> None:
        self.ensure_ready()
        helper = self.dialog_helper
        assert helper is not None
        self.session_store.update_game_state(
            user_id=user_id,
            focused_game=game_name,
            candidate_games=[game_name],
            primary_recommendation=None,
            last_router_intent="game_launch_followup" if str(action or "").strip().lower() == "launch" else "game_event",
            focus_source="launch_command",
        )
        if not user_id or helper.user_memory is None:
            return
        try:
            helper.user_memory.record_game_event(
                user_id,
                game_name=game_name,
                action=action,
                source=source,
            )
        except Exception as exc:
            logger.warning("game event record failed: %s", exc)

    def try_vision_reply(
        self,
        *,
        user_id: Optional[str],
        text: str,
        dialog_request_ctx: Dict[str, str],
    ) -> str:
        self.ensure_ready()
        helper = self.dialog_helper
        assert helper is not None
        if not helper.cfg.enable_vision_query or not helper._is_vision_query(text):
            return ""

        reply = helper._request_vision_description()
        reply = _finalize_static_tts_reply(helper, reply)
        if reply:
            self.session_store.record_general_turn(user_id=user_id, capability="vision_query")
            self.finalize_assistant_turn(
                user_id=user_id,
                user_text=text,
                answer_text=reply,
                dialog_request_ctx=dialog_request_ctx,
            )
        return reply

    def try_general_query_reply(
        self,
        *,
        user_id: Optional[str],
        text: str,
        dialog_request_ctx: Dict[str, str],
    ) -> str:
        self.ensure_ready()
        helper = self.dialog_helper
        assert helper is not None
        reply = _try_general_query_reply_text(text)
        if not reply:
            return ""
        reply = _finalize_static_tts_reply(helper, reply)
        if reply:
            self.session_store.record_general_turn(user_id=user_id, capability="general_chat")
            self.finalize_assistant_turn(
                user_id=user_id,
                user_text=text,
                answer_text=reply,
                dialog_request_ctx=dialog_request_ctx,
            )
        return reply


_UNIFIED_CONVERSATION_RUNTIME: Optional[_UnifiedConversationRuntime] = None
_UNIFIED_CONVERSATION_RUNTIME_LOCK = asyncio.Lock()


async def _get_unified_conversation_runtime(*, force_reload: bool = False) -> _UnifiedConversationRuntime:
    global _UNIFIED_CONVERSATION_RUNTIME
    async with _UNIFIED_CONVERSATION_RUNTIME_LOCK:
        if force_reload or _UNIFIED_CONVERSATION_RUNTIME is None:
            if _UNIFIED_CONVERSATION_RUNTIME is not None:
                _UNIFIED_CONVERSATION_RUNTIME.close()
            _UNIFIED_CONVERSATION_RUNTIME = _UnifiedConversationRuntime()
        return _UNIFIED_CONVERSATION_RUNTIME


def _finalize_static_tts_reply(dialog_helper: Any, reply_text: str) -> str:
    text = (reply_text or "").strip()
    if not text:
        return ""
    if sanitize_tts_text is not None:
        text = sanitize_tts_text(text)
    if not text:
        return ""
    if getattr(dialog_helper, "reply_compress", False):
        if compress_reply_for_latency is not None:
            text = compress_reply_for_latency(
                text,
                max_sentences=getattr(dialog_helper, "reply_max_sentences", 0),
                max_chars=getattr(dialog_helper, "reply_max_chars", 0),
            )
        if compress_reply_by_words is not None:
            text = compress_reply_by_words(text, getattr(dialog_helper, "reply_max_words", 0))
        if trim_trailing_connectors is not None:
            text = trim_trailing_connectors(text)
        if text and text[-1] not in ".!?。！？":
            text = f"{text}."
    return text.strip()


def _json_line(payload: Dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def _normalize_transcript_confidence(value: Optional[str]) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"high", "medium", "low"}:
        return normalized
    return ""


def _normalize_request_source(value: Optional[str]) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    normalized = re.sub(r"\s+", "_", normalized)
    return normalized


def _is_live_captions_request(payload: ConversationTurnRequest) -> bool:
    normalized = _normalize_request_source(payload.source)
    if not normalized:
        return False
    parts = [part for part in re.split(r"[:/]", normalized) if part]
    return "live_captions" in parts


def _should_clarify_uncertain_turn(payload: ConversationTurnRequest, route_type: str) -> bool:
    if str(route_type or "").strip().upper() != "QUERY":
        return False
    confidence = _normalize_transcript_confidence(payload.transcript_confidence)
    source = str(payload.transcript_source or "").strip().lower()
    if _is_live_captions_request(payload) and source != "stable_partial_fallback":
        return False
    if confidence == "low":
        return True
    return source == "stable_partial_fallback"


def _uncertain_turn_reply(payload: ConversationTurnRequest) -> str:
    source = str(payload.transcript_source or "").strip().lower()
    if source == "stable_partial_fallback":
        return "I may have misheard that. Please say it again."
    return "I may have heard that incorrectly. Please repeat it."


class _ReplyChunkAccumulator:
    def __init__(self) -> None:
        self.buffer = ""

    def push(self, delta: str) -> List[str]:
        if delta:
            self.buffer += delta
        return self._drain(final=False)

    def finish(self) -> List[str]:
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> List[str]:
        chunks: List[str] = []
        while True:
            boundary = _find_reply_boundary(self.buffer, final=final)
            if boundary <= 0:
                break
            chunk = self.buffer[:boundary].strip()
            self.buffer = self.buffer[boundary:].lstrip()
            if chunk:
                chunks.append(chunk)
            if not final and not self.buffer:
                break
            if not final and len(chunks) >= 2:
                break
        if final and self.buffer.strip():
            chunks.append(self.buffer.strip())
            self.buffer = ""
        return chunks


def _find_reply_boundary(text: str, *, final: bool) -> int:
    current = (text or "")
    if not current:
        return 0

    hard_boundaries = ".!?銆傦紒锛焅n"
    soft_boundaries = ",锛?锛?锛?"
    minimum_chunk_chars = 18
    for idx in range(len(current) - 1, minimum_chunk_chars - 1, -1):
        if current[idx] in hard_boundaries:
            return idx + 1

    if len(current) >= 72:
        for idx in range(min(len(current) - 1, 120), minimum_chunk_chars - 1, -1):
            if current[idx] in soft_boundaries:
                return idx + 1
    if final:
        return len(current)
    return 0


def _duration_seconds(audio: np.ndarray, sample_rate: int) -> Optional[float]:
    if audio.size <= 0 or sample_rate <= 0:
        return None
    return float(audio.size) / float(sample_rate)


def _audio_float_to_wav_bytes(audio: np.ndarray, sample_rate: int) -> bytes:
    # OpenAI transcription endpoint accepts file-like audio payloads.
    clipped = np.clip(audio, -1.0, 1.0)
    pcm16 = np.asarray(clipped * 32767.0, dtype=np.int16)
    with io.BytesIO() as buffer:
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm16.tobytes())
        return buffer.getvalue()


def _session_prompt_for_transcribe(
    state: "_SessionState", now: float, *, include_command_hints: bool
) -> str:
    base_prompt = _wake_word_prompt(include_command_hints=include_command_hints)
    if (
        WHISPER_STREAM_CONTEXT_CHARS > 0
        and state.last_text
        and now - (state.last_ts or 0.0) <= WHISPER_STREAM_SESSION_TTL_SECONDS
    ):
        return (base_prompt + " " + state.last_text[-WHISPER_STREAM_CONTEXT_CHARS:]).strip()
    return base_prompt


def _update_transcribe_session_state(
    *,
    state: "_SessionState",
    now: float,
    audio: np.ndarray,
    full_text: str,
) -> None:
    state.last_ts = now
    state.last_text = (full_text or "").strip()
    if WHISPER_STREAM_OVERLAP_SECONDS <= 0.0 or audio.size <= 0:
        return

    tail_samples = int(DEFAULT_SAMPLE_RATE * WHISPER_STREAM_OVERLAP_SECONDS)
    if tail_samples <= 0:
        return
    if audio.size >= tail_samples:
        state.audio_tail = np.asarray(audio[-tail_samples:], dtype=np.float32)
    else:
        state.audio_tail = np.asarray(audio, dtype=np.float32)

def _piper_http_base_url() -> str:
    # Base URL for the Piper HTTP wrapper (piper_http.py)
    # Defaults to local instance started by scripts/start_local_services.py
    return _environment("PIPER_HTTP_URL", "http://127.0.0.1:5005").rstrip("/")


def _compose_dialog_system_prompt(
    *,
    base_system_prompt: str,
    has_dialog_context: bool,
    barge_in: bool,
    user_text: str,
) -> str:
    lowered_user_text = (user_text or "").strip().lower()

    def requests_single_sentence_reply() -> bool:
        markers = (
            "one short sentence",
            "one sentence",
            "single sentence",
            "in one short sentence",
            "briefly in one sentence",
            "一句话",
        )
        return any(marker in lowered_user_text for marker in markers)

    def requests_brief_reply() -> bool:
        if requests_single_sentence_reply():
            return True
        markers = (
            "brief",
            "briefly",
            "short answer",
            "keep it short",
            "concise",
            "简短",
            "简洁",
        )
        return any(marker in lowered_user_text for marker in markers)

    instructions: List[str] = [
        "Conversation protocol:",
        "- Treat each turn as part of an ongoing dialogue, not a single-turn Q&A.",
        "- Keep the reply natural for speech output; no lists, no labels, no meta commentary.",
        "- Start with one short contextual bridge only when it truly helps continuity.",
        "- Then answer directly and concretely based on the latest user message.",
        "- Keep default length to 1-2 concise sentences unless clarification is required.",
        "- Do not add a generic follow-up question after a complete answer.",
        "- Do not invent emotions, symptoms, or user states that were not explicitly stated.",
        "- Do not repeat old advice when the user asks for a topic switch.",
        "- When replying from memory or local structured data, use natural spoken phrasing instead of system-style lead-ins.",
        "- Avoid phrasing like 'From what I have saved' or 'I don't have access to your personal history'.",
        "- When explaining a game, define the game plainly instead of inventing benefits.",
        "- Use literal, practical language; avoid poetic or metaphorical phrasing.",
        "- Treat simple preferences (for example morning/evening) as scheduling input, not emotional signals.",
        "- Do not address the user by name unless they clearly asked for that or the current turn makes it genuinely useful.",
        "- If the user shares a personal event or disappointment, respond to that content directly instead of converting it into coaching language.",
        "- If the user asks for help with a plan, answer that planning topic directly unless a specific missing detail truly blocks a useful answer.",
        "- If the user clearly switches away from games or says they do not want a game, do not mention prior games again unless the user explicitly asks about games.",
        "- For real-world or out-of-scope questions, give a brief honest limitation or a cautious general answer.",
        "- Do not say you can see, check, monitor, track, or find something unless tool or vision context explicitly provides it.",
        "- Do not invent exercise plans, stretches, walks, or activity recommendations unless the user explicitly asked for that kind of planning.",
        "- If the user asks for vague options without naming a domain, ask a short clarification instead of assuming games, exercise, or rehab.",
        "- Use an answer-first style: answer directly, and only ask one short follow-up when it is truly needed.",
        "- Avoid coaching phrases like 'How does that feel?' or 'what goals would you like to work on?' unless the user explicitly wants that style.",
        "- Reply in English only.",
    ]
    if requests_single_sentence_reply():
        instructions.append("- The user explicitly asked for one short sentence. Reply with exactly one short sentence.")
        instructions.append("- Do not add a second sentence, disclaimer, or follow-up question.")
    elif requests_brief_reply():
        instructions.append("- The user asked for a brief answer. Use one concise sentence if possible, at most two.")
        instructions.append("- Do not add a generic follow-up question.")
    instructions.append("- If user intent is genuinely ambiguous, ask one concise clarification question.")

    if has_dialog_context:
        instructions.append("- Reuse relevant facts from dialogue context when they improve coherence.")
    if barge_in:
        instructions.append("- The user interrupted playback. Treat any unfinished assistant sentence as canceled.")
        instructions.append("- Prioritize the latest user message and respond to that directly.")

    merged = (base_system_prompt or "").strip()
    if merged:
        merged += "\n\n"
    merged += "\n".join(instructions)
    return merged


async def _build_coach_prompt_package(
    user_text: str,
    system_override: Optional[str] = None,
    memory_context: Optional[str] = None,
    user_id: Optional[str] = None,
    dialog_context: Optional[str] = None,
    barge_in: bool = False,
    interrupted_tts_text: Optional[str] = None,
) -> tuple[str, str]:
    effective_system_prompt, _, _ = await _get_effective_ollama_system_prompt()
    prompt_parts: List[str] = []

    dialog_context_text = (dialog_context or "").strip()
    if dialog_context_text:
        if len(dialog_context_text) > 1200:
            dialog_context_text = dialog_context_text[:1200].rstrip()
        prompt_parts.append("Dialogue context (summary + recent turns):\n" + dialog_context_text)

    context_text = (memory_context or "").strip()
    if context_text:
        if len(context_text) > 600:
            context_text = context_text[:600].rstrip()
        prompt_parts.append(
            "User memory context (may be partial, use only when relevant):\n"
            f"{context_text}"
        )

    if user_id:
        prompt_parts.append(f"Active user id: {str(user_id).strip()}.")
    if barge_in:
        prompt_parts.append("Barge-in hint: user interrupted assistant playback.")
    interrupted_text = (interrupted_tts_text or "").strip()
    if barge_in and interrupted_text:
        if len(interrupted_text) > 260:
            interrupted_text = interrupted_text[:260].rstrip()
        prompt_parts.append(
            "Interrupted assistant text (may be incomplete; do not continue blindly): "
            + interrupted_text
        )

    prompt_parts.append("Latest user message:")
    prompt_parts.append(f"User: {user_text}")
    prompt_parts.append("Assistant:")

    merged_system = _compose_dialog_system_prompt(
        base_system_prompt=(system_override or "").strip() or effective_system_prompt,
        has_dialog_context=bool(dialog_context_text),
        barge_in=bool(barge_in),
        user_text=user_text,
    )
    prompt_text = "\n\n".join(prompt_parts)
    logger.info(
        "coach prompt budget: memory_chars=%d dialog_chars=%d prompt_chars=%d",
        len(context_text),
        len(dialog_context_text),
        len(prompt_text),
    )
    return merged_system, prompt_text


async def _build_ollama_generate_payload(
    user_text: str,
    *,
    system_override: Optional[str] = None,
    memory_context: Optional[str] = None,
    user_id: Optional[str] = None,
    dialog_context: Optional[str] = None,
    barge_in: bool = False,
    interrupted_tts_text: Optional[str] = None,
    stream: bool = False,
    model_override: Optional[str] = None,
) -> Dict[str, Any]:
    keep_alive = _environment("OLLAMA_KEEP_ALIVE", "30m")
    merged_system, prompt = await _build_coach_prompt_package(
        user_text,
        system_override=system_override,
        memory_context=memory_context,
        user_id=user_id,
        dialog_context=dialog_context,
        barge_in=barge_in,
        interrupted_tts_text=interrupted_tts_text,
    )
    payload: Dict[str, Any] = {
        "model": (model_override or _ollama_model()).strip() or _ollama_model(),
        "think": _ollama_think_enabled(),
        "system": merged_system,
        "prompt": prompt,
        "stream": bool(stream),
        "options": {
            "temperature": _environment_float("OLLAMA_TEMPERATURE", 0.7),
            "top_p": _environment_float("OLLAMA_TOP_P", 0.8),
            "top_k": _environment_int("OLLAMA_TOP_K", 20),
            "num_predict": _environment_int("OLLAMA_MAX_TOKENS", 120),
            "repeat_penalty": _environment_float("OLLAMA_REPEAT_PENALTY", 1.1),
        },
    }
    if keep_alive:
        payload["keep_alive"] = keep_alive
    return payload


async def _stream_ollama_reply(
    user_text: str,
    *,
    system_override: Optional[str] = None,
    memory_context: Optional[str] = None,
    user_id: Optional[str] = None,
    dialog_context: Optional[str] = None,
    barge_in: bool = False,
    interrupted_tts_text: Optional[str] = None,
    model_override: Optional[str] = None,
) -> AsyncIterator[str]:
    payload = await _build_ollama_generate_payload(
        user_text,
        system_override=system_override,
        memory_context=memory_context,
        user_id=user_id,
        dialog_context=dialog_context,
        barge_in=barge_in,
        interrupted_tts_text=interrupted_tts_text,
        stream=True,
        model_override=model_override,
    )
    url = f"{_ollama_base_url()}/api/generate"
    client = _AsyncHttpClient.get()
    try:
        async with client.stream("POST", url, json=payload, timeout=None) as response:
            if response.status_code != 200:
                detail = (await response.aread()).decode("utf-8", errors="ignore").strip()
                raise OllamaError(f"Ollama returned status {response.status_code}: {detail}")
            async for line in response.aiter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                token = str(data.get("response") or "")
                if token:
                    yield token
                if data.get("done"):
                    break
    except httpx.HTTPError as exc:
        raise OllamaError(f"Failed to contact Ollama at {url}: {exc}") from exc


async def _stream_openai_reply(
    user_text: str,
    *,
    system_override: Optional[str] = None,
    memory_context: Optional[str] = None,
    user_id: Optional[str] = None,
    dialog_context: Optional[str] = None,
    barge_in: bool = False,
    interrupted_tts_text: Optional[str] = None,
    model_override: Optional[str] = None,
) -> AsyncIterator[str]:
    client = _AsyncOpenAIClient.get()
    merged_system, prompt = await _build_coach_prompt_package(
        user_text,
        system_override=system_override,
        memory_context=memory_context,
        user_id=user_id,
        dialog_context=dialog_context,
        barge_in=barge_in,
        interrupted_tts_text=interrupted_tts_text,
    )
    model = (model_override or _openai_response_model()).strip() or _openai_response_model()
    try:
        stream = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": merged_system},
                {"role": "user", "content": prompt},
            ],
            temperature=_environment_float("OPENAI_RESPONSE_TEMPERATURE", 0.6),
            top_p=_environment_float("OPENAI_RESPONSE_TOP_P", 0.9),
            max_tokens=_environment_int("OPENAI_RESPONSE_MAX_TOKENS", 180),
            stream=True,
        )
    except Exception as exc:
        raise OpenAIResponseError(f"OpenAI response request failed: {exc}") from exc

    async for chunk in stream:
        try:
            choice = chunk.choices[0] if chunk.choices else None
            delta = getattr(choice, "delta", None)
            content = getattr(delta, "content", None) if delta is not None else None
        except Exception:
            content = None
        if isinstance(content, str) and content:
            yield content


async def _generate_coach_reply(
    user_text: str,
    system_override: Optional[str] = None,
    memory_context: Optional[str] = None,
    user_id: Optional[str] = None,
    dialog_context: Optional[str] = None,
    barge_in: bool = False,
    interrupted_tts_text: Optional[str] = None,
) -> str:
    payload = await _build_ollama_generate_payload(
        user_text,
        system_override=system_override,
        memory_context=memory_context,
        user_id=user_id,
        dialog_context=dialog_context,
        barge_in=barge_in,
        interrupted_tts_text=interrupted_tts_text,
        stream=False,
    )
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


def _build_structured_render_prompt(user_text: str, payload: Dict[str, Any]) -> Tuple[str, str]:
    compact_payload = {
        "type": str(payload.get("type") or "").strip(),
        "domain": str(payload.get("domain") or "").strip(),
        "answer_mode": str(payload.get("answer_mode") or "").strip(),
        "general_focus": str(payload.get("general_focus") or "").strip(),
        "result_kind": str(payload.get("result_kind") or "").strip(),
        "binding_state": str(payload.get("binding_state") or "").strip(),
        "primary_game_name": str(payload.get("primary_game_name") or payload.get("game_name") or "").strip(),
        "reference_game_name": str(payload.get("reference_game_name") or "").strip(),
        "candidate_games": [str(item).strip() for item in payload.get("candidate_games", []) or [] if str(item).strip()],
        "primary_entity": str(payload.get("primary_entity") or "").strip(),
        "candidate_entities": [str(item).strip() for item in payload.get("candidate_entities", []) or [] if str(item).strip()],
        "allowed_entities": [str(item).strip() for item in payload.get("allowed_entities", []) or [] if str(item).strip()],
        "related_entities": payload.get("related_entities", {}) or {},
        "related_entity_roles": payload.get("related_entity_roles", {}) or {},
        "intent": str(payload.get("intent") or "").strip(),
        "facts": payload.get("facts", []) or [],
        "notes": payload.get("notes", []) or [],
        "reason_text": str(payload.get("reason_text") or "").strip(),
        "recommendation_reason": str(payload.get("recommendation_reason") or "").strip(),
        "summary_text": str(payload.get("summary_text") or "").strip(),
        "summary_points": [str(item).strip() for item in payload.get("summary_points", []) or [] if str(item).strip()],
        "doc_snippets": [str(item).strip() for item in payload.get("doc_snippets", []) or [] if str(item).strip()],
        "doc_source_ids": [str(item).strip() for item in payload.get("doc_source_ids", []) or [] if str(item).strip()],
        "required_terms": [str(item).strip() for item in payload.get("required_terms", []) or [] if str(item).strip()],
        "allowed_game_names": [str(item).strip() for item in payload.get("allowed_game_names", []) or [] if str(item).strip()],
        "launchable_games": [str(item).strip() for item in payload.get("launchable_games", []) or [] if str(item).strip()],
        "clarify_kind": str(payload.get("clarify_kind") or "").strip(),
        "fallback_text": str(payload.get("summary_text") or payload.get("text") or "").strip(),
        "max_sentences": int(payload.get("max_sentences") or 2),
    }
    prompt = (
        "Rewrite the structured result as a short spoken reply.\n"
        "Keep the facts exact and keep every required term.\n"
        "Do not add any new facts.\n"
        "Do not mention any game outside allowed_game_names.\n"
        "Do not mention any entity outside allowed_entities.\n"
        "Do not use labels or bullet lists.\n\n"
        f"Latest user message:\n{user_text}\n\n"
        "Structured result JSON:\n"
        + json.dumps(compact_payload, ensure_ascii=False, indent=2)
        + "\n\nSpoken reply:"
    )
    return STRUCTURED_RENDER_SYSTEM_PROMPT, prompt


async def _generate_structured_spoken_reply(user_text: str, payload: Dict[str, Any]) -> str:
    provider = _conversation_effective_response_provider(_conversation_profile())
    system_prompt, prompt = _build_structured_render_prompt(user_text, payload)
    if provider == "openai":
        client = _AsyncOpenAIClient.get()
        model = _openai_response_model()
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.28,
                top_p=0.85,
                max_tokens=96,
                stream=False,
            )
        except Exception as exc:
            raise OpenAIResponseError(f"OpenAI structured render request failed: {exc}") from exc
        try:
            content = response.choices[0].message.content if response.choices else ""
        except Exception:
            content = ""
        reply = _normalize_final_reply_text(str(content or ""))
        if not reply:
            raise OpenAIResponseError("OpenAI structured render response was empty")
        return reply

    payload_json: Dict[str, Any] = {
        "model": _conversation_local_response_model(),
        "think": False,
        "system": system_prompt,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.22,
            "top_p": 0.82,
            "top_k": 20,
            "num_predict": 96,
            "repeat_penalty": 1.08,
        },
    }
    keep_alive = _environment("OLLAMA_KEEP_ALIVE", "30m")
    if keep_alive:
        payload_json["keep_alive"] = keep_alive
    url = f"{_ollama_base_url()}/api/generate"
    try:
        client = _AsyncHttpClient.get()
        response = await client.post(url, json=payload_json)
    except httpx.HTTPError as exc:
        raise OllamaError(f"Failed to contact Ollama at {url}: {exc}") from exc
    if response.status_code != 200:
        raise OllamaError(f"Ollama returned status {response.status_code}: {response.text.strip()}")
    data = response.json()
    reply = _normalize_final_reply_text(str(data.get("response") or ""))
    if not reply:
        raise OllamaError("Ollama structured render response was empty")
    return reply


async def _spoken_reply_from_payload(
    user_text: str,
    payload: Dict[str, Any],
    *,
    all_game_names: Optional[List[str]] = None,
) -> str:
    payload_type = str(payload.get("type") or "").strip().lower()
    if payload_type == "doc_answer":
        doc_snippets = [str(item).strip() for item in payload.get("doc_snippets", []) or [] if str(item).strip()]
        if not doc_snippets:
            return "I could not find that in the local documents."
    fallback_text = _normalize_final_reply_text(_structured_template_reply(payload, user_text) or str(payload.get("text") or ""))
    if fallback_text:
        payload = dict(payload)
        payload["text"] = fallback_text
    attempt_payload = dict(payload)
    for attempt in range(2):
        try:
            rendered = await _generate_structured_spoken_reply(user_text, attempt_payload)
        except (OllamaError, OpenAIResponseError, RuntimeError) as exc:
            logger.info("structured render fallback: %s", exc)
            return fallback_text
        valid, reason = _validate_structured_reply(rendered, attempt_payload, all_game_names=all_game_names)
        if valid:
            return rendered
        if attempt == 0 and str(reason or "").startswith("unexpected_entity:"):
            attempt_payload = dict(attempt_payload)
            attempt_payload["text"] = fallback_text
            attempt_payload["regenerate_allowed_only"] = True
            logger.info("structured render retry with allowed_entities only: %s", reason)
            continue
        if str(reason or "") == "compare_entities_missing":
            clarify_payload = dict(attempt_payload)
            clarify_payload["type"] = "doc_clarify"
            clarify_payload["answer_mode"] = "compare"
            clarify_payload["clarify_kind"] = "clarify_missing_entity"
            clarify_payload["text"] = ""
            return _normalize_final_reply_text(_structured_template_reply(clarify_payload, user_text))
        if str(reason or "") == "recommend_invalid_entity" or (
            str(reason or "").startswith("unexpected_entity:")
            and str(attempt_payload.get("answer_mode") or "").strip().lower() == "recommend"
        ):
            repaired_payload = dict(attempt_payload)
            valid_candidates = [
                str(item).strip()
                for item in (
                    repaired_payload.get("launchable_games")
                    or repaired_payload.get("allowed_entities")
                    or repaired_payload.get("candidate_entities")
                    or []
                )
                if str(item).strip()
            ]
            if valid_candidates:
                repaired_payload["primary_entity"] = valid_candidates[0]
                repaired_payload["required_terms"] = [valid_candidates[0]]
                repaired_payload["text"] = ""
                return _normalize_final_reply_text(_structured_template_reply(repaired_payload, user_text))
        logger.info("structured render validation fallback: %s", reason)
        return fallback_text
    return fallback_text


@app.on_event("startup")
async def _startup_event() -> None:
    # Trigger model loading during startup so the first ASR request does not pay the cost.
    if TRANSCRIBE_MODE_DEFAULT == TRANSCRIBE_MODE_WHISPER_LARGE_V3:
        _load_model()
    elif _is_moonshine_mode(TRANSCRIBE_MODE_DEFAULT):
        _load_moonshine_transcriber(_moonshine_profile_for_mode(TRANSCRIBE_MODE_DEFAULT))
    else:
        logger.info("ASR startup mode is '%s'; skipping local model preload.", TRANSCRIBE_MODE_DEFAULT)
    # Warm the shared HTTP client so the first request can reuse an existing connection.
    _AsyncHttpClient.get()
    if _conversation_pipeline_mode() == PIPELINE_MODE_DIRECT_UNIFIED:
        try:
            await _get_unified_conversation_runtime(force_reload=True)
        except Exception as exc:
            logger.warning("Unified conversation runtime warmup failed: %s", exc)


@app.on_event("shutdown")
async def _shutdown_event() -> None:
    global _UNIFIED_CONVERSATION_RUNTIME
    if _UNIFIED_CONVERSATION_RUNTIME is not None:
        try:
            _UNIFIED_CONVERSATION_RUNTIME.close()
        except Exception:
            pass
        _UNIFIED_CONVERSATION_RUNTIME = None
    await _AsyncHttpClient.aclose()
    await _AsyncOpenAIClient.aclose()
    _load_moonshine_transcriber.cache_clear()


@app.get("/healthz")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/transcribe/config", response_model=TranscribeConfigResponse)
async def get_transcribe_config() -> TranscribeConfigResponse:
    mode, source = await _get_effective_transcribe_mode()
    return TranscribeConfigResponse(
        status="ok",
        mode=mode,
        source=source,
        available_modes=TRANSCRIBE_AVAILABLE_MODES,
        openai_configured=_openai_configured(),
        openai_model=_openai_transcribe_model(),
    )


@app.post("/transcribe/config", response_model=TranscribeConfigResponse)
async def set_transcribe_config(payload: TranscribeConfigRequest) -> TranscribeConfigResponse:
    if payload.reset:
        await _set_runtime_transcribe_mode(None)
        mode, source = await _get_effective_transcribe_mode()
        return TranscribeConfigResponse(
            status="ok",
            mode=mode,
            source=source,
            available_modes=TRANSCRIBE_AVAILABLE_MODES,
            openai_configured=_openai_configured(),
            openai_model=_openai_transcribe_model(),
        )

    normalized = _normalize_transcribe_mode(payload.mode)
    if normalized is None:
        raise HTTPException(
            status_code=400,
            detail="mode must be one of: whisper-large-v3, moonshine-small, moonshine-medium, api",
        )
    if normalized == TRANSCRIBE_MODE_API and not _openai_configured():
        raise HTTPException(
            status_code=400,
            detail="OpenAI ASR requires installed openai SDK and OPENAI_API_KEY.",
        )
    if _is_moonshine_mode(normalized) and not _moonshine_configured():
        raise HTTPException(
            status_code=400,
            detail=_moonshine_install_hint(),
        )

    await _set_runtime_transcribe_mode(normalized)
    mode, source = await _get_effective_transcribe_mode()
    return TranscribeConfigResponse(
        status="ok",
        mode=mode,
        source=source,
        available_modes=TRANSCRIBE_AVAILABLE_MODES,
        openai_configured=_openai_configured(),
        openai_model=_openai_transcribe_model(),
    )


def _wake_word_prompt(*, include_command_hints: bool) -> str:
    # Include aliases to help Whisper bias toward the expected wake word variants.
    unique_terms = sorted({WAKE_WORD, *WAKE_WORD_ALIASES})
    prompt_terms: list[str] = []
    prompt_terms.extend(unique_terms)

    for prefix in WAKE_WORD_PREFIXES:
        if not prefix:
            continue
        for term in unique_terms:
            prompt_terms.append(f"{prefix} {term}")

    if include_command_hints:
        # Keep command hints for offline decoding only.
        prompt_terms.extend(WHISPER_OFFLINE_COMMAND_HINTS)

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


def _effective_request_language(request_language: Optional[str]) -> Optional[str]:
    if ASR_FORCE_LANGUAGE:
        return _normalize_language(ASR_FORCE_LANGUAGE)
    if request_language is None:
        return _normalize_language(ASR_DEFAULT_LANGUAGE)
    return _normalize_language(request_language)


def _effective_api_request_language(request_language: Optional[str]) -> Optional[str]:
    if ASR_API_FORCE_LANGUAGE:
        return _normalize_language(ASR_API_LANGUAGE) or "en"
    return _effective_request_language(request_language)


def _sanitize_english_text(text: str) -> str:
    cleaned = _ASCII_TEXT_FILTER.sub(" ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _maybe_enforce_english_only(
    *,
    text: str,
    words: List[dict],
    language: Optional[str],
) -> tuple[str, List[dict]]:
    language_key = (language or "").strip().lower()
    if not (ASR_ENGLISH_ONLY or language_key == "en"):
        return text, words

    cleaned_text = _sanitize_english_text(text)
    if not cleaned_text:
        return "", []

    if cleaned_text == (text or "").strip():
        return text, words

    return cleaned_text, []


def _normalize_prompt_tokens(text: str) -> List[str]:
    return [token for token in _REPETITION_TOKEN_PATTERN.findall((text or "").lower()) if token]


def _should_suppress_low_signal_prompt_leak(
    *,
    text: str,
    prompt: str,
    speech_fraction: float,
    max_amplitude: float,
    rms: float,
) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False

    tokens = _normalize_prompt_tokens(stripped)
    if len(tokens) < 2 or len(tokens) > 12:
        return False

    prompt_tokens = set(_normalize_prompt_tokens(prompt))
    if not prompt_tokens:
        return False

    covered = sum(1 for token in tokens if token in prompt_tokens)
    coverage_ratio = covered / max(1, len(tokens))
    if coverage_ratio < 0.8:
        return False

    return (
        speech_fraction <= WHISPER_HALLUCINATION_MAX_SPEECH_FRACTION
        and max_amplitude <= WHISPER_HALLUCINATION_MAX_AMPLITUDE
        and rms <= WHISPER_HALLUCINATION_MAX_RMS
    )


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

    Whisper can occasionally emit the same 1閳? token sequence multiple times in
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


def _build_legacy_word_result(words: Iterable[dict]) -> List[dict]:
    # Keep legacy "result" schema for compatibility with existing Unity parsers.
    return list(words)


def _normalize_transcribe_words(words: Iterable[dict]) -> List[dict]:
    normalized_words: List[dict] = []
    for item in words:
        if not isinstance(item, dict):
            continue
        original_word = str(item.get("word", "") or "").strip()
        if not original_word:
            continue
        canonical_word = _canonicalize_asr_text(original_word)
        if not canonical_word:
            continue
        normalized_item = dict(item)
        normalized_item["word"] = canonical_word
        normalized_words.append(normalized_item)
    return normalized_words


def _build_non_whisper_response(
    *,
    text: str,
    words: Iterable[dict],
    language: Optional[str],
    audio: np.ndarray,
    rms: float,
    max_amplitude: float,
    speech_fraction: float,
    provider: str,
    mode: str,
    start_time: float,
    duration: Optional[float] = None,
    language_probability: Optional[float] = None,
    speaker_meta: Optional[dict] = None,
) -> dict:
    normalized_text = _canonicalize_asr_text(text or "")
    normalized_words = _normalize_transcribe_words(words)
    if not normalized_text and normalized_words:
        normalized_text = _canonicalize_asr_text(
            " ".join(word["word"] for word in normalized_words if word.get("word")).strip()
        )

    normalized_text, normalized_words = _maybe_enforce_english_only(
        text=normalized_text,
        words=normalized_words,
        language=language,
    )

    response = {
        "text": normalized_text,
        "result": _build_legacy_word_result(normalized_words),
        "language": language,
        "duration": duration if duration is not None else _duration_seconds(audio, DEFAULT_SAMPLE_RATE),
        "language_probability": language_probability,
        "translation": False,
        "rms": rms,
        "max_amplitude": max_amplitude,
        "speech_fraction": speech_fraction,
        "provider": provider,
        "mode": mode,
        "processing_seconds": round(time.perf_counter() - start_time, 4),
    }
    if isinstance(speaker_meta, dict):
        if speaker_meta.get("speaker_index") is not None:
            response["speaker_index"] = int(speaker_meta["speaker_index"])
        if speaker_meta.get("speaker_id") is not None:
            response["speaker_id"] = int(speaker_meta["speaker_id"])
        speakers_payload = speaker_meta.get("speakers")
        if isinstance(speakers_payload, list) and speakers_payload:
            response["speakers"] = speakers_payload
    return response


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
        "initial_prompt": _wake_word_prompt(include_command_hints=True),
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


def _run_moonshine_transcription(
    transcriber: "MoonshineTranscriber",
    audio: np.ndarray,
    sample_rate: int,
) -> tuple[str, List[dict], dict]:
    # Moonshine's Python API accepts float samples in [-1, 1].
    transcript = transcriber.transcribe_without_streaming(audio.tolist(), sample_rate=sample_rate)

    words: List[dict] = []
    line_texts: List[str] = []
    speaker_durations: Dict[Tuple[int, int], float] = {}
    for line in getattr(transcript, "lines", []) or []:
        text_raw = str(getattr(line, "text", "") or "").strip()
        if not text_raw:
            continue

        text = _canonicalize_asr_text(text_raw)
        if text:
            line_texts.append(text)

        line_words = [token.strip() for token in text.split() if token.strip()]
        if not line_words:
            continue

        try:
            line_start = max(0.0, float(getattr(line, "start_time", 0.0) or 0.0))
        except (TypeError, ValueError):
            line_start = 0.0
        try:
            line_duration = max(0.0, float(getattr(line, "duration", 0.0) or 0.0))
        except (TypeError, ValueError):
            line_duration = 0.0
        has_speaker_id = bool(getattr(line, "has_speaker_id", False))
        speaker_index_value = 0
        speaker_id_value = 0
        if has_speaker_id:
            try:
                speaker_index_value = max(0, int(getattr(line, "speaker_index", 0) or 0))
            except (TypeError, ValueError):
                speaker_index_value = 0
            try:
                speaker_id_value = max(0, int(getattr(line, "speaker_id", 0) or 0))
            except (TypeError, ValueError):
                speaker_id_value = 0
            key = (speaker_index_value, speaker_id_value)
            speaker_durations[key] = speaker_durations.get(key, 0.0) + max(0.01, line_duration)

        if line_duration <= 0.0:
            for token in line_words:
                words.append(
                    {
                        "word": token,
                        "start": line_start,
                        "end": line_start,
                        "confidence": None,
                        "speaker_index": speaker_index_value if has_speaker_id else None,
                        "speaker_id": speaker_id_value if has_speaker_id else None,
                    }
                )
            continue

        step = line_duration / max(1, len(line_words))
        for index, token in enumerate(line_words):
            start = line_start + (step * index)
            end = line_start + (step * (index + 1))
            words.append(
                {
                    "word": token,
                    "start": max(0.0, start),
                    "end": max(0.0, end),
                    "confidence": None,
                    "speaker_index": speaker_index_value if has_speaker_id else None,
                    "speaker_id": speaker_id_value if has_speaker_id else None,
                }
            )

    text = _canonicalize_asr_text(" ".join(line_texts).strip())
    if not text and words:
        text = _canonicalize_asr_text(" ".join(word["word"] for word in words if word.get("word")).strip())

    speakers = [
        {
            "speaker_index": int(speaker_index),
            "speaker_id": int(speaker_id),
            "duration": round(float(duration), 4),
        }
        for (speaker_index, speaker_id), duration in sorted(
            speaker_durations.items(),
            key=lambda item: item[1],
            reverse=True,
        )
    ]
    primary = speakers[0] if speakers else None
    speaker_meta = {
        "speaker_index": primary.get("speaker_index") if primary else None,
        "speaker_id": primary.get("speaker_id") if primary else None,
        "speakers": speakers,
    }
    return text, words, speaker_meta


def _to_plain_dict(value: object) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    if hasattr(value, "dict"):
        try:
            dumped = value.dict()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    return {}


def _extract_openai_words(data: dict) -> List[dict]:
    words_out: List[dict] = []
    words_raw = data.get("words")
    if isinstance(words_raw, list):
        candidate_words = words_raw
    else:
        candidate_words = []
        segments = data.get("segments")
        if isinstance(segments, list):
            for seg in segments:
                if isinstance(seg, dict) and isinstance(seg.get("words"), list):
                    candidate_words.extend(seg.get("words"))

    for item in candidate_words:
        if not isinstance(item, dict):
            continue
        word_text = str(item.get("word", "")).strip()
        if not word_text:
            continue
        confidence_raw = item.get("probability", item.get("confidence"))
        confidence: Optional[float] = None
        try:
            if confidence_raw is not None:
                confidence = float(confidence_raw)
        except (TypeError, ValueError):
            confidence = None
        words_out.append(
            {
                "word": word_text,
                "start": max(0.0, float(item.get("start", 0.0) or 0.0)),
                "end": max(0.0, float(item.get("end", 0.0) or 0.0)),
                "confidence": round(confidence, 4) if confidence is not None else None,
            }
        )
    return words_out


async def _transcribe_with_openai(
    *,
    audio: np.ndarray,
    language: Optional[str],
) -> dict:
    client = _AsyncOpenAIClient.get()
    model = _openai_transcribe_model()
    wav_bytes = _audio_float_to_wav_bytes(audio, DEFAULT_SAMPLE_RATE)

    request_kwargs = {"model": model, "file": ("speech.wav", wav_bytes, "audio/wav")}
    if language:
        request_kwargs["language"] = language
    prompt = _openai_transcribe_prompt()
    if prompt:
        request_kwargs["prompt"] = prompt
    result = await client.audio.transcriptions.create(**request_kwargs)

    payload = _to_plain_dict(result)
    text = str(payload.get("text", "") or getattr(result, "text", "") or "").strip()
    words: List[dict] = _extract_openai_words(payload)

    language_out = payload.get("language")
    if not isinstance(language_out, str) or not language_out.strip():
        language_out = language
    if isinstance(language_out, str):
        language_out = language_out.strip()
    if ASR_API_FORCE_LANGUAGE:
        language_out = _normalize_language(ASR_API_LANGUAGE) or "en"

    return {
        "text": text,
        "words": words,
        "language": language_out,
        "duration": payload.get("duration"),
        "language_probability": payload.get("language_probability"),
    }


@app.post("/transcribe")
async def transcribe(
    request: Request,
    sample_rate: int = Query(DEFAULT_SAMPLE_RATE, ge=8000, le=48000),
    language: Optional[str] = Query(None, min_length=1, max_length=8),
    beam_size: int = Query(5, ge=1, le=10),
    vad: bool = Query(True, description="Whether to enable faster-whisper VAD filter (silence trimming)."),
    vad_fallback_retry: bool = Query(True, description="If VAD removes everything and text is empty, retry once with VAD disabled."),
    session_id: Optional[str] = Query(None, description="Client/session id for streaming context and overlap."),
    hotwords: Optional[str] = Query(None, description="Optional per-request hotwords (comma/space separated)."),
    mode: Optional[str] = Query(
        None,
        description="Optional per-request ASR mode: whisper-large-v3, moonshine-small, moonshine-medium, or api.",
    ),
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

    selected_mode = _normalize_transcribe_mode(mode)
    if mode is not None and selected_mode is None:
        raise HTTPException(
            status_code=400,
            detail="mode must be one of: whisper-large-v3, moonshine-small, moonshine-medium, api",
        )
    effective_mode = selected_mode
    if effective_mode is None:
        effective_mode, _ = await _get_effective_transcribe_mode()

    # Keep segmentation behavior consistent across ASR backends.
    audio = _extract_recent_speech_window(audio, DEFAULT_SAMPLE_RATE)

    rms, max_amplitude = _audio_energy_metrics(audio)
    speech_fraction = _energy_speech_fraction(audio, DEFAULT_SAMPLE_RATE)

    request_language = language if language is not None else ASR_DEFAULT_LANGUAGE
    if effective_mode == TRANSCRIBE_MODE_API:
        normalized_language = _effective_api_request_language(request_language)
    else:
        normalized_language = _effective_request_language(request_language)
    if effective_mode == TRANSCRIBE_MODE_API:
        try:
            openai_result = await _transcribe_with_openai(
                audio=audio,
                language=normalized_language,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("OpenAI transcription failed")
            raise HTTPException(status_code=502, detail=f"OpenAI transcription failed: {exc}") from exc

        response = _build_non_whisper_response(
            text=str(openai_result.get("text", "") or ""),
            words=list(openai_result.get("words", []) or []),
            language=normalized_language,
            audio=audio,
            rms=rms,
            max_amplitude=max_amplitude,
            speech_fraction=speech_fraction,
            provider="openai",
            mode=TRANSCRIBE_MODE_API,
            start_time=start_time,
            duration=_duration_seconds(audio, DEFAULT_SAMPLE_RATE),
            language_probability=None,
        )
        _update_transcribe_session_state(
            state=state,
            now=now,
            audio=audio,
            full_text=str(response.get("text", "") or ""),
        )
        return JSONResponse(response)

    if _is_moonshine_mode(effective_mode):
        if not _moonshine_configured():
            raise HTTPException(status_code=503, detail=_moonshine_install_hint())

        try:
            moonshine_transcriber, _, _ = _load_moonshine_transcriber(
                _moonshine_profile_for_mode(effective_mode)
            )
            full_text, words, speaker_meta = await asyncio.to_thread(
                _run_moonshine_transcription,
                moonshine_transcriber,
                audio,
                DEFAULT_SAMPLE_RATE,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Moonshine transcription failed")
            raise HTTPException(status_code=502, detail=f"Moonshine transcription failed: {exc}") from exc

        response = _build_non_whisper_response(
            text=full_text,
            words=words,
            language=normalized_language,
            audio=audio,
            rms=rms,
            max_amplitude=max_amplitude,
            speech_fraction=speech_fraction,
            provider="moonshine",
            mode=effective_mode,
            start_time=start_time,
            duration=_duration_seconds(audio, DEFAULT_SAMPLE_RATE),
            language_probability=None,
            speaker_meta=speaker_meta,
        )
        _update_transcribe_session_state(
            state=state,
            now=now,
            audio=audio,
            full_text=str(response.get("text", "") or ""),
        )
        return JSONResponse(response)

    include_command_hints = True
    session_prompt = _session_prompt_for_transcribe(
        state, now, include_command_hints=include_command_hints
    )

    model = _load_model()

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
            "initial_prompt": session_prompt,
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
            {"vad_filter": False, "initial_prompt": session_prompt},
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
                    "initial_prompt": session_prompt,
                },
            )
            # adopt retry result if it yields more meaningful text or better logprob
            texts_r = _collect_segment_texts(segments_r)
            full_text_r = _canonicalize_asr_text(" ".join(texts_r).strip())
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

    full_text = _canonicalize_asr_text(full_text)

    for word in words:
        original = word.get("word")
        if not isinstance(original, str):
            continue
        canonical = _canonicalize_asr_text(original)
        if canonical != original:
            word["word"] = canonical

    if words:
        token_strings = [word.get("word", "") for word in words]
        keep_indices = _limit_repeated_sequence_indices(token_strings)

        if keep_indices and len(keep_indices) < len(words):
            words = [words[index] for index in keep_indices]
            deduped_tokens = [token_strings[index] for index in keep_indices if token_strings[index]]
            if deduped_tokens:
                full_text = _canonicalize_asr_text(" ".join(deduped_tokens).strip()) or full_text

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

    full_text, words = _maybe_enforce_english_only(
        text=full_text,
        words=words,
        language=normalized_language,
    )

    response = {
        "text": full_text,
        "result": _build_legacy_word_result(words),
        "language": getattr(info, "language", normalized_language),
        "duration": getattr(info, "duration", None),
        "language_probability": getattr(info, "language_probability", None),
        "translation": False,
        "rms": rms,
        "max_amplitude": max_amplitude,
        "speech_fraction": speech_fraction,
        "provider": "faster-whisper",
        "mode": TRANSCRIBE_MODE_WHISPER_LARGE_V3,
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
    _update_transcribe_session_state(
        state=state,
        now=now,
        audio=audio,
        full_text=full_text,
    )

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
        reply = await _generate_coach_reply(
            user_text,
            system_override=(payload.system or None),
            memory_context=(payload.memory_context or None),
            user_id=(payload.user_id or None),
            dialog_context=(payload.dialog_context or None),
            barge_in=bool(payload.barge_in),
            interrupted_tts_text=(payload.interrupted_tts_text or None),
        )
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







@app.get("/conversation/config", response_model=ConversationConfigResponse)
async def get_conversation_config() -> ConversationConfigResponse:
    snapshot = _conversation_config_snapshot()
    return ConversationConfigResponse(status="ok", **snapshot)


@app.post("/conversation/config", response_model=ConversationConfigResponse)
async def set_conversation_config(payload: ConversationConfigRequest) -> ConversationConfigResponse:
    if payload.reset:
        _restore_conversation_env_defaults()
        await _AsyncOpenAIClient.aclose()
        await _get_unified_conversation_runtime(force_reload=True)
        snapshot = _conversation_config_snapshot()
        return ConversationConfigResponse(status="ok", **snapshot)

    if payload.pipeline_mode is not None:
        os.environ["VOICE_PIPELINE_MODE"] = _normalize_pipeline_mode(payload.pipeline_mode)
    if payload.profile is not None:
        os.environ["VOICE_CONVERSATION_PROFILE"] = _normalize_conversation_profile(payload.profile)
    if payload.local_asr_mode is not None:
        normalized = _normalize_transcribe_mode(payload.local_asr_mode)
        if normalized is None:
            raise HTTPException(status_code=400, detail="invalid local_asr_mode")
        os.environ["VOICE_LOCAL_ASR_MODE"] = normalized
    if payload.cloud_asr_mode is not None:
        normalized = _normalize_transcribe_mode(payload.cloud_asr_mode)
        if normalized is None:
            raise HTTPException(status_code=400, detail="invalid cloud_asr_mode")
        os.environ["VOICE_CLOUD_ASR_MODE"] = normalized
    if payload.cloud_response_provider is not None:
        os.environ["VOICE_CLOUD_RESPONSE_PROVIDER"] = _normalize_cloud_response_provider(payload.cloud_response_provider)
    if payload.openai_api_key is not None:
        candidate = (payload.openai_api_key or "").strip()
        if candidate:
            os.environ["OPENAI_API_KEY"] = candidate
        else:
            os.environ.pop("OPENAI_API_KEY", None)
    if payload.openai_base_url is not None:
        candidate = (payload.openai_base_url or "").strip()
        if candidate:
            os.environ["OPENAI_BASE_URL"] = candidate
        else:
            os.environ.pop("OPENAI_BASE_URL", None)
    if payload.openai_transcribe_model is not None:
        candidate = (payload.openai_transcribe_model or "").strip()
        if candidate:
            os.environ["OPENAI_TRANSCRIBE_MODEL"] = candidate
        else:
            os.environ.pop("OPENAI_TRANSCRIBE_MODEL", None)
    if payload.openai_transcribe_prompt is not None:
        candidate = payload.openai_transcribe_prompt or ""
        if candidate.strip():
            os.environ["OPENAI_TRANSCRIBE_PROMPT"] = candidate
        else:
            os.environ.pop("OPENAI_TRANSCRIBE_PROMPT", None)
    if payload.openai_response_model is not None:
        candidate = (payload.openai_response_model or "").strip()
        if candidate:
            os.environ["OPENAI_RESPONSE_MODEL"] = candidate
        else:
            os.environ.pop("OPENAI_RESPONSE_MODEL", None)
    if payload.local_response_model is not None:
        candidate = (payload.local_response_model or "").strip()
        if candidate:
            os.environ["OLLAMA_MODEL"] = candidate
        else:
            os.environ.pop("OLLAMA_MODEL", None)

    await _AsyncOpenAIClient.aclose()
    await _get_unified_conversation_runtime(force_reload=True)
    snapshot = _conversation_config_snapshot()
    return ConversationConfigResponse(status="ok", **snapshot)


def _command_ack_text(route_type: str, game_name: str) -> str:
    if route_type == "LAUNCH_GAME":
        name = (game_name or "game").strip() or "game"
        return f"Opening {name}."
    if route_type == "BACK_HOME":
        return "Going back home."
    return ""


def _mqtt_intent_host() -> str:
    return _environment("MQTT_HOST", "127.0.0.1").strip() or "127.0.0.1"


def _mqtt_intent_port() -> int:
    raw = _environment("MQTT_PORT", "1883").strip() or "1883"
    try:
        return max(1, int(raw))
    except Exception:
        return 1883


def _mqtt_intent_topic() -> str:
    return _environment("INTENT_TOPIC", "robot/intent").strip() or "robot/intent"


def _publish_command_intent_sync(route_type: str, *, game_name: str, corr_id: str, user_text: str) -> None:
    if mqtt_publish is None:
        raise RuntimeError("paho-mqtt publish support is unavailable")

    payload: Dict[str, Any] = {
        "type": route_type,
        "source": "conversation_service",
        "corr_id": corr_id,
        "ts": int(time.time() * 1000),
    }
    if user_text:
        payload["text"] = user_text
    if route_type == "LAUNCH_GAME":
        normalized_game_name = (game_name or "").strip()
        if not normalized_game_name:
            raise RuntimeError("launch route missing game_name")
        payload["game_name"] = normalized_game_name

    mqtt_publish.single(
        _mqtt_intent_topic(),
        json.dumps(payload, ensure_ascii=False),
        hostname=_mqtt_intent_host(),
        port=_mqtt_intent_port(),
    )


async def _dispatch_command_intent(route_type: str, *, game_name: str, corr_id: str, user_text: str) -> None:
    await asyncio.to_thread(
        _publish_command_intent_sync,
        route_type,
        game_name=game_name,
        corr_id=corr_id,
        user_text=user_text,
    )


def _normalize_final_reply_text(text: str) -> str:
    value = (text or "").strip()
    if not value:
        return ""
    value = re.sub(r"\s+", " ", value).strip()
    if sanitize_tts_text is not None:
        cleaned = sanitize_tts_text(value)
        if cleaned:
            value = cleaned.strip()
    return value


def _looks_like_system_prompt_leak(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    if not normalized:
        return False
    hits = sum(1 for marker in _SYSTEM_PROMPT_LEAK_MARKERS if marker in normalized)
    return hits >= 2


def _normalize_compare_text(value: str) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff\s]+", " ", text)
    return " ".join(text.split())


def _text_similarity(left: str, right: str) -> float:
    lhs = _normalize_compare_text(left)
    rhs = _normalize_compare_text(right)
    if not lhs or not rhs:
        return 0.0
    if lhs == rhs:
        return 1.0
    return difflib.SequenceMatcher(None, lhs, rhs).ratio()


def _token_overlap_ratio(left: str, right: str) -> float:
    lhs_tokens = _normalize_compare_text(left).split()
    rhs_tokens = _normalize_compare_text(right).split()
    if not lhs_tokens or not rhs_tokens:
        return 0.0
    lhs_set = set(lhs_tokens)
    rhs_set = set(rhs_tokens)
    overlap = len(lhs_set & rhs_set)
    return overlap / float(max(1, min(len(lhs_set), len(rhs_set))))


def _looks_like_question_text_for_guard(text: str) -> bool:
    raw = " ".join((text or "").strip().lower().split())
    if not raw:
        return False
    if "?" in text:
        return True
    return bool(re.match(r"^(who|what|when|where|why|how|do|does|did|can|could|would|will|should|is|are|am|was|were)\b", raw))


def _looks_like_question_echo(user_text: str, answer_text: str) -> bool:
    user_norm = _normalize_compare_text(user_text)
    answer_norm = _normalize_compare_text(answer_text)
    if not user_norm or not answer_norm:
        return False
    if not _looks_like_question_text_for_guard(user_text):
        return False
    if len(user_norm) < 12:
        return False
    if user_norm in answer_norm:
        return True
    similarity = _text_similarity(user_text, answer_text)
    overlap = _token_overlap_ratio(user_text, answer_text)
    return similarity >= 0.78 or overlap >= 0.85


def _try_general_query_reply_text(text: str) -> str:
    normalized = " ".join((text or "").strip().lower().split())
    if not normalized:
        return ""

    if normalized in {"can you hear me", "can you hear me?"}:
        return "Yes, I can hear you."

    if "median" in normalized and any(token in normalized for token in ("numbers", "number set", "set of numbers", "dataset", "data set")):
        return "Sort the numbers and take the middle value. If there are two middle values, average them."

    if "android" in normalized and "iphone" in normalized and "better than" in normalized:
        return "It depends on what you want: iPhone is usually simpler and more tightly integrated, while Android gives you more customization."

    if "weather" in normalized and any(token in normalized for token in ("today", "right now", "currently", "outside", "forecast")):
        return "I can't check live weather from here."

    if "recent activity" in normalized and any(token in normalized for token in ("backyard", "yard", "garden", "driveway", "front yard", "back yard")):
        return "I can't check live activity in your backyard from here."

    live_topic_match = re.search(
        r"\bwhat(?:'s| is)\s+happening\s+with\s+(.+?)(?:\s+right\s+now\w*|\s+currently|\s+today|\?|$)",
        normalized,
    )
    if live_topic_match:
        topic = (live_topic_match.group(1) or "").strip(" .,!?:;\"'")
        if topic:
            return f"I can't give live updates on {topic} from here, but I can give a brief general summary."
        return "I can't give live updates from here, but I can give a brief general summary."

    return ""


async def _stream_unified_conversation_events(
    runtime: _UnifiedConversationRuntime,
    payload: ConversationTurnRequest,
) -> AsyncIterator[bytes]:
    corr_id = (payload.corr_id or "").strip() or f"turn-{int(time.time() * 1000)}-{os.getpid()}"
    text = payload.text.strip()
    request_payload = payload.dict(exclude_none=True)
    identity_resolution = _normalize_identity_resolution(payload.identity_resolution)
    request_payload["identity_resolution"] = identity_resolution
    resolved_user_id = runtime.resolve_user_id(
        payload=request_payload,
        user_id=payload.user_id,
        identity_resolution=identity_resolution,
    )
    route = runtime.route_text(
        text,
        corr_id,
        payload=request_payload,
        user_id=resolved_user_id,
        identity_resolution=identity_resolution,
    )
    route_payload = route.payload or {}
    route_type = str(route_payload.get("type") or "QUERY").strip().upper() or "QUERY"
    route_game_name = str(route_payload.get("game_name") or "").strip()
    provider = _conversation_effective_response_provider(_conversation_profile())

    yield _json_line(
        {
            "type": "route",
            "corr_id": corr_id,
            "route": route_type,
            "game_name": route_game_name,
            "provider": provider,
        }
    )

    if _should_clarify_uncertain_turn(payload, route_type):
        clarification = _uncertain_turn_reply(payload)
        yield _json_line(
            {
                "type": "chunk",
                "corr_id": corr_id,
                "route": route_type,
                "text": clarification,
                "provider": "asr_guard",
            }
        )
        yield _json_line(
            {
                "type": "final",
                "corr_id": corr_id,
                "route": route_type,
                "text": clarification,
                "provider": "asr_guard",
                "user_id": resolved_user_id or "",
            }
        )
        return

    user_id, memory_context, dialog_request_ctx, memory_update = runtime.build_turn_context(
        payload=request_payload,
        text=text,
        user_id=payload.user_id,
        resolved_user_id=resolved_user_id,
        identity_resolution=identity_resolution,
    )
    runtime.remember_user_turn(user_id=user_id, text=text)

    if route_type in {"LAUNCH_GAME", "BACK_HOME"}:
        try:
            await _dispatch_command_intent(
                route_type,
                game_name=route_game_name,
                corr_id=corr_id,
                user_text=text,
            )
        except RuntimeError as exc:
            yield _json_line(
                {
                    "type": "error",
                    "corr_id": corr_id,
                    "route": route_type,
                    "provider": "command",
                    "message": str(exc),
                }
            )
            return
        if route_type == "LAUNCH_GAME" and route_game_name:
            runtime.record_game_event(
                user_id=user_id,
                game_name=route_game_name,
                action="launch",
                source="conversation",
            )
        reply_text = _command_ack_text(route_type, route_game_name)
        if reply_text:
            runtime.session_store.remember_turn(user_id=user_id, role="assistant", text=reply_text)
        if reply_text:
            yield _json_line(
                {
                    "type": "chunk",
                    "corr_id": corr_id,
                    "route": route_type,
                    "text": reply_text,
                    "provider": "command",
                }
            )
        yield _json_line(
            {
                "type": "final",
                "corr_id": corr_id,
                "route": route_type,
                "text": reply_text,
                "game_name": route_game_name,
                "provider": "command",
            }
        )
        return

    memory_write_reply = await runtime.try_memory_write_reply(
        user_id=user_id,
        text=text,
        memory_update=memory_update,
        dialog_request_ctx=dialog_request_ctx,
    )
    if memory_write_reply:
        yield _json_line({"type": "chunk", "corr_id": corr_id, "route": route_type, "text": memory_write_reply, "provider": "memory_write"})
        yield _json_line({"type": "final", "corr_id": corr_id, "route": route_type, "text": memory_write_reply, "provider": "memory_write", "user_id": user_id or ""})
        return

    capability_decision = await runtime.route_query_capability(
        user_id=user_id,
        text=text,
    )
    routed_query_text = capability_decision.routed_text or text
    decision_final_meta: Dict[str, Any] = {}
    if capability_decision.probe_telemetry:
        decision_final_meta["doc_probe"] = dict(capability_decision.probe_telemetry)
    if capability_decision.fallback_reason:
        decision_final_meta["fallback_reason"] = capability_decision.fallback_reason
    if capability_decision.label == "doc_query" and capability_decision.structured_payload:
        structured_payload = dict(capability_decision.structured_payload)
        decision_final_meta["structured_type"] = str(structured_payload.get("type") or "").strip()
        decision_final_meta["domain"] = str(structured_payload.get("domain") or "").strip()
        decision_final_meta["answer_mode"] = str(structured_payload.get("answer_mode") or "").strip()
        general_focus = str(structured_payload.get("general_focus") or "").strip()
        if general_focus:
            decision_final_meta["general_focus"] = general_focus
        clarify_kind = str(structured_payload.get("clarify_kind") or capability_decision.clarification_kind or "").strip()
        if clarify_kind:
            decision_final_meta["clarify_kind"] = clarify_kind

    if capability_decision.label == "clarify":
        clarification_text = capability_decision.clarification_text or "Could you clarify what kind of help you want?"
        runtime.session_store.save_clarification(
            user_id=user_id,
            kind=capability_decision.clarification_kind or "clarify",
            source_user_text=text,
            assistant_clarify_text=clarification_text,
        )
        runtime.session_store.record_general_turn(user_id=user_id, capability="clarify")
        runtime.finalize_assistant_turn(
            user_id=user_id,
            user_text=text,
            answer_text=clarification_text,
            dialog_request_ctx=dialog_request_ctx,
        )
        yield _json_line({"type": "chunk", "corr_id": corr_id, "route": route_type, "text": clarification_text, "provider": "capability_router"})
        yield _json_line(
            {
                "type": "final",
                "corr_id": corr_id,
                "route": route_type,
                "text": clarification_text,
                "provider": "capability_router",
                "user_id": user_id or "",
                **decision_final_meta,
            }
        )
        return

    if capability_decision.label == "memory_query":
        memory_reply = await runtime.try_memory_reply(
            user_id=user_id,
            text=routed_query_text,
            dialog_request_ctx=dialog_request_ctx,
        )
        if memory_reply:
            yield _json_line({"type": "chunk", "corr_id": corr_id, "route": route_type, "text": memory_reply, "provider": "memory"})
            yield _json_line(
                {
                    "type": "final",
                    "corr_id": corr_id,
                    "route": route_type,
                    "text": memory_reply,
                    "provider": "memory",
                    "user_id": user_id or "",
                    **decision_final_meta,
                }
            )
            return

    if capability_decision.label == "doc_query":
        payload_data = dict(capability_decision.structured_payload or {})
        payload_type = str(payload_data.get("type") or "").strip()
        domain = str(payload_data.get("domain") or "").strip().lower()
        answer_mode = str(payload_data.get("answer_mode") or "").strip().lower()
        general_focus = str(payload_data.get("general_focus") or "").strip().lower()
        clarify_kind = str(payload_data.get("clarify_kind") or capability_decision.clarification_kind or "").strip()
        candidate_entities = [
            str(item).strip()
            for item in payload_data.get("candidate_entities", []) or []
            if str(item).strip()
        ]
        primary_entity = str(payload_data.get("primary_entity") or "").strip()
        related_entities = payload_data.get("related_entities", {}) or {}
        doc_reply = ""
        if payload_data:
            doc_reply = await runtime._render_structured_reply(user_text=routed_query_text, payload=payload_data)
        if not doc_reply:
            doc_reply = str(payload_data.get("text") or capability_decision.clarification_text or "").strip()
        if not doc_reply:
            doc_reply = "I could not find stable local document evidence for that."
        if payload_data.get("summary_used") is not None:
            decision_final_meta["summary_used"] = bool(payload_data.get("summary_used"))
        if str(payload_data.get("summary_model") or "").strip():
            decision_final_meta["summary_model"] = str(payload_data.get("summary_model") or "").strip()
        if str(payload_data.get("summary_fallback_reason") or "").strip():
            decision_final_meta["summary_fallback_reason"] = str(payload_data.get("summary_fallback_reason") or "").strip()
        if general_focus:
            decision_final_meta["focus_domain"] = domain
            decision_final_meta["focus_general_focus"] = general_focus
        if payload_data.get("general_doc_kinds"):
            decision_final_meta["general_doc_kinds"] = list(payload_data.get("general_doc_kinds") or [])

        if payload_type == "doc_clarify":
            runtime.session_store.save_clarification(
                user_id=user_id,
                kind=clarify_kind or "doc_clarify",
                source_user_text=text,
                assistant_clarify_text=doc_reply,
                target_domain=domain,
                target_answer_mode=answer_mode,
                target_general_focus=general_focus,
                target_entities=candidate_entities or ([primary_entity] if primary_entity else None),
                related_entities=related_entities,
                resume_strategy=str(payload_data.get("resume_strategy") or "").strip(),
            )
            runtime.session_store.record_structured_capability(
                user_id=user_id,
                active_capability="doc_query",
                focused_entity="",
                candidate_entities=None,
                tentative_entity_hints=candidate_entities or None,
                last_structured_intent=answer_mode,
                focus_domain=domain,
                focus_general_focus=general_focus,
                related_entities=related_entities,
                focus_source="",
                tentative_source="clarify_hint",
            )
            if str(payload_data.get("resume_strategy") or "").strip():
                decision_final_meta["clarification_resume_strategy"] = str(payload_data.get("resume_strategy") or "").strip()
            runtime.finalize_assistant_turn(
                user_id=user_id,
                user_text=text,
                answer_text=doc_reply,
                dialog_request_ctx=dialog_request_ctx,
            )
            yield _json_line({"type": "chunk", "corr_id": corr_id, "route": route_type, "text": doc_reply, "provider": "doc_rag"})
            yield _json_line(
                {
                    "type": "final",
                    "corr_id": corr_id,
                    "route": route_type,
                    "text": doc_reply,
                    "provider": "doc_rag",
                    "user_id": user_id or "",
                    **decision_final_meta,
                }
            )
            return

        if payload_type == "doc_answer":
            if domain == "game":
                launchable_games = [
                    str(item).strip()
                    for item in payload_data.get("launchable_games", []) or []
                    if str(item).strip()
                ]
                recommendation_reason = str(payload_data.get("recommendation_reason") or "").strip()
                runtime.session_store.update_game_state(
                    user_id=user_id,
                    focused_game=primary_entity or None,
                    candidate_games=candidate_entities or None,
                    primary_recommendation=(primary_entity if answer_mode == "recommend" else None),
                    last_introduced_games=(candidate_entities[:4] if answer_mode in {"introduce", "compare", "availability"} and candidate_entities else None),
                    last_router_intent=answer_mode or "doc_answer",
                    focus_source="answer",
                )
                runtime.session_store.record_structured_capability(
                    user_id=user_id,
                    active_capability="doc_query",
                    focused_entity=primary_entity,
                    candidate_entities=candidate_entities or None,
                    last_structured_intent=answer_mode,
                    focus_domain=domain,
                    related_entities=related_entities,
                    focus_source="answer",
                )
                helper = runtime.dialog_helper
                if user_id and helper is not None and helper.user_memory is not None and primary_entity:
                    try:
                        helper._remember_game_context(
                            user_id=user_id,
                            text=doc_reply,
                            primary_game_name=primary_entity,
                            reference_kind=answer_mode or "doc_answer",
                            source="doc_rag",
                        )
                    except Exception as exc:
                        logger.warning("doc_rag game reference update failed: %s", exc)
                payload_data["launchable_games"] = launchable_games
                payload_data["recommendation_reason"] = recommendation_reason
            else:
                runtime.session_store.record_structured_capability(
                    user_id=user_id,
                    active_capability="doc_query",
                    focused_entity=primary_entity,
                    candidate_entities=candidate_entities or None,
                    last_structured_intent=answer_mode,
                    focus_domain=domain,
                    focus_general_focus=general_focus,
                    related_entities=related_entities,
                    focus_source="answer",
                )
            runtime.finalize_assistant_turn(
                user_id=user_id,
                user_text=text,
                answer_text=doc_reply,
                dialog_request_ctx=dialog_request_ctx,
            )
            yield _json_line({"type": "chunk", "corr_id": corr_id, "route": route_type, "text": doc_reply, "provider": "doc_rag"})
            yield _json_line(
                {
                    "type": "final",
                    "corr_id": corr_id,
                    "route": route_type,
                    "text": doc_reply,
                    "provider": "doc_rag",
                    "user_id": user_id or "",
                    **decision_final_meta,
                }
            )
            return

        runtime.session_store.record_structured_capability(
            user_id=user_id,
            active_capability="doc_query",
            last_structured_intent="no_evidence",
        )
        runtime.finalize_assistant_turn(
            user_id=user_id,
            user_text=text,
            answer_text=doc_reply,
            dialog_request_ctx=dialog_request_ctx,
        )
        yield _json_line({"type": "chunk", "corr_id": corr_id, "route": route_type, "text": doc_reply, "provider": "doc_rag"})
        yield _json_line(
            {
                "type": "final",
                "corr_id": corr_id,
                "route": route_type,
                "text": doc_reply,
                "provider": "doc_rag",
                "user_id": user_id or "",
                **decision_final_meta,
            }
        )
        return

    if capability_decision.label == "vision_query":
        vision_reply = runtime.try_vision_reply(
            user_id=user_id,
            text=routed_query_text,
            dialog_request_ctx=dialog_request_ctx,
        )
        if vision_reply:
            yield _json_line({"type": "chunk", "corr_id": corr_id, "route": route_type, "text": vision_reply, "provider": "vision"})
            yield _json_line(
                {
                    "type": "final",
                    "corr_id": corr_id,
                    "route": route_type,
                    "text": vision_reply,
                    "provider": "vision",
                    "user_id": user_id or "",
                    **decision_final_meta,
                }
            )
            return

    general_reply = runtime.try_general_query_reply(
        user_id=user_id,
        text=routed_query_text,
        dialog_request_ctx=dialog_request_ctx,
    )
    if general_reply:
        yield _json_line({"type": "chunk", "corr_id": corr_id, "route": route_type, "text": general_reply, "provider": "general_guard"})
        yield _json_line(
            {
                "type": "final",
                "corr_id": corr_id,
                "route": route_type,
                "text": general_reply,
                "provider": "general_guard",
                "user_id": user_id or "",
                **decision_final_meta,
            }
        )
        return

    stream_provider = _conversation_effective_response_provider(_conversation_profile())
    accumulator = _ReplyChunkAccumulator()
    full_parts: List[str] = []
    general_session_context = runtime.build_general_session_context(
        user_id=user_id,
        dialog_request_ctx=dialog_request_ctx,
        current_user_text=text,
    )
    try:
        if stream_provider == "openai":
            stream_iter = _stream_openai_reply(
                routed_query_text,
                memory_context=memory_context or None,
                user_id=user_id,
                dialog_context=general_session_context or None,
                barge_in=bool(payload.barge_in),
                interrupted_tts_text=payload.interrupted_tts_text,
                model_override=_openai_response_model(),
            )
        else:
            stream_iter = _stream_ollama_reply(
                routed_query_text,
                memory_context=memory_context or None,
                user_id=user_id,
                dialog_context=general_session_context or None,
                barge_in=bool(payload.barge_in),
                interrupted_tts_text=payload.interrupted_tts_text,
                model_override=_conversation_local_response_model(),
            )

        async for delta in stream_iter:
            if not delta:
                continue
            full_parts.append(delta)
            for chunk in accumulator.push(delta):
                yield _json_line(
                    {
                        "type": "chunk",
                        "corr_id": corr_id,
                        "route": route_type,
                        "text": chunk,
                        "provider": stream_provider,
                    }
                )
    except (OllamaError, OpenAIResponseError, RuntimeError) as exc:
        yield _json_line(
            {
                "type": "error",
                "corr_id": corr_id,
                "route": route_type,
                "provider": stream_provider,
                "message": str(exc),
            }
        )
        return

    final_text = _normalize_final_reply_text("".join(full_parts))
    if _looks_like_system_prompt_leak(final_text):
        fallback_text = _normalize_final_reply_text(
            _try_general_query_reply_text(routed_query_text) or "I want to answer that more clearly. Please ask it again in one short sentence."
        )
        if fallback_text:
            final_text = fallback_text
    dialog_helper = getattr(runtime, "dialog_helper", None)
    if dialog_helper is not None:
        final_text = _finalize_static_tts_reply(dialog_helper, final_text)
    if _looks_like_question_echo(text, final_text):
        fallback_text = _normalize_final_reply_text(
            _try_general_query_reply_text(routed_query_text) or "I don't want to just repeat your question. Please ask it again and I'll answer it more directly."
        )
        if dialog_helper is not None:
            fallback_text = _finalize_static_tts_reply(dialog_helper, fallback_text)
        if fallback_text:
            final_text = fallback_text
    if not final_text:
        yield _json_line(
            {
                "type": "error",
                "corr_id": corr_id,
                "route": route_type,
                "provider": stream_provider,
                "message": "empty reply from responder",
            }
        )
        return

    for chunk in accumulator.finish():
        yield _json_line(
            {
                "type": "chunk",
                "corr_id": corr_id,
                "route": route_type,
                "text": chunk,
                "provider": stream_provider,
            }
        )

    runtime.session_store.record_general_turn(user_id=user_id, capability="general_chat")
    runtime.finalize_assistant_turn(
        user_id=user_id,
        user_text=text,
        answer_text=final_text,
        dialog_request_ctx=dialog_request_ctx,
    )
    yield _json_line(
        {
            "type": "final",
            "corr_id": corr_id,
            "route": route_type,
            "text": final_text,
            "provider": stream_provider,
            "user_id": user_id or "",
            **decision_final_meta,
        }
    )


@app.post("/conversation/turn/stream")
async def conversation_turn_stream(payload: ConversationTurnRequest):
    runtime = await _get_unified_conversation_runtime()
    try:
        runtime.ensure_ready()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return StreamingResponse(
        _stream_unified_conversation_events(runtime, payload),
        media_type="application/x-ndjson",
    )


def create_app() -> FastAPI:
    return app
