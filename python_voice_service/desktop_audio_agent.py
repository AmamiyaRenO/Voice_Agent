from __future__ import annotations

import asyncio
import difflib
import importlib
import io
import json
import logging
import os
import random
import re
import subprocess
import sys
import threading
import time
import uuid
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import httpx
import numpy as np
import paho.mqtt.client as mqtt

try:
    genai = importlib.import_module("google.genai")
    genai_types = importlib.import_module("google.genai.types")
except Exception:  # pragma: no cover - optional at runtime
    genai = None
    genai_types = None

try:
    import sounddevice as sd
except Exception:  # pragma: no cover - optional at runtime
    sd = None

try:
    from scipy.signal import resample_poly
except Exception:  # pragma: no cover - optional at runtime
    resample_poly = None

try:
    import ctypes
except Exception:  # pragma: no cover
    ctypes = None

try:
    from .command_grammar import CommandGrammarMatcher
except Exception:
    from command_grammar import CommandGrammarMatcher

try:
    from .streaming_asr import (
        AsrEvent,
        HotwordEntry,
        HotwordPack,
        create_streaming_asr_backend,
        moonshine_streaming_available,
        normalize_streaming_asr_mode,
        STREAMING_ASR_MODE_API,
        STREAMING_ASR_MODE_GEMINI_LIVE,
        STREAMING_ASR_MODE_LIVE_CAPTIONS,
        supported_streaming_asr_modes,
    )
except Exception:
    from streaming_asr import (
        AsrEvent,
        HotwordEntry,
        HotwordPack,
        create_streaming_asr_backend,
        moonshine_streaming_available,
        normalize_streaming_asr_mode,
        STREAMING_ASR_MODE_API,
        STREAMING_ASR_MODE_GEMINI_LIVE,
        STREAMING_ASR_MODE_LIVE_CAPTIONS,
        supported_streaming_asr_modes,
    )

try:
    from .speaker_id import SpeakerIdService, SpeakerMatchResult
except Exception:
    from speaker_id import SpeakerIdService, SpeakerMatchResult

logger = logging.getLogger("desktop_audio_agent")


def _resolve_app_root() -> Path:
    if bool(getattr(sys, "frozen", False)):
        exe_dir = Path(sys.executable).resolve().parent
        if exe_dir.name.lower() == "services" and exe_dir.parent.name.lower() == "runtime":
            return exe_dir.parent.parent
        return exe_dir
    return Path(__file__).resolve().parents[1]


def _resolve_bundle_root(app_root: Path) -> Path:
    raw = getattr(sys, "_MEIPASS", "")
    if raw:
        try:
            return Path(str(raw)).resolve()
        except Exception:
            pass
    return app_root


def _resolve_state_dir(app_root: Path) -> Path:
    env_state = str(os.getenv("VOICE_AGENT_STATE_DIR") or "").strip()
    if env_state:
        try:
            return Path(os.path.expandvars(env_state)).expanduser().resolve()
        except Exception:
            return Path(os.path.expandvars(env_state)).expanduser()
    if bool(getattr(sys, "frozen", False)):
        local_app_data = str(os.getenv("LOCALAPPDATA") or "").strip()
        if local_app_data:
            return Path(local_app_data).expanduser() / "VoiceAgent"
        return Path.home() / "AppData" / "Local" / "VoiceAgent"
    return app_root / "runtime"


APP_ROOT = _resolve_app_root()
BUNDLE_ROOT = _resolve_bundle_root(APP_ROOT)
STATE_DIR = _resolve_state_dir(APP_ROOT)

for _dialog_dir in (
    APP_ROOT / "scripts" / "dialog_service",
    BUNDLE_ROOT / "scripts" / "dialog_service",
    Path(__file__).resolve().parents[1] / "scripts" / "dialog_service",
):
    if str(_dialog_dir) not in sys.path and _dialog_dir.exists():
        sys.path.insert(0, str(_dialog_dir))

try:
    from text_utils import sanitize_tts_text
except Exception:  # pragma: no cover - fallback for isolated tests
    def sanitize_tts_text(text: str) -> str:
        return str(text or "").strip()


def _resolve_default_live_captions_exe() -> str:
    env_override = str(os.getenv("LIVE_CAPTIONS_LISTENER_EXE") or "").strip()
    if env_override:
        return env_override

    repo_root = APP_ROOT
    sibling_root = repo_root.parent
    candidates = [
        repo_root / "runtime" / "live_captions" / "EnableLcMic.exe",
        BUNDLE_ROOT / "runtime" / "live_captions" / "EnableLcMic.exe",
        sibling_root / "LiveCaptionsListener" / "publish" / "win-x64-single" / "EnableLcMic.exe",
        sibling_root / "LiveCaptionsListener" / "temp_build" / "win-x64-single" / "EnableLcMic.exe",
        sibling_root
        / "LiveCaptionsListener"
        / "bin"
        / "Release"
        / "net8.0-windows10.0.19041.0"
        / "win-x64"
        / "EnableLcMic.exe",
        sibling_root
        / "LiveCaptionsListener"
        / "bin"
        / "Debug"
        / "net8.0-windows10.0.19041.0"
        / "EnableLcMic.exe",
        Path(r"C:\unityproject\LiveCaptionsListener\publish\win-x64-single\EnableLcMic.exe"),
        Path(r"C:\unityproject\LiveCaptionsListener\temp_build\win-x64-single\EnableLcMic.exe"),
        Path(r"D:\unityproject\LiveCaptionsListener\temp_build\win-x64-single\EnableLcMic.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate.resolve())
    return str(candidates[-1])

DEFAULT_ASR_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_PIPER_BASE_URL = "http://127.0.0.1:5005"
DEFAULT_QWEN_BASE_URL = "http://127.0.0.1:5006"
DEFAULT_KOKORO_BASE_URL = "http://127.0.0.1:5007"

DEFAULT_CAPTURE_SAMPLE_RATE = 16000
DEFAULT_OUTPUT_SAMPLE_RATE = int(os.getenv("AUDIO_AGENT_OUTPUT_SAMPLE_RATE", "22050") or "22050")
DEFAULT_INPUT_BLOCKSIZE = int(os.getenv("AUDIO_AGENT_INPUT_BLOCKSIZE", "160") or "160")
DEFAULT_OUTPUT_BLOCKSIZE = int(os.getenv("AUDIO_AGENT_OUTPUT_BLOCKSIZE", "512") or "512")
DEFAULT_INPUT_QUEUE_MAX_FRAMES = int(os.getenv("AUDIO_AGENT_INPUT_QUEUE_MAX_FRAMES", "240") or "240")
DEFAULT_INPUT_DEVICE_NAME = str(os.getenv("VOICE_AGENT_INPUT_DEVICE_NAME", "") or "").strip()
DEFAULT_INPUT_DEVICE_INDEX = str(os.getenv("VOICE_AGENT_INPUT_DEVICE_INDEX", "") or "").strip()
DEFAULT_AEC_DELAY_MS = int(os.getenv("AUDIO_AGENT_AEC_DELAY_MS", "40") or "40")
DEFAULT_HTTP_TIMEOUT = httpx.Timeout(60.0)
DEFAULT_TURN_TIMEOUT_SECONDS = float(os.getenv("AUDIO_AGENT_TURN_TIMEOUT_SECONDS", "90") or "90")
DEFAULT_PLAYBACK_TAIL_SECONDS = float(os.getenv("AUDIO_AGENT_PLAYBACK_TAIL_SECONDS", "0.12") or "0.12")
DEFAULT_DIRECT_TTS_MIN_CHARS = int(os.getenv("AUDIO_AGENT_DIRECT_TTS_MIN_CHARS", "28") or "28")
DEFAULT_DIRECT_TTS_MAX_CHARS = int(os.getenv("AUDIO_AGENT_DIRECT_TTS_MAX_CHARS", "120") or "120")
DEFAULT_DIRECT_TTS_IDLE_FLUSH_SECONDS = float(
    os.getenv("AUDIO_AGENT_DIRECT_TTS_IDLE_FLUSH_SECONDS", "0.16") or "0.16"
)
DEFAULT_BARGE_IN_MIN_CHARS = int(os.getenv("AUDIO_AGENT_BARGE_IN_MIN_CHARS", "2") or "2")
DEFAULT_FRONTEND_TARGET_RMS = float(os.getenv("AUDIO_AGENT_FRONTEND_TARGET_RMS", "0.05") or "0.05")
DEFAULT_FRONTEND_MAX_GAIN = float(os.getenv("AUDIO_AGENT_FRONTEND_MAX_GAIN", "2.5") or "2.5")
DEFAULT_FRONTEND_ATTACK = float(os.getenv("AUDIO_AGENT_FRONTEND_ATTACK", "0.7") or "0.7")
DEFAULT_FRONTEND_RELEASE = float(os.getenv("AUDIO_AGENT_FRONTEND_RELEASE", "0.3") or "0.3")
DEFAULT_FRONTEND_HP_ALPHA = float(os.getenv("AUDIO_AGENT_FRONTEND_HP_ALPHA", "0.985") or "0.985")
DEFAULT_FRONTEND_SILENCE_ATTENUATION = float(
    os.getenv("AUDIO_AGENT_FRONTEND_SILENCE_ATTENUATION", "0.9") or "0.9"
)
DEFAULT_FRONTEND_SPEECH_MARGIN_DB = float(os.getenv("AUDIO_AGENT_FRONTEND_SPEECH_MARGIN_DB", "8.0") or "8.0")
DEFAULT_FRONTEND_CLIP_THRESHOLD = float(os.getenv("AUDIO_AGENT_FRONTEND_CLIP_THRESHOLD", "0.985") or "0.985")
DEFAULT_FRONTEND_HANGOVER_FRAMES = int(os.getenv("AUDIO_AGENT_FRONTEND_HANGOVER_FRAMES", "28") or "28")
DEFAULT_FRONTEND_NOISE_BOOTSTRAP_FRAMES = int(
    os.getenv("AUDIO_AGENT_FRONTEND_NOISE_BOOTSTRAP_FRAMES", "18") or "18"
)
DEFAULT_PARTIAL_COMMIT_DELAY_SECONDS = float(
    os.getenv("AUDIO_AGENT_PARTIAL_COMMIT_DELAY_SECONDS", "0.85") or "0.85"
)
DEFAULT_PARTIAL_COMMIT_MIN_CHARS = int(os.getenv("AUDIO_AGENT_PARTIAL_COMMIT_MIN_CHARS", "3") or "3")
DEFAULT_PARTIAL_COMMIT_QUERY_MIN_CHARS = int(
    os.getenv("AUDIO_AGENT_PARTIAL_COMMIT_QUERY_MIN_CHARS", "12") or "12"
)
DEFAULT_PARTIAL_COMMIT_QUERY_MIN_WORDS = int(
    os.getenv("AUDIO_AGENT_PARTIAL_COMMIT_QUERY_MIN_WORDS", "3") or "3"
)
DEFAULT_API_ASR_PREROLL_MS = float(os.getenv("AUDIO_AGENT_API_ASR_PREROLL_MS", "220") or "220")
DEFAULT_API_ASR_MIN_TURN_MS = float(os.getenv("AUDIO_AGENT_API_ASR_MIN_TURN_MS", "260") or "260")
DEFAULT_SPEAKER_ID_PREROLL_MS = float(os.getenv("VOICE_SPEAKER_ID_PREROLL_MS", "180") or "180")
DEFAULT_SPEAKER_ID_SEGMENT_MAX_AGE_SECONDS = float(
    os.getenv("VOICE_SPEAKER_ID_SEGMENT_MAX_AGE_SECONDS", "8.0") or "8.0"
)
DEFAULT_SPEAKER_ID_SEGMENT_FALLBACK_AGE_SECONDS = float(
    os.getenv("VOICE_SPEAKER_ID_SEGMENT_FALLBACK_AGE_SECONDS", "18.0") or "18.0"
)
DEFAULT_SPEAKER_ID_STALE_FALLBACK_SECONDS = float(
    os.getenv("VOICE_SPEAKER_ID_STALE_FALLBACK_SECONDS", "12.0") or "12.0"
)
DEFAULT_SPEAKER_ID_RECENT_SEGMENTS = int(os.getenv("VOICE_SPEAKER_ID_RECENT_SEGMENTS", "8") or "8")
DEFAULT_SPEAKER_ID_ENROLL_TIMEOUT_SECONDS = float(
    os.getenv("VOICE_SPEAKER_ID_ENROLL_TIMEOUT_SECONDS", "20.0") or "20.0"
)
DEFAULT_SPEAKER_ID_ENROLL_SUPPRESS_SECONDS = float(
    os.getenv("VOICE_SPEAKER_ID_ENROLL_SUPPRESS_SECONDS", "3.0") or "3.0"
)
DEFAULT_SPEAKER_ID_LIVE_CAPTIONS_MAX_CANDIDATES = int(
    os.getenv("VOICE_SPEAKER_ID_LIVE_CAPTIONS_MAX_CANDIDATES", "4") or "4"
)
DEFAULT_GEMINI_LIVE_MODEL = (
    os.getenv("GEMINI_LIVE_MODEL", "models/gemini-2.5-flash-native-audio-latest")
    or "models/gemini-2.5-flash-native-audio-latest"
)
DEFAULT_GEMINI_LIVE_VOICE = os.getenv("GEMINI_LIVE_VOICE", "Kore") or "Kore"
DEFAULT_GEMINI_LIVE_OUTPUT_SAMPLE_RATE = 24000
DEFAULT_GEMINI_LIVE_QUEUE_MAX_FRAMES = int(os.getenv("GEMINI_LIVE_QUEUE_MAX_FRAMES", "96") or "96")
DEFAULT_GEMINI_LIVE_SYSTEM_PROMPT = (
    "You are Rachel, a warm voice companion in a rehabilitation and exercise game system. "
    "Sound natural and supportive. Keep spoken replies concise and clear. "
    "Treat each user turn as part of an ongoing conversation. "
    "Do not force every topic back to exercise. "
    "If the user asks to open, start, launch, or close a game, acknowledge briefly in one sentence. "
    "If intent is unclear, ask one short clarification question instead of guessing."
)
DEFAULT_LIVE_CAPTIONS_EXE = _resolve_default_live_captions_exe()
DEFAULT_LIVE_CAPTIONS_OUTPUT_DIR = os.getenv(
    "LIVE_CAPTIONS_OUTPUT_DIR",
    str((STATE_DIR / "live_captions").resolve()),
)
DEFAULT_LIVE_CAPTIONS_ASSISTANT_SUPPRESS_SECONDS = float(
    os.getenv("LIVE_CAPTIONS_ASSISTANT_SUPPRESS_SECONDS", "1.6") or "1.6"
)
DEFAULT_LIVE_CAPTIONS_ASSISTANT_HISTORY_SECONDS = float(
    os.getenv("LIVE_CAPTIONS_ASSISTANT_HISTORY_SECONDS", "45.0") or "45.0"
)
DEFAULT_LIVE_CAPTIONS_ASSISTANT_VARIANT_LIMIT = int(
    os.getenv("LIVE_CAPTIONS_ASSISTANT_VARIANT_LIMIT", "256") or "256"
)
DEFAULT_LIVE_CAPTIONS_MINIMIZE_WINDOW = str(
    os.getenv("LIVE_CAPTIONS_MINIMIZE_WINDOW", "1") or "1"
).strip().lower() in {"1", "true", "yes", "on"}
DEFAULT_LIVE_CAPTIONS_HELPER_STDERR_TAIL_LINES = int(
    os.getenv("LIVE_CAPTIONS_HELPER_STDERR_TAIL_LINES", "8") or "8"
)
DEFAULT_LIVE_CAPTIONS_HELPER_RETRY_WINDOW_SECONDS = float(
    os.getenv("LIVE_CAPTIONS_HELPER_RETRY_WINDOW_SECONDS", "8.0") or "8.0"
)
TRANSCRIPT_SOURCE_FINAL = "final"
TRANSCRIPT_SOURCE_STABLE_PARTIAL_COMMAND = "stable_partial_command"
TRANSCRIPT_SOURCE_STABLE_PARTIAL_FALLBACK = "stable_partial_fallback"
TRANSCRIPT_CONFIDENCE_HIGH = "high"
TRANSCRIPT_CONFIDENCE_MEDIUM = "medium"
TRANSCRIPT_CONFIDENCE_LOW = "low"

PIPELINE_MODE_DIRECT_UNIFIED = "direct_unified"
PIPELINE_MODE_LEGACY_MQTT = "legacy_mqtt"
CONVERSATION_PROFILE_LOCAL = "local"
CONVERSATION_PROFILE_CLOUD = "cloud"
RESPONSE_PROVIDER_OPENAI = "openai"
RESPONSE_PROVIDER_GEMINI = "gemini"
TTS_BACKEND_PIPER = "piper"
TTS_BACKEND_QWEN = "qwen"
TTS_BACKEND_KOKORO = "kokoro"
TTS_BACKENDS = [TTS_BACKEND_PIPER, TTS_BACKEND_QWEN, TTS_BACKEND_KOKORO]

TRANSCRIBE_MODE_WHISPER = "whisper-large-v3"
TRANSCRIBE_MODE_MOONSHINE_SMALL = "moonshine-small"
TRANSCRIBE_MODE_MOONSHINE_MEDIUM = "moonshine-medium"
TRANSCRIBE_MODE_API = "api"
HOTWORD_STRATEGY_OFF = "off"
HOTWORD_STRATEGY_COMMANDS_ONLY = "commands_only"
HOTWORD_STRATEGY_COMMANDS_GAMES = "commands_games"
HOTWORD_STRATEGY_COMMANDS_GAMES_MEMORY = "commands_games_memory"
HOTWORD_STRATEGIES = [
    HOTWORD_STRATEGY_OFF,
    HOTWORD_STRATEGY_COMMANDS_ONLY,
    HOTWORD_STRATEGY_COMMANDS_GAMES,
    HOTWORD_STRATEGY_COMMANDS_GAMES_MEMORY,
]
DEFAULT_STABLE_PARTIAL_REPEATS = int(os.getenv("VOICE_ASR_STABLE_PARTIAL_REPEATS", "2") or "2")
_PARTIAL_COMMIT_SENTENCE_END_RE = re.compile(r"[.!?;:\u2026][\"')\]]*$")
_PARTIAL_COMMIT_WORD_RE = re.compile(r"\b[\w']+\b", re.UNICODE)
_PARTIAL_COMMIT_TRAILING_CONNECTOR_RE = re.compile(
    r"(?:\b(?:and|or|but|to|of|with|for|in|on|at|through|about|into|from)\b[\s,;:]*)+$",
    re.IGNORECASE,
)


def normalize_hotword_strategy(value: Optional[str]) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"off", "none", "disabled"}:
        return HOTWORD_STRATEGY_OFF
    if normalized in {"commands", "commands_only", "command"}:
        return HOTWORD_STRATEGY_COMMANDS_ONLY
    if normalized in {"commands_games", "games"}:
        return HOTWORD_STRATEGY_COMMANDS_GAMES
    return HOTWORD_STRATEGY_COMMANDS_GAMES_MEMORY


def _should_include_hotword(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if re.fullmatch(r"user[\s_\-]*\d+", text, flags=re.IGNORECASE):
        return False
    return any(ch.isalpha() for ch in text)


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value else default


def _gemini_api_key() -> str:
    return _env("GEMINI_API_KEY", "") or _env("GEMINI_KEY", "")


def _normalize_cloud_response_provider(value: Optional[str]) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"gemini", "google", "google-ai", "google_ai"}:
        return RESPONSE_PROVIDER_GEMINI
    return RESPONSE_PROVIDER_OPENAI


def _normalize_tts_backend(value: Optional[str]) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {TTS_BACKEND_QWEN, "qwen_tts", "qwen-tts"}:
        return TTS_BACKEND_QWEN
    if normalized in {TTS_BACKEND_KOKORO, "kokoro_tts", "kokoro-tts"}:
        return TTS_BACKEND_KOKORO
    return TTS_BACKEND_PIPER


def _normalize_pipeline_mode(value: Optional[str]) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"legacy", "mqtt", "legacy_mqtt"}:
        return PIPELINE_MODE_LEGACY_MQTT
    return PIPELINE_MODE_DIRECT_UNIFIED


