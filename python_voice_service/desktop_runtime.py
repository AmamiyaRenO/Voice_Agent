from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

try:
    import cv2
except Exception:  # pragma: no cover - optional at runtime
    cv2 = None

from desktop_audio_agent import (
    DEFAULT_ASR_BASE_URL,
    ConversationLogStore,
    DesktopAudioAgent,
    normalize_asr_mode,
    normalize_hotword_strategy,
    normalize_streaming_asr_mode,
)
try:
    from .game_grounding import normalize_manifest_payload
    from .qmd_documents import (
        export_game_qmd,
        export_memory_qmd,
        import_game_qmd,
        import_memory_qmd,
    )
except Exception:
    from game_grounding import normalize_manifest_payload
    from qmd_documents import (
        export_game_qmd,
        export_memory_qmd,
        import_game_qmd,
        import_memory_qmd,
    )

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
            return Path(str(raw))
    return app_root


def _resolve_state_dir(app_root: Path) -> Path:
    raw_value = os.getenv("VOICE_AGENT_STATE_DIR")
    raw = raw_value.strip() if raw_value else ""
    if raw:
        path = Path(os.path.expandvars(raw)).expanduser()
        if path.is_absolute():
            return path
        return (app_root / path).resolve()
    if bool(getattr(sys, "frozen", False)):
        return Path.home() / "AppData" / "Local" / "VoiceAgent"
    return app_root / "runtime"


def _safe_json_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except Exception:
        return float(default)
    if not math.isfinite(number):
        return float(default)
    return number


def _choose_existing_path(preferred: Path, *fallbacks: Path) -> Path:
    candidates = [preferred, *fallbacks]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return preferred


APP_ROOT = _resolve_app_root()
BUNDLE_ROOT = _resolve_bundle_root(APP_ROOT)
STATE_DIR = _resolve_state_dir(APP_ROOT)
REPO_ROOT = APP_ROOT
SCRIPTS_DIR = APP_ROOT / "scripts"
PANEL_DIR = _choose_existing_path(
    APP_ROOT / "runtime" / "panel",
    APP_ROOT / "Assets" / "StreamingAssets" / "panel",
    BUNDLE_ROOT / "runtime" / "panel",
    BUNDLE_ROOT / "Assets" / "StreamingAssets" / "panel",
)
DEFAULT_LAUNCHER_CONFIG = (
    STATE_DIR / "local_services.user.json"
    if bool(getattr(sys, "frozen", False))
    else SCRIPTS_DIR / "local_services.user.json"
)
DEFAULT_LAUNCHER_DEFAULT_CONFIG = _choose_existing_path(
    SCRIPTS_DIR / "local_services.default.json",
    BUNDLE_ROOT / "scripts" / "local_services.default.json",
)
DEFAULT_GAME_MANIFEST = _choose_existing_path(
    STATE_DIR / "manifest.json" if bool(getattr(sys, "frozen", False)) else SCRIPTS_DIR / "intent_service" / "manifest.json",
    SCRIPTS_DIR / "intent_service" / "manifest.json",
    BUNDLE_ROOT / "scripts" / "intent_service" / "manifest.json",
)
DEFAULT_DIALOG_MEMORY = _choose_existing_path(
    STATE_DIR / "user_memory.json" if bool(getattr(sys, "frozen", False)) else SCRIPTS_DIR / "dialog_service" / "user_memory.json",
    SCRIPTS_DIR / "dialog_service" / "user_memory.json",
)
DEFAULT_QMD_ROOT = (
    STATE_DIR / "qmd"
    if bool(getattr(sys, "frozen", False))
    else APP_ROOT / "runtime" / "qmd"
)
DEFAULT_PANEL_PORT = int(os.getenv("PANEL_PORT", "8787") or "8787")
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_OPENAI_RESPONSE_MODEL = "gpt-4o-mini"
DEFAULT_OLLAMA_MODEL = "qwen3.5:0.8b"
DEFAULT_LAUNCH_TRIGGERS = ["open", "start", "launch", "play", "begin", "load"]
CORE_EXIT_KEYWORDS = ["back home", "go home", "return home", "go back"]
DEFAULT_EXIT_KEYWORDS = [*CORE_EXIT_KEYWORDS, "quit", "exit", "stop", "cancel", "close", "close game"]
DEFAULT_CAMERA_WIDTH = int(os.getenv("VOICE_AGENT_CAMERA_WIDTH", "640") or "640")
DEFAULT_CAMERA_HEIGHT = int(os.getenv("VOICE_AGENT_CAMERA_HEIGHT", "480") or "480")
DEFAULT_CAMERA_FPS = int(os.getenv("VOICE_AGENT_CAMERA_FPS", "15") or "15")
DEFAULT_CAMERA_DEVICE_INDEX = int(os.getenv("VOICE_AGENT_CAMERA_INDEX", "0") or "0")
DEFAULT_CAMERA_JPEG_QUALITY = int(os.getenv("VOICE_AGENT_CAMERA_JPEG_QUALITY", "70") or "70")
DEFAULT_CAMERA_ACTIVE_WINDOW_SECONDS = float(
    os.getenv("VOICE_AGENT_CAMERA_ACTIVE_WINDOW_SECONDS", "6") or "6"
)
DEFAULT_VISION_PROMPT = os.getenv(
    "VOICE_AGENT_DEFAULT_VISION_PROMPT",
    "Describe what you see in this camera frame in 2-4 concise sentences.",
).strip() or "Describe what you see in this camera frame in 2-4 concise sentences."
QWEN_SPEAKERS = [
    "Ryan",
    "Aiden",
    "Vivian",
    "Serena",
    "Uncle_Fu",
    "Dylan",
    "Eric",
    "Ono_Anna",
    "Sohee",
]


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value else default


def _resolve_launcher_config_path() -> Path:
    raw = _env("VOICE_AGENT_LAUNCHER_CONFIG", "")
    if raw:
        path = Path(os.path.expandvars(raw)).expanduser()
        if path.is_absolute():
            return path
        return (REPO_ROOT / path).resolve()
    return DEFAULT_LAUNCHER_CONFIG


def _resolve_launcher_default_config_path() -> Path:
    raw = _env("VOICE_AGENT_DEFAULT_CONFIG", "")
    if raw:
        path = Path(os.path.expandvars(raw)).expanduser()
        if path.is_absolute():
            return path
        return (REPO_ROOT / path).resolve()
    return DEFAULT_LAUNCHER_DEFAULT_CONFIG


