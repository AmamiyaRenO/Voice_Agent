#!/usr/bin/env python3
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Topics:
    dialog_query: str = "robot/dialog/query"
    dialog_answer: str = "robot/dialog/answer"
    tts_state: str = "robot/tts/state"
    tts_options: str = "robot/tts/options"


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 1883
    topics: Topics = field(default_factory=Topics)
    # Split base URLs: respond via python voice service (8000), TTS via Piper HTTP (5005)
    respond_api_url: str = "http://127.0.0.1:8000"
    tts_api_url: str = "http://127.0.0.1:5005"
    tts_endpoint: str = "/speak"  # GET ?text=...
    respond_endpoint: str = "/respond"  # POST {text}
    # IMPORTANT (AEC): For echo cancellation to work, TTS must be played through Unity so
    # the AudioListener render reference is available. Therefore, this service must NOT
    # play audio locally (winsound).
    speak_audio: bool = False
    source_label: str = "dialog_service"
    enable_user_memory: bool = True
    user_memory_path: str = str(Path(__file__).resolve().with_name("user_memory.json"))
    user_memory_max_notes: int = 12
    user_memory_prompt_max_chars: int = 380
    enable_user_memory_embeddings: bool = True
    user_memory_embedder: str = "minilm"
    user_memory_embedding_repo_id: str = ""
    user_memory_embedding_model_dir: str = ""
    user_memory_embedding_model_file: str = ""
    user_memory_embedding_tokenizer_file: str = ""
    user_memory_embedding_max_length: int = 256
    user_memory_retrieve_top_k: int = 3
    user_memory_embedding_auto_download: bool = True
    user_memory_embedding_cache_dir: str = ""
    user_memory_query_prefix: str = ""
    user_memory_doc_prefix: str = ""
    enable_dialog_context: bool = True
    enable_dialog_policy: bool = True
    dialog_history_turns: int = 8
    dialog_summary_max_chars: int = 420
    dialog_context_max_chars: int = 900
    memory_query_rule: bool = True
    memory_query_semantic: bool = True
    memory_query_threshold: float = 0.58
    enable_vision_query: bool = True
    vision_describe_url: str = "http://127.0.0.1:8787/api/vision/describe"
    vision_query_prompt: str = "Describe what you see in this camera frame in 2-4 concise sentences."
    vision_query_model: str = ""
    vision_timeout_seconds: float = 12.0


def _env_int(key: str, default: int, floor: int = 0) -> int:
    raw = os.environ.get(key)
    if raw is None:
        return max(floor, default)
    try:
        return max(floor, int(raw))
    except Exception:
        return max(floor, default)


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _env_float(key: str, default: float, floor: float = 0.0) -> float:
    raw = os.environ.get(key)
    if raw is None:
        return max(floor, float(default))
    try:
        return max(floor, float(raw))
    except Exception:
        return max(floor, float(default))


def _default_embedder_repo(embedder: str) -> str:
    key = (embedder or "").strip().lower()
    if key == "bge":
        return "Qdrant/bge-small-en-v1.5-onnx-Q"
    return "onnx-models/all-MiniLM-L6-v2-onnx"


def _default_query_prefix(embedder: str) -> str:
    key = (embedder or "").strip().lower()
    if key == "bge":
        return "Represent this sentence for searching relevant passages: "
    return ""


def _default_doc_prefix(embedder: str) -> str:
    key = (embedder or "").strip().lower()
    if key == "bge":
        return "Represent this sentence for searching relevant passages: "
    return ""