def _normalize_profile(value: Optional[str]) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"cloud", "openai", "gemini", "online"}:
        return CONVERSATION_PROFILE_CLOUD
    return CONVERSATION_PROFILE_LOCAL


def normalize_asr_mode(value: Optional[str]) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"whisper-large-v3", "offline", "local", "whisper", "faster-whisper", "large-v3"}:
        return TRANSCRIBE_MODE_WHISPER
    if normalized in {"moonshine-small", "moonshine_small", "small"}:
        return TRANSCRIBE_MODE_MOONSHINE_SMALL
    if normalized in {"moonshine-medium", "moonshine_medium", "moonshine", "medium"}:
        return TRANSCRIBE_MODE_MOONSHINE_MEDIUM
    if normalized in {"api", "openai", "gemini", "online", "cloud-api", "service-api"}:
        return TRANSCRIBE_MODE_API
    return TRANSCRIBE_MODE_MOONSHINE_MEDIUM


def _parse_pcm_sample_rate(mime_type: str, default: int) -> int:
    match = re.search(r"rate\s*=\s*(\d+)", str(mime_type or ""), flags=re.IGNORECASE)
    if match is None:
        return int(default)
    try:
        return max(1, int(match.group(1)))
    except Exception:
        return int(default)


def _coerce_string_list(value: Any) -> List[str]:
    if isinstance(value, list):
        values = [str(item or "").strip() for item in value]
        return [item for item in values if item]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                return [str(item or "").strip() for item in parsed if str(item or "").strip()]
        merged = text.replace("\r\n", "\n").replace(";", ",").replace("\n", ",")
        parts = [part.strip() for part in merged.split(",")]
        return [part for part in parts if part]
    return []


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
    if lhs in rhs or rhs in lhs:
        shorter = min(len(lhs), len(rhs))
        longer = max(len(lhs), len(rhs))
        return 0.82 + (0.18 * shorter / max(1, longer))
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


def _normalize_request_source(value: Optional[str]) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    normalized = re.sub(r"\s+", "_", normalized)
    return normalized


def _compose_turn_source(input_source: Optional[str]) -> str:
    normalized = _normalize_request_source(input_source)
    if not normalized:
        return "desktop_audio"
    if normalized.startswith("desktop_audio"):
        return normalized
    return f"desktop_audio:{normalized}"


def _assistant_text_variants(text: str) -> List[str]:
    normalized = " ".join(str(text or "").strip().split())
    if not normalized:
        return []
    variants: List[str] = [normalized]
    clauses = [part.strip() for part in re.split(r"[,.!?;:，。！？；：]+", normalized) if part.strip()]
    variants.extend(clauses)
    tokens = normalized.split()
    if len(tokens) >= 6:
        for window in (4, 5, 6, 8):
            if len(tokens) < window:
                continue
            step = max(1, window // 2)
            for index in range(0, len(tokens) - window + 1, step):
                variants.append(" ".join(tokens[index : index + window]))
    deduped: List[str] = []
    seen = set()
    for item in variants:
        key = _normalize_compare_text(item)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _estimate_transcript_confidence(
    *,
    text: str,
    grammar_route: str,
    grammar_confidence: float,
    avg_logprob: Optional[float],
    transcript_source: str,
    input_source: str = "",
) -> str:
    route = str(grammar_route or "").strip().upper()
    normalized_input_source = _normalize_request_source(input_source)
    live_captions_final = (
        transcript_source == TRANSCRIPT_SOURCE_FINAL
        and normalized_input_source.endswith("live_captions")
    )
    if route != "QUERY" and float(grammar_confidence or 0.0) >= 0.86:
        return TRANSCRIPT_CONFIDENCE_HIGH
    if avg_logprob is not None and float(avg_logprob) < -1.2:
        return TRANSCRIPT_CONFIDENCE_LOW
    token_count = len([part for part in re.split(r"\s+", str(text or "").strip()) if part])
    if transcript_source == TRANSCRIPT_SOURCE_STABLE_PARTIAL_FALLBACK:
        return TRANSCRIPT_CONFIDENCE_MEDIUM if route != "QUERY" else TRANSCRIPT_CONFIDENCE_LOW
    if route == "QUERY" and token_count <= 3:
        if live_captions_final:
            return TRANSCRIPT_CONFIDENCE_MEDIUM
        return TRANSCRIPT_CONFIDENCE_LOW
    return TRANSCRIPT_CONFIDENCE_MEDIUM


def _safe_resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate <= 0 or dst_rate <= 0 or audio.size == 0 or src_rate == dst_rate:
        return np.asarray(audio, dtype=np.float32)

    samples = np.asarray(audio, dtype=np.float32)
    if resample_poly is not None:
        try:
            return np.asarray(resample_poly(samples, dst_rate, src_rate), dtype=np.float32)
        except Exception:
            pass

    duration = samples.size / float(src_rate)
    dst_count = max(1, int(round(duration * dst_rate)))
    src_x = np.linspace(0.0, 1.0, num=samples.size, endpoint=False)
    dst_x = np.linspace(0.0, 1.0, num=dst_count, endpoint=False)
    return np.asarray(np.interp(dst_x, src_x, samples), dtype=np.float32)


def _collapse_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _pcm16_bytes_to_float32_mono(raw: bytes) -> np.ndarray:
    if not raw:
        return np.zeros(0, dtype=np.float32)
    frame_count = len(raw) // 2
    if frame_count <= 0:
        return np.zeros(0, dtype=np.float32)
    pcm = np.frombuffer(raw[: frame_count * 2], dtype="<i2")
    return (pcm.astype(np.float32) / 32768.0).reshape(-1)


def _float32_to_pcm16_bytes(audio: np.ndarray) -> bytes:
    if audio.size == 0:
        return b""
    clipped = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def _decode_wav_bytes(wav_bytes: bytes) -> Tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        channels = max(1, int(wf.getnchannels()))
        sample_rate = max(1, int(wf.getframerate()))
        sample_width = int(wf.getsampwidth())
        frames = wf.readframes(wf.getnframes())
    if sample_width != 2:
        raise RuntimeError(f"unsupported WAV sample width: {sample_width}")
    pcm = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1)
    return np.asarray(pcm, dtype=np.float32), sample_rate


def _append_stream_text(base: str, delta: str) -> str:
    head = str(base or "")
    tail = str(delta or "")
    if not head:
        return tail.strip()
    if not tail:
        return head.strip()
    if head[-1].isspace() or tail[0].isspace():
        return (head + tail).strip()
    if head[-1].islower() and tail[0].islower():
        return head + tail
    if head[-1] in ".!?;,:" and tail[0].isalnum():
        return f"{head} {tail}"
    if head[-1].isalnum() and tail[0].isalnum():
        return f"{head} {tail}"
    return head + tail


def _find_direct_tts_boundary(text: str, *, final: bool, idle: bool) -> int:
    current = str(text or "")
    if not current:
        return 0

    hard_boundaries = ".!?;:。！？；：\n"
    soft_boundaries = ",，、"
    minimum_chunk_chars = max(12, int(DEFAULT_DIRECT_TTS_MIN_CHARS))
    maximum_chunk_chars = max(minimum_chunk_chars, int(DEFAULT_DIRECT_TTS_MAX_CHARS))

    if len(current) >= minimum_chunk_chars:
        for idx in range(len(current) - 1, minimum_chunk_chars - 1, -1):
            if current[idx] in hard_boundaries:
                return idx + 1

    if len(current) >= maximum_chunk_chars or idle:
        upper = min(len(current) - 1, maximum_chunk_chars)
        for idx in range(upper, minimum_chunk_chars - 1, -1):
            if current[idx] in soft_boundaries or current[idx].isspace():
                return idx + 1
        if idle and len(current) >= minimum_chunk_chars:
            return len(current)

    if final:
        return len(current)
    return 0


@dataclass
class LogEntry:
    timestamp: float
    role: str
    message: str
    speaker: str = ""
    source: str = ""
    metadata: str = ""

    def to_payload(self) -> Dict[str, Any]:
        return {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self.timestamp)) + "Z",
            "role": self.role,
            "speaker": self.speaker,
            "message": self.message,
            "source": self.source,
            "metadata": self.metadata,
        }


class ConversationLogStore:
    def __init__(self, limit: int = 200) -> None:
        self._entries: Deque[LogEntry] = deque(maxlen=max(10, int(limit)))
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._version = 0

    def add(self, role: str, message: str, *, speaker: str = "", source: str = "", metadata: str = "") -> None:
        text = str(message or "").strip()
        if not text:
            return
        with self._condition:
            self._entries.append(
                LogEntry(
                    timestamp=time.time(),
                    role=str(role or "system").strip().lower() or "system",
                    message=text,
                    speaker=str(speaker or "").strip(),
                    source=str(source or "").strip(),
                    metadata=str(metadata or "").strip(),
                )
            )
            self._version += 1
            self._condition.notify_all()

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [entry.to_payload() for entry in self._entries]

    def snapshot_with_version(self) -> Tuple[int, List[Dict[str, Any]]]:
        with self._lock:
            return self._version, [entry.to_payload() for entry in self._entries]

    def wait_for_update(self, after_version: int, timeout: float = 15.0) -> Tuple[int, List[Dict[str, Any]]]:
        timeout_seconds = max(0.1, float(timeout))
        with self._condition:
            if self._version <= int(after_version):
                self._condition.wait(timeout_seconds)
            return self._version, [entry.to_payload() for entry in self._entries]


class WebRtcApm:
    def __init__(self, sample_rate_hz: int, *, delay_ms: int = DEFAULT_AEC_DELAY_MS) -> None:
        self.sample_rate_hz = int(sample_rate_hz)
        self.delay_ms = max(0, int(delay_ms))
        self._dll = None
        self._handle = None
        self._dll_dirs: List[Any] = []
        self.available = False
        self._load()

    def _load(self) -> None:
        if ctypes is None or os.name != "nt":
            return
        raw_candidates = [
            _env("WEBRTC_APM_DLL", ""),
            str(Path(__file__).resolve().parents[1] / "native" / "webrtc_apm_unity" / "build" / "webrtc_apm_unity.dll"),
            str(Path(__file__).resolve().parents[1] / "native" / "webrtc_apm_unity" / "build" / "libwebrtc_apm_unity.dll"),
        ]
        dll_path = None
        for raw_candidate in raw_candidates:
            text = str(raw_candidate or "").strip()
            if not text:
                continue
            path = Path(os.path.expandvars(text)).expanduser()
            if path.is_file():
                dll_path = path
                break
        if dll_path is None:
            return

        try:
            for candidate_dir in self._dependency_directories(dll_path):
                if hasattr(os, "add_dll_directory"):
                    try:
                        self._dll_dirs.append(os.add_dll_directory(str(candidate_dir)))
                    except Exception:
                        pass
            dll = ctypes.CDLL(str(dll_path))
        except Exception as exc:
            logger.warning("AEC DLL load failed: %s", exc)
            return

        dll.apm_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
        dll.apm_create.restype = ctypes.c_void_p
        dll.apm_destroy.argtypes = [ctypes.c_void_p]
        dll.apm_destroy.restype = None
        dll.apm_set_stream_delay_ms.argtypes = [ctypes.c_void_p, ctypes.c_int]
        dll.apm_set_stream_delay_ms.restype = ctypes.c_int
        dll.apm_process_reverse_stream.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.c_int]
        dll.apm_process_reverse_stream.restype = ctypes.c_int
        dll.apm_process_stream.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_float),
        ]
        dll.apm_process_stream.restype = ctypes.c_int

        handle = dll.apm_create(int(self.sample_rate_hz), 1, 1)
        if not handle:
            logger.warning("AEC DLL returned null handle")
            return
        dll.apm_set_stream_delay_ms(handle, int(self.delay_ms))
        self._dll = dll
        self._handle = handle
        self.available = True

    def process(self, capture: np.ndarray, render: np.ndarray) -> np.ndarray:
        samples = np.asarray(capture, dtype=np.float32).reshape(-1)
        if not self.available or samples.size == 0 or self._handle is None or self._dll is None:
            return samples
        reference = np.asarray(render, dtype=np.float32).reshape(-1)
        if reference.size != samples.size:
            if reference.size == 0:
                reference = np.zeros(samples.size, dtype=np.float32)
            else:
                reference = _safe_resample(reference, reference.size, samples.size)
                if reference.size != samples.size:
                    reference = np.resize(reference, samples.size).astype(np.float32)
        out = np.zeros_like(samples, dtype=np.float32)
        render_ptr = reference.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        capture_ptr = samples.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        out_ptr = out.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        try:
            self._dll.apm_process_reverse_stream(self._handle, render_ptr, int(samples.size))
            rc = self._dll.apm_process_stream(self._handle, capture_ptr, int(samples.size), out_ptr)
            if rc != 0:
                return samples
            return out
        except Exception:
            return samples

    def close(self) -> None:
        if self._dll is not None and self._handle is not None:
            try:
                self._dll.apm_destroy(self._handle)
            except Exception:
                pass
        self._dll = None
        self._handle = None
        while self._dll_dirs:
            handle = self._dll_dirs.pop()
            try:
                handle.close()
            except Exception:
                pass
        self.available = False

    @staticmethod
    def _dependency_directories(dll_path: Path) -> List[Path]:
        directories: List[Path] = []

        def add_directory(raw: Optional[str]) -> None:
            text = str(raw or "").strip()
            if not text:
                return
            path = Path(os.path.expandvars(text)).expanduser()
            if path.is_dir() and path not in directories:
                directories.append(path)

        add_directory(str(dll_path.parent))
        add_directory(_env("WEBRTC_APM_DEP_DIR", ""))
        add_directory(r"C:\msys64\ucrt64\bin")
        add_directory(r"C:\msys64\mingw64\bin")
        return directories