def _load_json_object(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _deep_merge_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _coerce_string_list(value: Any) -> List[str]:
    if isinstance(value, list):
        parts = [str(item).strip() for item in value]
        return [part for part in parts if part]
    if isinstance(value, str):
        merged = value.replace("\r\n", "\n").replace(";", ",").replace("\n", ",")
        parts = [part.strip() for part in merged.split(",")]
        return [part for part in parts if part]
    return []


def _merge_unique_strings(*groups: List[str]) -> List[str]:
    merged: List[str] = []
    seen = set()
    for group in groups:
        for item in group:
            text = str(item or "").strip()
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            merged.append(text)
    return merged


def _normalize_exit_keywords(value: Any) -> List[str]:
    configured = _coerce_string_list(value)
    merged = _merge_unique_strings(configured, DEFAULT_EXIT_KEYWORDS)
    return merged or list(DEFAULT_EXIT_KEYWORDS)


def _normalize_launcher_config(merged: Dict[str, Any]) -> Dict[str, Any]:
    intent_obj = _ensure_dict(merged, "intent")
    intent_obj["exit_keywords"] = _normalize_exit_keywords(intent_obj.get("exit_keywords"))
    return merged


def _load_launcher_config_pair() -> Tuple[Path, Path, Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    user_path = _resolve_launcher_config_path()
    default_path = _resolve_launcher_default_config_path()
    default_cfg = _load_json_object(default_path)
    user_cfg = _load_json_object(user_path)
    merged = _normalize_launcher_config(_deep_merge_dict(default_cfg, user_cfg))
    return user_path, default_path, default_cfg, user_cfg, merged


def _save_launcher_user_config(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(serialized, encoding="utf-8")


def _ensure_dict(node: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = node.get(key)
    if isinstance(value, dict):
        return value
    fresh: Dict[str, Any] = {}
    node[key] = fresh
    return fresh


def _normalize_pipeline_mode(value: Optional[str]) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"legacy", "mqtt", "legacy_mqtt"}:
        return "legacy_mqtt"
    return "direct_unified"


def _normalize_profile(value: Optional[str]) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"cloud", "openai", "online"}:
        return "cloud"
    return "local"


def _read_env_string(merged: Dict[str, Any], key: str, fallback: str) -> str:
    env_obj = _ensure_dict(merged, "env")
    value = env_obj.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def _normalize_path(raw: Optional[str], *, base_dir: Path, allow_command_name: bool) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if allow_command_name and all(marker not in text for marker in ("\\", "/", ":")):
        return text
    path = Path(os.path.expandvars(text)).expanduser()
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return str(path)


def _resolve_game_manifest_path(merged: Dict[str, Any]) -> Path:
    paths_obj = _ensure_dict(merged, "paths")
    candidate = _normalize_path(paths_obj.get("game_manifest") or paths_obj.get("intent_manifest"), base_dir=REPO_ROOT, allow_command_name=False)
    return Path(candidate) if candidate else DEFAULT_GAME_MANIFEST


def _resolve_memory_path(merged: Dict[str, Any]) -> Path:
    direct = _env("DIALOG_USER_MEMORY_PATH", "")
    if direct:
        return Path(_normalize_path(direct, base_dir=REPO_ROOT, allow_command_name=False))
    env_obj = _ensure_dict(merged, "env")
    candidate = _normalize_path(env_obj.get("DIALOG_USER_MEMORY_PATH"), base_dir=REPO_ROOT, allow_command_name=False)
    return Path(candidate) if candidate else DEFAULT_DIALOG_MEMORY


def _resolve_qmd_root(merged: Dict[str, Any]) -> Path:
    env_obj = _ensure_dict(merged, "env")
    candidate = _normalize_path(env_obj.get("VOICE_AGENT_QMD_ROOT"), base_dir=REPO_ROOT, allow_command_name=False)
    return Path(candidate) if candidate else DEFAULT_QMD_ROOT


def _resolve_ollama_base_url(merged: Dict[str, Any]) -> str:
    openai_obj = _ensure_dict(merged, "openai")
    _ = openai_obj
    return _env("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL).rstrip("/")


def _read_string_list(node: Dict[str, Any], key: str, fallback: List[str]) -> List[str]:
    result = _coerce_string_list(node.get(key))
    return result or list(fallback)


def _read_bool(node: Dict[str, Any], key: str, fallback: bool) -> bool:
    value = node.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return fallback


def _build_runtime_payload(merged: Dict[str, Any], *, user_path: Path, default_path: Path, message: str) -> Dict[str, Any]:
    openai_obj = _ensure_dict(merged, "openai")
    intent_obj = _ensure_dict(merged, "intent")
    env_obj = _ensure_dict(merged, "env")
    paths_obj = _ensure_dict(merged, "paths")
    openai_api_key = str(openai_obj.get("api_key") or _env("OPENAI_API_KEY", "")).strip()
    openai_base_url = str(openai_obj.get("base_url") or _env("OPENAI_BASE_URL", "")).strip()
    openai_transcribe_model = str(openai_obj.get("transcribe_model") or _env("OPENAI_TRANSCRIBE_MODEL", "")).strip()
    openai_transcribe_prompt = str(openai_obj.get("transcribe_prompt") or _env("OPENAI_TRANSCRIBE_PROMPT", "")).strip()
    return {
        "status": "ok",
        "message": message,
        "path": str(user_path),
        "default_path": str(default_path),
        "intent_manifest_path": _normalize_path(paths_obj.get("intent_manifest"), base_dir=REPO_ROOT, allow_command_name=False),
        "game_manifest_path": _normalize_path(paths_obj.get("game_manifest"), base_dir=REPO_ROOT, allow_command_name=False),
        "openai_api_key": openai_api_key,
        "openai_api_key_set": bool(openai_api_key),
        "openai_transcribe_model": openai_transcribe_model,
        "openai_base_url": openai_base_url,
        "openai_transcribe_prompt": openai_transcribe_prompt,
        "ollama_model": str(env_obj.get("OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL).strip() or DEFAULT_OLLAMA_MODEL,
        "conversation_pipeline_mode": _normalize_pipeline_mode(env_obj.get("VOICE_PIPELINE_MODE")),
        "conversation_profile": _normalize_profile(env_obj.get("VOICE_CONVERSATION_PROFILE")),
        "local_asr_mode": normalize_asr_mode(env_obj.get("VOICE_LOCAL_ASR_MODE")),
        "cloud_asr_mode": normalize_asr_mode(env_obj.get("VOICE_CLOUD_ASR_MODE")),
        "local_streaming_asr_mode": normalize_streaming_asr_mode(
            env_obj.get("VOICE_LOCAL_STREAMING_ASR_MODE") or env_obj.get("VOICE_LOCAL_ASR_MODE")
        ),
        "cloud_streaming_asr_mode": normalize_streaming_asr_mode(
            env_obj.get("VOICE_CLOUD_STREAMING_ASR_MODE") or env_obj.get("VOICE_CLOUD_ASR_MODE")
        ),
        "asr_hotword_strategy": normalize_hotword_strategy(env_obj.get("VOICE_ASR_HOTWORD_STRATEGY")),
        "asr_stable_partial_repeats": max(
            1,
            int(str(env_obj.get("VOICE_ASR_STABLE_PARTIAL_REPEATS") or "2").strip() or "2"),
        ),
        "openai_response_model": str(env_obj.get("OPENAI_RESPONSE_MODEL") or DEFAULT_OPENAI_RESPONSE_MODEL).strip() or DEFAULT_OPENAI_RESPONSE_MODEL,
        "launch_triggers": ", ".join(_read_string_list(intent_obj, "launch_triggers", DEFAULT_LAUNCH_TRIGGERS)),
        "exit_keywords": ", ".join(_normalize_exit_keywords(intent_obj.get("exit_keywords"))),
        "use_llm_intent_classifier": _read_bool(intent_obj, "use_llm_classifier", False),
        "use_moonshine_intent_recognizer": _read_bool(intent_obj, "use_moonshine_intent_recognizer", False),
        "effective_game_manifest_path": str(_resolve_game_manifest_path(merged)),
    }


def _load_game_manifest(path: Path) -> Dict[str, Any]:
    payload = _load_json_object(path)
    return normalize_manifest_payload(payload)


def _save_game_manifest(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _manifest_status_payload(path: Path) -> Dict[str, Any]:
    root = _load_game_manifest(path)
    games_out: List[Dict[str, Any]] = []
    unresolved = 0
    for item in root.get("games", []) or []:
        if not isinstance(item, dict):
            continue
        game_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or game_id).strip() or game_id
        exec_path = str(item.get("exec") or "").strip()
        workdir = str(item.get("workdir") or "").strip()
        if exec_path and not Path(exec_path).exists():
            unresolved += 1
        games_out.append(
            {
                "id": game_id,
                "name": name,
                "keywords": [str(value).strip() for value in item.get("synonyms", []) or [] if str(value).strip()],
                "exec": exec_path,
                "workdir": workdir,
                "description": str(item.get("description") or "").strip(),
                "how_to_play": str(item.get("how_to_play") or "").strip(),
                "players_min": int(item.get("players_min") or 1),
                "players_max": int(item.get("players_max") or 4),
                "tags": [str(value).strip() for value in item.get("tags", []) or [] if str(value).strip()],
                "activity_level": str(item.get("activity_level") or "").strip(),
                "recommendation_weight": float(item.get("recommendation_weight") or 0.5),
            }
        )
    return {
        "status": "ok",
        "message": "game manifest loaded",
        "path": str(path),
        "games": games_out,
        "unresolved_count": unresolved,
    }


def _load_memory_root(path: Path) -> Dict[str, Any]:
    payload = _load_json_object(path)
    if not payload:
        payload = {"version": 1, "next_user_index": 1, "identity_map": {}, "profiles": {}}
    payload.setdefault("version", 1)
    payload.setdefault("next_user_index", 1)
    payload.setdefault("identity_map", {})
    payload.setdefault("profiles", {})
    if not isinstance(payload["identity_map"], dict):
        payload["identity_map"] = {}
    if not isinstance(payload["profiles"], dict):
        payload["profiles"] = {}
    return payload


def _save_memory_root(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _legacy_profile_to_facts(profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    now_ts = float(time.time())
    facts: List[Dict[str, Any]] = []

    def add_fact(field: str, value: str) -> None:
        text = str(value or "").strip()
        if not text:
            return
        facts.append(
            {
                "id": f"panel-{field}-{abs(hash((field, text.casefold())))}",
                "field": field,
                "value": text,
                "normalized_value": text.casefold(),
                "status": "active",
                "confidence": 0.75,
                "source_text": "",
                "source_kind": "panel",
                "created_ts": now_ts,
                "updated_ts": now_ts,
                "last_confirmed_ts": now_ts,
                "explicit": True,
            }
        )

    add_fact("name", str(profile.get("name") or ""))
    add_fact("origin", str(profile.get("origin") or ""))
    add_fact("favorite_game", str(profile.get("favorite_game") or ""))
    add_fact("preferred_training_day", str(profile.get("preferred_training_day") or ""))
    add_fact("preferred_training_time", str(profile.get("preferred_training_time") or ""))
    for value in profile.get("likes", []) or []:
        add_fact("like", str(value))
    for value in profile.get("dislikes", []) or []:
        add_fact("dislike", str(value))
    for value in profile.get("goals", []) or []:
        add_fact("goal", str(value))
    return facts


def _normalize_memory_profile(user_id: str, incoming: Dict[str, Any], existing: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    base = dict(existing or {})
    base["display_name"] = str(incoming.get("display_name") or base.get("display_name") or user_id).strip() or user_id
    base["name"] = str(incoming.get("name") or base.get("name") or "").strip()
    for key in ("likes", "dislikes", "goals", "recent_notes", "memory_items", "dialog_turns", "facts", "episodes", "game_history"):
        value = incoming.get(key, base.get(key, []))
        if isinstance(value, list):
            base[key] = value
        else:
            base[key] = []
    for key in ("preferred_training_day", "preferred_training_time", "current_topic", "open_question", "dialog_summary", "origin", "favorite_game"):
        base[key] = str(incoming.get(key) or base.get(key) or "").strip()
    now_ts = float(time.time())
    base["first_seen_ts"] = float(incoming.get("first_seen_ts") or base.get("first_seen_ts") or now_ts)
    base["last_seen_ts"] = float(incoming.get("last_seen_ts") or base.get("last_seen_ts") or now_ts)
    base["utterance_count"] = int(incoming.get("utterance_count") or base.get("utterance_count") or 0)
    if not base["facts"]:
        base["facts"] = _legacy_profile_to_facts(base)
    return base


def _memory_payload(path: Path, selected_user_id: str, message: str) -> Dict[str, Any]:
    root = _load_memory_root(path)
    profiles = root.get("profiles", {})
    identity_map = root.get("identity_map", {})
    users: List[Dict[str, Any]] = []
    for user_id in sorted([str(key).strip() for key in profiles.keys() if str(key).strip()], key=str.lower):
        profile = profiles.get(user_id) if isinstance(profiles, dict) else {}
        if not isinstance(profile, dict):
            profile = {}
        sample_count = 0
        for identity in identity_map.values() if isinstance(identity_map, dict) else []:
            if isinstance(identity, dict) and str(identity.get("user_id") or "").strip() == user_id:
                sample_count += int(identity.get("sample_count") or 0)
        users.append(
            {
                "user_id": user_id,
                "display_name": str(profile.get("display_name") or user_id).strip() or user_id,
                "name": str(profile.get("name") or "").strip(),
                "likes_count": len(profile.get("likes", []) or []),
                "goals_count": len(profile.get("goals", []) or []),
                "recent_notes_count": len(profile.get("recent_notes", []) or []),
                "memory_items_count": len(profile.get("memory_items", []) or []),
                "facts_count": len([item for item in profile.get("facts", []) or [] if isinstance(item, dict) and str(item.get("status") or "active").strip().lower() == "active"]),
                "episodes_count": len(profile.get("episodes", []) or []),
                "utterance_count": int(profile.get("utterance_count") or 0),
                "sample_count": sample_count,
                "last_seen_iso": (
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(profile.get("last_seen_ts") or 0)))
                    if float(profile.get("last_seen_ts") or 0) > 0
                    else ""
                ),
            }
        )
    selected_profile = profiles.get(selected_user_id) if isinstance(profiles, dict) else {}
    if not isinstance(selected_profile, dict):
        selected_profile = {}
    selected_identity_keys = []
    for key, identity in identity_map.items() if isinstance(identity_map, dict) else []:
        if isinstance(identity, dict) and str(identity.get("user_id") or "").strip() == selected_user_id:
            selected_identity_keys.append(str(key))
    return {
        "status": "ok",
        "message": message,
        "path": str(path),
        "user_count": len(users),
        "users": users,
        "selected_user_id": selected_user_id,
        "selected_profile": selected_profile,
        "selected_identity_keys": selected_identity_keys,
    }


def _qmd_status_payload(root: Path) -> Dict[str, Any]:
    users_dir = root / "users"
    games_dir = root / "games"
    return {
        "status": "ok",
        "path": str(root),
        "users_dir": str(users_dir),
        "games_dir": str(games_dir),
        "user_docs": len(list(users_dir.glob("*.qmd"))) if users_dir.exists() else 0,
        "game_docs": len(list(games_dir.glob("*.qmd"))) if games_dir.exists() else 0,
    }


class DesktopCameraService:
    def __init__(self) -> None:
        self.width = max(64, DEFAULT_CAMERA_WIDTH)
        self.height = max(64, DEFAULT_CAMERA_HEIGHT)
        self.fps = max(1, DEFAULT_CAMERA_FPS)
        self.device_index = max(0, DEFAULT_CAMERA_DEVICE_INDEX)
        self.jpeg_quality = max(1, min(100, DEFAULT_CAMERA_JPEG_QUALITY))
        self.active_window_seconds = max(1.0, DEFAULT_CAMERA_ACTIVE_WINDOW_SECONDS)
        self.enabled = _read_bool({"value": _env("VOICE_AGENT_ENABLE_CAMERA", "true")}, "value", True)
        self.available = cv2 is not None and self.enabled

        self._lock = threading.Lock()
        self._capture = None
        self._capture_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._latest_jpeg: bytes = b""
        self._last_frame_ts = 0.0
        self._frame_count = 0
        self._client_active_until = 0.0
        self._last_error = ""
        self._opened_device_index: Optional[int] = None

    def close(self) -> None:
        self._stop_event.set()
        thread = None
        with self._lock:
            thread = self._capture_thread
            self._capture_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._release_capture()

    def ping(self) -> None:
        if not self.available:
            return
        with self._lock:
            self._client_active_until = time.time() + self.active_window_seconds
            should_start = self._capture_thread is None or not self._capture_thread.is_alive()
        if should_start:
            self._stop_event.clear()
            thread = threading.Thread(target=self._capture_loop, name="desktop-camera", daemon=True)
            with self._lock:
                self._capture_thread = thread
            thread.start()

    def status_payload(self) -> Dict[str, Any]:
        with self._lock:
            last_frame_age = time.time() - self._last_frame_ts if self._last_frame_ts > 0 else None
            active = self._capture is not None and self._last_frame_ts > 0 and (last_frame_age or 0.0) <= 2.5
            return {
                "status": "ok",
                "enabled": self.enabled,
                "available": self.available,
                "active": active,
                "message": self._last_error or ("camera ready" if self.available else "camera preview unavailable"),
                "frame_count": self._frame_count,
                "last_frame_age_seconds": round(last_frame_age, 3) if last_frame_age is not None else None,
                "device_index": self._opened_device_index if self._opened_device_index is not None else self.device_index,
                "width": self.width,
                "height": self.height,
                "fps": self.fps,
            }

    def get_latest_jpeg(self, *, wait_ms: int = 0, activate: bool = True) -> bytes:
        if activate:
            self.ping()
        deadline = time.time() + max(0, int(wait_ms)) / 1000.0
        while True:
            with self._lock:
                jpeg = bytes(self._latest_jpeg)
                frame_ts = self._last_frame_ts
            if jpeg and frame_ts > 0:
                return jpeg
            if wait_ms <= 0 or time.time() >= deadline:
                return b""
            time.sleep(0.03)

    def mjpeg_stream(self):
        boundary = b"--frame\r\nContent-Type: image/jpeg\r\nCache-Control: no-store\r\n\r\n"
        while not self._stop_event.is_set():
            frame = self.get_latest_jpeg(wait_ms=1200, activate=True)
            if frame:
                yield boundary + frame + b"\r\n"
            else:
                time.sleep(0.15)

    def _capture_loop(self) -> None:
        target_delay = 1.0 / float(max(1, self.fps))
        while not self._stop_event.is_set():
            if not self.available:
                time.sleep(0.25)
                continue
            with self._lock:
                active = time.time() <= self._client_active_until
            if not active:
                self._release_capture()
                time.sleep(0.1)
                continue
            capture = self._ensure_capture()
            if capture is None:
                time.sleep(1.0)
                continue
            started = time.time()
            ok, frame = capture.read()
            if not ok or frame is None:
                self._set_error("camera frame read failed")
                self._release_capture()
                time.sleep(0.25)
                continue
            try:
                ok, encoded = cv2.imencode(
                    ".jpg",
                    frame,
                    [int(cv2.IMWRITE_JPEG_QUALITY), int(self.jpeg_quality)],
                )
            except Exception as exc:
                self._set_error(f"camera jpeg encode failed: {exc}")
                ok = False
                encoded = None
            if ok and encoded is not None:
                with self._lock:
                    self._latest_jpeg = encoded.tobytes()
                    self._last_frame_ts = time.time()
                    self._frame_count += 1
                    self._last_error = ""
            elapsed = time.time() - started
            if elapsed < target_delay:
                time.sleep(target_delay - elapsed)
        self._release_capture()

    def _ensure_capture(self):
        with self._lock:
            existing = self._capture
        if existing is not None:
            return existing
        if cv2 is None:
            self._set_error("opencv-python is not installed")
            return None
        last_error = "camera open failed"
        candidates = [self.device_index]
        for idx in range(4):
            if idx not in candidates:
                candidates.append(idx)
        for candidate in candidates:
            capture = None
            try:
                if os.name == "nt":
                    capture = cv2.VideoCapture(candidate, cv2.CAP_DSHOW)
                    if not capture or not capture.isOpened():
                        if capture is not None:
                            capture.release()
                        capture = cv2.VideoCapture(candidate)
                else:
                    capture = cv2.VideoCapture(candidate)
                if capture is None or not capture.isOpened():
                    if capture is not None:
                        capture.release()
                    continue
                capture.set(cv2.CAP_PROP_FRAME_WIDTH, float(self.width))
                capture.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self.height))
                capture.set(cv2.CAP_PROP_FPS, float(self.fps))
                with self._lock:
                    self._capture = capture
                    self._opened_device_index = candidate
                    self._last_error = ""
                return capture
            except Exception as exc:
                last_error = str(exc)
                if capture is not None:
                    try:
                        capture.release()
                    except Exception:
                        pass
        self._set_error(last_error)
        return None

    def _release_capture(self) -> None:
        capture = None
        with self._lock:
            capture = self._capture
            self._capture = None
            self._opened_device_index = None
        if capture is not None:
            try:
                capture.release()
            except Exception:
                pass

    def _set_error(self, message: str) -> None:
        with self._lock:
            self._last_error = str(message or "").strip() or "camera error"


log_store = ConversationLogStore()
audio_agent = DesktopAudioAgent(log_store=log_store, asr_base_url=DEFAULT_ASR_BASE_URL)
camera_service = DesktopCameraService()
ollama_pull_requests: set[str] = set()
panel_http = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
app = FastAPI(title="Voice Agent Desktop Runtime")


@app.on_event("startup")
async def _on_startup() -> None:
    await audio_agent.start()
    try:
        _, _, _, _, merged = _load_launcher_config_pair()
        note = await _ensure_ollama_running(merged)
        if note:
            log_store.add("system", note, source="desktop_runtime")
    except Exception as exc:
        log_store.add("system", f"ollama autostart failed: {exc}", source="desktop_runtime")


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    await panel_http.aclose()
    camera_service.close()
    await audio_agent.stop()


def _panel_file_response(name: str) -> FileResponse:
    path = PANEL_DIR / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="panel asset not found")
    return FileResponse(path)


async def _probe_ollama(model: str) -> Dict[str, Any]:
    base_url = _resolve_ollama_base_url(_load_launcher_config_pair()[4])
    payload = {
        "reachable": False,
        "model_available": False,
        "error": "",
        "base_url": base_url,
        "model": model,
    }
    try:
        tags = await panel_http.get(f"{base_url}/api/tags")
        if not tags.is_success:
            payload["error"] = tags.text.strip() or f"HTTP {tags.status_code}"
            return payload
        payload["reachable"] = True
        data = tags.json()
        models = data.get("models", []) if isinstance(data, dict) else []
        payload["model_available"] = any(str(item.get("name") or "").strip() == model for item in models if isinstance(item, dict))
        return payload
    except Exception as exc:
        payload["error"] = str(exc)
        return payload


def _bool_from_env(name: str, default: bool) -> bool:
    return _read_bool({"value": _env(name, "true" if default else "false")}, "value", default)


async def _ensure_ollama_running(merged: Dict[str, Any]) -> str:
    if not _bool_from_env("VOICE_AGENT_AUTOSTART_OLLAMA", True):
        return ""
    runtime = _build_runtime_payload(
        merged,
        user_path=_resolve_launcher_config_path(),
        default_path=_resolve_launcher_default_config_path(),
        message="runtime config",
    )
    if runtime["conversation_profile"] != "local":
        return ""
    model = runtime["ollama_model"]
    probe = await _probe_ollama(model)
    if probe["reachable"]:
        return ""

    ollama_exe = shutil.which("ollama") or ""
    if not ollama_exe:
        return "ollama is not installed; local response model will stay unavailable."

    try:
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(
            [ollama_exe, "serve"],
            cwd=str(REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
    except Exception as exc:
        return f"failed to start ollama serve: {exc}"

    last_error = ""
    for _ in range(20):
        await asyncio.sleep(0.5)
        probe = await _probe_ollama(model)
        if probe["reachable"]:
            if probe["model_available"]:
                return "started local Ollama service."
            if _bool_from_env("VOICE_AGENT_AUTOPULL_OLLAMA_MODEL", True):
                if model not in ollama_pull_requests:
                    ollama_pull_requests.add(model)
                    try:
                        subprocess.Popen(
                            [ollama_exe, "pull", model],
                            cwd=str(REPO_ROOT),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=creation_flags,
                        )
                        return f"started local Ollama service; model pull started: {model}"
                    except Exception as exc:
                        return f"started local Ollama service; failed to pull model {model}: {exc}"
            return f"started local Ollama service; model still missing: {model}"
        last_error = str(probe.get("error") or "").strip()
    if last_error:
        return f"started ollama serve, but it is still warming up: {last_error}"
    return "started ollama serve, but it has not responded yet."


def _resolve_vision_model(model: Optional[str], merged: Dict[str, Any]) -> str:
    requested = str(model or "").strip()
    if requested:
        return requested
    return _read_env_string(merged, "OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)


def _camera_no_frame_hint() -> str:
    status = camera_service.status_payload()
    return (
        "no camera frame. Start Preview, wait 1-2 seconds, and verify a webcam is available "
        f"(active={status.get('active')}, frame_count={status.get('frame_count')}, message={status.get('message')})."
    )


def _piper_prereq_payload() -> Dict[str, Any]:
    exe = _env("PIPER_EXECUTABLE", "piper")
    model = _env("PIPER_MODEL_PATH", "")
    config = _env("PIPER_CONFIG_PATH", "")
    model_path = Path(os.path.expandvars(model)).expanduser() if model else None
    exe_exists = bool(exe) and (
        Path(exe).exists() if any(marker in exe for marker in ("\\", "/", ":")) else bool(shutil.which(exe))
    )
    model_exists = model_path.exists() if model_path is not None else False
    return {
        "piper_ready": exe_exists and model_exists,
        "piper_executable_path": exe,
        "piper_model_path": str(model_path) if model_path is not None else "",
        "piper_config_path": config,
        "piper_executable_exists": exe_exists,
        "piper_model_exists": model_exists,
    }


async def _apply_runtime_live(merged: Dict[str, Any]) -> str:
    runtime = _build_runtime_payload(
        merged,
        user_path=_resolve_launcher_config_path(),
        default_path=_resolve_launcher_default_config_path(),
        message="saved",
    )
    intent_obj = _ensure_dict(merged, "intent")
    os.environ["INTENT_LAUNCH_TRIGGERS"] = json.dumps(
        _read_string_list(intent_obj, "launch_triggers", DEFAULT_LAUNCH_TRIGGERS),
        ensure_ascii=False,
    )
    os.environ["INTENT_EXIT_KEYWORDS"] = json.dumps(
        _normalize_exit_keywords(intent_obj.get("exit_keywords")),
        ensure_ascii=False,
    )
    os.environ["INTENT_MANIFEST_PATH"] = runtime["intent_manifest_path"]
    os.environ["GAME_LAUNCHER_MANIFEST_PATH"] = runtime["effective_game_manifest_path"]
    os.environ["DIALOG_USER_MEMORY_PATH"] = str(_resolve_memory_path(merged))
    os.environ["VOICE_LOCAL_STREAMING_ASR_MODE"] = runtime["local_streaming_asr_mode"]
    os.environ["VOICE_CLOUD_STREAMING_ASR_MODE"] = runtime["cloud_streaming_asr_mode"]
    os.environ["VOICE_ASR_HOTWORD_STRATEGY"] = runtime["asr_hotword_strategy"]
    os.environ["VOICE_ASR_STABLE_PARTIAL_REPEATS"] = str(runtime["asr_stable_partial_repeats"])
    request_payload = {
        "pipeline_mode": runtime["conversation_pipeline_mode"],
        "profile": runtime["conversation_profile"],
        "local_asr_mode": runtime["local_asr_mode"],
        "cloud_asr_mode": runtime["cloud_asr_mode"],
        "cloud_response_provider": "openai",
        "openai_api_key": runtime["openai_api_key"],
        "openai_base_url": runtime["openai_base_url"],
        "openai_transcribe_model": runtime["openai_transcribe_model"],
        "openai_transcribe_prompt": runtime["openai_transcribe_prompt"],
        "openai_response_model": runtime["openai_response_model"],
        "local_response_model": runtime["ollama_model"],
    }
    notes: List[str] = []
    try:
        response = await panel_http.post(f"{DEFAULT_ASR_BASE_URL}/conversation/config", json=request_payload)
        if not response.is_success:
            notes.append(response.text.strip() or f"conversation config HTTP {response.status_code}")
    except Exception as exc:
        notes.append(f"conversation config failed: {exc}")

    try:
        await audio_agent.apply_runtime_config(
            pipeline_mode=runtime["conversation_pipeline_mode"],
            profile=runtime["conversation_profile"],
            local_asr_mode=runtime["local_asr_mode"],
            cloud_asr_mode=runtime["cloud_asr_mode"],
            local_streaming_asr_mode=runtime["local_streaming_asr_mode"],
            cloud_streaming_asr_mode=runtime["cloud_streaming_asr_mode"],
            hotword_strategy=runtime["asr_hotword_strategy"],
            stable_partial_repeats=runtime["asr_stable_partial_repeats"],
        )
    except Exception as exc:
        notes.append(f"audio runtime failed: {exc}")
    if not notes:
        return "saved and applied to runtime."
    return "saved; live apply partial: " + " | ".join(notes)


async def _pick_file_dialog(title: str, filter_text: str, initial_dir: str, initial_filename: str) -> Tuple[bool, bool, str, str]:
    def _worker() -> Tuple[bool, bool, str, str]:
        try:
            import tkinter as tk
            from tkinter import filedialog
        except Exception as exc:
            return False, False, "", str(exc)
        root = tk.Tk()
        root.withdraw()
        filetypes = []
        parts = [part.strip() for part in str(filter_text or "").split("|")]
        for idx in range(0, len(parts), 2):
            label = parts[idx]
            pattern = parts[idx + 1] if idx + 1 < len(parts) else "*.*"
            filetypes.append((label or pattern, pattern))
        filename = filedialog.askopenfilename(
            title=title,
            initialdir=initial_dir or None,
            initialfile=initial_filename or None,
            filetypes=filetypes or [("All Files", "*.*")],
        )
        root.destroy()
        if not filename:
            return True, True, "", ""
        return True, False, str(Path(filename).resolve()), ""

    return await asyncio.to_thread(_worker)


@app.get("/")
@app.get("/index.html")
@app.get("/panel.html")
async def panel_index() -> FileResponse:
    return _panel_file_response("panel.html")


@app.get("/games")
@app.get("/games.html")
async def panel_games() -> FileResponse:
    return _panel_file_response("games.html")


@app.get("/runtime")
@app.get("/runtime.html")
async def panel_runtime() -> FileResponse:
    return _panel_file_response("runtime.html")


@app.get("/memory")
@app.get("/memory.html")
async def panel_memory() -> FileResponse:
    return _panel_file_response("memory.html")


@app.get("/setup")
@app.get("/setup.html")
async def panel_setup() -> FileResponse:
    return _panel_file_response("setup.html")


@app.get("/sdk")
@app.get("/sdk.html")
async def panel_sdk() -> FileResponse:
    return _panel_file_response("sdk.html")


@app.get("/telemetry")
@app.get("/telemetry.html")
async def panel_telemetry() -> FileResponse:
    return _panel_file_response("telemetry.html")


@app.get("/healthz")
async def healthz() -> Dict[str, Any]:
    status = audio_agent.status()
    return {
        "status": "ok",
        "message": "desktop runtime alive",
        "listening": status.listening,
        "assistant_speaking": status.assistant_speaking,
        "last_error": status.last_error,
    }


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)


@app.get("/api/logs")
async def api_logs() -> Dict[str, Any]:
    version, entries = log_store.snapshot_with_version()
    return {"version": version, "entries": entries}


@app.get("/api/logs/stream")
async def api_logs_stream() -> StreamingResponse:
    async def _iter():
        version = -1
        while True:
            next_version, entries = await asyncio.to_thread(log_store.wait_for_update, version, 15.0)
            if next_version == version:
                yield b": keep-alive\n\n"
                continue
            version = next_version
            payload = json.dumps({"version": version, "entries": entries}, ensure_ascii=False)
            yield f"data: {payload}\n\n".encode("utf-8")

    return StreamingResponse(
        _iter(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/voice/options")
async def api_voice_options() -> Dict[str, Any]:
    models_dir = _env("VOICE_MODELS_DIR", _env("PIPER_MODELS_DIR", r"D:\piper\models"))
    models: List[str] = []
    current_model = audio_agent.active_tts_model or _env("PIPER_MODEL_PATH", "")
    try:
        path = Path(os.path.expandvars(models_dir)).expanduser()
        if path.exists():
            for file in sorted(path.rglob("*.onnx")):
                models.append(str(file))
    except Exception:
        models = []
    return {
        "voices": ["en_US"],
        "current": audio_agent.active_voice_code,
        "models": models or ([current_model] if current_model else []),
        "modelCurrent": current_model,
    }


@app.get("/api/qwen/options")
async def api_qwen_options() -> Dict[str, Any]:
    return {"speakers": QWEN_SPEAKERS, "current": audio_agent.active_qwen_speaker}


@app.post("/api/voice")
async def api_voice(request: Request) -> Dict[str, Any]:
    payload = await request.json()
    action = str(payload.get("action") or "").strip().lower()
    if action in {"set", "set_voice"}:
        voice = str(payload.get("voice") or payload.get("value") or "").strip()
        if not voice:
            raise HTTPException(status_code=400, detail="voice code required")
        await audio_agent.set_tts_options(voice=voice)
        return {"status": "ok", "message": f"voice set to {voice}"}
    if action in {"set_model", "model"}:
        model = str(payload.get("model") or payload.get("value") or "").strip()
        if not model:
            raise HTTPException(status_code=400, detail="model identifier required")
        await audio_agent.set_tts_options(model=model)
        return {"status": "ok", "message": f"tts model set to {model}"}
    raise HTTPException(status_code=400, detail="unknown voice action")


@app.post("/api/speak")
async def api_speak(request: Request) -> Dict[str, Any]:
    payload = await request.json()
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    await audio_agent.manual_speak(
        text=text,
        voice=str(payload.get("voice") or "").strip() or audio_agent.active_voice_code,
        model=str(payload.get("model") or "").strip() or audio_agent.active_tts_model,
        instruct=str(payload.get("instruct") or "").strip(),
        backend="piper",
        source="tester_panel",
    )
    return {"status": "ok", "message": "playing locally"}


@app.post("/api/qwen/speak")
async def api_qwen_speak(request: Request) -> Dict[str, Any]:
    payload = await request.json()
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    speaker = str(payload.get("speaker") or payload.get("voice") or "").strip() or audio_agent.active_qwen_speaker
    await audio_agent.manual_speak(
        text=text,
        voice=speaker,
        instruct=str(payload.get("instruct") or "").strip(),
        backend="qwen",
        source="tester_panel_qwen",
    )
    return {"status": "ok", "message": "playing locally (qwen)"}


@app.get("/api/asr")
async def api_asr_status() -> Dict[str, Any]:
    return await _build_asr_status_payload()


async def _build_asr_status_payload() -> Dict[str, Any]:
    status = audio_agent.status()
    payload: Dict[str, Any] = {
        "status": "ok",
        "message": "streaming asr status",
        "mode": str(status.asr_mode or ""),
        "streaming_backend": str(status.streaming_backend or ""),
        "available_modes": [str(item or "") for item in list(status.streaming_available_modes or [])],
        "listening": bool(status.listening),
        "assistant_speaking": bool(status.assistant_speaking),
        "supports_hotwords": bool(status.supports_hotwords),
        "hotwords_count": int(status.hotwords_count),
        "hotword_strategy": str(status.hotword_strategy or ""),
        "current_partial": str(status.current_partial or ""),
        "stable_partial": str(status.stable_partial or ""),
        "moonshine_available": bool(status.moonshine_available),
        "input_level_dbfs": _safe_json_float(status.input_level_dbfs, -96.0),
        "input_peak_dbfs": _safe_json_float(status.input_peak_dbfs, -96.0),
        "noise_floor_dbfs": _safe_json_float(status.noise_floor_dbfs, -72.0),
        "frontend_gain_db": _safe_json_float(status.frontend_gain_db, 0.0),
        "speech_active": bool(status.speech_active),
        "clipped_recently": bool(status.clipped_recently),
        "clip_events": int(status.clip_events),
        "queued_input_frames": int(status.queued_input_frames),
        "dropped_input_frames": int(status.dropped_input_frames),
        "last_error": str(status.last_error or ""),
        "live_captions_available": bool(getattr(status, "live_captions_available", False)),
        "live_captions_output_path": str(getattr(status, "live_captions_output_path", "") or ""),
    }
    try:
        response = await panel_http.get(f"{DEFAULT_ASR_BASE_URL}/transcribe/config")
        if response.is_success:
            payload["server_transcribe"] = response.json()
    except Exception:
        pass
    return payload


@app.post("/api/asr/backend")
async def api_asr_backend_update(request: Request) -> Dict[str, Any]:
    payload = await request.json()
    action = str(payload.get("action") or "").strip().lower()
    if action in {"", "status"}:
        response = await panel_http.get(f"{DEFAULT_ASR_BASE_URL}/transcribe/config")
        if not response.is_success:
            raise HTTPException(status_code=response.status_code, detail=response.text.strip() or "backend asr status failed")
        data = response.json()
        data["status"] = "ok"
        return data
    if action in {"set_mode", "mode"}:
        requested_mode = normalize_asr_mode(payload.get("mode") or payload.get("value"))
        response = await panel_http.post(f"{DEFAULT_ASR_BASE_URL}/transcribe/config", json={"mode": requested_mode})
        if not response.is_success:
            raise HTTPException(status_code=response.status_code, detail=response.text.strip() or "backend asr update failed")
        data = response.json()
        data["status"] = "ok"
        data["message"] = f"backend asr mode set to {data.get('mode') or requested_mode}"
        return data
    raise HTTPException(status_code=400, detail="unknown backend asr action")


@app.post("/api/asr")
async def api_asr_update(request: Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
        action = str(payload.get("action") or "").strip().lower()
        if action in {"", "status"}:
            return await _build_asr_status_payload()
        if action in {"set_mode", "mode"}:
            requested_mode = normalize_streaming_asr_mode(payload.get("mode") or payload.get("value"))
            await audio_agent.set_asr_mode(requested_mode)
            result = await _build_asr_status_payload()
            actual_mode = str(result.get("mode") or "").strip()
            if actual_mode and actual_mode != requested_mode:
                result["message"] = f"requested {requested_mode}; runtime normalized it to {actual_mode}."
            else:
                result["message"] = f"streaming asr mode set to {actual_mode or requested_mode}"
            return result
        if action in {"start_listening", "resume_listening"}:
            await audio_agent.set_listening(True)
            result = await _build_asr_status_payload()
            result["message"] = "agent listening started"
            return result
        if action in {"pause_listening", "stop_listening"}:
            await audio_agent.set_listening(False)
            result = await _build_asr_status_payload()
            result["message"] = "agent listening paused"
            return result
        if action in {"set_listening", "listening"}:
            target = bool(payload.get("listening"))
            await audio_agent.set_listening(target)
            result = await _build_asr_status_payload()
            result["message"] = "agent listening started" if target else "agent listening paused"
            return result
        raise HTTPException(status_code=400, detail="unknown asr action")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"asr update failed: {exc}") from exc


@app.get("/api/llm/prompt")
async def api_llm_prompt_get() -> Dict[str, Any]:
    try:
        response = await panel_http.get(f"{DEFAULT_ASR_BASE_URL}/respond/config")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not response.is_success:
        raise HTTPException(status_code=response.status_code, detail=response.text.strip() or "failed to load llm prompt")
    return response.json()


@app.post("/api/llm/prompt")
async def api_llm_prompt_post(request: Request) -> Dict[str, Any]:
    payload = await request.json()
    try:
        if bool(payload.get("reset")):
            response = await panel_http.post(f"{DEFAULT_ASR_BASE_URL}/respond/config", json={"reset": True})
        else:
            prompt = str(payload.get("prompt") or "").strip()
            if not prompt:
                raise HTTPException(status_code=400, detail="prompt required unless reset=true")
            response = await panel_http.post(f"{DEFAULT_ASR_BASE_URL}/respond/config", json={"system_prompt": prompt})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not response.is_success:
        raise HTTPException(status_code=response.status_code, detail=response.text.strip() or "failed to update llm prompt")
    latest = await panel_http.get(f"{DEFAULT_ASR_BASE_URL}/respond/config")
    if not latest.is_success:
        raise HTTPException(status_code=latest.status_code, detail=latest.text.strip() or "failed to load updated llm prompt")
    return latest.json()


@app.get("/api/runtime/config")
async def api_runtime_config_get() -> Dict[str, Any]:
    user_path, default_path, _, _, merged = _load_launcher_config_pair()
    return _build_runtime_payload(merged, user_path=user_path, default_path=default_path, message="runtime config")


@app.post("/api/runtime/config")
async def api_runtime_config_post(request: Request) -> Dict[str, Any]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="json object body is required")
    user_path, default_path, _, user_cfg, merged = _load_launcher_config_pair()
    openai_obj = _ensure_dict(user_cfg, "openai")
    intent_obj = _ensure_dict(user_cfg, "intent")
    env_obj = _ensure_dict(user_cfg, "env")
    paths_obj = _ensure_dict(user_cfg, "paths")

    if "intent_manifest_path" in payload:
        paths_obj["intent_manifest"] = _normalize_path(payload.get("intent_manifest_path"), base_dir=REPO_ROOT, allow_command_name=False)
    if "game_manifest_path" in payload:
        paths_obj["game_manifest"] = _normalize_path(payload.get("game_manifest_path"), base_dir=REPO_ROOT, allow_command_name=False)
    if "openai_api_key" in payload:
        openai_obj["api_key"] = str(payload.get("openai_api_key") or "").strip()
    if "openai_transcribe_model" in payload:
        openai_obj["transcribe_model"] = str(payload.get("openai_transcribe_model") or "").strip()
    if "openai_base_url" in payload:
        openai_obj["base_url"] = str(payload.get("openai_base_url") or "").strip()
    if "openai_transcribe_prompt" in payload:
        openai_obj["transcribe_prompt"] = str(payload.get("openai_transcribe_prompt") or "").strip()
    if "ollama_model" in payload:
        env_obj["OLLAMA_MODEL"] = str(payload.get("ollama_model") or "").strip()
    if "conversation_pipeline_mode" in payload:
        env_obj["VOICE_PIPELINE_MODE"] = _normalize_pipeline_mode(payload.get("conversation_pipeline_mode"))
    if "conversation_profile" in payload:
        env_obj["VOICE_CONVERSATION_PROFILE"] = _normalize_profile(payload.get("conversation_profile"))
    if "local_asr_mode" in payload:
        env_obj["VOICE_LOCAL_ASR_MODE"] = normalize_asr_mode(payload.get("local_asr_mode"))
    if "cloud_asr_mode" in payload:
        env_obj["VOICE_CLOUD_ASR_MODE"] = normalize_asr_mode(payload.get("cloud_asr_mode"))
    if "local_streaming_asr_mode" in payload:
        env_obj["VOICE_LOCAL_STREAMING_ASR_MODE"] = normalize_streaming_asr_mode(payload.get("local_streaming_asr_mode"))
    if "cloud_streaming_asr_mode" in payload:
        env_obj["VOICE_CLOUD_STREAMING_ASR_MODE"] = normalize_streaming_asr_mode(payload.get("cloud_streaming_asr_mode"))
    if "asr_hotword_strategy" in payload:
        env_obj["VOICE_ASR_HOTWORD_STRATEGY"] = normalize_hotword_strategy(payload.get("asr_hotword_strategy"))
    if "asr_stable_partial_repeats" in payload:
        try:
            repeats = max(1, int(payload.get("asr_stable_partial_repeats") or 2))
        except Exception:
            repeats = 2
        env_obj["VOICE_ASR_STABLE_PARTIAL_REPEATS"] = str(repeats)
    if "openai_response_model" in payload:
        env_obj["OPENAI_RESPONSE_MODEL"] = str(payload.get("openai_response_model") or "").strip()
    if "launch_triggers" in payload:
        intent_obj["launch_triggers"] = [part.strip() for part in str(payload.get("launch_triggers") or "").replace(";", ",").split(",") if part.strip()]
    if "exit_keywords" in payload:
        intent_obj["exit_keywords"] = _normalize_exit_keywords(payload.get("exit_keywords"))
    if "use_llm_intent_classifier" in payload:
        intent_obj["use_llm_classifier"] = bool(payload.get("use_llm_intent_classifier"))
    if "use_moonshine_intent_recognizer" in payload:
        intent_obj["use_moonshine_intent_recognizer"] = bool(payload.get("use_moonshine_intent_recognizer"))

    intent_obj["exit_keywords"] = _normalize_exit_keywords(intent_obj.get("exit_keywords"))
    _save_launcher_user_config(user_path, user_cfg)
    merged = _normalize_launcher_config(_deep_merge_dict(_load_json_object(default_path), _load_json_object(user_path)))
    message = await _apply_runtime_live(merged)
    ollama_note = await _ensure_ollama_running(merged)
    if ollama_note:
        message = f"{message} {ollama_note}".strip()
    return _build_runtime_payload(merged, user_path=user_path, default_path=default_path, message=message)


@app.get("/api/runtime/prereq")
async def api_runtime_prereq() -> Dict[str, Any]:
    _, _, _, _, merged = _load_launcher_config_pair()
    model = _read_env_string(merged, "OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
    probe = await _probe_ollama(model)
    payload = _piper_prereq_payload()
    ollama_exe = shutil.which("ollama") or ""
    payload.update(
        {
            "status": "ok",
            "ollama_base_url": probe["base_url"],
            "ollama_executable_path": ollama_exe,
            "ollama_installed": bool(ollama_exe),
            "ollama_running": probe["reachable"],
            "ollama_model": model,
            "ollama_model_available": probe["model_available"],
            "ollama_error": probe["error"],
            "ollama_download_url": "https://ollama.com/download/windows",
            "ollama_install_command": "winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements",
            "ollama_pull_command": "ollama pull " + model,
            "needs_piper_setup": not payload["piper_ready"],
            "needs_ollama_setup": not probe["reachable"] or not probe["model_available"],
        }
    )
    return payload


@app.post("/api/runtime/ollama")
async def api_runtime_ollama(request: Request) -> Dict[str, Any]:
    payload = await request.json()
    action = str(payload.get("action") or "").strip().lower()
    model = str(payload.get("model") or DEFAULT_OLLAMA_MODEL).strip() or DEFAULT_OLLAMA_MODEL
    if action == "open_download":
        webbrowser.open("https://ollama.com/download/windows")
        return {"status": "ok", "message": "opened Ollama download page"}
    if action == "install":
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", "winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements"],
                cwd=str(REPO_ROOT),
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"status": "ok", "message": "started Ollama installation via winget"}
    if action == "pull_model":
        try:
            subprocess.Popen(["ollama", "pull", model], cwd=str(REPO_ROOT))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"status": "ok", "message": f"started model pull: {model}"}
    raise HTTPException(status_code=400, detail="unknown ollama action")


@app.post("/api/face")
async def api_face(request: Request) -> Dict[str, Any]:
    payload = await request.json()
    try:
        await audio_agent.publish_face(payload if isinstance(payload, dict) else {})
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "ok", "message": "face command sent"}


@app.post("/api/led")
async def api_led(request: Request) -> Dict[str, Any]:
    payload = await request.json()
    mode = str((payload or {}).get("mode") or "").strip().lower() if isinstance(payload, dict) else ""
    if mode not in {"breathe", "solid", "random", "off"}:
        raise HTTPException(status_code=400, detail="unknown led mode")
    try:
        await audio_agent.publish_led(payload if isinstance(payload, dict) else {})
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "ok", "message": "led command sent"}


@app.post("/api/flower")
async def api_flower(request: Request) -> Dict[str, Any]:
    payload = await request.json()
    action = str((payload or {}).get("action") or "").strip().lower() if isinstance(payload, dict) else ""
    if action not in {"open", "close", "open_hold", "close_hold", "center", "stop", "open_slow", "close_slow"}:
        raise HTTPException(status_code=400, detail="unknown flower action")
    try:
        await audio_agent.publish_flower(payload if isinstance(payload, dict) else {})
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "ok", "message": "flower command sent"}


@app.post("/api/game")
async def api_game(request: Request) -> Dict[str, Any]:
    payload = await request.json()
    action = str(payload.get("action") or "").strip().lower()
    name = str(payload.get("name") or "").strip()
    try:
        await audio_agent.handle_panel_game_intent(action=action, name=name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "ok", "message": ("launching " + name) if action in {"launch", "open"} else "exit intent sent"}


@app.get("/api/game/manifest")
async def api_game_manifest_get() -> Dict[str, Any]:
    merged = _load_launcher_config_pair()[4]
    return _manifest_status_payload(_resolve_game_manifest_path(merged))


@app.post("/api/game/manifest")
async def api_game_manifest_post(request: Request) -> Dict[str, Any]:
    payload = await request.json()
    games = payload.get("games")
    if not isinstance(games, list):
        raise HTTPException(status_code=400, detail="games array is required")
    merged = _load_launcher_config_pair()[4]
    path = _resolve_game_manifest_path(merged)
    root = _load_game_manifest(path)
    next_games: List[Dict[str, Any]] = []
    seen = set()
    for index, row in enumerate(games):
        if not isinstance(row, dict):
            continue
        game_id = str(row.get("id") or row.get("name") or "").strip()
        if not game_id:
            raise HTTPException(status_code=400, detail=f"invalid game id at index {index}")
        if game_id.lower() in seen:
            raise HTTPException(status_code=400, detail=f"duplicate game id: {game_id}")
        seen.add(game_id.lower())
        next_games.append(
            {
                "id": game_id,
                "name": str(row.get("name") or game_id).strip() or game_id,
                "synonyms": [str(value).strip() for value in row.get("keywords", []) or [] if str(value).strip()],
                "exec": _normalize_path(row.get("exec"), base_dir=path.parent, allow_command_name=True),
                "workdir": _normalize_path(row.get("workdir"), base_dir=path.parent, allow_command_name=False),
                "args": [],
                "env": {},
                "description": str(row.get("description") or "").strip(),
                "how_to_play": str(row.get("how_to_play") or "").strip(),
                "players_min": max(1, int(row.get("players_min") or 1)),
                "players_max": max(1, int(row.get("players_max") or 4)),
                "tags": [str(value).strip() for value in row.get("tags", []) or [] if str(value).strip()],
                "activity_level": str(row.get("activity_level") or "").strip(),
                "recommendation_weight": float(row.get("recommendation_weight") or 0.5),
            }
        )
    root["games"] = next_games
    _save_game_manifest(path, root)
    return {"status": "ok", "message": "saved. restart intent_service and game_launcher to apply immediately."}


@app.post("/api/file/pick")
async def api_file_pick(request: Request) -> Dict[str, Any]:
    payload = await request.json()
    success, cancelled, selected_path, error = await _pick_file_dialog(
        str(payload.get("title") or "Select File").strip(),
        str(payload.get("filter") or "Executable Files (*.exe)|*.exe|All Files (*.*)|*.*").strip(),
        _normalize_path(payload.get("initial_dir"), base_dir=REPO_ROOT, allow_command_name=False),
        _normalize_path(payload.get("initial_filename"), base_dir=REPO_ROOT, allow_command_name=False),
    )
    if not success:
        raise HTTPException(status_code=500, detail=error or "file picker failed")
    return {
        "status": "ok",
        "cancelled": cancelled,
        "path": selected_path,
        "directory": str(Path(selected_path).parent) if selected_path else "",
    }


@app.get("/api/memory")
async def api_memory_get(user_id: str = Query(default="")) -> Dict[str, Any]:
    merged = _load_launcher_config_pair()[4]
    return _memory_payload(_resolve_memory_path(merged), str(user_id or "").strip(), "memory loaded")


@app.post("/api/memory")
async def api_memory_post(request: Request) -> Dict[str, Any]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="json object body is required")
    action = str(payload.get("action") or "").strip().lower()
    user_id = str(payload.get("user_id") or "").strip()
    merged = _load_launcher_config_pair()[4]
    path = _resolve_memory_path(merged)
    root = _load_memory_root(path)
    profiles = root["profiles"]
    identity_map = root["identity_map"]
    if action == "update_user_raw":
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id is required")
        profile = payload.get("profile")
        if not isinstance(profile, dict):
            raise HTTPException(status_code=400, detail="profile object is required")
        profiles[user_id] = _normalize_memory_profile(user_id, profile, profiles.get(user_id))
        _save_memory_root(path, root)
        return _memory_payload(path, user_id, "memory user updated")
    if action == "delete_user":
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id is required")
        profiles.pop(user_id, None)
        if isinstance(identity_map, dict):
            for key in list(identity_map.keys()):
                mapped = identity_map.get(key)
                if isinstance(mapped, dict) and str(mapped.get("user_id") or "").strip() == user_id:
                    identity_map.pop(key, None)
        _save_memory_root(path, root)
        return _memory_payload(path, "", "memory user deleted")
    raise HTTPException(status_code=400, detail="unknown action")


@app.get("/api/qmd")
async def api_qmd_get() -> Dict[str, Any]:
    merged = _load_launcher_config_pair()[4]
    return _qmd_status_payload(_resolve_qmd_root(merged))


@app.post("/api/qmd")
async def api_qmd_post(request: Request) -> Dict[str, Any]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="json object body is required")
    action = str(payload.get("action") or "").strip().lower()
    merged = _load_launcher_config_pair()[4]
    qmd_root = _resolve_qmd_root(merged)
    memory_path = _resolve_memory_path(merged)
    manifest_path = _resolve_game_manifest_path(merged)
    if action == "export":
        memory_root = _load_memory_root(memory_path)
        manifest_root = _load_game_manifest(manifest_path)
        memory_result = export_memory_qmd(memory_root, qmd_root)
        game_result = export_game_qmd(manifest_root, qmd_root)
        response = _qmd_status_payload(qmd_root)
        response["message"] = "qmd export complete"
        response["memory_result"] = memory_result
        response["game_result"] = game_result
        return response
    if action == "import":
        memory_root = _load_memory_root(memory_path)
        manifest_root = _load_game_manifest(manifest_path)
        memory_root, memory_result = import_memory_qmd(memory_root, qmd_root)
        manifest_root, game_result = import_game_qmd(manifest_root, qmd_root)
        _save_memory_root(memory_path, memory_root)
        _save_game_manifest(manifest_path, manifest_root)
        response = _qmd_status_payload(qmd_root)
        response["message"] = "qmd import complete"
        response["memory_result"] = memory_result
        response["game_result"] = game_result
        return response
    raise HTTPException(status_code=400, detail="unknown action")


