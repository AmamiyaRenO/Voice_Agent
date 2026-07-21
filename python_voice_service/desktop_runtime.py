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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

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
    from .speaker_id import resolve_speaker_profiles_path
except Exception:
    from speaker_id import resolve_speaker_profiles_path
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
    return Path(__file__).resolve().parents[1]


def _resolve_state_dir(app_root: Path) -> Path:
    raw_value = os.getenv("VOICE_AGENT_STATE_DIR")
    raw = raw_value.strip() if raw_value else ""
    if raw:
        path = Path(os.path.expandvars(raw)).expanduser()
        if path.is_absolute():
            return path
        return (app_root / path).resolve()
    return app_root / "runtime"


def _safe_json_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except Exception:
        return float(default)
    if not math.isfinite(number):
        return float(default)
    return number


def _parse_user_sequence(user_id: str) -> Optional[int]:
    normalized = str(user_id or "").strip().lower()
    if not normalized.startswith("user_"):
        return None
    suffix = normalized[5:]
    if not suffix.isdigit():
        return None
    try:
        return max(1, int(suffix))
    except Exception:
        return None


def _format_user_id(index: int) -> str:
    return f"user_{max(1, int(index)):03d}"


def _next_memory_user_id(root: Dict[str, Any]) -> Tuple[str, int]:
    profiles = root.get("profiles", {})
    used_ids = {
        str(key or "").strip().lower()
        for key in (profiles.keys() if isinstance(profiles, dict) else [])
        if str(key or "").strip()
    }
    try:
        next_index = max(1, int(root.get("next_user_index") or 1))
    except Exception:
        next_index = 1
    candidate = next_index
    candidate_id = _format_user_id(candidate)
    while candidate_id.lower() in used_ids:
        candidate += 1
        candidate_id = _format_user_id(candidate)
    return candidate_id, candidate


def _advance_memory_next_user_index(root: Dict[str, Any], user_id: str) -> None:
    try:
        current = max(1, int(root.get("next_user_index") or 1))
    except Exception:
        current = 1
    sequence = _parse_user_sequence(user_id)
    if sequence is None:
        root["next_user_index"] = current
        return
    root["next_user_index"] = max(current, sequence + 1)


def _choose_existing_path(preferred: Path, *fallbacks: Path) -> Path:
    candidates = [preferred, *fallbacks]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return preferred


APP_ROOT = _resolve_app_root()
STATE_DIR = _resolve_state_dir(APP_ROOT)
REPO_ROOT = APP_ROOT
SCRIPTS_DIR = APP_ROOT / "scripts"
PANEL_DIR_CANDIDATES = [
    APP_ROOT / "Assets" / "StreamingAssets" / "panel",
]
DEFAULT_LAUNCHER_CONFIG = SCRIPTS_DIR / "local_services.user.json"
DEFAULT_LAUNCHER_DEFAULT_CONFIG = _choose_existing_path(
    SCRIPTS_DIR / "local_services.default.json",
)
DEFAULT_GAME_MANIFEST = _choose_existing_path(
    SCRIPTS_DIR / "intent_service" / "manifest.json",
)
DEFAULT_DIALOG_MEMORY = _choose_existing_path(
    SCRIPTS_DIR / "dialog_service" / "user_memory.json",
)
DEFAULT_QMD_ROOT = APP_ROOT / "runtime" / "qmd"
DEFAULT_PANEL_PORT = int(os.getenv("PANEL_PORT", "8787") or "8787")
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_OPENAI_RESPONSE_MODEL = "gpt-4o-mini"
DEFAULT_GEMINI_RESPONSE_MODEL = "gemini-2.5-flash"
DEFAULT_GEMINI_LIVE_MODEL = "models/gemini-2.5-flash-native-audio-latest"
DEFAULT_GEMINI_LIVE_VOICE = "Kore"
DEFAULT_OLLAMA_MODEL = "qwen3.5:0.8b"
OLLAMA_MODEL_OPTIONS = [
    "qwen3.5:0.8b",
    "qwen3.5:2b",
    "qwen3.5:4b",
    "qwen3.5:9b",
]
DEFAULT_LAUNCH_TRIGGERS = ["open", "start", "launch", "play", "begin", "load"]
CORE_EXIT_KEYWORDS = ["back home", "go home", "return home", "go back"]
DEFAULT_EXIT_KEYWORDS = [*CORE_EXIT_KEYWORDS, "quit", "exit", "stop", "cancel", "close", "close game"]
OPERATOR_ASR_MODES = ["live-captions", "api", "gemini-live"]
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
KOKORO_VOICES = [
    "af_alloy",
    "af_aoede",
    "af_bella",
    "af_heart",
    "af_jessica",
    "af_kore",
    "af_nicole",
    "af_nova",
    "af_river",
    "af_sarah",
    "af_sky",
    "am_adam",
    "am_echo",
    "am_eric",
    "am_fenrir",
    "am_liam",
    "am_michael",
    "am_onyx",
    "am_puck",
    "am_santa",
    "bf_alice",
    "bf_emma",
    "bf_isabella",
    "bf_lily",
    "bm_daniel",
    "bm_fable",
    "bm_george",
    "bm_lewis",
    "ef_dora",
    "em_alex",
    "em_santa",
    "ff_siwis",
    "hf_alpha",
    "hf_beta",
    "hm_omega",
    "hm_psi",
    "if_sara",
    "im_nicola",
    "jf_alpha",
    "jf_gongitsune",
    "jf_nezumi",
    "jf_tebukuro",
    "jm_kumo",
    "pf_dora",
    "pm_alex",
    "pm_santa",
    "zf_xiaobei",
    "zf_xiaoni",
    "zf_xiaoxiao",
    "zf_xiaoyi",
    "zm_yunjian",
    "zm_yunxi",
    "zm_yunxia",
    "zm_yunyang",
]
KOKORO_LANG_CODES = {
    "a": "American English",
    "b": "British English",
    "e": "Spanish",
    "f": "French",
    "h": "Hindi",
    "i": "Italian",
    "j": "Japanese",
    "p": "Brazilian Portuguese",
    "z": "Mandarin Chinese",
}
DEFAULT_KOKORO_VOICE = "af_heart"
FACE_PRESET_OPTIONS = [
    "neutral",
    "happy",
    "excited",
    "sad",
    "verySad",
    "confused",
    "concerned",
    "upset",
    "ANeutral",
    "AHappy",
    "AConcerned",
    "AConfused",
    "AUpset",
    "BNeutral",
    "BHappy",
    "BConcerned",
    "BConfused",
    "BUpset",
    "CNeutral",
    "CHappy",
    "CConcerned",
    "CConfused",
    "CUpset",
    "DNeutral",
    "DHappy",
    "DConcerned",
    "DConfused",
    "DUpset",
]


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value else default