class PcmPlayer:
    def __init__(self, *, output_sample_rate: int, capture_sample_rate: int) -> None:
        self.output_sample_rate = int(output_sample_rate)
        self.capture_sample_rate = int(capture_sample_rate)
        self._queue: Deque[np.ndarray] = deque()
        self._queue_lock = threading.Lock()
        self._current = np.zeros(0, dtype=np.float32)
        self._current_offset = 0
        self._stream = None
        self._render_blocks: Deque[np.ndarray] = deque()
        self._render_lock = threading.Lock()
        self._render_tail = np.zeros(0, dtype=np.float32)
        self._active_stream_count = 0
        self._stream_count_lock = threading.Lock()
        self._last_output_audio_at = 0.0
        self._playback_state_lock = threading.Lock()

    @property
    def available(self) -> bool:
        return sd is not None

    def start(self) -> None:
        if sd is None:
            raise RuntimeError("sounddevice is not installed")
        if self._stream is not None:
            return
        self._stream = sd.OutputStream(
            samplerate=self.output_sample_rate,
            blocksize=max(0, int(DEFAULT_OUTPUT_BLOCKSIZE)),
            channels=1,
            dtype="float32",
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass
        self.clear()

    def begin_stream(self) -> None:
        with self._stream_count_lock:
            self._active_stream_count += 1

    def end_stream(self) -> None:
        with self._stream_count_lock:
            self._active_stream_count = max(0, self._active_stream_count - 1)

    def clear(self) -> None:
        with self._queue_lock:
            self._queue.clear()
            self._current = np.zeros(0, dtype=np.float32)
            self._current_offset = 0
        with self._render_lock:
            self._render_blocks.clear()
            self._render_tail = np.zeros(0, dtype=np.float32)
        with self._playback_state_lock:
            self._last_output_audio_at = 0.0

    def is_playing(self) -> bool:
        with self._queue_lock:
            queued = bool(self._queue) or self._current_offset < self._current.size
        if queued:
            return True
        with self._playback_state_lock:
            last_output_audio_at = self._last_output_audio_at
        if last_output_audio_at <= 0.0:
            return False
        return (time.time() - last_output_audio_at) < DEFAULT_PLAYBACK_TAIL_SECONDS

    def enqueue_audio(self, audio: np.ndarray, sample_rate: int) -> None:
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            return
        if int(sample_rate) != self.output_sample_rate:
            samples = _safe_resample(samples, int(sample_rate), self.output_sample_rate)
        with self._queue_lock:
            self._queue.append(np.asarray(samples, dtype=np.float32))

    def enqueue_pcm16(self, raw: bytes, sample_rate: int) -> None:
        audio = _pcm16_bytes_to_float32_mono(raw)
        if audio.size == 0:
            return
        self.enqueue_audio(audio, sample_rate)

    def pop_render_block(self, frames: int) -> np.ndarray:
        need = max(0, int(frames))
        if need <= 0:
            return np.zeros(0, dtype=np.float32)
        with self._render_lock:
            if not self._render_blocks:
                return np.zeros(need, dtype=np.float32)
            out = self._render_blocks.popleft()
        if out.size == need:
            return out
        if out.size == 0:
            return np.zeros(need, dtype=np.float32)
        if out.size > need:
            keep = out[need:].astype(np.float32, copy=True)
            head = out[:need].astype(np.float32, copy=True)
            with self._render_lock:
                self._render_blocks.appendleft(keep)
            return head
        padded = np.zeros(need, dtype=np.float32)
        padded[: out.size] = out
        return padded

    def _callback(self, outdata, frames, time_info, status) -> None:
        if status:
            logger.debug("output stream status: %s", status)
        frames = max(0, int(frames))
        out = np.zeros(frames, dtype=np.float32)
        copied = 0
        if frames > 0:
            with self._queue_lock:
                cursor = 0
                while cursor < frames:
                    if self._current_offset >= self._current.size:
                        if not self._queue:
                            break
                        self._current = self._queue.popleft()
                        self._current_offset = 0
                    take = min(frames - cursor, self._current.size - self._current_offset)
                    if take <= 0:
                        break
                    out[cursor : cursor + take] = self._current[self._current_offset : self._current_offset + take]
                    self._current_offset += take
                    cursor += take
                    if self._current_offset >= self._current.size:
                        self._current = np.zeros(0, dtype=np.float32)
                        self._current_offset = 0
                copied = cursor
        outdata[:, 0] = out
        if copied > 0:
            with self._playback_state_lock:
                self._last_output_audio_at = time.time()
        self._append_render_reference(out)

    def _append_render_reference(self, output_block: np.ndarray) -> None:
        samples = np.asarray(output_block, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            return
        if self.output_sample_rate != self.capture_sample_rate:
            samples = _safe_resample(samples, self.output_sample_rate, self.capture_sample_rate)
        chunk = np.concatenate([self._render_tail, samples], axis=0) if self._render_tail.size else samples
        block_size = max(1, int(DEFAULT_INPUT_BLOCKSIZE))
        ready_count = chunk.size // block_size
        if ready_count <= 0:
            with self._render_lock:
                self._render_tail = chunk.astype(np.float32, copy=True)
            return
        ready = chunk[: ready_count * block_size]
        tail = chunk[ready_count * block_size :]
        blocks = ready.reshape(ready_count, block_size)
        with self._render_lock:
            for idx in range(blocks.shape[0]):
                self._render_blocks.append(np.asarray(blocks[idx], dtype=np.float32))
                while len(self._render_blocks) > 120:
                    self._render_blocks.popleft()
            self._render_tail = np.asarray(tail, dtype=np.float32)


class AudioInputBuffer:
    def __init__(self, *, frame_size: int, max_frames: int) -> None:
        self.frame_size = max(1, int(frame_size))
        self.max_frames = max(1, int(max_frames))
        self._queue: Deque[np.ndarray] = deque()
        self._carry = np.zeros(0, dtype=np.float32)
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._closed = False
        self._dropped_frames = 0

    def reset(self) -> None:
        with self._condition:
            self._queue.clear()
            self._carry = np.zeros(0, dtype=np.float32)
            self._closed = False
            self._dropped_frames = 0
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def push(self, samples: np.ndarray) -> None:
        chunk = np.asarray(samples, dtype=np.float32).reshape(-1)
        if chunk.size == 0:
            return
        with self._condition:
            merged = np.concatenate([self._carry, chunk], axis=0) if self._carry.size else chunk
            ready_count = merged.size // self.frame_size
            if ready_count <= 0:
                self._carry = np.asarray(merged, dtype=np.float32)
                return
            ready = merged[: ready_count * self.frame_size].reshape(ready_count, self.frame_size)
            self._carry = np.asarray(merged[ready_count * self.frame_size :], dtype=np.float32)
            for idx in range(ready.shape[0]):
                self._queue.append(np.asarray(ready[idx], dtype=np.float32))
            while len(self._queue) > self.max_frames:
                self._queue.popleft()
                self._dropped_frames += 1
            self._condition.notify_all()

    def pop(self, timeout: float = 0.2) -> Optional[np.ndarray]:
        wait_timeout = max(0.01, float(timeout))
        with self._condition:
            if not self._queue and not self._closed:
                self._condition.wait(wait_timeout)
            if self._queue:
                return self._queue.popleft()
            return None

    def queued_frames(self) -> int:
        with self._lock:
            return len(self._queue)

    def dropped_frames(self) -> int:
        with self._lock:
            return self._dropped_frames


@dataclass
class AudioFrontEndStats:
    input_level_dbfs: float = -96.0
    input_peak_dbfs: float = -96.0
    noise_floor_dbfs: float = -72.0
    frontend_gain_db: float = 0.0
    speech_active: bool = False
    clipped_recently: bool = False
    clip_events: int = 0


class AudioFrontEndProcessor:
    def __init__(self, *, sample_rate_hz: int, frame_size: int) -> None:
        self.sample_rate_hz = max(1, int(sample_rate_hz))
        self.frame_size = max(1, int(frame_size))
        self._prev_x = 0.0
        self._prev_y = 0.0
        self._gain = 1.0
        self._noise_floor_dbfs = -72.0
        self._noise_bootstrap_frames = DEFAULT_FRONTEND_NOISE_BOOTSTRAP_FRAMES
        self._noise_bootstrap_remaining = self._noise_bootstrap_frames
        self._speech_active = False
        self._hangover_frames = 0
        self._clip_events = 0
        self._last_clip_at = 0.0
        self._stats_lock = threading.Lock()
        self._stats = AudioFrontEndStats()

    def reset(self) -> None:
        with self._stats_lock:
            self._prev_x = 0.0
            self._prev_y = 0.0
            self._gain = 1.0
            self._noise_floor_dbfs = -72.0
            self._noise_bootstrap_remaining = self._noise_bootstrap_frames
            self._speech_active = False
            self._hangover_frames = 0
            self._clip_events = 0
            self._last_clip_at = 0.0
            self._stats = AudioFrontEndStats(noise_floor_dbfs=self._noise_floor_dbfs)

    def process(self, samples: np.ndarray) -> np.ndarray:
        audio = np.asarray(samples, dtype=np.float32).reshape(-1)
        if audio.size == 0:
            return audio
        filtered = self._high_pass(audio)
        rms = float(np.sqrt(np.mean(np.square(filtered), dtype=np.float64) + 1e-12))
        peak = float(np.max(np.abs(filtered))) if filtered.size else 0.0
        rms_dbfs = self._to_dbfs(rms)
        peak_dbfs = self._to_dbfs(max(peak, 1e-6))
        if self._noise_bootstrap_remaining > 0:
            self._noise_bootstrap_remaining -= 1
            self._noise_floor_dbfs = (0.7 * self._noise_floor_dbfs) + (0.3 * rms_dbfs)
            speaking = peak_dbfs > -18.0 or rms_dbfs > -30.0
        else:
            speaking = (
                rms_dbfs > (self._noise_floor_dbfs + DEFAULT_FRONTEND_SPEECH_MARGIN_DB)
                or peak_dbfs > -24.0
                or rms_dbfs > -36.0
            )
        if speaking:
            self._hangover_frames = DEFAULT_FRONTEND_HANGOVER_FRAMES
        elif self._hangover_frames > 0:
            speaking = True
            self._hangover_frames -= 1

        if speaking:
            if rms_dbfs < (self._noise_floor_dbfs + DEFAULT_FRONTEND_SPEECH_MARGIN_DB + 3.0):
                target_floor = rms_dbfs - 2.0
                self._noise_floor_dbfs = (0.93 * self._noise_floor_dbfs) + (0.07 * target_floor)
            else:
                target_floor = min(rms_dbfs - 6.0, self._noise_floor_dbfs)
                self._noise_floor_dbfs = (0.992 * self._noise_floor_dbfs) + (0.008 * target_floor)
        else:
            self._noise_floor_dbfs = (0.9 * self._noise_floor_dbfs) + (0.1 * rms_dbfs)
        self._noise_floor_dbfs = float(np.clip(self._noise_floor_dbfs, -96.0, -18.0))

        target_gain = min(DEFAULT_FRONTEND_MAX_GAIN, DEFAULT_FRONTEND_TARGET_RMS / max(rms, 1e-4))
        smoothing = DEFAULT_FRONTEND_ATTACK if target_gain < self._gain else DEFAULT_FRONTEND_RELEASE
        self._gain = (smoothing * self._gain) + ((1.0 - smoothing) * target_gain)
        processed = np.asarray(filtered * self._gain, dtype=np.float32)
        if not speaking:
            processed *= DEFAULT_FRONTEND_SILENCE_ATTENUATION
        processed = np.clip(processed, -1.0, 1.0)

        post_peak = float(np.max(np.abs(processed))) if processed.size else 0.0
        clipped = peak >= DEFAULT_FRONTEND_CLIP_THRESHOLD or post_peak >= DEFAULT_FRONTEND_CLIP_THRESHOLD
        if clipped:
            self._clip_events += 1
            self._last_clip_at = time.time()
        with self._stats_lock:
            self._speech_active = speaking
            self._stats = AudioFrontEndStats(
                input_level_dbfs=rms_dbfs,
                input_peak_dbfs=peak_dbfs,
                noise_floor_dbfs=self._noise_floor_dbfs,
                frontend_gain_db=self._to_dbfs(max(self._gain, 1e-6)),
                speech_active=speaking,
                clipped_recently=(time.time() - self._last_clip_at) < 2.0,
                clip_events=self._clip_events,
            )
        return processed

    def status(self) -> AudioFrontEndStats:
        with self._stats_lock:
            return AudioFrontEndStats(**self._stats.__dict__)

    def _high_pass(self, audio: np.ndarray) -> np.ndarray:
        alpha = float(np.clip(DEFAULT_FRONTEND_HP_ALPHA, 0.0, 0.9999))
        output = np.zeros_like(audio, dtype=np.float32)
        prev_x = self._prev_x
        prev_y = self._prev_y
        for idx, value in enumerate(audio):
            current = float(value)
            y = current - prev_x + (alpha * prev_y)
            output[idx] = y
            prev_x = current
            prev_y = y
        self._prev_x = prev_x
        self._prev_y = prev_y
        return output

    @staticmethod
    def _to_dbfs(value: float) -> float:
        return 20.0 * np.log10(max(float(value), 1e-6))


class LiveCaptionsTranscriptSource:
    def __init__(
        self,
        *,
        exe_path: str,
        output_dir: str,
        on_caption: Callable[[str, float], None],
        on_error: Callable[[str], None],
    ) -> None:
        self.exe_path = str(exe_path or "").strip()
        self.output_dir = str(output_dir or "").strip()
        self._on_caption = on_caption
        self._on_error = on_error
        self._process: Optional[subprocess.Popen[str]] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._output_path = ""
        self._last_status = ""
        self._last_error = ""
        self._stderr_tail: Deque[str] = deque(maxlen=max(1, DEFAULT_LIVE_CAPTIONS_HELPER_STDERR_TAIL_LINES))
        self._launch_started_at = 0.0
        self._current_show_live_captions = False
        self._visible_retry_attempted = False

    @property
    def output_path(self) -> str:
        with self._lock:
            return self._output_path

    @property
    def last_status(self) -> str:
        with self._lock:
            return self._last_status

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    def is_available(self) -> bool:
        return Path(self.exe_path).exists()

    def is_running(self) -> bool:
        with self._lock:
            process = self._process
        return process is not None and process.poll() is None

    def _terminate_stale_helpers(self) -> None:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        script = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -eq 'EnableLcMic.exe' -and $_.CommandLine -like '*--headless*' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=creationflags,
                timeout=8.0,
            )
        except Exception:
            pass

    def start(self) -> None:
        if self.is_running():
            return
        exe = Path(self.exe_path)
        if not exe.exists():
            raise FileNotFoundError(
                f"Live Captions listener not found: {exe}. "
                "Set LIVE_CAPTIONS_LISTENER_EXE to your EnableLcMic.exe path."
            )
        self._terminate_stale_helpers()
        with self._lock:
            self._stderr_tail.clear()
            self._visible_retry_attempted = False
        self._launch_process(
            show_live_captions=not DEFAULT_LIVE_CAPTIONS_MINIMIZE_WINDOW,
            restart_reason="starting",
        )

    def _launch_process(self, *, show_live_captions: bool, restart_reason: str) -> None:
        exe = Path(self.exe_path)
        output_dir = Path(self.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"captions_{int(time.time() * 1000)}_{os.getpid()}.txt"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        args = [str(exe), "--headless"]
        if show_live_captions:
            args.append("--show-live-captions")
        args.extend(["--output", str(output_path)])
        process = subprocess.Popen(
            args,
            cwd=str(exe.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        with self._lock:
            self._process = process
            self._output_path = str(output_path)
            self._last_status = str(restart_reason or "starting")
            self._last_error = ""
            self._stderr_tail.clear()
            self._launch_started_at = time.monotonic()
            self._current_show_live_captions = bool(show_live_captions)
        self._stop_event.clear()
        self._start_worker_threads()

    def _start_worker_threads(self) -> None:
        self._stdout_thread = threading.Thread(target=self._stdout_loop, name="live-captions-stdout", daemon=True)
        self._stderr_thread = threading.Thread(target=self._stderr_loop, name="live-captions-stderr", daemon=True)
        self._monitor_thread = threading.Thread(target=self._monitor_loop, name="live-captions-monitor", daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        self._monitor_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            process = self._process
            self._process = None
            self._last_status = "stopped"
            self._current_show_live_captions = False
            self._visible_retry_attempted = False
        if process is not None:
            try:
                process.terminate()
            except Exception:
                pass
            try:
                process.wait(timeout=3.0)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        for worker in [self._stdout_thread, self._stderr_thread, self._monitor_thread]:
            if worker is not None:
                worker.join(timeout=1.0)
        self._stdout_thread = None
        self._stderr_thread = None
        self._monitor_thread = None

    def _stdout_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while not self._stop_event.is_set():
                line = process.stdout.readline()
                if not line:
                    break
                observed_at = time.time()
                text = self._extract_caption_text(line)
                if text:
                    self._on_caption(text, observed_at)
        except Exception as exc:
            self._record_error(f"live captions stdout failed: {exc}")

    def _stderr_loop(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while not self._stop_event.is_set():
                line = process.stderr.readline()
                if not line:
                    break
                text = str(line or "").strip()
                if not text:
                    continue
                with self._lock:
                    self._last_status = text
                    self._stderr_tail.append(text)
                lowered = text.casefold()
                if "failed" in lowered or "error" in lowered:
                    self._record_error(text)
        except Exception as exc:
            self._record_error(f"live captions stderr failed: {exc}")

    def _monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                process = self._process
            if process is None:
                return
            code = process.poll()
            if code is None:
                time.sleep(0.3)
                continue
            if not self._stop_event.is_set() and code != 0:
                exit_message = self._build_exit_error_message(code)
                if self._restart_visible_fallback(exit_message):
                    return
                self._record_error(exit_message)
            return

    def _build_exit_error_message(self, code: int) -> str:
        with self._lock:
            tail = [item for item in self._stderr_tail if item]
        message = f"live captions listener exited with code {code}"
        if tail:
            message += f"; last stderr: {' | '.join(tail[-3:])}"
        return message

    def _restart_visible_fallback(self, exit_message: str) -> bool:
        with self._lock:
            launched_at = float(self._launch_started_at or 0.0)
            show_live_captions = bool(self._current_show_live_captions)
            retry_attempted = bool(self._visible_retry_attempted)
            if show_live_captions or retry_attempted:
                return False
            if launched_at > 0.0 and (time.monotonic() - launched_at) > DEFAULT_LIVE_CAPTIONS_HELPER_RETRY_WINDOW_SECONDS:
                return False
            self._visible_retry_attempted = True

        self._record_error(f"{exit_message}; retrying with visible Live Captions window")
        try:
            self._launch_process(
                show_live_captions=True,
                restart_reason="retrying live captions with visible window",
            )
            return True
        except Exception as exc:
            self._record_error(f"live captions visible-window retry failed: {exc}")
            return False

    def _record_error(self, message: str) -> None:
        text = str(message or "").strip()
        if not text:
            return
        with self._lock:
            self._last_error = text
        self._on_error(text)

    @staticmethod
    def _extract_caption_text(line: str) -> str:
        text = str(line or "").strip()
        if not text:
            return ""
        match = re.match(r"^\[\d{2}:\d{2}:\d{2}\]\s*(.+?)\s*$", text)
        if match:
            return match.group(1).strip()
        return text


@dataclass
class AudioAgentStatus:
    listening: bool
    asr_mode: str
    pipeline_mode: str
    profile: str
    tts_backend: str
    assistant_speaking: bool
    sounddevice_available: bool
    moonshine_available: bool
    aec_available: bool
    streaming_available_modes: List[str]
    streaming_backend: str
    supports_hotwords: bool
    hotwords_count: int
    hotword_strategy: str
    current_partial: str = ""
    stable_partial: str = ""
    input_level_dbfs: float = -96.0
    input_peak_dbfs: float = -96.0
    noise_floor_dbfs: float = -72.0
    frontend_gain_db: float = 0.0
    speech_active: bool = False
    clipped_recently: bool = False
    clip_events: int = 0
    queued_input_frames: int = 0
    dropped_input_frames: int = 0
    last_error: str = ""
    live_captions_available: bool = False
    live_captions_output_path: str = ""
    live_captions_status: str = ""
    live_captions_error: str = ""
    speaker_id_enabled: bool = False
    speaker_id_ready: bool = False
    active_user_id: str = ""
    last_speaker_match: Optional[Dict[str, Any]] = None
    live_capture_enabled: bool = False
    input_device_name: str = ""
    input_device_index: int = -1
    input_device_hostapi: str = ""
    input_device_source: str = ""
    input_device_sample_rate: float = 0.0


@dataclass
class CapturedSpeechSegment:
    audio: np.ndarray
    sample_rate: int
    started_at: float
    ended_at: float
    speech_seconds: float
    caption_uses: int = 0
    last_caption_ts: float = 0.0


class DesktopAudioAgent:
    def __init__(
        self,
        *,
        log_store: ConversationLogStore,
        asr_base_url: str = DEFAULT_ASR_BASE_URL,
        piper_base_url: str = DEFAULT_PIPER_BASE_URL,
        qwen_base_url: str = DEFAULT_QWEN_BASE_URL,
        kokoro_base_url: str = DEFAULT_KOKORO_BASE_URL,
    ) -> None:
        self.log_store = log_store
        self.asr_base_url = asr_base_url.rstrip("/")
        self.piper_base_url = piper_base_url.rstrip("/")
        self.qwen_base_url = qwen_base_url.rstrip("/")
        self.kokoro_base_url = kokoro_base_url.rstrip("/")

        self.capture_sample_rate = DEFAULT_CAPTURE_SAMPLE_RATE
        self.output_sample_rate = DEFAULT_OUTPUT_SAMPLE_RATE
        self.input_blocksize = DEFAULT_INPUT_BLOCKSIZE
        self.pipeline_mode = _normalize_pipeline_mode(_env("VOICE_PIPELINE_MODE", PIPELINE_MODE_DIRECT_UNIFIED))
        self.profile = _normalize_profile(_env("VOICE_CONVERSATION_PROFILE", CONVERSATION_PROFILE_LOCAL))
        self.cloud_response_provider = _normalize_cloud_response_provider(
            _env("VOICE_CLOUD_RESPONSE_PROVIDER", RESPONSE_PROVIDER_OPENAI)
        )
        self.local_asr_mode = normalize_asr_mode(_env("VOICE_LOCAL_ASR_MODE", TRANSCRIBE_MODE_MOONSHINE_MEDIUM))
        self.cloud_asr_mode = normalize_asr_mode(_env("VOICE_CLOUD_ASR_MODE", TRANSCRIBE_MODE_API))
        self.local_streaming_asr_mode = normalize_streaming_asr_mode(
            _env("VOICE_LOCAL_STREAMING_ASR_MODE", self.local_asr_mode)
        )
        self.cloud_streaming_asr_mode = normalize_streaming_asr_mode(
            _env("VOICE_CLOUD_STREAMING_ASR_MODE", self.cloud_asr_mode)
        )
        self.current_asr_mode = self._preferred_streaming_asr_mode(self.profile)
        self.active_voice_code = _env("VOICE_AGENT_DEFAULT_VOICE", "en_US")
        self.active_tts_model = _env("VOICE_AGENT_DEFAULT_TTS_MODEL", "").strip()
        self.active_tts_backend = _normalize_tts_backend(_env("VOICE_AGENT_TTS_BACKEND", TTS_BACKEND_PIPER))
        self.active_qwen_speaker = _env("QWEN_TTS_SPEAKER", "Ryan")
        self.active_kokoro_voice = _env("KOKORO_TTS_VOICE", "af_heart")
        self.gemini_live_model = _env("GEMINI_LIVE_MODEL", DEFAULT_GEMINI_LIVE_MODEL)
        self.gemini_live_voice = _env("GEMINI_LIVE_VOICE", DEFAULT_GEMINI_LIVE_VOICE)
        self.hotword_strategy = normalize_hotword_strategy(
            _env("VOICE_ASR_HOTWORD_STRATEGY", HOTWORD_STRATEGY_COMMANDS_GAMES_MEMORY)
        )
        self.stable_partial_repeats = max(
            1,
            int(_env("VOICE_ASR_STABLE_PARTIAL_REPEATS", str(DEFAULT_STABLE_PARTIAL_REPEATS)) or DEFAULT_STABLE_PARTIAL_REPEATS),
        )

        self._assistant_buffer_text = ""
        self._assistant_corr_id = ""
        self._pending_barge_in = False
        self._pending_interrupted_text = ""
        self._pending_interrupted_corr_id = ""
        self._last_error = ""
        self._last_partial_text = ""
        self._last_stable_partial_text = ""
        self._last_assistant_spoke_at = 0.0
        self._active_user_id = ""
        self._last_speaker_match: Dict[str, Any] = {}
        self._recent_assistant_texts: Deque[Tuple[float, str]] = deque(
            maxlen=max(32, DEFAULT_LIVE_CAPTIONS_ASSISTANT_VARIANT_LIMIT)
        )
        self._speaker_id = SpeakerIdService()
        self._hotword_pack = HotwordPack()
        self._command_grammar = CommandGrammarMatcher.from_sources(
            launch_triggers=[],
            exit_keywords=[],
            manifest_path="",
        )
        self._rebuild_hotword_pack()

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client = httpx.AsyncClient(timeout=DEFAULT_HTTP_TIMEOUT)
        self._running = False
        self._listening = True
        self._manual_task: Optional[asyncio.Task[Any]] = None
        self._conversation_task: Optional[asyncio.Task[Any]] = None
        self._active_tts_tasks: set[asyncio.Task[Any]] = set()
        self._active_api_asr_tasks: set[asyncio.Task[Any]] = set()
        self._gemini_live_session_task: Optional[asyncio.Task[Any]] = None
        self._gemini_live_send_queue: Optional[asyncio.Queue[Optional[bytes]]] = None
        self._gemini_live_output_open = False
        self._gemini_live_connected = False
        self._gemini_live_output_text = ""
        self._gemini_live_logged_output_text = ""
        self._gemini_live_last_input_text = ""

        self._player = PcmPlayer(
            output_sample_rate=self.output_sample_rate,
            capture_sample_rate=self.capture_sample_rate,
        )
        self._aec = WebRtcApm(self.capture_sample_rate)
        self._frontend = AudioFrontEndProcessor(
            sample_rate_hz=self.capture_sample_rate,
            frame_size=self.input_blocksize,
        )
        self._input_buffer = AudioInputBuffer(
            frame_size=self.input_blocksize,
            max_frames=DEFAULT_INPUT_QUEUE_MAX_FRAMES,
        )
        self._input_stream = None
        self._input_stream_lock = threading.Lock()
        self._selected_input_device_index = -1
        self._selected_input_device_name = ""
        self._selected_input_device_hostapi = ""
        self._selected_input_device_source = ""
        self._selected_input_device_sample_rate = 0.0
        self._input_stream_sample_rate = float(self.capture_sample_rate)
        self._capture_worker_stop = threading.Event()
        self._capture_worker_thread: Optional[threading.Thread] = None
        self._live_captions_source = LiveCaptionsTranscriptSource(
            exe_path=_env("LIVE_CAPTIONS_LISTENER_EXE", DEFAULT_LIVE_CAPTIONS_EXE),
            output_dir=_env("LIVE_CAPTIONS_OUTPUT_DIR", DEFAULT_LIVE_CAPTIONS_OUTPUT_DIR),
            on_caption=self._on_live_captions_caption,
            on_error=self._on_live_captions_error,
        )

        self._asr_backend = None
        self._asr_backend_lock = threading.RLock()

        self._mqtt = None
        self._mqtt_connected = False
        self._mqtt_lock = threading.Lock()
        self._latest_legacy_corr_id = ""
        self._partial_commit_task: Optional[asyncio.Task[Any]] = None
        self._partial_commit_anchor_at = 0.0
        self._speech_active_last = False
        self._speech_started_at = 0.0
        self._speech_ended_at = 0.0
        self._last_partial_event_at = 0.0
        self._last_partial_text_changed_at = 0.0
        self._last_stable_partial_text_changed_at = 0.0
        self._last_final_event_at = 0.0
        self._last_user_submit_text = ""
        self._last_user_submit_at = 0.0
        self._api_asr_preroll_frames: Deque[np.ndarray] = deque(
            maxlen=max(
                1,
                int(
                    round(
                        max(0.0, DEFAULT_API_ASR_PREROLL_MS)
                        * self.capture_sample_rate
                        / max(1, self.input_blocksize)
                        / 1000.0
                    )
                ),
            )
        )
        self._api_asr_turn_active = False
        self._api_asr_turn_frames: List[np.ndarray] = []
        self._api_asr_min_samples = max(
            1,
            int(round(max(0.0, DEFAULT_API_ASR_MIN_TURN_MS) * self.capture_sample_rate / 1000.0)),
        )
        self._speaker_segment_preroll_frames: Deque[np.ndarray] = deque(
            maxlen=max(
                1,
                int(
                    round(
                        max(0.0, DEFAULT_SPEAKER_ID_PREROLL_MS)
                        * self.capture_sample_rate
                        / max(1, self.input_blocksize)
                        / 1000.0
                    )
                ),
            )
        )
        self._speaker_segment_active = False
        self._speaker_segment_frames: List[np.ndarray] = []
        self._speaker_segment_started_at = 0.0
        self._recent_speaker_segments: Deque[CapturedSpeechSegment] = deque(
            maxlen=max(1, DEFAULT_SPEAKER_ID_RECENT_SEGMENTS)
        )
        self._speaker_enrollment_requests: Deque[Tuple[str, asyncio.Future[Dict[str, Any]], float]] = deque()
        self._speaker_enrollment_lock = threading.Lock()
        self._last_enrollment_suppression_log_at = 0.0
        self._speaker_enrollment_suppress_until = 0.0

    def _preferred_asr_mode(self, profile: str) -> str:
        normalized = _normalize_profile(profile)
        if normalized == CONVERSATION_PROFILE_CLOUD:
            return normalize_asr_mode(self.cloud_asr_mode)
        return normalize_asr_mode(self.local_asr_mode)

    def _preferred_streaming_asr_mode(self, profile: str) -> str:
        normalized = _normalize_profile(profile)
        if normalized == CONVERSATION_PROFILE_CLOUD:
            return self._sanitize_streaming_mode(self.cloud_streaming_asr_mode)
        return self._sanitize_streaming_mode(self.local_streaming_asr_mode)

    def _sanitize_streaming_mode(self, mode: str) -> str:
        return normalize_streaming_asr_mode(mode)

    def _speaker_id_enabled(self) -> bool:
        return bool(self._speaker_id.enabled)

    def _speaker_capture_enabled(self) -> bool:
        if self.current_asr_mode != STREAMING_ASR_MODE_LIVE_CAPTIONS:
            return True
        if self._speaker_id_enabled():
            return True
        with self._speaker_enrollment_lock:
            return bool(self._speaker_enrollment_requests)

    def _speaker_enrollment_pending(self) -> bool:
        with self._speaker_enrollment_lock:
            while self._speaker_enrollment_requests and self._speaker_enrollment_requests[0][1].done():
                self._speaker_enrollment_requests.popleft()
            return bool(self._speaker_enrollment_requests)

    def _remove_speaker_enrollment_future(self, future: "asyncio.Future[Dict[str, Any]]") -> None:
        with self._speaker_enrollment_lock:
            self._speaker_enrollment_requests = deque(
                [item for item in self._speaker_enrollment_requests if item[1] is not future]
            )

    def _requeue_speaker_enrollment_request(
        self,
        request: Tuple[str, "asyncio.Future[Dict[str, Any]]", float],
    ) -> None:
        with self._speaker_enrollment_lock:
            future = request[1]
            if future.done():
                return
            self._speaker_enrollment_requests.appendleft(request)

    def _arm_speaker_enrollment_suppression(self, *, seconds: float) -> None:
        duration = max(0.0, float(seconds))
        if duration <= 0.0:
            return
        self._speaker_enrollment_suppress_until = max(
            float(self._speaker_enrollment_suppress_until or 0.0),
            time.time() + duration,
        )

    def _suppress_transcript_during_enrollment(self, *, source: str, text: str = "") -> bool:
        pending = self._speaker_enrollment_pending()
        suppress_until = float(self._speaker_enrollment_suppress_until or 0.0)
        if not pending and time.time() >= suppress_until:
            return False
        now = time.time()
        if (now - float(self._last_enrollment_suppression_log_at or 0.0)) >= 1.5:
            self._last_enrollment_suppression_log_at = now
            normalized = str(text or "").strip()
            detail = f" ({normalized})" if normalized else ""
            state = "active" if pending else "cooldown"
            self.log_store.add(
                "system",
                f"speaker enrollment {state}; ignored transcript from {source}{detail}",
                speaker="system",
                source="speaker_enrollment",
            )
        return True

    def _update_last_speaker_match(
        self,
        result: SpeakerMatchResult,
        *,
        source: str,
        extras: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload = result.to_payload()
        payload["profile_candidate_count"] = int(payload.get("candidate_count") or 0)
        payload["source"] = str(source or "")
        payload["ts"] = time.time()
        if extras:
            payload.update(extras)
        self._last_speaker_match = payload
        self._active_user_id = str(result.user_id or "").strip() if result.matched else ""

    def _speaker_match_payload(self) -> Dict[str, Any]:
        return dict(self._last_speaker_match or {})

    def _sounddevice_hostapi_name(self, hostapi_index: Any) -> str:
        if sd is None:
            return ""
        try:
            index = int(hostapi_index)
            hostapi = sd.query_hostapis(index)
            if isinstance(hostapi, dict):
                return str(hostapi.get("name") or "")
        except Exception:
            return ""
        return ""

    @staticmethod
    def _normalize_audio_device_name(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip()).casefold()

    def _device_details_for_index(self, index: Any) -> Tuple[int, str, str, float]:
        if sd is None:
            return -1, "", "", 0.0
        try:
            resolved_index = int(index)
            device_info = sd.query_devices(resolved_index)
        except Exception:
            return -1, "", "", 0.0
        if not isinstance(device_info, dict):
            return resolved_index, "", "", 0.0
        return (
            resolved_index,
            str(device_info.get("name") or ""),
            self._sounddevice_hostapi_name(device_info.get("hostapi")),
            float(device_info.get("default_samplerate") or 0.0),
        )

    def _resolve_preferred_input_device(self) -> Tuple[int, str, str, str]:
        if sd is None:
            return -1, "", "", ""
        env_index_raw = str(DEFAULT_INPUT_DEVICE_INDEX or "").strip()
        if env_index_raw:
            try:
                env_index = int(env_index_raw)
            except Exception:
                env_index = -1
            if env_index >= 0:
                resolved_index, name, hostapi, _sample_rate = self._device_details_for_index(env_index)
                if resolved_index >= 0:
                    return resolved_index, name, hostapi, "env_index"

        env_name = self._normalize_audio_device_name(DEFAULT_INPUT_DEVICE_NAME)
        if env_name:
            try:
                devices = list(sd.query_devices() or [])
            except Exception:
                devices = []
            best_match: Tuple[int, str, str, int] = (-1, "", "", 99)
            for index, device_info in enumerate(devices):
                if not isinstance(device_info, dict):
                    continue
                if int(device_info.get("max_input_channels") or 0) <= 0:
                    continue
                device_name = str(device_info.get("name") or "")
                normalized_name = self._normalize_audio_device_name(device_name)
                if not normalized_name:
                    continue
                rank = 99
                if normalized_name == env_name:
                    rank = 0
                elif env_name in normalized_name:
                    rank = 1
                elif normalized_name in env_name:
                    rank = 2
                if rank >= best_match[3]:
                    continue
                best_match = (index, device_name, self._sounddevice_hostapi_name(device_info.get("hostapi")), rank)
            if best_match[0] >= 0:
                return best_match[0], best_match[1], best_match[2], "env_name"

        if os.name == "nt":
            try:
                hostapis = list(sd.query_hostapis() or [])
            except Exception:
                hostapis = []
            for hostapi in hostapis:
                if not isinstance(hostapi, dict):
                    continue
                hostapi_name = str(hostapi.get("name") or "")
                if "wasapi" not in hostapi_name.casefold():
                    continue
                default_input = int(hostapi.get("default_input_device") or -1)
                if default_input >= 0:
                    resolved_index, name, resolved_hostapi, _sample_rate = self._device_details_for_index(default_input)
                    if resolved_index >= 0:
                        return resolved_index, name, resolved_hostapi or hostapi_name, "windows_default_wasapi"

        try:
            default_device = getattr(getattr(sd, "default", None), "device", None)
            if isinstance(default_device, (list, tuple)) and default_device:
                default_input = default_device[0]
            else:
                default_input = default_device
            resolved_index, name, hostapi, _sample_rate = self._device_details_for_index(default_input)
            if resolved_index >= 0:
                return resolved_index, name, hostapi, "sounddevice_default"
        except Exception:
            pass
        return -1, "", "", ""

    def _input_device_details(self) -> Tuple[int, str, str, str]:
        if sd is None:
            return -1, "", "", ""
        try:
            with self._input_stream_lock:
                stream = self._input_stream
            if self._selected_input_device_index >= 0:
                return (
                    int(self._selected_input_device_index),
                    str(self._selected_input_device_name or ""),
                    str(self._selected_input_device_hostapi or ""),
                    str(self._selected_input_device_source or ""),
                )
            device_value = getattr(stream, "device", None) if stream is not None else None
            if isinstance(device_value, (list, tuple)) and device_value:
                input_device = device_value[0]
            elif device_value is not None:
                input_device = device_value
            else:
                default_device = getattr(sd, "default", None)
                default_pair = getattr(default_device, "device", None)
                if isinstance(default_pair, (list, tuple)) and default_pair:
                    input_device = default_pair[0]
                else:
                    input_device = default_pair
            index, name, hostapi, _sample_rate = self._device_details_for_index(input_device)
            return index, name, hostapi, "stream_runtime"
        except Exception:
            return -1, "", "", ""

    def _preferred_input_stream_config(self) -> Tuple[int, str, str, str, float, int]:
        device_index, device_name, device_hostapi, device_source = self._resolve_preferred_input_device()
        device_sample_rate = float(self.capture_sample_rate)
        if device_index >= 0:
            _resolved_index, _resolved_name, _resolved_hostapi, resolved_sample_rate = self._device_details_for_index(device_index)
            if resolved_sample_rate > 0.0:
                device_sample_rate = float(resolved_sample_rate)
        if device_sample_rate <= 0.0:
            device_sample_rate = float(self.capture_sample_rate)
        stream_blocksize = max(
            1,
            int(round(float(self.input_blocksize) * float(device_sample_rate) / float(max(1, self.capture_sample_rate)))),
        )
        return (
            int(device_index),
            str(device_name or ""),
            str(device_hostapi or ""),
            str(device_source or ""),
            float(device_sample_rate),
            int(stream_blocksize),
        )

    def _remember_speaker_segment(self, audio: np.ndarray, *, started_at: float, ended_at: float) -> None:
        segment_audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        if segment_audio.size <= 0:
            return
        segment = CapturedSpeechSegment(
            audio=segment_audio.copy(),
            sample_rate=self.capture_sample_rate,
            started_at=float(started_at),
            ended_at=float(ended_at),
            speech_seconds=segment_audio.size / float(max(1, self.capture_sample_rate)),
        )
        self._recent_speaker_segments.append(segment)
        if self._loop is None:
            return
        with self._speaker_enrollment_lock:
            has_pending = bool(self._speaker_enrollment_requests)
        if has_pending:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._consume_next_enrollment_segment(segment))
            )

    def _update_speaker_capture(self, audio: np.ndarray, *, speech_active: bool) -> None:
        if not self._speaker_capture_enabled():
            return
        frame_copy = np.asarray(audio, dtype=np.float32).reshape(-1).copy()
        if frame_copy.size <= 0:
            return
        now = time.time()
        if speech_active and not self.is_assistant_speaking():
            if not self._speaker_segment_active:
                self._speaker_segment_frames = [
                    np.asarray(item, dtype=np.float32).copy() for item in self._speaker_segment_preroll_frames
                ]
                self._speaker_segment_active = True
                self._speaker_segment_started_at = now
                self._speaker_segment_preroll_frames.clear()
            self._speaker_segment_frames.append(frame_copy)
            return
        if self._speaker_segment_active:
            self._speaker_segment_frames.append(frame_copy)
            full_audio = (
                np.concatenate(self._speaker_segment_frames).astype(np.float32, copy=False)
                if self._speaker_segment_frames
                else np.zeros(0, dtype=np.float32)
            )
            started_at = float(self._speaker_segment_started_at or now)
            self._speaker_segment_active = False
            self._speaker_segment_frames = []
            self._speaker_segment_started_at = 0.0
            self._speaker_segment_preroll_frames.clear()
            if full_audio.size > 0:
                self._remember_speaker_segment(full_audio, started_at=started_at, ended_at=now)
            return
        self._speaker_segment_preroll_frames.append(frame_copy)

    def _recent_speaker_segment_candidates(
        self,
        *,
        reference_ts: float,
        max_age_seconds: float,
        fallback_age_seconds: float,
        max_candidates: int,
    ) -> List[Tuple[CapturedSpeechSegment, bool, float]]:
        now = float(reference_ts or time.time())
        strict_age = max(0.1, float(max_age_seconds))
        fallback_age = max(strict_age, float(fallback_age_seconds))
        prune_age = max(fallback_age + 5.0, strict_age + 5.0)
        while self._recent_speaker_segments:
            age = now - float(self._recent_speaker_segments[0].ended_at)
            if age <= prune_age:
                break
            self._recent_speaker_segments.popleft()
        strict_candidates: List[Tuple[CapturedSpeechSegment, bool, float]] = []
        fallback_candidates: List[Tuple[CapturedSpeechSegment, bool, float]] = []
        for segment in reversed(self._recent_speaker_segments):
            age = now - float(segment.ended_at)
            if age < -0.35:
                continue
            item = (segment, False, age)
            if age <= strict_age:
                strict_candidates.append(item)
            elif age <= fallback_age:
                fallback_candidates.append((segment, True, age))
        candidates = strict_candidates + fallback_candidates
        candidates.sort(key=lambda item: (item[0].caption_uses > 0, item[2]))
        return candidates[: max(1, int(max_candidates))]

    def _reset_api_asr_turn(self, *, clear_preroll: bool = False) -> None:
        self._api_asr_turn_active = False
        self._api_asr_turn_frames = []
        if clear_preroll:
            self._api_asr_preroll_frames.clear()

    def _cancel_api_asr_tasks(self) -> None:
        for task in list(self._active_api_asr_tasks):
            task.cancel()
        self._active_api_asr_tasks.clear()

    def _reload_provider_settings_from_env(self) -> None:
        self.cloud_response_provider = _normalize_cloud_response_provider(
            _env("VOICE_CLOUD_RESPONSE_PROVIDER", RESPONSE_PROVIDER_OPENAI)
        )
        self.active_tts_backend = _normalize_tts_backend(_env("VOICE_AGENT_TTS_BACKEND", self.active_tts_backend))
        self.active_qwen_speaker = _env("QWEN_TTS_SPEAKER", self.active_qwen_speaker)
        self.active_kokoro_voice = _env("KOKORO_TTS_VOICE", self.active_kokoro_voice)
        self.gemini_live_model = _env("GEMINI_LIVE_MODEL", DEFAULT_GEMINI_LIVE_MODEL)
        self.gemini_live_voice = _env("GEMINI_LIVE_VOICE", DEFAULT_GEMINI_LIVE_VOICE)
        self._speaker_id.reload_from_env(memory_path=_env("DIALOG_USER_MEMORY_PATH", ""))

    def _close_gemini_live_output(self, *, clear_player: bool) -> None:
        if self._gemini_live_output_open:
            self._player.end_stream()
            self._gemini_live_output_open = False
        if clear_player:
            self._player.clear()

    def _stop_gemini_live_session(self) -> None:
        queue = self._gemini_live_send_queue
        self._gemini_live_send_queue = None
        if queue is not None:
            try:
                queue.put_nowait(None)
            except Exception:
                pass
        task = self._gemini_live_session_task
        self._gemini_live_session_task = None
        if task is not None:
            task.cancel()
        self._gemini_live_connected = False
        self._close_gemini_live_output(clear_player=True)
        self._assistant_buffer_text = ""
        self._assistant_corr_id = ""
        self._gemini_live_output_text = ""
        self._gemini_live_logged_output_text = ""
        self._last_partial_text = ""
        self._last_stable_partial_text = ""

    def _start_gemini_live_session(self) -> None:
        self._stop_gemini_live_session()
        if self._loop is None:
            return
        self._reload_provider_settings_from_env()
        if genai is None or genai_types is None:
            self._last_error = "Gemini Live requires dependency 'google-genai'"
            return
        api_key = _gemini_api_key()
        if not api_key:
            self._last_error = "Gemini key is not set (expected GEMINI_API_KEY or GEMINI_KEY)"
            return
        self._gemini_live_send_queue = asyncio.Queue(maxsize=max(8, DEFAULT_GEMINI_LIVE_QUEUE_MAX_FRAMES))
        self._gemini_live_session_task = asyncio.create_task(self._run_gemini_live_supervisor(api_key))
        self._last_error = ""

    def _queue_gemini_live_audio(self, raw_pcm: bytes) -> None:
        queue = self._gemini_live_send_queue
        if queue is None:
            return
        payload = bytes(raw_pcm or b"")
        if not payload:
            return
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            self._last_error = "Gemini Live audio queue overflow; dropping input audio"

    def _handle_gemini_live_frame(self, audio: np.ndarray) -> None:
        frame = np.asarray(audio, dtype=np.float32).reshape(-1)
        if frame.size <= 0 or self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._queue_gemini_live_audio, _float32_to_pcm16_bytes(frame))

    async def _run_gemini_live_supervisor(self, api_key: str) -> None:
        backoff_seconds = 1.0
        while self._running and self.current_asr_mode == STREAMING_ASR_MODE_GEMINI_LIVE:
            try:
                await self._run_gemini_live_session_once(api_key)
                if not self._running or self.current_asr_mode != STREAMING_ASR_MODE_GEMINI_LIVE:
                    return
                await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._gemini_live_connected = False
                self._last_error = f"Gemini Live session failed: {exc}"
                self.log_store.add("system", self._last_error, speaker="system", source="gemini_live")
                if not self._running or self.current_asr_mode != STREAMING_ASR_MODE_GEMINI_LIVE:
                    return
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(5.0, backoff_seconds + 1.0)

    async def _run_gemini_live_session_once(self, api_key: str) -> None:
        client = genai.Client(api_key=api_key)
        config = genai_types.LiveConnectConfig(
            response_modalities=[genai_types.Modality.AUDIO],
            system_instruction=DEFAULT_GEMINI_LIVE_SYSTEM_PROMPT,
            input_audio_transcription=genai_types.AudioTranscriptionConfig(),
            output_audio_transcription=genai_types.AudioTranscriptionConfig(),
            realtime_input_config=genai_types.RealtimeInputConfig(
                automatic_activity_detection=genai_types.AutomaticActivityDetection(
                    prefix_padding_ms=180,
                    silence_duration_ms=320,
                    start_of_speech_sensitivity=genai_types.StartSensitivity.START_SENSITIVITY_HIGH,
                    end_of_speech_sensitivity=genai_types.EndSensitivity.END_SENSITIVITY_LOW,
                ),
                activity_handling=genai_types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
                turn_coverage=genai_types.TurnCoverage.TURN_INCLUDES_ALL_INPUT,
            ),
        )
        voice_name = str(self.gemini_live_voice or "").strip()
        if voice_name:
            config.speech_config = genai_types.SpeechConfig(
                voice_config=genai_types.VoiceConfig(
                    prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(voice_name=voice_name)
                )
            )

        try:
            async with client.aio.live.connect(model=self.gemini_live_model, config=config) as session:
                self._gemini_live_connected = True
                self._last_error = ""
                sender = asyncio.create_task(self._gemini_live_sender(session))
                receiver = asyncio.create_task(self._gemini_live_receiver(session))
                try:
                    done, pending = await asyncio.wait(
                        {sender, receiver},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    for task in done:
                        task.result()
                finally:
                    for task in (sender, receiver):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(sender, receiver, return_exceptions=True)
                    self._gemini_live_connected = False
                    self._close_gemini_live_output(clear_player=False)
        finally:
            try:
                await client.aio.aclose()
            except Exception:
                pass

    async def _gemini_live_sender(self, session: Any) -> None:
        while self._running and self.current_asr_mode == STREAMING_ASR_MODE_GEMINI_LIVE:
            queue = self._gemini_live_send_queue
            if queue is None:
                return
            payload = await queue.get()
            if payload is None:
                return
            await session.send_realtime_input(
                audio=genai_types.Blob(
                    data=payload,
                    mime_type=f"audio/pcm;rate={self.capture_sample_rate}",
                )
            )

    async def _gemini_live_receiver(self, session: Any) -> None:
        async for message in session.receive():
            server_content = getattr(message, "server_content", None)
            if server_content is None:
                continue
            await self._handle_gemini_live_server_content(server_content)

    async def _handle_gemini_live_server_content(self, server_content: Any) -> None:
        input_transcription = getattr(server_content, "input_transcription", None)
        if input_transcription is not None:
            await self._handle_gemini_live_input_transcription(input_transcription)

        output_transcription = getattr(server_content, "output_transcription", None)
        if output_transcription is not None:
            self._handle_gemini_live_output_transcription(output_transcription)

        model_turn = getattr(server_content, "model_turn", None)
        if model_turn is not None:
            for part in getattr(model_turn, "parts", []) or []:
                inline_data = getattr(part, "inline_data", None)
                if inline_data is None:
                    inline_data = getattr(part, "inlineData", None)
                raw_audio = getattr(inline_data, "data", b"") if inline_data is not None else b""
                if not raw_audio:
                    continue
                mime_type = str(
                    getattr(inline_data, "mime_type", "") or getattr(inline_data, "mimeType", "") or ""
                ).strip()
                if "audio/pcm" not in mime_type.lower():
                    continue
                if not self._gemini_live_output_open:
                    self._player.begin_stream()
                    self._gemini_live_output_open = True
                self._player.enqueue_pcm16(
                    raw_audio,
                    _parse_pcm_sample_rate(mime_type, DEFAULT_GEMINI_LIVE_OUTPUT_SAMPLE_RATE),
                )

        if bool(getattr(server_content, "interrupted", False)):
            interrupted_text = self._sanitize_assistant_text(
                self._assistant_buffer_text or self._gemini_live_output_text or ""
            )
            if interrupted_text and interrupted_text != self._gemini_live_logged_output_text:
                self._remember_assistant_text(interrupted_text)
                self.log_store.add(
                    "coach",
                    interrupted_text,
                    speaker="RACHEL",
                    source="gemini_live_partial",
                    metadata="interrupted",
                )
                self._gemini_live_logged_output_text = interrupted_text
            self._assistant_buffer_text = ""
            self._assistant_corr_id = ""
            self._gemini_live_output_text = ""
            self._close_gemini_live_output(clear_player=True)

        if bool(getattr(server_content, "turn_complete", False)):
            if self._gemini_live_output_open:
                await self._wait_for_playback_drain()
            self._close_gemini_live_output(clear_player=False)
            final_text = self._sanitize_assistant_text(self._assistant_buffer_text or self._gemini_live_output_text or "")
            if final_text and final_text != self._gemini_live_logged_output_text:
                self._remember_assistant_text(final_text)
                self.log_store.add("coach", final_text, speaker="RACHEL", source="gemini_live")
                self._gemini_live_logged_output_text = final_text
            self._assistant_buffer_text = ""
            self._assistant_corr_id = ""
            self._gemini_live_output_text = ""

    async def _handle_gemini_live_input_transcription(self, transcription: Any) -> None:
        text = str(getattr(transcription, "text", "") or "").strip()
        finished = bool(getattr(transcription, "finished", False))
        if not finished:
            self._last_partial_text = text
            self._last_stable_partial_text = text
            return
        self._last_partial_text = ""
        self._last_stable_partial_text = ""
        if not text:
            return
        await self._handle_gemini_live_user_turn(text)

    def _handle_gemini_live_output_transcription(self, transcription: Any) -> None:
        text = str(getattr(transcription, "text", "") or "").strip()
        finished = bool(getattr(transcription, "finished", False))
        if not text:
            return
        self._assistant_buffer_text = text
        self._gemini_live_output_text = text
        if finished:
            clean_text = self._sanitize_assistant_text(text)
            if clean_text and clean_text != self._gemini_live_logged_output_text:
                self._assistant_buffer_text = clean_text
                self._gemini_live_output_text = clean_text
                self._remember_assistant_text(clean_text)
                self.log_store.add("coach", clean_text, speaker="RACHEL", source="gemini_live")
                self._gemini_live_logged_output_text = clean_text

    async def _handle_gemini_live_user_turn(self, raw_text: str) -> None:
        normalized = str(raw_text or "").strip()
        if not normalized or not self._listening:
            return
        now = time.time()
        if normalized == self._gemini_live_last_input_text and (now - self._last_final_event_at) <= 1.0:
            return
        self._gemini_live_last_input_text = normalized
        self._last_final_event_at = now
        if normalized == self._last_user_submit_text and (now - self._last_user_submit_at) <= 2.0:
            return
        self._last_user_submit_text = normalized
        self._last_user_submit_at = now

        grammar_match = self._command_grammar.canonicalize(normalized)
        final_text = str(grammar_match.canonical_text or normalized).strip()
        metadata_parts: List[str] = ["asr=gemini_live:high"]
        if grammar_match.route_type and grammar_match.route_type != "QUERY":
            metadata_parts.append(f"grammar={grammar_match.route_type}:{grammar_match.confidence:.2f}")
        self.log_store.add(
            "user",
            final_text,
            speaker="User",
            source="gemini_live",
            metadata=" | ".join(metadata_parts),
        )
        if grammar_match.route_type not in {"LAUNCH_GAME", "BACK_HOME"} or grammar_match.confidence < 0.86:
            return
        payload: Dict[str, Any] = {
            "type": grammar_match.route_type,
            "source": "gemini_live",
            "corr_id": uuid.uuid4().hex,
            "ts": int(time.time() * 1000),
            "text": final_text,
        }
        if grammar_match.route_type == "LAUNCH_GAME" and grammar_match.game_name:
            payload["game_name"] = grammar_match.game_name
        try:
            await self._publish_mqtt("robot/intent", payload)
        except Exception as exc:
            self.log_store.add(
                "system",
                f"Gemini Live command dispatch failed: {exc}",
                speaker="system",
                source="gemini_live",
            )

    def _current_hotword_entries(self) -> List[HotwordEntry]:
        if self.hotword_strategy == HOTWORD_STRATEGY_OFF:
            return []

        entries: List[HotwordEntry] = []
        seen = set()

        def add_entry(phrase: str, *aliases: str) -> None:
            canonical = str(phrase or "").strip()
            if not _should_include_hotword(canonical):
                return
            key = canonical.casefold()
            if key in seen:
                return
            seen.add(key)
            alias_values = [
                str(item or "").strip()
                for item in aliases
                if _should_include_hotword(str(item or "").strip())
            ]
            entries.append(HotwordEntry(phrase=canonical, aliases=alias_values))

        launch_triggers = _coerce_string_list(_env("INTENT_LAUNCH_TRIGGERS", ""))
        exit_keywords = _coerce_string_list(_env("INTENT_EXIT_KEYWORDS", ""))
        if not launch_triggers:
            launch_triggers = ["open", "start", "launch", "play", "begin", "load"]
        if not exit_keywords:
            exit_keywords = ["back home", "go home", "return home", "go back", "quit", "exit", "close game"]
        for phrase in launch_triggers:
            add_entry(phrase)
        for phrase in exit_keywords:
            add_entry(phrase)
        wake_word = str(_env("WAKE_WORD", "rachel") or "rachel").strip()
        wake_word_aliases = _coerce_string_list(
            _env(
                "WAKE_WORD_ALIASES",
                "rachel, rachael, richel, richelle, rachal, raychel, ra chel, rach el",
            )
        )
        if wake_word:
            add_entry(wake_word, *wake_word_aliases)

        if self.hotword_strategy in {HOTWORD_STRATEGY_COMMANDS_GAMES, HOTWORD_STRATEGY_COMMANDS_GAMES_MEMORY}:
            manifest_path = _env("INTENT_MANIFEST_PATH", "") or _env("GAME_LAUNCHER_MANIFEST_PATH", "")
            if manifest_path:
                try:
                    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8-sig"))
                except Exception:
                    manifest = {}
                for item in manifest.get("games", []) if isinstance(manifest, dict) else []:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or item.get("id") or "").strip()
                    aliases = _coerce_string_list(item.get("synonyms") or item.get("keywords") or [])
                    if name:
                        if " " in name:
                            aliases.append(name.replace(" ", ""))
                        add_entry(name, *aliases)

        if self.hotword_strategy == HOTWORD_STRATEGY_COMMANDS_GAMES_MEMORY:
            memory_path = _env("DIALOG_USER_MEMORY_PATH", "")
            if memory_path:
                try:
                    memory_root = json.loads(Path(memory_path).read_text(encoding="utf-8-sig"))
                except Exception:
                    memory_root = {}
                profiles = memory_root.get("profiles", {}) if isinstance(memory_root, dict) else {}
                if isinstance(profiles, dict):
                    for profile in profiles.values():
                        if not isinstance(profile, dict):
                            continue
                        add_entry(str(profile.get("name") or "").strip())
                        add_entry(str(profile.get("display_name") or "").strip())
                        add_entry(str(profile.get("favorite_game") or "").strip())

        return entries

    def _rebuild_hotword_pack(self) -> None:
        self._hotword_pack = HotwordPack(entries=self._current_hotword_entries())

    def _rebuild_command_grammar(self) -> None:
        launch_triggers = _coerce_string_list(_env("INTENT_LAUNCH_TRIGGERS", ""))
        exit_keywords = _coerce_string_list(_env("INTENT_EXIT_KEYWORDS", ""))
        manifest_path = _env("INTENT_MANIFEST_PATH", "") or _env("GAME_LAUNCHER_MANIFEST_PATH", "")
        self._command_grammar = CommandGrammarMatcher.from_sources(
            launch_triggers=launch_triggers,
            exit_keywords=exit_keywords,
            manifest_path=manifest_path,
        )

    def _close_asr_backend(self) -> None:
        with self._asr_backend_lock:
            backend = self._asr_backend
            self._asr_backend = None
        if backend is None:
            return
        try:
            backend.stop()
        except Exception:
            pass

    def _rebuild_asr_backend(self) -> None:
        self._close_asr_backend()
        self._cancel_api_asr_tasks()
        self._reset_api_asr_turn(clear_preroll=True)
        self._stop_gemini_live_session()
        self._reload_provider_settings_from_env()
        self._rebuild_hotword_pack()
        self._rebuild_command_grammar()
        if self.current_asr_mode == STREAMING_ASR_MODE_LIVE_CAPTIONS:
            self._last_error = ""
            self._last_partial_text = ""
            self._last_stable_partial_text = ""
            return
        if self.current_asr_mode == STREAMING_ASR_MODE_GEMINI_LIVE:
            self._last_error = ""
            self._last_partial_text = ""
            self._last_stable_partial_text = ""
            self._start_gemini_live_session()
            return
        if self.current_asr_mode == STREAMING_ASR_MODE_API:
            self._last_error = ""
            self._last_partial_text = ""
            self._last_stable_partial_text = ""
            return
        try:
            backend = create_streaming_asr_backend(
                mode=self.current_asr_mode,
                stable_partial_repeats=self.stable_partial_repeats,
                hotword_pack=self._hotword_pack,
                on_event=self._on_asr_backend_event,
                on_error=self._on_asr_backend_error,
            )
            backend.start()
            with self._asr_backend_lock:
                self._asr_backend = backend
            self._last_error = ""
            self._last_partial_text = ""
            self._last_stable_partial_text = ""
        except Exception as exc:
            self._last_error = f"failed to initialize streaming ASR backend: {exc}"
            logger.warning(self._last_error)
            fallback_mode = normalize_streaming_asr_mode(TRANSCRIBE_MODE_MOONSHINE_MEDIUM)
            if self.current_asr_mode == fallback_mode:
                return
            try:
                backend = create_streaming_asr_backend(
                    mode=fallback_mode,
                    stable_partial_repeats=self.stable_partial_repeats,
                    hotword_pack=self._hotword_pack,
                    on_event=self._on_asr_backend_event,
                    on_error=self._on_asr_backend_error,
                )
                backend.start()
                with self._asr_backend_lock:
                    self._asr_backend = backend
                self.current_asr_mode = fallback_mode
                self._last_error = f"{self._last_error}; fell back to {fallback_mode}"
            except Exception as fallback_exc:
                self._last_error = f"{self._last_error}; fallback failed: {fallback_exc}"

    def _on_asr_backend_error(self, message: str) -> None:
        self._last_error = str(message or "").strip()
        if self._loop is None or not self._last_error:
            return
        self._loop.call_soon_threadsafe(
            lambda: self.log_store.add("system", self._last_error, speaker="system", source="streaming_asr")
        )

    def _on_live_captions_error(self, message: str) -> None:
        self._last_error = str(message or "").strip()
        if self._loop is None or not self._last_error:
            return
        self._loop.call_soon_threadsafe(
            lambda: self.log_store.add("system", self._last_error, speaker="system", source="live_captions")
        )

    async def _resolve_recent_speaker_user(self, *, source: str, observed_at: Optional[float] = None) -> Tuple[str, str]:
        if not self._speaker_id_enabled():
            return "", "none"
        reference_ts = float(observed_at or time.time())
        candidates = self._recent_speaker_segment_candidates(
            reference_ts=reference_ts,
            max_age_seconds=DEFAULT_SPEAKER_ID_SEGMENT_MAX_AGE_SECONDS,
            fallback_age_seconds=DEFAULT_SPEAKER_ID_SEGMENT_FALLBACK_AGE_SECONDS,
            max_candidates=DEFAULT_SPEAKER_ID_LIVE_CAPTIONS_MAX_CANDIDATES,
        )
        strict_candidates = [item for item in candidates if not item[1]]
        fallback_candidates = [item for item in candidates if item[1]]
        candidate_ages = [float(item[2]) for item in candidates]
        diagnostic_extras = {
            "segment_candidate_count": len(candidates),
            "strict_segment_candidate_count": len(strict_candidates),
            "fallback_segment_candidate_count": len(fallback_candidates),
        }
        if candidate_ages:
            diagnostic_extras["freshest_segment_age_seconds"] = round(min(candidate_ages), 4)
            diagnostic_extras["oldest_segment_age_seconds"] = round(max(candidate_ages), 4)
        if not candidates:
            self._update_last_speaker_match(
                SpeakerMatchResult(reason="no_recent_segment"),
                source=source,
                extras=diagnostic_extras,
            )
            return "", "none"
        evaluation_candidates = strict_candidates
        selection_window = "strict"
        if not evaluation_candidates:
            freshest_fallback_age = min((float(item[2]) for item in fallback_candidates), default=float("inf"))
            stale_fallback_seconds = max(
                DEFAULT_SPEAKER_ID_SEGMENT_MAX_AGE_SECONDS,
                min(DEFAULT_SPEAKER_ID_SEGMENT_FALLBACK_AGE_SECONDS, DEFAULT_SPEAKER_ID_STALE_FALLBACK_SECONDS),
            )
            diagnostic_extras["stale_fallback_age_limit_seconds"] = round(float(stale_fallback_seconds), 4)
            if freshest_fallback_age > stale_fallback_seconds:
                diagnostic_extras["segment_used_fallback_window"] = True
                diagnostic_extras["segment_age_seconds"] = round(float(freshest_fallback_age), 4)
                self._update_last_speaker_match(
                    SpeakerMatchResult(reason="stale_fallback_segment"),
                    source=source,
                    extras=diagnostic_extras,
                )
                return "", "none"
            evaluation_candidates = fallback_candidates
            selection_window = "fallback"
        best_result: Optional[SpeakerMatchResult] = None
        best_segment: Optional[CapturedSpeechSegment] = None
        best_fallback = selection_window == "fallback"
        best_age = 0.0
        best_rank = -1
        for index, (segment, _used_fallback, age) in enumerate(evaluation_candidates):
            result = await asyncio.to_thread(self._speaker_id.match_audio, segment.audio, segment.sample_rate)
            if best_result is None:
                best_result = result
                best_segment = segment
                best_age = age
                best_rank = index
                continue
            replace = False
            if result.matched and not best_result.matched:
                replace = True
            elif result.matched == best_result.matched:
                if float(result.score) > float(best_result.score) + 1e-6:
                    replace = True
                elif abs(float(result.score) - float(best_result.score)) <= 1e-6 and age < best_age:
                    replace = True
            if replace:
                best_result = result
                best_segment = segment
                best_age = age
                best_rank = index
        assert best_result is not None
        extras = {
            **diagnostic_extras,
            "segment_age_seconds": round(float(best_age), 4),
            "segment_used_fallback_window": bool(best_fallback),
            "segment_selection_window": selection_window,
            "segment_candidate_rank": int(best_rank),
        }
        if best_segment is not None:
            best_segment.caption_uses += 1
            best_segment.last_caption_ts = reference_ts
            extras["segment_duration_seconds"] = round(float(best_segment.speech_seconds), 4)
        self._update_last_speaker_match(best_result, source=source, extras=extras)
        if best_result.matched:
            return str(best_result.user_id or "").strip(), "auto"
        return "", "none"

    async def _consume_next_enrollment_segment(self, segment: CapturedSpeechSegment) -> None:
        if self._loop is None:
            return
        request: Optional[Tuple[str, asyncio.Future[Dict[str, Any]], float]] = None
        with self._speaker_enrollment_lock:
            while self._speaker_enrollment_requests:
                candidate = self._speaker_enrollment_requests[0]
                if candidate[1].done():
                    self._speaker_enrollment_requests.popleft()
                    continue
                request = self._speaker_enrollment_requests.popleft()
                break
        if request is None:
            return
        user_id, future, _created_at = request
        if future.done():
            return
        try:
            summary = await asyncio.to_thread(
                self._speaker_id.add_pending_clip,
                user_id,
                segment.audio,
                segment.sample_rate,
            )
            self._arm_speaker_enrollment_suppression(seconds=DEFAULT_SPEAKER_ID_ENROLL_SUPPRESS_SECONDS)
            future.set_result(summary)
        except ValueError as exc:
            self.log_store.add(
                "system",
                f"ignored invalid enrollment clip for {user_id}: {exc}",
                speaker="system",
                source="speaker_enrollment",
            )
            self._requeue_speaker_enrollment_request(request)
        except Exception as exc:
            future.set_exception(exc)

    def _on_live_captions_caption(self, text: str, observed_at: float) -> None:
        if self._loop is None:
            return
        normalized = str(text or "").strip()
        if not normalized:
            return
        observed_ts = float(observed_at or time.time())

        async def _forward() -> None:
            if self._suppress_transcript_during_enrollment(source="live_captions", text=normalized):
                return
            user_id, identity_resolution = await self._resolve_recent_speaker_user(
                source="live_captions",
                observed_at=observed_ts,
            )
            await self._handle_external_transcript_final(
                normalized,
                source="live_captions",
                user_id=user_id,
                identity_resolution=identity_resolution,
            )

        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(_forward())
        )

    def _on_asr_backend_event(self, event: AsrEvent) -> None:
        if self._loop is None:
            return
        if event.event_type == "started":
            self._loop.call_soon_threadsafe(self._handle_streaming_started, event)
            return
        if event.event_type == "partial":
            self._loop.call_soon_threadsafe(self._handle_streaming_partial, event)
            return
        if event.event_type == "final":
            self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._handle_streaming_final(event)))

    async def start(self) -> None:
        if self._running:
            return
        self._loop = asyncio.get_running_loop()
        self._running = True
        self._last_error = ""
        await self._ensure_mqtt()
        self._rebuild_asr_backend()
        self._start_input_stream()
        try:
            self._player.start()
        except Exception as exc:
            self._last_error = f"audio output start failed: {exc}"
        self.log_store.add("system", "desktop audio agent started", source="desktop_runtime")

    async def stop(self) -> None:
        self._running = False
        self._cancel_partial_commit()
        self._speech_active_last = False
        self._reset_api_asr_turn(clear_preroll=True)
        self.cancel_current_turn(reason="shutdown", capture_barge_in=False)
        if self._manual_task is not None:
            self._manual_task.cancel()
            self._manual_task = None
        self._cancel_api_asr_tasks()
        self._stop_gemini_live_session()
        self._stop_input_stream()
        self._close_asr_backend()
        self._player.stop()
        self._aec.close()
        with self._speaker_enrollment_lock:
            while self._speaker_enrollment_requests:
                _user_id, future, _created_at = self._speaker_enrollment_requests.popleft()
                if not future.done():
                    future.cancel()
        self._recent_speaker_segments.clear()
        self._speaker_segment_preroll_frames.clear()
        self._speaker_segment_active = False
        self._speaker_segment_frames = []
        self._speaker_segment_started_at = 0.0
        self._speaker_enrollment_suppress_until = 0.0
        self._active_user_id = ""
        self._last_speaker_match = {}
        await self._client.aclose()
        self._disconnect_mqtt()
        self.log_store.add("system", "desktop audio agent stopped", source="desktop_runtime")

    async def _ensure_mqtt(self) -> None:
        with self._mqtt_lock:
            if self._mqtt is not None:
                return
            callback_api = getattr(getattr(mqtt, "CallbackAPIVersion", None), "VERSION1", None)
            client = mqtt.Client(callback_api) if callback_api is not None else mqtt.Client()
            client.on_connect = self._on_mqtt_connect
            client.on_message = self._on_mqtt_message
            self._mqtt = client
        host = _env("MQTT_HOST", "127.0.0.1")
        port_text = _env("MQTT_PORT", "1883")
        try:
            port = int(port_text)
        except Exception:
            port = 1883
        try:
            client.connect(host, port, keepalive=60)
            client.loop_start()
        except Exception as exc:
            self._last_error = f"MQTT connect failed: {exc}"
            logger.warning(self._last_error)

    def _disconnect_mqtt(self) -> None:
        with self._mqtt_lock:
            client = self._mqtt
            self._mqtt = None
            self._mqtt_connected = False
        if client is None:
            return
        try:
            client.loop_stop()
        except Exception:
            pass
        try:
            client.disconnect()
        except Exception:
            pass

    def _on_mqtt_connect(self, client, userdata, flags, rc):  # pragma: no cover - runtime callback
        self._mqtt_connected = rc == 0
        if rc == 0:
            try:
                client.subscribe("robot/dialog/answer")
            except Exception:
                pass

    def _on_mqtt_message(self, client, userdata, msg):  # pragma: no cover - runtime callback
        if not msg or not msg.topic or msg.topic != "robot/dialog/answer":
            return
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return
        corr_id = str(payload.get("corr_id") or "").strip()
        if corr_id and self._latest_legacy_corr_id and corr_id != self._latest_legacy_corr_id:
            return
        text = str(payload.get("text") or "").strip()
        text = self._sanitize_assistant_text(text)
        if not text:
            return
        speaker = str(payload.get("tts_speaker") or "").strip() or self.active_voice_code
        self.log_store.add("coach", text, speaker="RACHEL", source="dialog_service")
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(
                self._play_piper_text(
                    text=text,
                    voice=speaker,
                    model=self.active_tts_model or None,
                    source="dialog_service",
                    log_message=False,
                )
            )
        )

    def status(self) -> AudioAgentStatus:
        with self._asr_backend_lock:
            backend = self._asr_backend
        frontend = self._frontend.status()
        input_device_index, input_device_name, input_device_hostapi, input_device_source = self._input_device_details()
        input_device_sample_rate = float(self._selected_input_device_sample_rate or self._input_stream_sample_rate or 0.0)
        streaming_backend = backend.backend_name if backend is not None else ""
        if self.current_asr_mode == STREAMING_ASR_MODE_LIVE_CAPTIONS:
            streaming_backend = "live-captions"
        elif self.current_asr_mode == STREAMING_ASR_MODE_GEMINI_LIVE:
            streaming_backend = "gemini-live"
        elif self.current_asr_mode == STREAMING_ASR_MODE_API:
            streaming_backend = "service-api"
        return AudioAgentStatus(
            listening=self._listening,
            asr_mode=self.current_asr_mode,
            pipeline_mode=self.pipeline_mode,
            profile=self.profile,
            tts_backend=self.active_tts_backend,
            assistant_speaking=self.is_assistant_speaking(),
            sounddevice_available=sd is not None,
            moonshine_available=moonshine_streaming_available(),
            aec_available=self._aec.available,
            streaming_available_modes=supported_streaming_asr_modes(),
            streaming_backend=streaming_backend,
            supports_hotwords=backend.supports_hotwords() if backend is not None else False,
            hotwords_count=len(self._hotword_pack.entries),
            hotword_strategy=self.hotword_strategy,
            current_partial=self._last_partial_text,
            stable_partial=self._last_stable_partial_text,
            input_level_dbfs=frontend.input_level_dbfs,
            input_peak_dbfs=frontend.input_peak_dbfs,
            noise_floor_dbfs=frontend.noise_floor_dbfs,
            frontend_gain_db=frontend.frontend_gain_db,
            speech_active=frontend.speech_active,
            clipped_recently=frontend.clipped_recently,
            clip_events=frontend.clip_events,
            queued_input_frames=self._input_buffer.queued_frames(),
            dropped_input_frames=self._input_buffer.dropped_frames(),
            last_error=self._last_error,
            live_captions_available=self._live_captions_source.is_available(),
            live_captions_output_path=self._live_captions_source.output_path,
            live_captions_status=self._live_captions_source.last_status,
            live_captions_error=self._live_captions_source.last_error,
            speaker_id_enabled=self._speaker_id.enabled,
            speaker_id_ready=self._speaker_id.ready,
            active_user_id=self._active_user_id,
            last_speaker_match=self._speaker_match_payload(),
            live_capture_enabled=bool(self._input_stream is not None),
            input_device_index=input_device_index,
            input_device_name=input_device_name,
            input_device_hostapi=input_device_hostapi,
            input_device_source=input_device_source,
            input_device_sample_rate=input_device_sample_rate,
        )

    def is_assistant_speaking(self) -> bool:
        if self._player.is_playing():
            return True
        if self._conversation_task is not None and not self._conversation_task.done():
            return True
        return self._manual_task is not None and not self._manual_task.done()

    async def speaker_profiles_status(self) -> Dict[str, Any]:
        payload = await asyncio.to_thread(self._speaker_id.status_payload)
        payload["active_user_id"] = str(self._active_user_id or "")
        payload["last_speaker_match"] = self._speaker_match_payload()
        payload["live_capture_enabled"] = bool(self._input_stream is not None)
        payload["asr_mode"] = str(self.current_asr_mode or "")
        return payload

    async def record_speaker_profile_sample(
        self,
        *,
        user_id: str,
        timeout_seconds: float = DEFAULT_SPEAKER_ID_ENROLL_TIMEOUT_SECONDS,
    ) -> Dict[str, Any]:
        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            raise ValueError("user_id is required")
        if not self._speaker_id_enabled():
            raise RuntimeError("speaker id is disabled")
        if not self._speaker_id.ready:
            raise RuntimeError(self._speaker_id.error or "speaker id is not ready")
        if self._loop is None:
            raise RuntimeError("audio agent loop is not ready")
        if not self._running:
            raise RuntimeError("audio agent is not running")
        if not self._listening:
            raise RuntimeError("start listening before recording a speaker sample")
        future: asyncio.Future[Dict[str, Any]] = self._loop.create_future()
        with self._speaker_enrollment_lock:
            self._speaker_enrollment_requests.append((normalized_user_id, future, time.time()))
        self.log_store.add(
            "system",
            f"speaker enrollment armed for {normalized_user_id}; next valid speech clip will be captured without agent reply",
            speaker="system",
            source="speaker_enrollment",
        )
        if self._running and self.current_asr_mode == STREAMING_ASR_MODE_LIVE_CAPTIONS and self._input_stream is None:
            self._start_input_stream()
        try:
            result = await asyncio.wait_for(future, timeout=max(1.0, float(timeout_seconds)))
            return result
        except Exception:
            self._remove_speaker_enrollment_future(future)
            raise

    async def commit_speaker_profile(self, *, user_id: str) -> Dict[str, Any]:
        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            raise ValueError("user_id is required")
        return await asyncio.to_thread(self._speaker_id.commit_pending_clips, normalized_user_id)

    async def clear_speaker_profile(self, *, user_id: str) -> Dict[str, Any]:
        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            raise ValueError("user_id is required")
        if self._active_user_id == normalized_user_id:
            self._active_user_id = ""
        return await asyncio.to_thread(self._speaker_id.clear_profile, normalized_user_id)

    async def set_listening(self, listening: bool) -> None:
        self._listening = bool(listening)
        if not self._listening:
            self._cancel_partial_commit()
            self._speech_active_last = False
            self._last_partial_text = ""
            self._last_stable_partial_text = ""
            self._cancel_api_asr_tasks()
            self._reset_api_asr_turn(clear_preroll=True)

    async def set_asr_mode(self, mode: str) -> None:
        restart_input = self._running
        normalized = self._sanitize_streaming_mode(mode)
        if restart_input:
            self._stop_input_stream()
        self.current_asr_mode = normalized
        if self.profile == CONVERSATION_PROFILE_LOCAL:
            self.local_streaming_asr_mode = normalized
        else:
            self.cloud_streaming_asr_mode = normalized
        try:
            self._rebuild_asr_backend()
        finally:
            if restart_input:
                self._start_input_stream()

    async def apply_runtime_config(
        self,
        *,
        pipeline_mode: str,
        profile: str,
        local_asr_mode: str,
        cloud_asr_mode: str,
        local_streaming_asr_mode: Optional[str] = None,
        cloud_streaming_asr_mode: Optional[str] = None,
        hotword_strategy: Optional[str] = None,
        stable_partial_repeats: Optional[int] = None,
    ) -> None:
        self._reload_provider_settings_from_env()
        self.pipeline_mode = _normalize_pipeline_mode(pipeline_mode)
        self.profile = _normalize_profile(profile)
        self.local_asr_mode = normalize_asr_mode(local_asr_mode)
        self.cloud_asr_mode = normalize_asr_mode(cloud_asr_mode)
        if local_streaming_asr_mode is not None:
            self.local_streaming_asr_mode = self._sanitize_streaming_mode(local_streaming_asr_mode)
        if cloud_streaming_asr_mode is not None:
            self.cloud_streaming_asr_mode = self._sanitize_streaming_mode(cloud_streaming_asr_mode)
        if hotword_strategy is not None:
            self.hotword_strategy = normalize_hotword_strategy(hotword_strategy)
        if stable_partial_repeats is not None:
            self.stable_partial_repeats = max(1, int(stable_partial_repeats))
        target_asr_mode = self._preferred_streaming_asr_mode(self.profile)
        restart_input = self._running
        if restart_input:
            self._stop_input_stream()
        self.current_asr_mode = target_asr_mode
        try:
            self._rebuild_asr_backend()
        finally:
            if restart_input:
                self._start_input_stream()

    async def set_tts_options(
        self,
        *,
        voice: Optional[str] = None,
        model: Optional[str] = None,
        backend: Optional[str] = None,
        qwen_speaker: Optional[str] = None,
        kokoro_voice: Optional[str] = None,
    ) -> None:
        if voice is not None:
            self.active_voice_code = str(voice or "").strip() or self.active_voice_code
        if model is not None:
            self.active_tts_model = str(model or "").strip()
        if backend is not None:
            self.active_tts_backend = _normalize_tts_backend(backend)
        if qwen_speaker is not None:
            self.active_qwen_speaker = str(qwen_speaker or "").strip() or self.active_qwen_speaker
        if kokoro_voice is not None:
            self.active_kokoro_voice = str(kokoro_voice or "").strip() or self.active_kokoro_voice
        try:
            await self._publish_mqtt(
                "robot/tts/options",
                {
                    "voice": self.active_voice_code,
                    "model": self.active_tts_model,
                    "backend": self.active_tts_backend,
                    "qwen_speaker": self.active_qwen_speaker,
                    "kokoro_voice": self.active_kokoro_voice,
                },
            )
        except Exception:
            pass

    async def manual_speak(
        self,
        *,
        text: str,
        voice: Optional[str] = None,
        model: Optional[str] = None,
        instruct: Optional[str] = None,
        backend: str = TTS_BACKEND_PIPER,
        source: str = "wizard_panel",
        speaker_label: str = "Wizard Override",
    ) -> None:
        self.cancel_current_turn(reason="manual_speak", capture_barge_in=False)
        normalized_text = str(text or "").strip()
        if not normalized_text:
            return
        self.log_store.add("wizard", normalized_text, speaker=speaker_label, source=source)
        selected_backend = _normalize_tts_backend(backend or self.active_tts_backend)
        self._manual_task = asyncio.create_task(
            self._play_tts_text(
                text=normalized_text,
                backend=selected_backend,
                voice=voice,
                model=model or self.active_tts_model or None,
                instruct=instruct,
                source=source,
                log_message=False,
            )
        )

    async def handle_panel_game_intent(self, *, action: str, name: str = "", source: str = "desktop_panel") -> None:
        normalized = str(action or "").strip().lower()
        if normalized in {"launch", "open"}:
            if not name.strip():
                raise ValueError("game name required")
            await self._publish_mqtt("robot/intent", {"type": "LAUNCH_GAME", "game_name": name.strip(), "source": source})
            return
        if normalized in {"exit", "close"}:
            await self._publish_mqtt("robot/intent", {"type": "EXIT_GAME", "source": source})
            return
        raise ValueError("unknown game action")

    async def publish_face(self, payload: Dict[str, Any]) -> None:
        data = payload or {}
        raw_mode = str(data.get("mode") or "").strip()
        raw_value = str(data.get("value") or "").strip()
        mode = raw_mode.lower()
        if mode == "custom" and raw_value:
            mode = raw_value.lower()
        elif not mode and raw_value:
            mode = raw_value.lower()
        seconds_raw = data.get("seconds")
        value = mode or "neutral"
        try:
            seconds = float(seconds_raw)
        except Exception:
            seconds = 0.0
        if seconds > 0:
            value = f"{value}:{seconds:g}"
        await self._publish_mqtt("robot/pi/face/cmd", {"action": "face", "value": value})

    async def publish_led(self, payload: Dict[str, Any]) -> None:
        data = payload or {}
        mode = str(data.get("mode") or "").strip().lower()
        duration = data.get("duration")
        brightness = data.get("brightness")
        period = data.get("period")
        color = str(data.get("color") or "").strip() or "#{:02x}{:02x}{:02x}".format(
            random.randint(0, 255),
            random.randint(0, 255),
            random.randint(0, 255),
        )

        def _float_text(value: Any, default: float) -> str:
            try:
                return f"{float(value):g}"
            except Exception:
                return f"{default:g}"

        if mode == "off":
            value = "off"
        elif mode == "breathe":
            value = ":".join(
                [
                    "breathe",
                    color,
                    _float_text(duration, 60.0),
                    _float_text(brightness, 1.0),
                    _float_text(period, 1.5),
                ]
            )
        else:
            led_mode = "on" if mode in {"solid", "random", ""} else mode
            value = ":".join(
                [
                    led_mode,
                    color,
                    _float_text(duration, 60.0),
                    _float_text(brightness, 1.0),
                ]
            )
        await self._publish_mqtt("robot/pi/led/cmd", {"action": "led", "value": value})

    async def publish_flower(self, payload: Dict[str, Any]) -> None:
        action = str((payload or {}).get("action") or (payload or {}).get("value") or "").strip().lower()
        value = action or "open"
        await self._publish_mqtt("robot/pi/servo/cmd", {"action": "servo", "value": value})

    async def _publish_mqtt(self, topic: str, payload: Dict[str, Any]) -> None:
        with self._mqtt_lock:
            client = self._mqtt
            connected = self._mqtt_connected
        if client is None or not connected:
            raise RuntimeError("MQTT is not connected")
        info = client.publish(topic, json.dumps(payload, ensure_ascii=False))
        if getattr(info, "rc", 0) != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT publish failed: rc={getattr(info, 'rc', -1)}")

    def cancel_current_turn(self, *, reason: str, capture_barge_in: bool) -> None:
        if capture_barge_in and self.is_assistant_speaking():
            interrupted = self._assistant_buffer_text.strip()
            self._pending_barge_in = True
            self._pending_interrupted_text = interrupted[:260]
            self._pending_interrupted_corr_id = self._assistant_corr_id
        task = self._conversation_task
        self._conversation_task = None
        if task is not None:
            task.cancel()
        manual = self._manual_task
        self._manual_task = None
        if manual is not None:
            manual.cancel()
        for active in list(self._active_tts_tasks):
            active.cancel()
        self._active_tts_tasks.clear()
        self._assistant_buffer_text = ""
        self._assistant_corr_id = ""
        self._last_partial_text = ""
        self._last_stable_partial_text = ""
        if self.current_asr_mode == STREAMING_ASR_MODE_GEMINI_LIVE:
            self._close_gemini_live_output(clear_player=True)
            self._gemini_live_output_text = ""
            self._gemini_live_logged_output_text = ""
        else:
            self._player.clear()

    def _consume_barge_in(self) -> Tuple[bool, str, str]:
        barge_in = self._pending_barge_in
        interrupted_text = self._pending_interrupted_text
        interrupted_corr_id = self._pending_interrupted_corr_id
        self._pending_barge_in = False
        self._pending_interrupted_text = ""
        self._pending_interrupted_corr_id = ""
        return barge_in, interrupted_text, interrupted_corr_id

    def _sanitize_assistant_text(self, text: str) -> str:
        return sanitize_tts_text(str(text or "").strip())

    def _remember_assistant_text(self, text: str) -> None:
        variants = _assistant_text_variants(self._sanitize_assistant_text(text))
        if not variants:
            return
        now = time.time()
        self._last_assistant_spoke_at = now
        for variant in variants:
            self._recent_assistant_texts.append((now, variant))

    def _looks_complete_query_candidate(self, text: str) -> bool:
        candidate = str(text or "").strip()
        if not candidate:
            return False
        if _PARTIAL_COMMIT_SENTENCE_END_RE.search(candidate):
            return True
        if _PARTIAL_COMMIT_TRAILING_CONNECTOR_RE.search(candidate):
            return False
        words = _PARTIAL_COMMIT_WORD_RE.findall(candidate)
        if len(words) >= DEFAULT_PARTIAL_COMMIT_QUERY_MIN_WORDS:
            return True
        return len(candidate) >= DEFAULT_PARTIAL_COMMIT_QUERY_MIN_CHARS

    def _schedule_partial_commit(self, speech_ended_at: float, *, anchor_at: Optional[float] = None) -> None:
        if self.current_asr_mode == STREAMING_ASR_MODE_GEMINI_LIVE:
            return
        if self._loop is None or not self._listening or self.is_assistant_speaking():
            return
        commit_anchor = float(anchor_at if anchor_at is not None else time.time())
        self._cancel_partial_commit()
        self._partial_commit_anchor_at = commit_anchor
        self._partial_commit_task = asyncio.run_coroutine_threadsafe(
            self._commit_partial_after_delay(speech_ended_at, commit_anchor),
            self._loop,
        )

    def _should_ignore_live_captions_echo(self, text: str) -> bool:
        normalized = str(text or "").strip()
        if not normalized:
            return True
        now = time.time()
        if (now - self._last_assistant_spoke_at) <= DEFAULT_LIVE_CAPTIONS_ASSISTANT_SUPPRESS_SECONDS:
            return True
        while self._recent_assistant_texts and (now - self._recent_assistant_texts[0][0]) > DEFAULT_LIVE_CAPTIONS_ASSISTANT_HISTORY_SECONDS:
            self._recent_assistant_texts.popleft()
        for _, recent in self._recent_assistant_texts:
            if _text_similarity(normalized, recent) >= 0.78:
                return True
            if _token_overlap_ratio(normalized, recent) >= 0.72:
                return True
        return False

    def _start_capture_worker(self) -> None:
        worker = self._capture_worker_thread
        if worker is not None and worker.is_alive():
            return
        self._capture_worker_stop.clear()
        self._input_buffer.reset()
        self._frontend.reset()
        worker = threading.Thread(
            target=self._capture_worker_loop,
            name="voice-audio-capture",
            daemon=True,
        )
        worker.start()
        self._capture_worker_thread = worker

    def _stop_capture_worker(self) -> None:
        self._capture_worker_stop.set()
        self._input_buffer.close()
        worker = self._capture_worker_thread
        self._capture_worker_thread = None
        if worker is not None:
            worker.join(timeout=2.0)

    def _capture_worker_loop(self) -> None:
        while not self._capture_worker_stop.is_set():
            frame = self._input_buffer.pop(timeout=0.2)
            if frame is None:
                continue
            if not self._running or not self._listening:
                continue
            if self.current_asr_mode != STREAMING_ASR_MODE_GEMINI_LIVE and self.is_assistant_speaking():
                continue
            render = self._player.pop_render_block(frame.size)
            processed = self._aec.process(frame, render)
            processed = self._frontend.process(processed)
            speech_active = self._frontend.status().speech_active
            self._update_speech_activity(speech_active)
            self._update_speaker_capture(processed, speech_active=speech_active)
            if self.current_asr_mode == STREAMING_ASR_MODE_GEMINI_LIVE:
                try:
                    self._handle_gemini_live_frame(processed)
                except Exception as exc:
                    self._last_error = f"Gemini Live capture failed: {exc}"
                    logger.warning(self._last_error)
                continue
            if self.current_asr_mode == STREAMING_ASR_MODE_API:
                try:
                    self._handle_api_asr_frame(processed, speech_active=speech_active)
                except Exception as exc:
                    self._last_error = f"service api ASR capture failed: {exc}"
                    logger.warning(self._last_error)
                continue
            try:
                with self._asr_backend_lock:
                    backend = self._asr_backend
                    if backend is None:
                        continue
                    backend.push_audio(processed, self.capture_sample_rate)
            except Exception as exc:
                self._last_error = f"streaming ASR push failed: {exc}"
                logger.warning(self._last_error)

    def _start_input_stream(self) -> None:
        if self.current_asr_mode == STREAMING_ASR_MODE_LIVE_CAPTIONS:
            try:
                self._live_captions_source.start()
                self._last_error = ""
            except Exception as exc:
                self._last_error = f"live captions start failed: {exc}"
                logger.warning(self._last_error)
        if not self._speaker_capture_enabled() and self.current_asr_mode == STREAMING_ASR_MODE_LIVE_CAPTIONS:
            return
        if sd is None:
            self._last_error = "sounddevice is not installed"
            return
        with self._input_stream_lock:
            if self._input_stream is not None:
                return
            self._start_capture_worker()
            try:
                (
                    device_index,
                    device_name,
                    device_hostapi,
                    device_source,
                    device_sample_rate,
                    stream_blocksize,
                ) = self._preferred_input_stream_config()
                stream = sd.InputStream(
                    samplerate=device_sample_rate,
                    blocksize=stream_blocksize,
                    channels=1,
                    dtype="float32",
                    callback=self._input_callback,
                    device=device_index if device_index >= 0 else None,
                )
                stream.start()
                self._input_stream = stream
                self._selected_input_device_index = int(device_index)
                self._selected_input_device_name = str(device_name or "")
                self._selected_input_device_hostapi = str(device_hostapi or "")
                self._selected_input_device_source = str(device_source or "")
                self._selected_input_device_sample_rate = float(device_sample_rate or 0.0)
                self._input_stream_sample_rate = float(device_sample_rate or self.capture_sample_rate)
            except Exception:
                self._selected_input_device_index = -1
                self._selected_input_device_name = ""
                self._selected_input_device_hostapi = ""
                self._selected_input_device_source = ""
                self._selected_input_device_sample_rate = 0.0
                self._input_stream_sample_rate = float(self.capture_sample_rate)
                self._stop_capture_worker()
                raise

    def _stop_input_stream(self) -> None:
        if self.current_asr_mode == STREAMING_ASR_MODE_LIVE_CAPTIONS:
            self._live_captions_source.stop()
        with self._input_stream_lock:
            stream = self._input_stream
            self._input_stream = None
            self._selected_input_device_index = -1
            self._selected_input_device_name = ""
            self._selected_input_device_hostapi = ""
            self._selected_input_device_source = ""
            self._selected_input_device_sample_rate = 0.0
            self._input_stream_sample_rate = float(self.capture_sample_rate)
        self._stop_capture_worker()
        if stream is None:
            return
        try:
            stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    def _input_callback(self, indata, frames, time_info, status) -> None:  # pragma: no cover - runtime callback
        if not self._running or not self._listening:
            return
        if self.current_asr_mode != STREAMING_ASR_MODE_GEMINI_LIVE and self.is_assistant_speaking():
            return
        if status:
            logger.debug("input stream status: %s", status)
        if indata is None:
            return
        samples = np.asarray(indata[:, 0], dtype=np.float32).reshape(-1)
        if samples.size <= 0:
            return
        stream_sample_rate = float(self._input_stream_sample_rate or self.capture_sample_rate)
        if int(round(stream_sample_rate)) != int(self.capture_sample_rate):
            samples = _safe_resample(samples, int(round(stream_sample_rate)), int(self.capture_sample_rate))
            samples = np.asarray(samples, dtype=np.float32).reshape(-1)
            if samples.size <= 0:
                return
        try:
            self._input_buffer.push(samples)
        except Exception as exc:
            self._last_error = f"streaming input buffering failed: {exc}"

    def _handle_streaming_started(self, event: AsrEvent) -> None:
        _ = event
        self._cancel_partial_commit()
        self._partial_commit_anchor_at = 0.0
        self._last_partial_text = ""
        self._last_stable_partial_text = ""
        self._last_partial_text_changed_at = 0.0
        self._last_stable_partial_text_changed_at = 0.0

    def _handle_streaming_partial(self, event: AsrEvent) -> None:
        now = time.time()
        partial_text = str(event.text or "").strip()
        stable_text = str(event.stable_text or "").strip()
        self._last_partial_event_at = now
        if partial_text != self._last_partial_text:
            self._last_partial_text_changed_at = now
        if stable_text != self._last_stable_partial_text:
            self._last_stable_partial_text_changed_at = now
        self._last_partial_text = partial_text
        self._last_stable_partial_text = stable_text
        stable_text = self._last_stable_partial_text
        if not stable_text or not self._listening or self.is_assistant_speaking():
            return
        if self._suppress_transcript_during_enrollment(source="streaming_partial", text=stable_text):
            self._last_partial_text = ""
            self._last_stable_partial_text = ""
            self._last_partial_text_changed_at = 0.0
            self._last_stable_partial_text_changed_at = 0.0
            return
        grammar_match = self._command_grammar.canonicalize(stable_text)
        if grammar_match.route_type == "QUERY" or grammar_match.confidence < 0.86:
            if (
                not self._speech_active_last
                and self._speech_ended_at > 0.0
                and max(self._last_partial_text_changed_at, self._last_stable_partial_text_changed_at) >= self._speech_ended_at
            ):
                self._schedule_partial_commit(
                    self._speech_ended_at,
                    anchor_at=max(self._last_partial_text_changed_at, self._last_stable_partial_text_changed_at, now),
                )
            return
        self._cancel_partial_commit()
        self._partial_commit_anchor_at = 0.0
        self._last_partial_text = ""
        self._last_stable_partial_text = ""
        self._last_partial_text_changed_at = 0.0
        self._last_stable_partial_text_changed_at = 0.0
        asyncio.create_task(
            self._submit_user_turn(
                text=str(grammar_match.canonical_text or stable_text).strip(),
                original_text=stable_text,
                grammar_route=grammar_match.route_type,
                grammar_game_name=grammar_match.game_name,
                grammar_confidence=grammar_match.confidence,
                avg_logprob=None,
                speaker_index=event.speaker_index if event.has_speaker_id else None,
                speaker_id=event.speaker_id if event.has_speaker_id else None,
                user_id="",
                identity_resolution="auto",
                transcript_source=TRANSCRIPT_SOURCE_STABLE_PARTIAL_COMMAND,
                transcript_confidence=TRANSCRIPT_CONFIDENCE_HIGH,
            )
        )

    async def _handle_streaming_final(self, event: AsrEvent) -> None:
        self._cancel_partial_commit()
        self._last_final_event_at = time.time()
        raw_final_text = str(event.text or "").strip()
        grammar_match = self._command_grammar.canonicalize(raw_final_text)
        final_text = str(grammar_match.canonical_text or raw_final_text).strip()
        transcript_source = TRANSCRIPT_SOURCE_FINAL
        transcript_confidence = _estimate_transcript_confidence(
            text=final_text,
            grammar_route=grammar_match.route_type,
            grammar_confidence=grammar_match.confidence,
            avg_logprob=event.avg_logprob,
            transcript_source=transcript_source,
            input_source=str(event.backend or self.current_asr_mode or "streaming_final"),
        )
        self._last_partial_text = ""
        self._last_stable_partial_text = ""
        self._last_partial_text_changed_at = 0.0
        self._last_stable_partial_text_changed_at = 0.0
        if not final_text or not self._listening:
            return
        if self.is_assistant_speaking():
            return
        if self._suppress_transcript_during_enrollment(source="streaming_final", text=final_text):
            return
        await self._submit_user_turn(
            text=final_text,
            original_text=raw_final_text,
            grammar_route=grammar_match.route_type,
            grammar_game_name=grammar_match.game_name,
            grammar_confidence=grammar_match.confidence,
            avg_logprob=event.avg_logprob,
            speaker_index=event.speaker_index if event.has_speaker_id else None,
            speaker_id=event.speaker_id if event.has_speaker_id else None,
            user_id="",
            identity_resolution="auto",
            transcript_source=transcript_source,
            transcript_confidence=transcript_confidence,
            input_source=str(event.backend or self.current_asr_mode or "streaming_final"),
        )

    async def _handle_external_transcript_final(
        self,
        raw_text: str,
        *,
        source: str,
        user_id: str = "",
        identity_resolution: str = "none",
    ) -> None:
        self._cancel_partial_commit()
        self._last_final_event_at = time.time()
        grammar_match = self._command_grammar.canonicalize(raw_text)
        final_text = str(grammar_match.canonical_text or raw_text).strip()
        self._last_partial_text = ""
        self._last_stable_partial_text = ""
        self._last_partial_text_changed_at = 0.0
        self._last_stable_partial_text_changed_at = 0.0
        if not final_text or not self._listening or self.is_assistant_speaking():
            return
        if self._suppress_transcript_during_enrollment(source=source, text=final_text):
            return
        if source == "live_captions" and self._should_ignore_live_captions_echo(final_text):
            self.log_store.add(
                "system",
                f"ignored likely live-captions echo: {final_text}",
                speaker="system",
                source="live_captions_filter",
            )
            return
        await self._submit_user_turn(
            text=final_text,
            original_text=raw_text,
            grammar_route=grammar_match.route_type,
            grammar_game_name=grammar_match.game_name,
            grammar_confidence=grammar_match.confidence,
            avg_logprob=None,
            speaker_index=None,
            speaker_id=None,
            user_id=user_id,
            identity_resolution=identity_resolution,
            transcript_source=TRANSCRIPT_SOURCE_FINAL,
            transcript_confidence=_estimate_transcript_confidence(
                text=final_text,
                grammar_route=grammar_match.route_type,
                grammar_confidence=grammar_match.confidence,
                avg_logprob=None,
                transcript_source=TRANSCRIPT_SOURCE_FINAL,
                input_source=source,
            ),
            input_source=source,
        )

    def _handle_api_asr_frame(self, audio: np.ndarray, *, speech_active: bool) -> None:
        frame = np.asarray(audio, dtype=np.float32).reshape(-1)
        if frame.size <= 0:
            return
        frame_copy = np.asarray(frame, dtype=np.float32).copy()
        if speech_active:
            if not self._api_asr_turn_active:
                self._api_asr_turn_frames = [
                    np.asarray(item, dtype=np.float32).copy() for item in self._api_asr_preroll_frames
                ]
                self._api_asr_turn_active = True
                self._api_asr_preroll_frames.clear()
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(
                        self._handle_streaming_started,
                        AsrEvent(event_type="started", backend="service-api"),
                    )
            self._api_asr_turn_frames.append(frame_copy)
            return

        if self._api_asr_turn_active:
            self._api_asr_turn_frames.append(frame_copy)
            full_audio = (
                np.concatenate(self._api_asr_turn_frames).astype(np.float32, copy=False)
                if self._api_asr_turn_frames
                else np.zeros(0, dtype=np.float32)
            )
            self._reset_api_asr_turn(clear_preroll=True)
            if full_audio.size >= self._api_asr_min_samples and self._loop is not None:
                self._loop.call_soon_threadsafe(
                    self._start_api_asr_task,
                    np.asarray(full_audio, dtype=np.float32),
                )
            return

        self._api_asr_preroll_frames.append(frame_copy)

    def _start_api_asr_task(self, audio: np.ndarray) -> None:
        if self._speaker_enrollment_pending():
            self.log_store.add(
                "system",
                "speaker enrollment active; skipped API transcription turn",
                speaker="system",
                source="speaker_enrollment",
            )
            return
        task = asyncio.create_task(self._run_api_asr_turn(np.asarray(audio, dtype=np.float32)))
        self._active_api_asr_tasks.add(task)
        task.add_done_callback(lambda completed: self._active_api_asr_tasks.discard(completed))

    async def _run_api_asr_turn(self, audio: np.ndarray) -> None:
        normalized_audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        if normalized_audio.size <= 0 or not self._listening:
            return
        speaker_task: Optional[asyncio.Task[SpeakerMatchResult]] = None
        if self._speaker_id_enabled():
            speaker_task = asyncio.create_task(
                asyncio.to_thread(self._speaker_id.match_audio, normalized_audio, self.capture_sample_rate)
            )

        try:
            response = await self._client.post(
                f"{self.asr_base_url}/transcribe",
                params={
                    "sample_rate": self.capture_sample_rate,
                    "mode": TRANSCRIBE_MODE_API,
                },
                content=_float32_to_pcm16_bytes(normalized_audio),
                timeout=DEFAULT_TURN_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json() if response.content else {}
        except Exception as exc:
            if speaker_task is not None:
                speaker_task.cancel()
            self._last_error = f"service API transcription failed: {exc}"
            self.log_store.add("system", self._last_error, speaker="system", source="service_api_asr")
            return

        transcript = str(payload.get("text") or "").strip() if isinstance(payload, dict) else ""
        if not transcript:
            if speaker_task is not None:
                try:
                    await speaker_task
                except Exception:
                    pass
            return
        self._last_error = ""
        provider = str(payload.get("provider") or "api").strip().lower() if isinstance(payload, dict) else "api"
        resolved_user_id = ""
        identity_resolution = "none"
        if speaker_task is not None:
            try:
                match_result = await speaker_task
            except Exception as exc:
                match_result = SpeakerMatchResult(reason=str(exc))
            self._update_last_speaker_match(match_result, source=f"api_{provider or 'api'}")
            if match_result.matched:
                resolved_user_id = str(match_result.user_id or "").strip()
                identity_resolution = "auto"
        await self._handle_external_transcript_final(
            transcript,
            source=f"api_{provider or 'api'}",
            user_id=resolved_user_id,
            identity_resolution=identity_resolution,
        )

    def _cancel_partial_commit(self) -> None:
        task = self._partial_commit_task
        self._partial_commit_task = None
        self._partial_commit_anchor_at = 0.0
        if task is not None and not task.done():
            task.cancel()

    def _update_speech_activity(self, speaking: bool) -> None:
        now = time.time()
        if speaking:
            if not self._speech_active_last:
                self._speech_started_at = now
            self._speech_active_last = True
            self._cancel_partial_commit()
            return
        if self._speech_active_last:
            self._speech_ended_at = now
            self._speech_active_last = False
            if self.current_asr_mode == STREAMING_ASR_MODE_GEMINI_LIVE:
                return
            self._schedule_partial_commit(now, anchor_at=now)

    async def _commit_partial_after_delay(self, speech_ended_at: float, commit_anchor_at: float) -> None:
        try:
            if self.current_asr_mode == STREAMING_ASR_MODE_GEMINI_LIVE:
                return
            await asyncio.sleep(DEFAULT_PARTIAL_COMMIT_DELAY_SECONDS)
            if not self._listening or self.is_assistant_speaking():
                return
            if self._suppress_transcript_during_enrollment(source="stable_partial_fallback"):
                self._last_partial_text = ""
                self._last_stable_partial_text = ""
                return
            if self._speech_active_last or self._speech_ended_at != speech_ended_at:
                return
            if self._partial_commit_anchor_at and self._partial_commit_anchor_at != commit_anchor_at:
                return
            if self._last_final_event_at >= speech_ended_at:
                return
            raw_partial = str(self._last_partial_text or "").strip()
            stable_partial = str(self._last_stable_partial_text or "").strip()
            grammar_seed = stable_partial or raw_partial
            if not grammar_seed:
                return
            grammar_match = self._command_grammar.canonicalize(grammar_seed)
            is_query = grammar_match.route_type == "QUERY" or grammar_match.confidence < 0.86
            if is_query:
                if not stable_partial:
                    return
                if self._last_partial_text_changed_at > commit_anchor_at:
                    return
                if self._last_stable_partial_text_changed_at > commit_anchor_at:
                    return
                candidate = stable_partial
                if not self._looks_complete_query_candidate(candidate):
                    return
            else:
                if max(self._last_partial_text_changed_at, self._last_stable_partial_text_changed_at) > commit_anchor_at:
                    return
                candidate = stable_partial or raw_partial
            if len(candidate) < DEFAULT_PARTIAL_COMMIT_MIN_CHARS:
                return
            grammar_match = self._command_grammar.canonicalize(candidate)
            final_text = str(grammar_match.canonical_text or candidate).strip()
            if not final_text:
                return
            self.log_store.add(
                "system",
                f"committed stable partial after speech end: {candidate}",
                speaker="system",
                source="streaming_asr_fallback",
            )
            self._last_final_event_at = time.time()
            self._last_partial_text = ""
            self._last_stable_partial_text = ""
            self._last_partial_text_changed_at = 0.0
            self._last_stable_partial_text_changed_at = 0.0
            await self._submit_user_turn(
                text=final_text,
                original_text=candidate,
                grammar_route=grammar_match.route_type,
                grammar_game_name=grammar_match.game_name,
                grammar_confidence=grammar_match.confidence,
                avg_logprob=None,
                speaker_index=None,
                speaker_id=None,
                user_id="",
                identity_resolution="auto",
                transcript_source=TRANSCRIPT_SOURCE_STABLE_PARTIAL_FALLBACK,
                transcript_confidence=_estimate_transcript_confidence(
                    text=final_text,
                    grammar_route=grammar_match.route_type,
                    grammar_confidence=grammar_match.confidence,
                    avg_logprob=None,
                    transcript_source=TRANSCRIPT_SOURCE_STABLE_PARTIAL_FALLBACK,
                    input_source=self.current_asr_mode,
                ),
                input_source=self.current_asr_mode,
            )
        except asyncio.CancelledError:
            return

    async def _submit_user_turn(
        self,
        *,
        text: str,
        original_text: str,
        grammar_route: str,
        grammar_game_name: str,
        grammar_confidence: float,
        avg_logprob: Optional[float],
        speaker_index: Optional[int],
        speaker_id: Optional[int],
        user_id: str,
        identity_resolution: str,
        transcript_source: str,
        transcript_confidence: str,
        input_source: str = "",
    ) -> None:
        normalized = str(text or "").strip()
        if not normalized:
            return
        now = time.time()
        if (
            normalized == self._last_user_submit_text
            and (now - self._last_user_submit_at) <= 2.0
        ):
            return
        self._last_user_submit_text = normalized
        self._last_user_submit_at = now
        corr_id = uuid.uuid4().hex
        barge_in, interrupted_text, interrupted_corr_id = self._consume_barge_in()
        request_source = _compose_turn_source(input_source)
        metadata_parts: List[str] = []
        raw_value = _collapse_spaces(original_text)
        if raw_value and raw_value != normalized:
            metadata_parts.append(f"raw={raw_value}")
        if grammar_route and grammar_route != "QUERY":
            metadata_parts.append(f"grammar={grammar_route}:{grammar_confidence:.2f}")
        if transcript_source:
            metadata_parts.append(f"asr={transcript_source}:{transcript_confidence}")
        if request_source != "desktop_audio":
            metadata_parts.append(f"source={request_source}")
        self.log_store.add(
            "user",
            normalized,
            speaker="User",
            source="desktop_audio",
            metadata=" | ".join(metadata_parts),
        )

        if self.pipeline_mode == PIPELINE_MODE_LEGACY_MQTT:
            self._latest_legacy_corr_id = corr_id
            payload: Dict[str, Any] = {
                "text": normalized,
                "source": request_source,
                "corr_id": corr_id,
                "ts": int(time.time() * 1000),
            }
            if avg_logprob is not None:
                payload["avg_logprob"] = float(avg_logprob)
            if speaker_index is not None:
                payload["speaker_index"] = int(speaker_index)
            if speaker_id is not None:
                payload["speaker_id"] = int(speaker_id)
            if user_id:
                payload["user_id"] = str(user_id)
            if identity_resolution:
                payload["identity_resolution"] = str(identity_resolution)
            if grammar_route and grammar_route != "QUERY":
                payload["grammar_route"] = grammar_route
            if grammar_game_name:
                payload["grammar_game_name"] = grammar_game_name
            if grammar_confidence > 0:
                payload["grammar_confidence"] = round(float(grammar_confidence), 4)
            if transcript_source:
                payload["transcript_source"] = transcript_source
            if transcript_confidence:
                payload["transcript_confidence"] = transcript_confidence
            if barge_in:
                payload["barge_in"] = True
                if interrupted_text:
                    payload["interrupted_tts_text"] = interrupted_text
                if interrupted_corr_id:
                    payload["interrupted_tts_corr_id"] = interrupted_corr_id
            await self._publish_mqtt("robot/voice/text", payload)
            return

        self.cancel_current_turn(reason="new_direct_turn", capture_barge_in=False)
        self._conversation_task = asyncio.create_task(
            self._run_direct_conversation(
                text=normalized,
                corr_id=corr_id,
                avg_logprob=avg_logprob,
                speaker_index=speaker_index,
                speaker_id=speaker_id,
                user_id=user_id,
                identity_resolution=identity_resolution,
                grammar_route=grammar_route,
                grammar_game_name=grammar_game_name,
                grammar_confidence=grammar_confidence,
                barge_in=barge_in,
                interrupted_tts_text=interrupted_text,
                interrupted_tts_corr_id=interrupted_corr_id,
                transcript_source=transcript_source,
                transcript_confidence=transcript_confidence,
                input_source=input_source,
            )
        )

    async def _run_direct_conversation(
        self,
        *,
        text: str,
        corr_id: str,
        avg_logprob: Optional[float],
        speaker_index: Optional[int],
        speaker_id: Optional[int],
        user_id: str,
        identity_resolution: str,
        grammar_route: str,
        grammar_game_name: str,
        grammar_confidence: float,
        barge_in: bool,
        interrupted_tts_text: str,
        interrupted_tts_corr_id: str,
        transcript_source: str,
        transcript_confidence: str,
        input_source: str,
    ) -> None:
        request_source = _compose_turn_source(input_source)
        payload: Dict[str, Any] = {
            "text": text,
            "corr_id": corr_id,
            "source": request_source,
            "barge_in": bool(barge_in),
        }
        if avg_logprob is not None:
            payload["avg_logprob"] = float(avg_logprob)
        if speaker_index is not None:
            payload["speaker_index"] = int(speaker_index)
        if speaker_id is not None:
            payload["speaker_id"] = int(speaker_id)
        if user_id:
            payload["user_id"] = str(user_id)
        if identity_resolution:
            payload["identity_resolution"] = str(identity_resolution)
        if grammar_route and grammar_route != "QUERY":
            payload["grammar_route"] = grammar_route
        if grammar_game_name:
            payload["grammar_game_name"] = grammar_game_name
        if grammar_confidence > 0:
            payload["grammar_confidence"] = round(float(grammar_confidence), 4)
        if transcript_source:
            payload["transcript_source"] = transcript_source
        if transcript_confidence:
            payload["transcript_confidence"] = transcript_confidence
        if interrupted_tts_text:
            payload["interrupted_tts_text"] = interrupted_tts_text
        if interrupted_tts_corr_id:
            payload["interrupted_tts_corr_id"] = interrupted_tts_corr_id

        tts_queue: "asyncio.Queue[Optional[str]]" = asyncio.Queue()
        self._assistant_buffer_text = ""
        self._assistant_corr_id = corr_id
        tts_worker = asyncio.create_task(self._direct_tts_worker(tts_queue))
        self._active_tts_tasks.add(tts_worker)
        final_text = ""
        final_logged = False
        try:
            async with self._client.stream(
                "POST",
                f"{self.asr_base_url}/conversation/turn/stream",
                json=payload,
                timeout=DEFAULT_TURN_TIMEOUT_SECONDS,
            ) as response:
                response.raise_for_status()
                async for raw_line in response.aiter_lines():
                    line = str(raw_line or "").strip()
                    if not line:
                        continue
                    event = json.loads(line)
                    event_type = str(event.get("type") or "").strip().lower()
                    if event_type == "chunk":
                        chunk = str(event.get("text") or "").strip()
                        if chunk:
                            self._assistant_buffer_text = _append_stream_text(self._assistant_buffer_text, chunk)
                            await tts_queue.put(chunk)
                        continue
                    if event_type == "final":
                        final_text = self._sanitize_assistant_text(event.get("text") or "")
                        if final_text:
                            self._assistant_buffer_text = final_text
                            if not final_logged:
                                self.log_store.add("coach", final_text, speaker="RACHEL", source="voice_service")
                                final_logged = True
                        continue
                    if event_type == "error":
                        message = str(event.get("message") or "conversation stream error").strip()
                        self.log_store.add("system", message, speaker="system", source="voice_service")
                        break
        finally:
            await tts_queue.put(None)
            try:
                await tts_worker
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self._last_error = f"TTS worker failed: {exc}"
            self._active_tts_tasks.discard(tts_worker)
        if final_text and not final_logged:
            self.log_store.add("coach", final_text, speaker="RACHEL", source="voice_service")
        elif not final_logged:
            interrupted_text = self._sanitize_assistant_text(self._assistant_buffer_text or "")
            if interrupted_text:
                self.log_store.add("coach", interrupted_text, speaker="RACHEL", source="voice_service_partial", metadata="interrupted")
        self._assistant_buffer_text = ""
        self._assistant_corr_id = ""

    async def _direct_tts_worker(self, queue: "asyncio.Queue[Optional[str]]") -> None:
        buffer = ""

        async def flush_buffer(*, final: bool, idle: bool) -> None:
            nonlocal buffer
            while True:
                boundary = _find_direct_tts_boundary(buffer, final=final, idle=idle)
                if boundary <= 0:
                    break
                segment = buffer[:boundary].strip()
                buffer = buffer[boundary:].lstrip()
                if not segment:
                    continue
                await self._play_tts_text(
                    text=segment,
                    backend=self.active_tts_backend,
                    voice=None,
                    model=self.active_tts_model or None,
                    instruct=None,
                    source="direct_tts",
                    log_message=False,
                    wait_for_drain=False,
                )
                if not final and idle:
                    break

        while True:
            try:
                timeout = DEFAULT_DIRECT_TTS_IDLE_FLUSH_SECONDS if buffer else None
                if timeout is None:
                    chunk = await queue.get()
                else:
                    chunk = await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                await flush_buffer(final=False, idle=True)
                continue
            if chunk is None:
                await flush_buffer(final=True, idle=True)
                await self._wait_for_playback_drain()
                break
            normalized = str(chunk or "").strip()
            if not normalized:
                continue
            buffer = _append_stream_text(buffer, normalized)
            await flush_buffer(final=False, idle=False)

    async def _play_tts_text(
        self,
        *,
        text: str,
        backend: str,
        voice: Optional[str],
        model: Optional[str],
        instruct: Optional[str],
        source: str,
        log_message: bool,
        wait_for_drain: bool = True,
    ) -> None:
        selected_backend = _normalize_tts_backend(backend or self.active_tts_backend)
        if selected_backend == TTS_BACKEND_QWEN:
            await self._play_qwen_text(
                text=text,
                speaker=voice or self.active_qwen_speaker,
                instruct=instruct,
                source=source,
                log_message=log_message,
                wait_for_drain=wait_for_drain,
            )
            return
        if selected_backend == TTS_BACKEND_KOKORO:
            await self._play_kokoro_text(
                text=text,
                voice=voice or self.active_kokoro_voice,
                source=source,
                log_message=log_message,
                wait_for_drain=wait_for_drain,
            )
            return
        await self._play_piper_text(
            text=text,
            voice=voice or self.active_voice_code,
            model=model or self.active_tts_model or None,
            instruct=instruct,
            source=source,
            log_message=log_message,
            wait_for_drain=wait_for_drain,
        )

    async def _play_piper_text(
        self,
        *,
        text: str,
        voice: Optional[str],
        model: Optional[str],
        instruct: Optional[str] = None,
        source: str,
        log_message: bool,
        wait_for_drain: bool = True,
    ) -> None:
        normalized = self._sanitize_assistant_text(text)
        if not normalized:
            return
        self._remember_assistant_text(normalized)
        params: Dict[str, str] = {"text": normalized}
        if voice:
            params["voice"] = str(voice)
        if model:
            params["model"] = str(model)
        if instruct:
            params["instruct"] = str(instruct)
        stream_url = f"{self.piper_base_url}/speak_stream"
        remainder = b""
        sample_rate = self.output_sample_rate
        self._player.begin_stream()
        try:
            async with self._client.stream("GET", stream_url, params=params, timeout=DEFAULT_TURN_TIMEOUT_SECONDS) as response:
                response.raise_for_status()
                sample_rate = int(response.headers.get("X-Audio-Sample-Rate", str(self.output_sample_rate)) or self.output_sample_rate)
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    frame_bytes = remainder + chunk
                    usable = len(frame_bytes) - (len(frame_bytes) % 2)
                    if usable > 0:
                        self._player.enqueue_pcm16(frame_bytes[:usable], sample_rate)
                    remainder = frame_bytes[usable:]
            if remainder:
                usable = len(remainder) - (len(remainder) % 2)
                if usable > 0:
                    self._player.enqueue_pcm16(remainder[:usable], sample_rate)
            if log_message:
                self.log_store.add("coach", normalized, speaker="RACHEL", source=source)
            if wait_for_drain:
                await self._wait_for_playback_drain()
        finally:
            self._player.end_stream()

    async def _play_kokoro_text(
        self,
        *,
        text: str,
        voice: Optional[str],
        source: str,
        log_message: bool,
        wait_for_drain: bool = True,
    ) -> None:
        normalized = self._sanitize_assistant_text(text)
        if not normalized:
            return
        self._remember_assistant_text(normalized)
        params: Dict[str, str] = {"text": normalized}
        if voice:
            params["voice"] = str(voice)
        response = await self._client.get(
            f"{self.kokoro_base_url}/speak",
            params=params,
            timeout=DEFAULT_TURN_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        audio, sample_rate = _decode_wav_bytes(response.content)
        self._player.begin_stream()
        try:
            self._player.enqueue_audio(audio, sample_rate)
            if log_message:
                self.log_store.add("coach", normalized, speaker="RACHEL", source=source)
            if wait_for_drain:
                await self._wait_for_playback_drain()
        finally:
            self._player.end_stream()

    async def _play_qwen_text(
        self,
        *,
        text: str,
        speaker: Optional[str],
        instruct: Optional[str],
        source: str,
        log_message: bool,
        wait_for_drain: bool = True,
    ) -> None:
        normalized = self._sanitize_assistant_text(text)
        if not normalized:
            return
        self._remember_assistant_text(normalized)
        params: Dict[str, str] = {"text": normalized}
        if speaker:
            params["speaker"] = str(speaker)
        if instruct:
            params["instruct"] = str(instruct)
        response = await self._client.get(f"{self.qwen_base_url}/speak", params=params, timeout=DEFAULT_TURN_TIMEOUT_SECONDS)
        response.raise_for_status()
        audio, sample_rate = _decode_wav_bytes(response.content)
        self._player.begin_stream()
        try:
            self._player.enqueue_audio(audio, sample_rate)
            if log_message:
                self.log_store.add("coach", normalized, speaker="RACHEL", source=source)
            if wait_for_drain:
                await self._wait_for_playback_drain()
        finally:
            self._player.end_stream()

    async def _wait_for_playback_drain(self) -> None:
        started = time.time()
        while self._player.is_playing():
            if time.time() - started > DEFAULT_TURN_TIMEOUT_SECONDS:
                break
            await asyncio.sleep(0.03)