@app.get("/camera.jpg")
async def camera_jpg() -> Response:
    jpeg = await asyncio.to_thread(camera_service.get_latest_jpeg, wait_ms=1500, activate=True)
    if not jpeg:
        raise HTTPException(status_code=503, detail=_camera_no_frame_hint())
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.get("/camera.mjpg")
async def camera_mjpg() -> StreamingResponse:
    if not camera_service.available:
        raise HTTPException(status_code=503, detail="camera preview unavailable")
    camera_service.ping()
    return StreamingResponse(
        camera_service.mjpeg_stream(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/api/camera/status")
async def api_camera_status() -> Dict[str, Any]:
    return camera_service.status_payload()


@app.get("/api/camera/ping")
@app.post("/api/camera/ping")
async def api_camera_ping() -> Dict[str, Any]:
    camera_service.ping()
    payload = camera_service.status_payload()
    payload["message"] = payload.get("message") or "camera heartbeat"
    return payload


@app.post("/api/vision/describe")
async def api_vision_describe(request: Request) -> Dict[str, Any]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="json object body is required")
    if not camera_service.available:
        raise HTTPException(status_code=503, detail="camera preview unavailable")
    jpeg = await asyncio.to_thread(camera_service.get_latest_jpeg, wait_ms=1500, activate=True)
    if not jpeg:
        raise HTTPException(status_code=503, detail=_camera_no_frame_hint())

    _, _, _, _, merged = _load_launcher_config_pair()
    prompt = str(payload.get("prompt") or "").strip() or DEFAULT_VISION_PROMPT
    model = _resolve_vision_model(payload.get("model"), merged)
    probe = await _probe_ollama(model)
    if not probe["reachable"]:
        raise HTTPException(
            status_code=503,
            detail=f"vision backend unavailable at {probe['base_url']}: {probe['error']}",
        )
    if not probe["model_available"]:
        raise HTTPException(
            status_code=503,
            detail=f"vision model not available in Ollama: {model}. Run: ollama pull {model}",
        )

    image_base64 = base64.b64encode(jpeg).decode("ascii")
    try:
        response = await panel_http.post(
            f"{probe['base_url']}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "think": False,
                "stream": False,
                "images": [image_base64],
            },
            timeout=httpx.Timeout(300.0),
        )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=504,
            detail="vision request timed out after 300s (5 minutes). Check Ollama is running and model is loaded.",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"vision request failed: {exc}") from exc

    if not response.is_success:
        raise HTTPException(status_code=response.status_code, detail=response.text.strip() or "vision request failed")

    body = response.json() if response.content else {}
    description = str(body.get("response") or "").strip() if isinstance(body, dict) else ""
    if not description:
        description = response.text.strip()
    return {
        "status": "ok",
        "message": "vision description ready",
        "model": model,
        "prompt": prompt,
        "description": description,
    }


if __name__ == "__main__":
    import uvicorn

    port = int(_env("PANEL_PORT", str(DEFAULT_PANEL_PORT)) or str(DEFAULT_PANEL_PORT))
    uvicorn.run("desktop_runtime:app", host="0.0.0.0", port=port)