def _gemini_api_key_from_env() -> str:
    return _env("GEMINI_API_KEY", "") or _env("GEMINI_KEY", "")


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
    if normalized in {"cloud", "openai", "gemini", "online"}:
        return "cloud"
    return "local"


def _normalize_cloud_response_provider(value: Optional[str]) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"gemini", "google", "google-ai", "google_ai"}:
        return "gemini"
    return "openai"


def _normalize_tts_backend(value: Optional[str]) -> str:
    normalized = (value or "").strip().lower().replace("_", "-")
    if normalized in {"kokoro", "kokoro_tts", "kokoro-tts"}:
        return "kokoro"
    if normalized in {"google-cloud", "google", "google-cloud-tts", "cloud-tts"}:
        return "google-cloud"
    return "piper"


def _normalize_operator_asr_mode(value: Optional[str]) -> str:
    normalized = normalize_streaming_asr_mode(value)
    if normalized not in OPERATOR_ASR_MODES:
        raise HTTPException(
            status_code=400,
            detail="supported ASR providers are: Windows Captions, OpenAI API, Google API",
        )
    return normalized


def _coerce_cloud_pipeline_for_provider(
    *,
    profile: str,
    cloud_response_provider: str,
    cloud_asr_mode: str,
    cloud_streaming_asr_mode: str,
) -> Tuple[str, str]:
    normalized_profile = _normalize_profile(profile)
    normalized_provider = _normalize_cloud_response_provider(cloud_response_provider)
    normalized_cloud_asr = normalize_asr_mode(cloud_asr_mode)
    normalized_cloud_streaming = normalize_streaming_asr_mode(cloud_streaming_asr_mode)
    if normalized_profile == "cloud" and normalized_provider == "gemini":
        return normalize_asr_mode("api"), normalize_streaming_asr_mode("gemini-live")
    return normalized_cloud_asr, normalized_cloud_streaming


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
    gemini_obj = _ensure_dict(merged, "gemini")
    intent_obj = _ensure_dict(merged, "intent")
    env_obj = _ensure_dict(merged, "env")
    paths_obj = _ensure_dict(merged, "paths")
    openai_api_key = str(openai_obj.get("api_key") or _env("OPENAI_API_KEY", "")).strip()
    openai_base_url = str(openai_obj.get("base_url") or _env("OPENAI_BASE_URL", "")).strip()
    openai_transcribe_model = str(openai_obj.get("transcribe_model") or _env("OPENAI_TRANSCRIBE_MODEL", "")).strip()
    openai_transcribe_prompt = str(openai_obj.get("transcribe_prompt") or _env("OPENAI_TRANSCRIBE_PROMPT", "")).strip()
    gemini_api_key = str(gemini_obj.get("api_key") or _gemini_api_key_from_env()).strip()
    gemini_live_model = str(
        env_obj.get("GEMINI_LIVE_MODEL")
        or DEFAULT_GEMINI_LIVE_MODEL
    ).strip() or DEFAULT_GEMINI_LIVE_MODEL
    gemini_response_model = str(
        env_obj.get("GEMINI_RESPONSE_MODEL")
        or DEFAULT_GEMINI_RESPONSE_MODEL
    ).strip() or DEFAULT_GEMINI_RESPONSE_MODEL
    gemini_live_voice = str(env_obj.get("GEMINI_LIVE_VOICE") or DEFAULT_GEMINI_LIVE_VOICE).strip() or DEFAULT_GEMINI_LIVE_VOICE
    gemini_live_native_response = _read_bool(env_obj, "VOICE_GEMINI_LIVE_NATIVE_RESPONSE", False)
    speaker_id_enabled = _read_bool(env_obj, "VOICE_SPEAKER_ID_ENABLED", False)
    speaker_auto_learning_enabled = _read_bool(env_obj, "VOICE_SPEAKER_ID_AUTO_GUEST_LEARNING", False)
    tts_backend = _normalize_tts_backend(env_obj.get("VOICE_AGENT_TTS_BACKEND"))
    kokoro_voice = str(env_obj.get("KOKORO_TTS_VOICE") or DEFAULT_KOKORO_VOICE).strip() or DEFAULT_KOKORO_VOICE
    kokoro_lang_code = str(env_obj.get("KOKORO_TTS_LANG_CODE") or "a").strip().lower() or "a"
    conversation_profile = _normalize_profile(env_obj.get("VOICE_CONVERSATION_PROFILE"))
    cloud_response_provider = _normalize_cloud_response_provider(env_obj.get("VOICE_CLOUD_RESPONSE_PROVIDER"))
    cloud_asr_mode, cloud_streaming_asr_mode = _coerce_cloud_pipeline_for_provider(
        profile=conversation_profile,
        cloud_response_provider=cloud_response_provider,
        cloud_asr_mode=normalize_asr_mode(env_obj.get("VOICE_CLOUD_ASR_MODE")),
        cloud_streaming_asr_mode=normalize_streaming_asr_mode(
            env_obj.get("VOICE_CLOUD_STREAMING_ASR_MODE") or env_obj.get("VOICE_CLOUD_ASR_MODE")
        ),
    )
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
        "gemini_api_key": gemini_api_key,
        "gemini_api_key_set": bool(gemini_api_key),
        "gemini_live_model": gemini_live_model,
        "gemini_response_model": gemini_response_model,
        "gemini_live_voice": gemini_live_voice,
        "gemini_live_native_response": gemini_live_native_response,
        "speaker_id_enabled": speaker_id_enabled,
        "speaker_auto_learning_enabled": speaker_auto_learning_enabled,
        "cloud_response_provider": cloud_response_provider,
        "tts_backend": tts_backend,
        "kokoro_voice": kokoro_voice,
        "kokoro_lang_code": kokoro_lang_code,
        "kokoro_supported_lang_codes": dict(KOKORO_LANG_CODES),
        "ollama_model": str(env_obj.get("OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL).strip() or DEFAULT_OLLAMA_MODEL,
        "ollama_model_options": list(OLLAMA_MODEL_OPTIONS),
        "conversation_pipeline_mode": _normalize_pipeline_mode(env_obj.get("VOICE_PIPELINE_MODE")),
        "conversation_profile": conversation_profile,
        "local_asr_mode": normalize_asr_mode(env_obj.get("VOICE_LOCAL_ASR_MODE")),
        "cloud_asr_mode": cloud_asr_mode,
        "local_streaming_asr_mode": normalize_streaming_asr_mode(
            env_obj.get("VOICE_LOCAL_STREAMING_ASR_MODE") or env_obj.get("VOICE_LOCAL_ASR_MODE")
        ),
        "cloud_streaming_asr_mode": cloud_streaming_asr_mode,
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
        expanded_exec = os.path.expandvars(exec_path) if exec_path else ""
        expanded_workdir = os.path.expandvars(workdir) if workdir else ""
        executable_exists = bool(expanded_exec) and (
            Path(expanded_exec).is_file()
            if any(marker in expanded_exec for marker in ("\\", "/", ":"))
            else bool(shutil.which(expanded_exec))
        )
        workdir_exists = not expanded_workdir or Path(expanded_workdir).is_dir()
        path_errors: List[str] = []
        if not expanded_exec:
            path_errors.append("Executable path is not configured")
        elif not executable_exists:
            path_errors.append(f"Executable not found: {exec_path}")
        if expanded_workdir and not workdir_exists:
            path_errors.append(f"Working directory not found: {workdir}")
        if path_errors:
            unresolved += len(path_errors)
        games_out.append(
            {
                "id": game_id,
                "name": name,
                "keywords": [str(value).strip() for value in item.get("synonyms", []) or [] if str(value).strip()],
                "exec": exec_path,
                "workdir": workdir,
                "launch_ready": bool(executable_exists and workdir_exists),
                "path_error": "; ".join(path_errors),
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
    compact = dict(payload or {})
    profiles = compact.get("profiles", {})
    compact["profiles"] = {
        str(user_id): _normalize_memory_profile(str(user_id), profile if isinstance(profile, dict) else {}, None)
        for user_id, profile in profiles.items()
        if str(user_id).strip()
    } if isinstance(profiles, dict) else {}
    path.write_text(json.dumps(compact, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_speaker_profiles_root(path: Path) -> Dict[str, Any]:
    payload = _load_json_object(path)
    if not payload:
        payload = {"version": 1, "users": {}}
    payload.setdefault("version", 1)
    payload.setdefault("users", {})
    if not isinstance(payload["users"], dict):
        payload["users"] = {}
    return payload


def _save_speaker_profiles_root(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _clear_speaker_profile_for_user(memory_path: Path, user_id: str) -> bool:
    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id:
        return False
    profiles_path = resolve_speaker_profiles_path(memory_path=str(memory_path))
    root = _load_speaker_profiles_root(profiles_path)
    users = root.get("users", {}) if isinstance(root, dict) else {}
    if not isinstance(users, dict):
        root["users"] = {}
        users = root["users"]
    removed = users.pop(normalized_user_id, None) is not None
    if removed:
        _save_speaker_profiles_root(profiles_path, root)
    return removed


def _speaker_profiles_summary(memory_path: Path) -> Dict[str, Dict[str, Any]]:
    root = _load_speaker_profiles_root(resolve_speaker_profiles_path(memory_path=str(memory_path)))
    users = root.get("users", {}) if isinstance(root, dict) else {}
    output: Dict[str, Dict[str, Any]] = {}
    if not isinstance(users, dict):
        return output
    for user_id, payload in users.items():
        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id or not isinstance(payload, dict):
            continue
        output[normalized_user_id] = {
            "clip_count": int(payload.get("clip_count") or 0),
            "updated_ts": float(payload.get("updated_ts") or 0.0),
            "created_ts": float(payload.get("created_ts") or 0.0),
            "has_profile": bool(payload.get("centroid")),
        }
    return output


async def _clear_unlinked_runtime_speaker_profiles(valid_user_ids: set[str]) -> List[str]:
    speaker_status = await audio_agent.speaker_profiles_status()
    cleared: List[str] = []
    for item in list(speaker_status.get("users") or []):
        user_id = str(item.get("user_id") or "").strip()
        if not user_id or user_id == "__guest__" or user_id in valid_user_ids:
            continue
        await audio_agent.clear_speaker_profile(user_id=user_id)
        cleared.append(user_id)
    return cleared


def _normalize_memory_profile(user_id: str, incoming: Dict[str, Any], existing: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    source = dict(existing or {})
    source.update(incoming or {})
    display_name = str(source.get("display_name") or source.get("name") or user_id).strip() or user_id
    turns: List[Dict[str, Any]] = []
    for item in source.get("dialog_turns", []) if isinstance(source.get("dialog_turns"), list) else []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        turns.append(
            {
                "role": str(item.get("role") or "user").strip().lower() or "user",
                "text": text,
                "ts": float(item.get("ts") or time.time()),
            }
        )
    return {"display_name": display_name, "dialog_turns": turns[-500:]}


def _migrate_guest_turns_to_memory(path: Path, user_id: str, turns: List[Dict[str, Any]]) -> int:
    normalized_user_id = str(user_id or "").strip()
    valid_turns = [
        {
            "role": str(item.get("role") or "user").strip() or "user",
            "text": str(item.get("text") or "").strip(),
            "ts": float(item.get("ts") or time.time()),
        }
        for item in turns
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    if not normalized_user_id or not valid_turns:
        return 0
    root = _load_memory_root(path)
    profiles = root["profiles"]
    profile = _normalize_memory_profile(normalized_user_id, {}, profiles.get(normalized_user_id))
    dialog_turns = list(profile.get("dialog_turns") or [])
    dialog_turns.extend(valid_turns)
    profile["dialog_turns"] = dialog_turns[-500:]
    profiles[normalized_user_id] = profile
    _save_memory_root(path, root)
    return len(valid_turns)


async def _auto_identity_handler(*, candidate_user_id: str = "", name: str = "") -> Dict[str, Any]:
    merged = _load_launcher_config_pair()[4]
    memory_path = _resolve_memory_path(merged)
    root = _load_memory_root(memory_path)
    profiles = root["profiles"]
    target_user_id = ""
    display_name = str(name or "").strip()
    created = False
    participant_status = dict((await audio_agent.speaker_profiles_status()).get("participant") or {})
    if not bool(participant_status.get("ready_to_confirm")):
        return {"status": "learning", "message": "voice learning is not ready yet"}

    candidate = str(candidate_user_id or "").strip()
    if candidate:
        if candidate not in profiles:
            return {"status": "unlinked_voice_profile", "message": f"{candidate} has no memory profile"}
        target_user_id = candidate
        profile = profiles.get(candidate) or {}
        display_name = str(profile.get("display_name") or profile.get("name") or candidate).strip() or candidate
    elif display_name:
        name_key = display_name.casefold()
        matches = [
            user_id
            for user_id, profile in profiles.items()
            if isinstance(profile, dict)
            and name_key
            in {
                str(profile.get("display_name") or "").strip().casefold(),
                str(profile.get("name") or "").strip().casefold(),
            }
        ]
        if len(matches) > 1:
            return {"status": "ambiguous_name", "message": f"multiple profiles use the name {display_name}"}
        if matches:
            target_user_id = matches[0]
        else:
            target_user_id, _ = _next_memory_user_id(root)
            profiles[target_user_id] = _normalize_memory_profile(
                target_user_id,
                {"display_name": display_name, "name": display_name},
                None,
            )
            _advance_memory_next_user_index(root, target_user_id)
            _save_memory_root(memory_path, root)
            created = True
    else:
        return {}

    try:
        await audio_agent.confirm_guest_participant(user_id=target_user_id)
    except Exception:
        if created:
            root = _load_memory_root(memory_path)
            root["profiles"].pop(target_user_id, None)
            _save_memory_root(memory_path, root)
        raise
    migrated_turns = _migrate_guest_turns_to_memory(
        memory_path,
        target_user_id,
        audio_agent.consume_guest_turns(),
    )
    return {
        "status": "created" if created else "matched",
        "user_id": target_user_id,
        "display_name": display_name or target_user_id,
        "created": created,
        "migrated_guest_turns": migrated_turns,
        "message": (
            f"Created a new participant profile for {display_name}."
            if created
            else f"Using the existing participant profile for {display_name or target_user_id}."
        ),
    }


def _memory_payload(path: Path, selected_user_id: str, message: str) -> Dict[str, Any]:
    root = _load_memory_root(path)
    profiles = root.get("profiles", {})
    identity_map = root.get("identity_map", {})
    speaker_profiles = _speaker_profiles_summary(path)
    unlinked_speaker_profiles = [
        {
            "user_id": user_id,
            "clip_count": int(profile.get("clip_count") or 0),
            "updated_ts": float(profile.get("updated_ts") or 0.0),
        }
        for user_id, profile in sorted(speaker_profiles.items(), key=lambda item: str(item[0]).lower())
        if user_id not in profiles and user_id != "__guest__"
    ]
    suggested_user_id, next_user_index = _next_memory_user_id(root)
    users: List[Dict[str, Any]] = []
    for user_id in sorted([str(key).strip() for key in profiles.keys() if str(key).strip()], key=str.lower):
        profile = profiles.get(user_id) if isinstance(profiles, dict) else {}
        if not isinstance(profile, dict):
            profile = {}
        identity_hits = 0
        for identity in identity_map.values() if isinstance(identity_map, dict) else []:
            if isinstance(identity, dict) and str(identity.get("user_id") or "").strip() == user_id:
                identity_hits += int(identity.get("sample_count") or 0)
        speaker_profile = speaker_profiles.get(user_id) or {}
        users.append(
            {
                "user_id": user_id,
                "display_name": str(profile.get("display_name") or user_id).strip() or user_id,
                "dialog_turn_count": len(profile.get("dialog_turns", []) or []),
                "utterance_count": len(
                    [
                        item
                        for item in profile.get("dialog_turns", []) or []
                        if isinstance(item, dict) and str(item.get("role") or "user").strip().lower() == "user"
                    ]
                ),
                "identity_hits": identity_hits,
                "enrollment_clip_count": int(speaker_profile.get("clip_count") or 0),
                "speaker_profile_ready": bool(speaker_profile.get("has_profile")),
                "last_seen_iso": (
                    time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ",
                        time.gmtime(float((profile.get("dialog_turns") or [{}])[-1].get("ts") or 0)),
                    )
                    if profile.get("dialog_turns")
                    else ""
                ),
            }
        )
    selected_profile = profiles.get(selected_user_id) if isinstance(profiles, dict) else {}
    if not isinstance(selected_profile, dict):
        selected_profile = {}
    selected_speaker_profile = speaker_profiles.get(selected_user_id) or {}
    selected_identity_keys = []
    for key, identity in identity_map.items() if isinstance(identity_map, dict) else []:
        if isinstance(identity, dict) and str(identity.get("user_id") or "").strip() == selected_user_id:
            selected_identity_keys.append(str(key))
    return {
        "status": "ok",
        "message": message,
        "path": str(path),
        "user_count": len(users),
        "next_user_index": int(next_user_index),
        "suggested_user_id": str(suggested_user_id),
        "users": users,
        "unlinked_speaker_profiles": unlinked_speaker_profiles,
        "selected_user_id": selected_user_id,
        "selected_profile": selected_profile,
        "selected_identity_keys": selected_identity_keys,
        "selected_speaker_profile": selected_speaker_profile,
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
audio_agent.set_auto_identity_handler(_auto_identity_handler)
camera_service = DesktopCameraService()
ollama_pull_requests: set[str] = set()
panel_http = httpx.AsyncClient(timeout=httpx.Timeout(30.0))


async def _on_startup() -> None:
    await audio_agent.start()
    try:
        _, _, _, _, merged = _load_launcher_config_pair()
        note = await _apply_runtime_live(merged)
        if note:
            log_store.add("system", note, source="desktop_runtime")
        note = await _ensure_ollama_running(merged)
        if note:
            log_store.add("system", note, source="desktop_runtime")
    except Exception as exc:
        log_store.add("system", f"ollama autostart failed: {exc}", source="desktop_runtime")


async def _on_shutdown() -> None:
    await panel_http.aclose()
    camera_service.close()
    await audio_agent.stop()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    await _on_startup()
    try:
        yield
    finally:
        await _on_shutdown()


app = FastAPI(title="Voice Agent Desktop Runtime", lifespan=_lifespan)


PANEL_ASSET_MEDIA_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".webp": "image/webp",
    ".txt": "text/plain; charset=utf-8",
}


def _resolve_panel_asset_from_candidates(name: str, panel_dirs: List[Path]) -> Tuple[Path, str]:
    normalized = str(name or "").strip()
    if (
        not normalized
        or normalized.startswith(("/", "\\"))
        or "/" in normalized
        or "\\" in normalized
        or "\0" in normalized
        or normalized in {".", ".."}
    ):
        raise HTTPException(status_code=404, detail="panel asset not found")
    media_type = PANEL_ASSET_MEDIA_TYPES.get(Path(normalized).suffix.lower())
    if media_type is None:
        raise HTTPException(status_code=404, detail="panel asset not found")
    for panel_dir in panel_dirs:
        root = panel_dir.resolve()
        candidate = (root / normalized).resolve()
        if candidate.parent == root and candidate.is_file():
            return candidate, media_type
    raise HTTPException(status_code=404, detail="panel asset not found")


def _panel_file_response_from_candidates(name: str, panel_dirs: List[Path]) -> FileResponse:
    for panel_dir in panel_dirs:
        path = panel_dir / name
        if path.exists():
            return FileResponse(path)
    raise HTTPException(status_code=404, detail="panel asset not found")


def _panel_file_response(name: str) -> FileResponse:
    return _panel_file_response_from_candidates(name, PANEL_DIR_CANDIDATES)


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
    os.environ["VOICE_CLOUD_RESPONSE_PROVIDER"] = runtime["cloud_response_provider"]
    os.environ["VOICE_AGENT_TTS_BACKEND"] = runtime["tts_backend"]
    os.environ["KOKORO_TTS_VOICE"] = runtime["kokoro_voice"]
    os.environ["KOKORO_TTS_LANG_CODE"] = runtime["kokoro_lang_code"]
    if runtime["gemini_api_key"]:
        os.environ["GEMINI_API_KEY"] = runtime["gemini_api_key"]
    else:
        os.environ.pop("GEMINI_API_KEY", None)
    os.environ["GEMINI_LIVE_MODEL"] = runtime["gemini_live_model"]
    os.environ["GEMINI_RESPONSE_MODEL"] = runtime["gemini_response_model"]
    os.environ["GEMINI_LIVE_VOICE"] = runtime["gemini_live_voice"]
    os.environ["VOICE_GEMINI_LIVE_NATIVE_RESPONSE"] = "1" if runtime["gemini_live_native_response"] else "0"
    os.environ["VOICE_SPEAKER_ID_ENABLED"] = "1" if runtime["speaker_id_enabled"] else "0"
    os.environ["VOICE_SPEAKER_ID_AUTO_GUEST_LEARNING"] = "1" if runtime["speaker_auto_learning_enabled"] else "0"
    request_payload = {
        "pipeline_mode": runtime["conversation_pipeline_mode"],
        "profile": runtime["conversation_profile"],
        "local_asr_mode": runtime["local_asr_mode"],
        "cloud_asr_mode": runtime["cloud_asr_mode"],
        "cloud_response_provider": runtime["cloud_response_provider"],
        "gemini_api_key": runtime["gemini_api_key"],
        "gemini_response_model": runtime["gemini_response_model"],
        "openai_api_key": runtime["openai_api_key"],
        "openai_base_url": runtime["openai_base_url"],
        "openai_transcribe_model": runtime["openai_transcribe_model"],
        "openai_transcribe_prompt": runtime["openai_transcribe_prompt"],
        "openai_response_model": runtime["openai_response_model"],
        "local_response_model": runtime["ollama_model"],
    }
    notes: List[str] = []
    conversation_config_error = ""
    for attempt in range(3):
        try:
            response = await panel_http.post(f"{DEFAULT_ASR_BASE_URL}/conversation/config", json=request_payload)
            if response.is_success:
                conversation_config_error = ""
                break
            conversation_config_error = response.text.strip() or f"conversation config HTTP {response.status_code}"
        except Exception as exc:
            conversation_config_error = f"conversation config failed: {exc}"
        if attempt < 2:
            await asyncio.sleep(0.75)
    if conversation_config_error:
        notes.append(conversation_config_error)

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


@app.get("/index.html")
async def panel_index() -> FileResponse:
    return _panel_file_response("index.html")


@app.get("/panel-assets/{asset_name:path}")
async def panel_asset(asset_name: str) -> FileResponse:
    path, media_type = _resolve_panel_asset_from_candidates(asset_name, PANEL_DIR_CANDIDATES)
    return FileResponse(path, media_type=media_type)


@app.get("/")
@app.get("/panel.html")
async def panel_home_redirect() -> RedirectResponse:
    return RedirectResponse(url="/index.html", status_code=307)


@app.get("/games")
@app.get("/games.html")
async def panel_games() -> FileResponse:
    return _panel_file_response("games.html")


@app.get("/controls")
@app.get("/controls.html")
async def panel_controls() -> FileResponse:
    return _panel_file_response("controls.html")


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


@app.get("/sdk-manifest")
@app.get("/sdk-manifest.json")
async def panel_sdk_manifest() -> FileResponse:
    return _panel_file_response("sdk-manifest.json")


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
async def api_voice_options(request: Request) -> Dict[str, Any]:
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
    google_cloud = await audio_agent.get_google_cloud_voice_options(
        request.headers.get("X-Google-Cloud-TTS-Api-Key", "")
    )
    return {
        "voices": ["en_US"],
        "current": audio_agent.active_voice_code,
        "models": models or ([current_model] if current_model else []),
        "modelCurrent": current_model,
        "backends": ["piper", "kokoro", "google-cloud"],
        "backendCurrent": audio_agent.active_tts_backend,
        "kokoroVoiceCurrent": audio_agent.active_kokoro_voice,
        "googleCloudVoices": google_cloud["voices"],
        "googleCloudVoiceCurrent": google_cloud["current"],
        "googleCloudLanguageCode": google_cloud["languageCode"],
        "googleCloudReady": google_cloud["ready"],
        "googleCloudError": google_cloud["error"],
    }


@app.get("/api/kokoro/options")
async def api_kokoro_options() -> Dict[str, Any]:
    configured = _coerce_string_list(_env("KOKORO_TTS_VOICES", ""))
    current = str(audio_agent.active_kokoro_voice or DEFAULT_KOKORO_VOICE).strip() or DEFAULT_KOKORO_VOICE
    voices = configured or list(KOKORO_VOICES)
    if current and current not in voices:
        voices.append(current)
    return {
        "voices": voices,
        "current": current,
        "langCode": _env("KOKORO_TTS_LANG_CODE", "a"),
        "supportedLangCodes": dict(KOKORO_LANG_CODES),
    }


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
    if action in {"set_backend", "backend"}:
        backend = _normalize_tts_backend(payload.get("backend") or payload.get("value"))
        await audio_agent.set_tts_options(backend=backend)
        return {"status": "ok", "message": f"tts backend set to {backend}"}
    if action in {"set_kokoro_voice", "kokoro_voice"}:
        kokoro_voice = str(payload.get("voice") or payload.get("value") or "").strip()
        if not kokoro_voice:
            raise HTTPException(status_code=400, detail="kokoro voice required")
        await audio_agent.set_tts_options(kokoro_voice=kokoro_voice)
        return {"status": "ok", "message": f"kokoro voice set to {kokoro_voice}"}
    if action in {"set_google_cloud_voice", "google_cloud_voice"}:
        google_cloud_voice = str(payload.get("voice") or payload.get("value") or "").strip()
        if not google_cloud_voice:
            raise HTTPException(status_code=400, detail="google cloud voice required")
        await audio_agent.set_tts_options(google_cloud_voice=google_cloud_voice)
        return {"status": "ok", "message": f"google cloud voice set to {google_cloud_voice}"}
    raise HTTPException(status_code=400, detail="unknown voice action")


@app.post("/api/speak")
async def api_speak(request: Request) -> Dict[str, Any]:
    payload = await request.json()
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    backend = _normalize_tts_backend(payload.get("backend") or audio_agent.active_tts_backend)
    try:
        await audio_agent.manual_speak(
            text=text,
            voice=str(payload.get("voice") or "").strip() or None,
            model=str(payload.get("model") or "").strip() or audio_agent.active_tts_model,
            instruct=str(payload.get("instruct") or "").strip(),
            backend=backend,
            google_cloud_api_key=str(payload.get("googleCloudApiKey") or "").strip() or None,
            source="tester_panel",
        )
    except Exception as exc:
        if backend == "google-cloud":
            raise HTTPException(status_code=502, detail=f"Google Cloud TTS failed: {exc}") from exc
        raise
    return {"status": "ok", "message": f"speech started ({backend})"}


@app.post("/api/kokoro/speak")
async def api_kokoro_speak(request: Request) -> Dict[str, Any]:
    payload = await request.json()
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    voice = str(payload.get("voice") or payload.get("speaker") or "").strip() or audio_agent.active_kokoro_voice
    await audio_agent.manual_speak(
        text=text,
        voice=voice,
        instruct=str(payload.get("instruct") or "").strip(),
        backend="kokoro",
        source="tester_panel_kokoro",
    )
    return {"status": "ok", "message": "playing locally (kokoro)"}


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
        "tts_backend": str(status.tts_backend or ""),
        "available_modes": list(OPERATOR_ASR_MODES),
        "listening": bool(status.listening),
        "assistant_speaking": bool(status.assistant_speaking),
        "conversation_dispatch_enabled": bool(getattr(status, "conversation_dispatch_enabled", True)),
        "supports_hotwords": bool(status.supports_hotwords),
        "hotwords_count": int(status.hotwords_count),
        "hotword_strategy": str(status.hotword_strategy or ""),
        "command_asr_enabled": bool(getattr(status, "command_asr_enabled", False)),
        "command_asr_provider": str(getattr(status, "command_asr_provider", "") or ""),
        "command_asr_status": str(getattr(status, "command_asr_status", "") or ""),
        "gemini_live_command_tools_enabled": bool(getattr(status, "gemini_live_command_tools_enabled", False)),
        "gemini_live_local_knowledge_enabled": bool(getattr(status, "gemini_live_local_knowledge_enabled", False)),
        "current_partial": str(status.current_partial or ""),
        "stable_partial": str(status.stable_partial or ""),
        "final_transcript": str(getattr(status, "final_transcript", "") or ""),
        "final_transcript_seq": int(getattr(status, "final_transcript_seq", 0) or 0),
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
        "live_captions_status": str(getattr(status, "live_captions_status", "") or ""),
        "live_captions_error": str(getattr(status, "live_captions_error", "") or ""),
        "speaker_id_enabled": bool(getattr(status, "speaker_id_enabled", False)),
        "speaker_id_ready": bool(getattr(status, "speaker_id_ready", False)),
        "active_user_id": str(getattr(status, "active_user_id", "") or ""),
        "last_speaker_match": dict(getattr(status, "last_speaker_match", {}) or {}),
        "live_capture_enabled": bool(getattr(status, "live_capture_enabled", False)),
        "input_device_index": int(getattr(status, "input_device_index", -1) or -1),
        "input_device_name": str(getattr(status, "input_device_name", "") or ""),
        "input_device_hostapi": str(getattr(status, "input_device_hostapi", "") or ""),
        "input_device_source": str(getattr(status, "input_device_source", "") or ""),
        "input_device_sample_rate": _safe_json_float(getattr(status, "input_device_sample_rate", 0.0), 0.0),
    }
    try:
        speaker_status = await audio_agent.speaker_profiles_status()
        payload["participant"] = dict(speaker_status.get("participant") or {})
    except Exception:
        payload["participant"] = {}
    runtime_cfg: Optional[Dict[str, Any]] = None
    try:
        _, _, _, _, merged = _load_launcher_config_pair()
        runtime_cfg = _build_runtime_payload(
            merged,
            user_path=_resolve_launcher_config_path(),
            default_path=_resolve_launcher_default_config_path(),
            message="runtime config",
        )
        payload["conversation_profile"] = str(runtime_cfg.get("conversation_profile") or "")
        payload["cloud_response_provider"] = str(runtime_cfg.get("cloud_response_provider") or "")
        payload["gemini_response_model"] = str(runtime_cfg.get("gemini_response_model") or "")
        payload["gemini_live_model"] = str(runtime_cfg.get("gemini_live_model") or "")
        payload["gemini_live_native_response"] = bool(runtime_cfg.get("gemini_live_native_response"))
    except Exception:
        runtime_cfg = None
    if str(status.asr_mode or "") == "gemini-live":
        payload["server_transcribe"] = {
            "status": "ok",
            "mode": "not_used",
            "source": "gemini_live",
            "message": "Gemini Live handles conversation audio directly; command words use the configured command ASR path.",
        }
        return payload
    try:
        response = await panel_http.get(f"{DEFAULT_ASR_BASE_URL}/transcribe/config")
        if response.is_success:
            payload["server_transcribe"] = response.json()
    except Exception:
        pass
    return payload


def _build_asr_event_payload(*, event_type: str) -> Dict[str, Any]:
    status = audio_agent.status()
    return {
        "status": "ok",
        "message": "streaming asr event",
        "event_type": str(event_type or "update"),
        "mode": str(status.asr_mode or ""),
        "streaming_backend": str(status.streaming_backend or ""),
        "tts_backend": str(status.tts_backend or ""),
        "listening": bool(status.listening),
        "assistant_speaking": bool(status.assistant_speaking),
        "conversation_dispatch_enabled": bool(getattr(status, "conversation_dispatch_enabled", True)),
        "current_partial": str(status.current_partial or ""),
        "stable_partial": str(status.stable_partial or ""),
        "final_transcript": str(getattr(status, "final_transcript", "") or ""),
        "final_transcript_seq": int(getattr(status, "final_transcript_seq", 0) or 0),
        "supports_hotwords": bool(status.supports_hotwords),
        "hotwords_count": int(status.hotwords_count),
        "hotword_strategy": str(status.hotword_strategy or ""),
        "command_asr_enabled": bool(getattr(status, "command_asr_enabled", False)),
        "command_asr_provider": str(getattr(status, "command_asr_provider", "") or ""),
        "command_asr_status": str(getattr(status, "command_asr_status", "") or ""),
        "gemini_live_command_tools_enabled": bool(getattr(status, "gemini_live_command_tools_enabled", False)),
        "gemini_live_local_knowledge_enabled": bool(getattr(status, "gemini_live_local_knowledge_enabled", False)),
    }


@app.get("/api/asr/events")
async def api_asr_events(request: Request) -> StreamingResponse:
    async def event_stream():
        last_payload = _build_asr_event_payload(event_type="snapshot")
        yield f"event: status\ndata: {json.dumps(last_payload, ensure_ascii=False)}\n\n"
        last_signature = json.dumps(last_payload, ensure_ascii=False, sort_keys=True)
        last_keepalive_at = time.time()
        try:
            while True:
                if await request.is_disconnected():
                    break
                payload = _build_asr_event_payload(event_type="update")
                signature = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                if signature != last_signature:
                    last_signature = signature
                    yield f"event: update\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    last_keepalive_at = time.time()
                elif time.time() - last_keepalive_at >= 10.0:
                    yield ": keepalive\n\n"
                    last_keepalive_at = time.time()
                await asyncio.sleep(0.15)
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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
            requested_mode = _normalize_operator_asr_mode(payload.get("mode") or payload.get("value"))
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
        if action in {"set_conversation_dispatch_enabled", "set_conversation_dispatch", "conversation_dispatch"}:
            target = payload.get("enabled")
            if target is None:
                target = payload.get("conversation_dispatch_enabled")
            target = bool(target)
            await audio_agent.set_conversation_dispatch_enabled(target)
            result = await _build_asr_status_payload()
            result["message"] = (
                "automatic conversation dispatch enabled"
                if target
                else "automatic conversation dispatch disabled"
            )
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
    gemini_obj = _ensure_dict(user_cfg, "gemini")
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
    if "gemini_api_key" in payload:
        gemini_obj["api_key"] = str(payload.get("gemini_api_key") or "").strip()
    if "ollama_model" in payload:
        env_obj["OLLAMA_MODEL"] = str(payload.get("ollama_model") or "").strip()
    if "conversation_pipeline_mode" in payload:
        env_obj["VOICE_PIPELINE_MODE"] = _normalize_pipeline_mode(payload.get("conversation_pipeline_mode"))
    if "conversation_profile" in payload:
        env_obj["VOICE_CONVERSATION_PROFILE"] = _normalize_profile(payload.get("conversation_profile"))
    if "cloud_response_provider" in payload:
        env_obj["VOICE_CLOUD_RESPONSE_PROVIDER"] = _normalize_cloud_response_provider(payload.get("cloud_response_provider"))
    if "tts_backend" in payload:
        env_obj["VOICE_AGENT_TTS_BACKEND"] = _normalize_tts_backend(payload.get("tts_backend"))
    if "kokoro_voice" in payload:
        env_obj["KOKORO_TTS_VOICE"] = str(payload.get("kokoro_voice") or "").strip()
    if "kokoro_lang_code" in payload:
        env_obj["KOKORO_TTS_LANG_CODE"] = str(payload.get("kokoro_lang_code") or "").strip().lower()
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
    if "gemini_response_model" in payload:
        env_obj["GEMINI_RESPONSE_MODEL"] = str(payload.get("gemini_response_model") or "").strip()
    if "gemini_live_model" in payload:
        env_obj["GEMINI_LIVE_MODEL"] = str(payload.get("gemini_live_model") or "").strip()
    if "gemini_live_voice" in payload:
        env_obj["GEMINI_LIVE_VOICE"] = str(payload.get("gemini_live_voice") or "").strip()
    if "gemini_live_native_response" in payload:
        env_obj["VOICE_GEMINI_LIVE_NATIVE_RESPONSE"] = "1" if bool(payload.get("gemini_live_native_response")) else "0"
    if "speaker_id_enabled" in payload:
        env_obj["VOICE_SPEAKER_ID_ENABLED"] = "1" if bool(payload.get("speaker_id_enabled")) else "0"
    if "speaker_auto_learning_enabled" in payload:
        env_obj["VOICE_SPEAKER_ID_AUTO_GUEST_LEARNING"] = "1" if bool(payload.get("speaker_auto_learning_enabled")) else "0"
    coerced_cloud_asr_mode, coerced_cloud_streaming_asr_mode = _coerce_cloud_pipeline_for_provider(
        profile=env_obj.get("VOICE_CONVERSATION_PROFILE"),
        cloud_response_provider=env_obj.get("VOICE_CLOUD_RESPONSE_PROVIDER"),
        cloud_asr_mode=env_obj.get("VOICE_CLOUD_ASR_MODE"),
        cloud_streaming_asr_mode=env_obj.get("VOICE_CLOUD_STREAMING_ASR_MODE") or env_obj.get("VOICE_CLOUD_ASR_MODE"),
    )
    env_obj["VOICE_CLOUD_ASR_MODE"] = coerced_cloud_asr_mode
    env_obj["VOICE_CLOUD_STREAMING_ASR_MODE"] = coerced_cloud_streaming_asr_mode
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


@app.get("/api/face/options")
async def api_face_options() -> Dict[str, Any]:
    return {
        "status": "ok",
        "message": "face presets loaded",
        "presets": list(FACE_PRESET_OPTIONS),
        "current": "",
    }


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
    if action == "create_user":
        requested_user_id = str(payload.get("user_id") or "").strip()
        created_user_id = requested_user_id
        if not created_user_id:
            created_user_id, _ = _next_memory_user_id(root)
        if created_user_id in profiles:
            raise HTTPException(status_code=400, detail=f"user already exists: {created_user_id}")
        profiles[created_user_id] = _normalize_memory_profile(
            created_user_id,
            {"display_name": created_user_id},
            None,
        )
        _advance_memory_next_user_index(root, created_user_id)
        _save_memory_root(path, root)
        return _memory_payload(path, created_user_id, "memory user created")
    if action == "update_user_raw":
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id is required")
        profile = payload.get("profile")
        if not isinstance(profile, dict):
            raise HTTPException(status_code=400, detail="profile object is required")
        created = user_id not in profiles
        profiles[user_id] = _normalize_memory_profile(user_id, profile, profiles.get(user_id))
        if created:
            _advance_memory_next_user_index(root, user_id)
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
        await audio_agent.clear_speaker_profile(user_id=user_id)
        cleared_speaker_profile = _clear_speaker_profile_for_user(path, user_id)
        _save_memory_root(path, root)
        cleared_unlinked = await _clear_unlinked_runtime_speaker_profiles(set(profiles.keys()))
        message = "memory user deleted"
        if cleared_speaker_profile:
            message = "memory user deleted and speaker profile cleared"
        if cleared_unlinked:
            message += f"; removed {len(cleared_unlinked)} unlinked speaker profile(s)"
        return _memory_payload(path, "", message)
    raise HTTPException(status_code=400, detail="unknown action")


@app.get("/api/speaker-profiles")
async def api_speaker_profiles_get() -> Dict[str, Any]:
    payload = await audio_agent.speaker_profiles_status()
    payload["status"] = "ok"
    payload["message"] = "speaker profiles loaded"
    return payload


@app.post("/api/speaker-profiles")
async def api_speaker_profiles_post(request: Request) -> Dict[str, Any]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="json object body is required")
    action = str(payload.get("action") or "").strip().lower()
    user_id = str(payload.get("user_id") or "").strip()
    try:
        if action in {"", "status"}:
            result = await audio_agent.speaker_profiles_status()
            result["status"] = "ok"
            result["message"] = "speaker profiles loaded"
            return result
        if action == "record_sample":
            timeout_seconds = _safe_json_float(payload.get("timeout_seconds"), 20.0)
            sample_summary = await audio_agent.record_speaker_profile_sample(
                user_id=user_id,
                timeout_seconds=timeout_seconds,
            )
            result = await audio_agent.speaker_profiles_status()
            result["status"] = "ok"
            result["message"] = "speaker enrollment clip captured"
            result["sample"] = sample_summary
            return result
        if action == "commit_profile":
            summary = await audio_agent.commit_speaker_profile(user_id=user_id)
            result = await audio_agent.speaker_profiles_status()
            result["status"] = "ok"
            result["message"] = "speaker profile committed"
            result["profile"] = summary
            return result
        if action == "clear_profile":
            summary = await audio_agent.clear_speaker_profile(user_id=user_id)
            result = await audio_agent.speaker_profiles_status()
            result["status"] = "ok"
            result["message"] = "speaker profile cleared"
            result["profile"] = summary
            return result
        if action == "confirm_participant":
            if not user_id:
                raise ValueError("user_id is required")
            merged = _load_launcher_config_pair()[4]
            memory_path = _resolve_memory_path(merged)
            memory_root = _load_memory_root(memory_path)
            restored_memory_profile = False
            if user_id not in memory_root["profiles"]:
                speaker_profiles = _speaker_profiles_summary(memory_path)
                if user_id not in speaker_profiles:
                    raise ValueError(f"participant does not exist: {user_id}")
                memory_root["profiles"][user_id] = _normalize_memory_profile(
                    user_id,
                    {"display_name": user_id},
                    None,
                )
                _advance_memory_next_user_index(memory_root, user_id)
                _save_memory_root(memory_path, memory_root)
                restored_memory_profile = True
            summary = await audio_agent.confirm_guest_participant(user_id=user_id)
            migrated_turns = _migrate_guest_turns_to_memory(
                memory_path,
                user_id,
                audio_agent.consume_guest_turns(),
            )
            result = await audio_agent.speaker_profiles_status()
            result["status"] = "ok"
            result["message"] = (
                f"restored memory profile and confirmed participant as {user_id}"
                if restored_memory_profile
                else f"participant confirmed as {user_id}"
            )
            result["profile"] = summary
            result["migrated_guest_turns"] = migrated_turns
            result["restored_memory_profile"] = restored_memory_profile
            return result
        if action == "create_participant":
            display_name = str(payload.get("display_name") or "").strip()
            if not display_name:
                raise ValueError("display_name is required")
            current = await audio_agent.speaker_profiles_status()
            participant = dict(current.get("participant") or {})
            if not bool(participant.get("ready_to_confirm")):
                raise ValueError("keep talking until voice learning is ready")
            merged = _load_launcher_config_pair()[4]
            memory_path = _resolve_memory_path(merged)
            memory_root = _load_memory_root(memory_path)
            profiles = memory_root["profiles"]
            created_user_id = user_id
            if not created_user_id:
                created_user_id, _ = _next_memory_user_id(memory_root)
            if created_user_id in profiles:
                raise ValueError(f"user already exists: {created_user_id}")
            profiles[created_user_id] = _normalize_memory_profile(
                created_user_id,
                {"display_name": display_name, "name": display_name},
                None,
            )
            _advance_memory_next_user_index(memory_root, created_user_id)
            _save_memory_root(memory_path, memory_root)
            try:
                summary = await audio_agent.confirm_guest_participant(user_id=created_user_id)
            except Exception:
                profiles.pop(created_user_id, None)
                _save_memory_root(memory_path, memory_root)
                raise
            migrated_turns = _migrate_guest_turns_to_memory(
                memory_path,
                created_user_id,
                audio_agent.consume_guest_turns(),
            )
            result = await audio_agent.speaker_profiles_status()
            result["status"] = "ok"
            result["message"] = f"participant created as {display_name}"
            result["created_user_id"] = created_user_id
            result["profile"] = summary
            result["migrated_guest_turns"] = migrated_turns
            return result
        if action == "keep_guest":
            await audio_agent.keep_guest_participant()
            result = await audio_agent.speaker_profiles_status()
            result["status"] = "ok"
            result["message"] = "continuing this session as guest"
            return result
        if action == "start_fresh_guest":
            await audio_agent.start_fresh_guest_participant()
            result = await audio_agent.speaker_profiles_status()
            result["status"] = "ok"
            result["message"] = "started a fresh guest session"
            return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=408, detail="timed out waiting for next enrollment clip") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
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