def load_config() -> Config:
    cfg = Config()
    cfg.host = os.environ.get("MQTT_HOST", cfg.host)
    cfg.port = int(os.environ.get("MQTT_PORT", cfg.port))
    cfg.topics = Topics(
        dialog_query=os.environ.get("DIALOG_QUERY_TOPIC", cfg.topics.dialog_query),
        dialog_answer=os.environ.get("DIALOG_ANSWER_TOPIC", cfg.topics.dialog_answer),
        tts_state=os.environ.get("DIALOG_TTS_STATE_TOPIC", cfg.topics.tts_state),
        tts_options=os.environ.get("DIALOG_TTS_OPTIONS_TOPIC", cfg.topics.tts_options),
    )
    # Backward compatibility: VOICE_API_URL used to serve both; now prefer RESPOND_API_URL and TTS_API_URL/PIPER_HTTP_URL
    cfg.respond_api_url = os.environ.get("RESPOND_API_URL", os.environ.get("VOICE_API_URL", cfg.respond_api_url)).rstrip("/")
    cfg.tts_api_url = os.environ.get("TTS_API_URL", os.environ.get("PIPER_HTTP_URL", cfg.tts_api_url)).rstrip("/")
    # Force-disable local playback (even if env is set) to avoid double audio.
    cfg.speak_audio = False
    cfg.source_label = os.environ.get("DIALOG_SOURCE_LABEL", cfg.source_label)
    cfg.enable_user_memory = _env_bool("DIALOG_ENABLE_USER_MEMORY", cfg.enable_user_memory)
    cfg.user_memory_path = (
        os.environ.get("DIALOG_USER_MEMORY_PATH", cfg.user_memory_path).strip() or cfg.user_memory_path
    )
    cfg.user_memory_max_notes = _env_int("DIALOG_USER_MEMORY_MAX_NOTES", cfg.user_memory_max_notes, floor=0)
    cfg.user_memory_prompt_max_chars = _env_int(
        "DIALOG_USER_MEMORY_PROMPT_MAX_CHARS",
        cfg.user_memory_prompt_max_chars,
        floor=80,
    )
    cfg.enable_user_memory_embeddings = _env_bool(
        "DIALOG_ENABLE_USER_MEMORY_EMBEDDINGS",
        cfg.enable_user_memory_embeddings,
    )
    cfg.user_memory_embedder = (
        os.environ.get("DIALOG_USER_MEMORY_EMBEDDER", cfg.user_memory_embedder).strip().lower()
        or cfg.user_memory_embedder
    )
    cfg.user_memory_embedding_repo_id = (
        os.environ.get("DIALOG_USER_MEMORY_EMBEDDING_REPO_ID", cfg.user_memory_embedding_repo_id).strip()
    )
    cfg.user_memory_embedding_model_dir = (
        os.environ.get("DIALOG_USER_MEMORY_EMBEDDING_MODEL_DIR", cfg.user_memory_embedding_model_dir).strip()
    )
    cfg.user_memory_embedding_model_file = (
        os.environ.get("DIALOG_USER_MEMORY_EMBEDDING_MODEL_FILE", cfg.user_memory_embedding_model_file).strip()
    )
    cfg.user_memory_embedding_tokenizer_file = (
        os.environ.get(
            "DIALOG_USER_MEMORY_EMBEDDING_TOKENIZER_FILE",
            cfg.user_memory_embedding_tokenizer_file,
        ).strip()
    )
    cfg.user_memory_embedding_max_length = _env_int(
        "DIALOG_USER_MEMORY_EMBEDDING_MAX_LENGTH",
        cfg.user_memory_embedding_max_length,
        floor=16,
    )
    cfg.user_memory_retrieve_top_k = _env_int(
        "DIALOG_USER_MEMORY_RETRIEVE_TOP_K",
        cfg.user_memory_retrieve_top_k,
        floor=1,
    )
    cfg.user_memory_embedding_auto_download = _env_bool(
        "DIALOG_USER_MEMORY_EMBEDDING_AUTO_DOWNLOAD",
        cfg.user_memory_embedding_auto_download,
    )
    cfg.user_memory_embedding_cache_dir = (
        os.environ.get(
            "DIALOG_USER_MEMORY_EMBEDDING_CACHE_DIR",
            cfg.user_memory_embedding_cache_dir,
        ).strip()
    )
    cfg.user_memory_query_prefix = (
        os.environ.get("DIALOG_USER_MEMORY_QUERY_PREFIX", cfg.user_memory_query_prefix).strip()
    )
    cfg.user_memory_doc_prefix = (
        os.environ.get("DIALOG_USER_MEMORY_DOC_PREFIX", cfg.user_memory_doc_prefix).strip()
    )
    cfg.enable_dialog_context = _env_bool("DIALOG_ENABLE_CONTEXT_MEMORY", cfg.enable_dialog_context)
    cfg.enable_dialog_policy = _env_bool("DIALOG_ENABLE_POLICY", cfg.enable_dialog_policy)
    cfg.dialog_history_turns = _env_int("DIALOG_HISTORY_TURNS", cfg.dialog_history_turns, floor=2)
    cfg.dialog_summary_max_chars = _env_int(
        "DIALOG_SUMMARY_MAX_CHARS",
        cfg.dialog_summary_max_chars,
        floor=120,
    )
    cfg.dialog_context_max_chars = _env_int(
        "DIALOG_CONTEXT_MAX_CHARS",
        cfg.dialog_context_max_chars,
        floor=180,
    )
    cfg.memory_query_rule = _env_bool("DIALOG_MEMORY_QUERY_RULE", cfg.memory_query_rule)
    cfg.memory_query_semantic = _env_bool(
        "DIALOG_MEMORY_QUERY_SEMANTIC",
        cfg.memory_query_semantic,
    )
    cfg.memory_query_threshold = _env_float(
        "DIALOG_MEMORY_QUERY_THRESHOLD",
        cfg.memory_query_threshold,
        floor=0.0,
    )
    cfg.enable_vision_query = _env_bool("DIALOG_ENABLE_VISION_QUERY", cfg.enable_vision_query)
    cfg.vision_describe_url = (
        os.environ.get("DIALOG_VISION_DESCRIBE_URL", cfg.vision_describe_url).strip() or cfg.vision_describe_url
    )
    cfg.vision_query_prompt = os.environ.get("DIALOG_VISION_QUERY_PROMPT", cfg.vision_query_prompt).strip()
    cfg.vision_query_model = os.environ.get("DIALOG_VISION_QUERY_MODEL", cfg.vision_query_model).strip()
    cfg.vision_timeout_seconds = _env_float(
        "DIALOG_VISION_TIMEOUT_SECONDS",
        cfg.vision_timeout_seconds,
        floor=1.0,
    )
    return cfg
