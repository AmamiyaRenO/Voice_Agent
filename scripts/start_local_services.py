#!/usr/bin/env python3
"""Utility to launch the local Voice Agent stack (MQTT + ASR/TTS helpers)."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from common.env_utils import apply_env_file
from common.process_supervisor import ProcessHandle, run_process_supervisor


@dataclass
class LauncherDefaults:
    repo_root: Path
    script_dir: Path
    service_dir: Path
    launcher_config_path: Path
    launcher_default_config_path: Path
    default_hub_cmd: Optional[str]
    asr_python: str
    tts_python: str
    intent_manifest_path: str
    game_manifest_path: str
    openai_api_key: str
    openai_base_url: str
    openai_transcribe_model: str
    openai_transcribe_prompt: str
    intent_launch_triggers: List[str]
    intent_exit_keywords: List[str]
    intent_use_llm_classifier: bool
    intent_use_moonshine_recognizer: bool
    extra_env: Dict[str, str]
    default_voice_cmd: str
    default_voice_dir: str
    default_piper_http_cmd: str
    default_piper_http_dir: str
    default_qwen_http_cmd: str
    default_qwen_http_dir: str
    default_kokoro_http_cmd: str
    default_kokoro_http_dir: str
    default_desktop_runtime_cmd: str
    default_desktop_runtime_dir: str
    default_intent_cmd: str
    default_intent_dir: str
    default_dialog_cmd: str
    default_dialog_dir: str
    default_telemetry_cmd: str
    default_telemetry_dir: str
    default_launcher_cmd: str
    default_launcher_dir: str


@dataclass
class CommandSet:
    hub: Optional[List[str]]
    voice: List[str]
    orchestrator: Optional[List[str]]
    piper_http: Optional[List[str]]
    qwen_http: Optional[List[str]]
    kokoro_http: Optional[List[str]]
    desktop_runtime: Optional[List[str]]
    intent: Optional[List[str]]
    dialog: Optional[List[str]]
    telemetry: Optional[List[str]]
    launcher: Optional[List[str]]


@dataclass
class DirectorySet:
    hub: Path
    voice: Path
    orchestrator: Path
    piper_http: Path
    qwen_http: Path
    kokoro_http: Path
    desktop_runtime: Path
    intent: Path
    dialog: Path
    telemetry: Path
    launcher: Path


def parse_command(value: str, *, windows: bool) -> List[str]:
    if not value:
        raise ValueError("Command string cannot be empty")
    parts = shlex.split(value, posix=not windows)
    if not windows:
        return parts

    # shlex with posix=False keeps surrounding quotes in tokens on Windows.
    # Strip only one matching outer pair so CreateProcess receives a valid path/arg.
    normalized: List[str] = []
    for part in parts:
        token = part.strip()
        # Accept accidentally escaped quotes from environment/batch values like:
        # \"C:\path\python.exe\" -m uvicorn ...
        if len(token) >= 4 and token.startswith('\\"') and token.endswith('\\"'):
            token = token[2:-2]
        if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
            token = token[1:-1]
        normalized.append(token)
    return normalized


def _normalize_string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


CORE_EXIT_KEYWORDS = ["back home", "go home", "return home", "go back"]
DEFAULT_EXIT_KEYWORDS = [*CORE_EXIT_KEYWORDS, "quit", "exit", "stop", "cancel", "close", "close game"]


def _env_or_default(name: str, default: Optional[str]) -> Optional[str]:
    raw = os.environ.get(name)
    if not isinstance(raw, str):
        return default
    text = raw.strip()
    if text:
        return text
    return default


def _json_object(value: object) -> Dict[str, object]:
    return value if isinstance(value, dict) else {}


def _config_get_string(data: Dict[str, object], *path: str) -> str:
    node: object = data
    for key in path:
        if not isinstance(node, dict):
            return ""
        node = node.get(key)
    return _normalize_string(node)


def _config_get_string_list(data: Dict[str, object], *path: str) -> List[str]:
    node: object = data
    for key in path:
        if not isinstance(node, dict):
            return []
        node = node.get(key)

    if isinstance(node, list):
        values: List[str] = []
        for item in node:
            text = _normalize_string(item)
            if text:
                values.append(text)
        return values

    if isinstance(node, str):
        merged = node.replace("\r\n", "\n").replace(";", ",").replace("\n", ",")
        values = [part.strip() for part in merged.split(",")]
        return [value for value in values if value]

    return []


def _merge_unique_strings(*groups: List[str]) -> List[str]:
    merged: List[str] = []
    seen = set()
    for group in groups:
        for item in group:
            text = _normalize_string(item)
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            merged.append(text)
    return merged


def _normalize_exit_keywords(values: List[str]) -> List[str]:
    merged = _merge_unique_strings(values, DEFAULT_EXIT_KEYWORDS)
    return merged or list(DEFAULT_EXIT_KEYWORDS)


def _config_get_bool(data: Dict[str, object], *path: str) -> Optional[bool]:
    node: object = data
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)

    if isinstance(node, bool):
        return node
    if isinstance(node, (int, float)):
        return bool(node)
    if isinstance(node, str):
        text = node.strip().lower()
        if text in {"1", "true", "t", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "f", "no", "n", "off"}:
            return False
    return None


def _normalize_manifest_path(raw: str, repo_root: Path) -> str:
    text = _normalize_string(raw)
    if not text:
        return ""

    expanded = Path(os.path.expandvars(text)).expanduser()
    if not expanded.is_absolute():
        expanded = (repo_root / expanded).resolve()
    candidate = str(expanded)

    if Path(candidate).exists():
        return candidate

    # Legacy buggy value from panel: <install>\app\scripts\... -> <install>\scripts\...
    normalized = candidate.replace("/", "\\")
    marker = "\\app\\scripts\\"
    idx = normalized.lower().find(marker)
    if idx >= 0:
        repaired = normalized[:idx] + normalized[idx + len("\\app") :]
        repaired_path = Path(repaired)
        if repaired_path.exists():
            return str(repaired_path)

    return candidate


def _resolve_launcher_config_path(repo_root: Path) -> Path:
    raw = _normalize_string(os.environ.get("VOICE_AGENT_LAUNCHER_CONFIG", ""))
    if raw:
        expanded = Path(os.path.expandvars(raw)).expanduser()
        if expanded.is_absolute():
            return expanded
        return (repo_root / expanded).resolve()

    # Installed one-click mode should default to user-writable config.
    if bool(getattr(sys, "frozen", False)):
        state_raw = _normalize_string(os.environ.get("VOICE_AGENT_STATE_DIR", ""))
        if state_raw:
            state_dir = Path(os.path.expandvars(state_raw)).expanduser()
        else:
            local_app_data = _normalize_string(os.environ.get("LOCALAPPDATA", ""))
            if local_app_data:
                state_dir = Path(local_app_data) / "VoiceAgent"
            else:
                state_dir = Path.home() / "AppData" / "Local" / "VoiceAgent"
        return state_dir / "local_services.user.json"

    return repo_root / "scripts" / "local_services.user.json"


def _resolve_launcher_default_config_path(repo_root: Path) -> Path:
    raw = _normalize_string(os.environ.get("VOICE_AGENT_DEFAULT_CONFIG", ""))
    if raw:
        expanded = Path(os.path.expandvars(raw)).expanduser()
        if expanded.is_absolute():
            return expanded
        return (repo_root / expanded).resolve()
    return repo_root / "scripts" / "local_services.default.json"


def _deep_merge_dict(base: Dict[str, object], override: Dict[str, object]) -> Dict[str, object]:
    merged: Dict[str, object] = dict(base)
    for key, value in override.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge_dict(merged[key], value)  # type: ignore[arg-type]
        else:
            merged[key] = value
    return merged


def _load_json_object(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            print(f"[voice-agent] launcher config ignored (root must be object): {path}")
            return {}
        return loaded
    except Exception as exc:
        print(f"[voice-agent] failed to load launcher config {path}: {exc}")
        return {}


def _load_launcher_config(repo_root: Path) -> Tuple[Path, Path, Dict[str, object]]:
    user_config_path = _resolve_launcher_config_path(repo_root)
    default_config_path = _resolve_launcher_default_config_path(repo_root)
    default_cfg = _load_json_object(default_config_path)
    user_cfg = _load_json_object(user_config_path)
    merged = _deep_merge_dict(default_cfg, user_cfg)
    intent_cfg = merged.get("intent")
    if isinstance(intent_cfg, dict):
        intent_cfg["exit_keywords"] = _normalize_exit_keywords(
            _config_get_string_list(merged, "intent", "exit_keywords")
        )
    return user_config_path, default_config_path, merged


def _service_runtime_roots(repo_root: Path) -> List[Path]:
    roots = [
        repo_root / "runtime" / "services",
        repo_root / "dist" / "services",
        repo_root / "runtime",
        repo_root / "dist",
        repo_root / "services",
        repo_root / "bin",
    ]
    return roots


def _find_packaged_service_executable(repo_root: Path, stem: str) -> str:
    candidates = [stem, stem.replace("_", "-"), stem.replace("-", "_")]
    extensions = [".exe"] if os.name == "nt" else [""]
    for root in _service_runtime_roots(repo_root):
        for candidate in candidates:
            for ext in extensions:
                path = root / f"{candidate}{ext}"
                if path.exists():
                    return str(path)
                nested = root / candidate / f"{candidate}{ext}"
                if nested.exists():
                    return str(nested)
    return ""


def _resolve_executable_override(value: str, *, base_dir: Path) -> str:
    raw = _normalize_string(value)
    if not raw:
        return ""

    # If the user provided a command name (python/py), resolve from PATH.
    if "\\" not in raw and "/" not in raw and ":" not in raw:
        from_path = shutil.which(raw)
        if from_path:
            return from_path
        return ""

    expanded = Path(os.path.expandvars(raw)).expanduser()
    if not expanded.is_absolute():
        expanded = (base_dir / expanded).resolve()
    if expanded.exists():
        return str(expanded)
    return ""


def _quote_command_token(token: str) -> str:
    raw = token.strip()
    if not raw:
        return raw
    if os.name == "nt":
        if raw.startswith('"') and raw.endswith('"'):
            return raw
        if any(ch.isspace() for ch in raw):
            return f'"{raw}"'
        return raw
    return shlex.quote(raw)


def _detect_mosquitto_executable() -> Optional[str]:
    """Return a usable mosquitto executable path if available."""
    env_override = os.environ.get("VOICE_AGENT_MOSQUITTO_EXE", "").strip()
    if env_override:
        candidate = Path(os.path.expandvars(env_override)).expanduser()
        if candidate.exists():
            return str(candidate)

    from_path = shutil.which("mosquitto")
    if from_path:
        return from_path

    if os.name == "nt":
        win_candidates = [
            Path(r"C:\Program Files\mosquitto\mosquitto.exe"),
            Path(r"C:\Program Files (x86)\mosquitto\mosquitto.exe"),
        ]
        for candidate in win_candidates:
            if candidate.exists():
                return str(candidate)

    return None


def _resolve_default_hub_command(repo_root: Path, mqtt_port: Optional[str] = None) -> Optional[str]:
    """Resolve a default local MQTT broker command without Robot_opr dependency."""
    broker_cmd = os.environ.get("VOICE_AGENT_BROKER_CMD", "").strip()
    if broker_cmd:
        return broker_cmd

    mosquitto_exe = _detect_mosquitto_executable()
    if not mosquitto_exe:
        return None

    normalized_port = _normalize_string(mqtt_port)
    conf_candidates: List[Path] = []
    if normalized_port and normalized_port != "1883":
        conf_candidates.append(repo_root / "scripts" / "mqtt" / f"mosquitto_{normalized_port}.conf")
    conf_candidates.append(repo_root / "scripts" / "mqtt" / "mosquitto.conf")

    for conf in conf_candidates:
        if conf.exists():
            return f'"{mosquitto_exe}" -c "{conf}" -v'

    if normalized_port and normalized_port != "1883":
        return f'"{mosquitto_exe}" -p {normalized_port} -v'
    return f'"{mosquitto_exe}" -v'


def _resolve_python_executable(
    *,
    preferred_paths: List[Path],
    config_override: str,
    config_base_dir: Path,
    env_var: str,
    fallback: str,
) -> str:
    config_candidate = _resolve_executable_override(config_override, base_dir=config_base_dir)
    if config_candidate:
        return config_candidate

    env_override = os.environ.get(env_var, "").strip()
    if env_override:
        env_candidate = _resolve_executable_override(env_override, base_dir=config_base_dir)
        if env_candidate:
            return env_candidate

    for path in preferred_paths:
        if path.exists():
            return str(path)

    return fallback


def _collapse_spaces(value: str) -> str:
    return " ".join(value.split())


def _maybe_normalize_bare_uvicorn_env(
    *,
    env_var: str,
    app: str,
    python_exe: str,
    port: int,
) -> None:
    raw = _normalize_string(os.environ.get(env_var, ""))
    if not raw:
        return

    normalized = _collapse_spaces(raw).lower()
    allowed = {
        f"uvicorn {app}",
        f"uvicorn {app} --port {port}",
        f"uvicorn {app} --host 0.0.0.0 --port {port}",
        f"uvicorn {app} --host 127.0.0.1 --port {port}",
    }
    if normalized not in allowed:
        return

    normalized_cmd = (
        f"{_quote_command_token(python_exe)} -m uvicorn {app} --host 0.0.0.0 --port {port}"
    )
    os.environ[env_var] = normalized_cmd
    print(f"[voice-agent] normalized {env_var} to use {python_exe}")


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "f", "no", "n", "off"}:
        return False
    return default


def _python_major_minor(python_exe: str) -> Optional[Tuple[int, int]]:
    try:
        result = subprocess.run(
            [python_exe, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    raw = (result.stdout or "").strip()
    if not raw:
        return None
    parts = raw.split(".", 1)
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _python_from_py_launcher(tag: str) -> str:
    if os.name != "nt":
        return ""
    try:
        result = subprocess.run(
            ["py", f"-{tag}", "-c", "import sys; print(sys.executable)"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    path = (result.stdout or "").strip()
    if not path:
        return ""
    candidate = Path(path)
    if candidate.exists():
        return str(candidate)
    return ""


def _pick_bootstrap_python(default_python: str) -> str:
    override = _resolve_executable_override(
        os.environ.get("VOICE_AGENT_BOOTSTRAP_PYTHON", ""),
        base_dir=Path.cwd(),
    )
    if override:
        return override

    default_version = _python_major_minor(default_python)
    if os.name == "nt" and default_version and default_version >= (3, 13):
        for tag in ("3.12", "3.11", "3.10"):
            candidate = _python_from_py_launcher(tag)
            if not candidate:
                continue
            candidate_version = _python_major_minor(candidate)
            if not candidate_version:
                continue
            print(f"[voice-agent] bootstrap: using Python {tag} for venv setup ({candidate})")
            return candidate
        print("[voice-agent] bootstrap: Python 3.13 detected and no 3.12/3.11/3.10 launcher found.")
    return default_python


def _venv_python_path(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _python_imports_ok(python_exe: Path, imports: List[str]) -> bool:
    if not python_exe.exists():
        return False
    script = "; ".join([f"import {name}" for name in imports])
    try:
        result = subprocess.run(
            [str(python_exe), "-c", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=25,
        )
    except Exception:
        return False
    return result.returncode == 0


def _run_bootstrap_command(cmd: List[str], cwd: Path) -> bool:
    try:
        print("[voice-agent] bootstrap:", " ".join(cmd))
        result = subprocess.run(cmd, cwd=str(cwd), check=False)
        return result.returncode == 0
    except Exception as exc:
        print(f"[voice-agent] bootstrap command failed: {exc}")
        return False


def _ensure_python_venv(
    *,
    venv_dir: Path,
    requirements_files: List[Path],
    import_checks: List[str],
    bootstrap_python: str,
    cwd: Path,
) -> bool:
    venv_python = _venv_python_path(venv_dir)
    bootstrap_version = _python_major_minor(bootstrap_python)
    if venv_python.exists():
        if _python_imports_ok(venv_python, import_checks):
            return True
        venv_version = _python_major_minor(str(venv_python))
        if bootstrap_version and venv_version and venv_version != bootstrap_version:
            print(
                f"[voice-agent] bootstrap: recreating {venv_dir.name} "
                f"(found Python {venv_version[0]}.{venv_version[1]}, expected {bootstrap_version[0]}.{bootstrap_version[1]})"
            )
            try:
                shutil.rmtree(venv_dir)
            except Exception as exc:
                print(f"[voice-agent] bootstrap: failed to remove stale venv {venv_dir}: {exc}")
                return False
            venv_python = _venv_python_path(venv_dir)

    if not venv_python.exists():
        if not _run_bootstrap_command([bootstrap_python, "-m", "venv", str(venv_dir)], cwd):
            return False

    if not _run_bootstrap_command([str(venv_python), "-m", "pip", "install", "-U", "pip"], cwd):
        return False

    for req in requirements_files:
        if not req.exists():
            continue
        if not _run_bootstrap_command(
            [str(venv_python), "-m", "pip", "install", "-r", str(req)],
            cwd,
        ):
            return False

    return _python_imports_ok(venv_python, import_checks)


def _build_defaults(repo_root: Path) -> LauncherDefaults:
    script_dir = repo_root / "scripts"
    if not script_dir.exists():
        script_dir = Path(__file__).resolve().parents[0]
    service_dir = repo_root / "python_voice_service"
    config_path, default_config_path, launcher_config = _load_launcher_config(repo_root)

    python_cfg = _json_object(launcher_config.get("python"))
    paths_cfg = _json_object(launcher_config.get("paths"))
    openai_cfg = _json_object(launcher_config.get("openai"))
    intent_cfg = _json_object(launcher_config.get("intent"))
    raw_env_cfg = _json_object(launcher_config.get("env"))
    env_cfg: Dict[str, str] = {}
    for key, value in raw_env_cfg.items():
        k = _normalize_string(key)
        v = _normalize_string(value)
        if k and v:
            env_cfg[k] = v

    asr_override = _config_get_string(python_cfg, "asr") or _config_get_string(
        launcher_config, "asr_python"
    )
    tts_override = _config_get_string(python_cfg, "tts") or _config_get_string(
        launcher_config, "tts_python"
    )

    running_frozen = bool(getattr(sys, "frozen", False))
    bootstrap_python = _pick_bootstrap_python(sys.executable)
    auto_bootstrap_venv = _env_bool("VOICE_AGENT_AUTO_BOOTSTRAP_VENV", True)
    if running_frozen:
        # In packaged mode we prefer bundled service executables and should not
        # try to create venvs from a frozen launcher executable.
        auto_bootstrap_venv = False
    if auto_bootstrap_venv and not asr_override:
        asr_ok = _ensure_python_venv(
            venv_dir=service_dir / ".venv_asr",
            requirements_files=[service_dir / "requirements.txt"],
            import_checks=["fastapi", "uvicorn", "numpy", "httpx", "paho.mqtt.client", "yaml", "sounddevice", "cv2"],
            bootstrap_python=bootstrap_python,
            cwd=service_dir,
        )
        if not asr_ok:
            print("[voice-agent] warning: failed to prepare .venv_asr, will fall back to system python.")

    if auto_bootstrap_venv and not tts_override:
        tts_ok = _ensure_python_venv(
            venv_dir=service_dir / ".venv_tts",
            requirements_files=[service_dir / "requirements_qwen_tts.txt"],
            import_checks=["fastapi", "uvicorn", "numpy", "kokoro"],
            bootstrap_python=bootstrap_python,
            cwd=service_dir,
        )
        if not tts_ok:
            print("[voice-agent] warning: failed to prepare .venv_tts, will fall back to system python.")

    asr_python = _resolve_python_executable(
        preferred_paths=[
            service_dir / ".venv_asr" / "Scripts" / "python.exe",
            service_dir / ".venv" / "Scripts" / "python.exe",
        ],
        config_override=asr_override,
        config_base_dir=repo_root,
        env_var="VOICE_AGENT_ASR_PYTHON",
        fallback="python" if running_frozen else bootstrap_python,
    )
    tts_python = _resolve_python_executable(
        preferred_paths=[
            service_dir / ".venv_tts" / "Scripts" / "python.exe",
            service_dir / ".venv" / "Scripts" / "python.exe",
        ],
        config_override=tts_override,
        config_base_dir=repo_root,
        env_var="VOICE_AGENT_TTS_PYTHON",
        fallback=asr_python,
    )
    _maybe_normalize_bare_uvicorn_env(
        env_var="VOICE_AGENT_VOICE_CMD",
        app="main:app",
        python_exe=asr_python,
        port=8000,
    )
    _maybe_normalize_bare_uvicorn_env(
        env_var="VOICE_AGENT_PIPER_HTTP_CMD",
        app="piper_http:app",
        python_exe=tts_python,
        port=5005,
    )
    _maybe_normalize_bare_uvicorn_env(
        env_var="VOICE_AGENT_QWEN_HTTP_CMD",
        app="qwen_tts_http:app",
        python_exe=tts_python,
        port=5006,
    )
    _maybe_normalize_bare_uvicorn_env(
        env_var="VOICE_AGENT_KOKORO_HTTP_CMD",
        app="kokoro_tts_http:app",
        python_exe=tts_python,
        port=5007,
    )
    _maybe_normalize_bare_uvicorn_env(
        env_var="VOICE_AGENT_DESKTOP_RUNTIME_CMD",
        app="desktop_runtime:app",
        python_exe=asr_python,
        port=8787,
    )

    intent_manifest = _config_get_string(paths_cfg, "intent_manifest") or _config_get_string(
        launcher_config, "intent_manifest_path"
    )
    game_manifest = _config_get_string(paths_cfg, "game_manifest") or _config_get_string(
        launcher_config, "game_manifest_path"
    )
    if intent_manifest:
        intent_manifest = _normalize_manifest_path(intent_manifest, repo_root)
    if game_manifest:
        game_manifest = _normalize_manifest_path(game_manifest, repo_root)

    openai_api_key = _config_get_string(openai_cfg, "api_key") or _config_get_string(
        launcher_config, "openai_api_key"
    )
    openai_base_url = _config_get_string(openai_cfg, "base_url") or _config_get_string(
        launcher_config, "openai_base_url"
    )
    openai_transcribe_model = _config_get_string(
        openai_cfg, "transcribe_model"
    ) or _config_get_string(launcher_config, "openai_transcribe_model")
    openai_transcribe_prompt = _config_get_string(
        openai_cfg, "transcribe_prompt"
    ) or _config_get_string(launcher_config, "openai_transcribe_prompt")
    intent_launch_triggers = _config_get_string_list(
        intent_cfg, "launch_triggers"
    ) or _config_get_string_list(launcher_config, "launch_triggers")
    intent_exit_keywords = _config_get_string_list(
        intent_cfg, "exit_keywords"
    ) or _config_get_string_list(launcher_config, "exit_keywords")
    intent_exit_keywords = _normalize_exit_keywords(intent_exit_keywords)
    intent_use_llm_classifier = _config_get_bool(intent_cfg, "use_llm_classifier")
    if intent_use_llm_classifier is None:
        intent_use_llm_classifier = False
    intent_use_moonshine_recognizer = _config_get_bool(
        intent_cfg, "use_moonshine_intent_recognizer"
    )
    if intent_use_moonshine_recognizer is None:
        intent_use_moonshine_recognizer = False

    asr_python_cmd = _quote_command_token(asr_python)
    tts_python_cmd = _quote_command_token(tts_python)
    telemetry_main = _quote_command_token(str(script_dir / "telemetry_service" / "main.py"))
    launcher_main = _quote_command_token(str(script_dir / "game_launcher" / "main.py"))

    # In source/dev mode we should default to Python scripts so local edits take
    # effect immediately. Packaged services are used by default only when frozen.
    use_packaged_services = running_frozen or _env_bool("VOICE_AGENT_USE_PACKAGED_SERVICES", False)
    if use_packaged_services:
        voice_exe = _find_packaged_service_executable(repo_root, "voice_service")
        piper_exe = _find_packaged_service_executable(repo_root, "piper_http")
        qwen_exe = _find_packaged_service_executable(repo_root, "qwen_tts_http")
        kokoro_exe = _find_packaged_service_executable(repo_root, "kokoro_tts_http")
        desktop_runtime_exe = _find_packaged_service_executable(repo_root, "desktop_runtime")
        intent_exe = _find_packaged_service_executable(repo_root, "intent_service")
        dialog_exe = _find_packaged_service_executable(repo_root, "dialog_service")
        telemetry_exe = _find_packaged_service_executable(repo_root, "telemetry_service")
        launcher_exe = _find_packaged_service_executable(repo_root, "game_launcher")
    else:
        voice_exe = ""
        piper_exe = ""
        qwen_exe = ""
        kokoro_exe = ""
        desktop_runtime_exe = ""
        intent_exe = ""
        dialog_exe = ""
        telemetry_exe = ""
        launcher_exe = ""

    source_service_available = service_dir.exists()

    default_voice_cmd = (
        _quote_command_token(voice_exe)
        if voice_exe
        else f"{asr_python_cmd} -m uvicorn main:app --host 0.0.0.0 --port 8000"
    )
    default_voice_dir = str(Path(voice_exe).resolve().parent) if voice_exe else str(service_dir)
    default_piper_http_cmd = (
        _quote_command_token(piper_exe)
        if piper_exe
        else f"{tts_python_cmd} -m uvicorn piper_http:app --host 0.0.0.0 --port 5005"
    )
    default_piper_http_dir = str(Path(piper_exe).resolve().parent) if piper_exe else str(service_dir)
    if qwen_exe:
        default_qwen_http_cmd = _quote_command_token(qwen_exe)
        default_qwen_http_dir = str(Path(qwen_exe).resolve().parent)
    elif source_service_available:
        default_qwen_http_cmd = f"{tts_python_cmd} -m uvicorn qwen_tts_http:app --host 0.0.0.0 --port 5006"
        default_qwen_http_dir = str(service_dir)
    else:
        # Packaged installs that do not ship qwen_tts_http.exe should not fail
        # validation by default.
        default_qwen_http_cmd = ""
        default_qwen_http_dir = str(repo_root)
    if kokoro_exe:
        default_kokoro_http_cmd = _quote_command_token(kokoro_exe)
        default_kokoro_http_dir = str(Path(kokoro_exe).resolve().parent)
    elif source_service_available:
        default_kokoro_http_cmd = f"{tts_python_cmd} -m uvicorn kokoro_tts_http:app --host 0.0.0.0 --port 5007"
        default_kokoro_http_dir = str(service_dir)
    else:
        default_kokoro_http_cmd = ""
        default_kokoro_http_dir = str(repo_root)
    default_desktop_runtime_cmd = (
        _quote_command_token(desktop_runtime_exe)
        if desktop_runtime_exe
        else f"{asr_python_cmd} -m uvicorn desktop_runtime:app --host 0.0.0.0 --port 8787"
    )
    default_desktop_runtime_dir = (
        str(Path(desktop_runtime_exe).resolve().parent)
        if desktop_runtime_exe
        else str(service_dir)
    )
    default_telemetry_cmd = (
        _quote_command_token(telemetry_exe)
        if telemetry_exe
        else f"{tts_python_cmd} {telemetry_main}"
    )
    default_telemetry_dir = (
        str(Path(telemetry_exe).resolve().parent)
        if telemetry_exe
        else str(script_dir / "telemetry_service")
    )
    default_launcher_cmd = (
        _quote_command_token(launcher_exe)
        if launcher_exe
        else f"{asr_python_cmd} {launcher_main}"
    )
    default_launcher_dir = (
        str(Path(launcher_exe).resolve().parent)
        if launcher_exe
        else str(script_dir / "game_launcher")
    )

    default_intent_cmd = (
        _quote_command_token(intent_exe)
        if intent_exe
        else (
            _quote_command_token(asr_python)
            + " "
            + _quote_command_token(str(script_dir / "intent_service" / "main.py"))
        )
    )
    default_intent_dir = (
        str(Path(intent_exe).resolve().parent)
        if intent_exe
        else str(script_dir / "intent_service")
    )
    default_dialog_cmd = (
        _quote_command_token(dialog_exe)
        if dialog_exe
        else (
            _quote_command_token(asr_python)
            + " "
            + _quote_command_token(str(script_dir / "dialog_service" / "main.py"))
        )
    )
    default_dialog_dir = (
        str(Path(dialog_exe).resolve().parent)
        if dialog_exe
        else str(script_dir / "dialog_service")
    )

    default_hub_cmd = _normalize_string(env_cfg.get("VOICE_AGENT_BROKER_CMD")) or _resolve_default_hub_command(
        repo_root,
        mqtt_port=_normalize_string(env_cfg.get("MQTT_PORT")),
    )

    return LauncherDefaults(
        repo_root=repo_root,
        script_dir=script_dir,
        service_dir=service_dir,
        launcher_config_path=config_path,
        launcher_default_config_path=default_config_path,
        default_hub_cmd=default_hub_cmd,
        asr_python=asr_python,
        tts_python=tts_python,
        intent_manifest_path=intent_manifest,
        game_manifest_path=game_manifest,
        openai_api_key=openai_api_key,
        openai_base_url=openai_base_url,
        openai_transcribe_model=openai_transcribe_model,
        openai_transcribe_prompt=openai_transcribe_prompt,
        intent_launch_triggers=intent_launch_triggers,
        intent_exit_keywords=intent_exit_keywords,
        intent_use_llm_classifier=intent_use_llm_classifier,
        intent_use_moonshine_recognizer=intent_use_moonshine_recognizer,
        extra_env=env_cfg,
        default_voice_cmd=default_voice_cmd,
        default_voice_dir=default_voice_dir,
        default_piper_http_cmd=default_piper_http_cmd,
        default_piper_http_dir=default_piper_http_dir,
        default_qwen_http_cmd=default_qwen_http_cmd,
        default_qwen_http_dir=default_qwen_http_dir,
        default_kokoro_http_cmd=default_kokoro_http_cmd,
        default_kokoro_http_dir=default_kokoro_http_dir,
        default_desktop_runtime_cmd=default_desktop_runtime_cmd,
        default_desktop_runtime_dir=default_desktop_runtime_dir,
        default_intent_cmd=default_intent_cmd,
        default_intent_dir=default_intent_dir,
        default_dialog_cmd=default_dialog_cmd,
        default_dialog_dir=default_dialog_dir,
        default_telemetry_cmd=default_telemetry_cmd,
        default_telemetry_dir=default_telemetry_dir,
        default_launcher_cmd=default_launcher_cmd,
        default_launcher_dir=default_launcher_dir,
    )


def _build_parser(defaults: LauncherDefaults) -> argparse.ArgumentParser:

    parser = argparse.ArgumentParser(
        description="Launch the local Voice Agent services in one command.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--hub-cmd",
        default=defaults.default_hub_cmd if defaults.default_hub_cmd else None,
        help=(
            "Optional command used to start the local MQTT broker. If omitted, "
            "the launcher tries VOICE_AGENT_BROKER_CMD or a local Mosquitto install."
        ),
    )
    parser.add_argument(
        "--hub-dir",
        default=str(defaults.repo_root),
        help="Working directory for the messaging hub command.",
    )
    parser.add_argument(
        "--no-hub",
        action="store_true",
        help="Do not start an MQTT broker (use an already-running external broker).",
    )
    parser.add_argument(
        "--voice-cmd",
        default=_env_or_default("VOICE_AGENT_VOICE_CMD", defaults.default_voice_cmd),
        help="Command used to start the Python voice service.",
    )
    parser.add_argument(
        "--voice-dir",
        default=_env_or_default("VOICE_AGENT_VOICE_CWD", defaults.default_voice_dir),
        help="Working directory for the Python voice service command.",
    )
    parser.add_argument(
        "--orchestrator-cmd",
        default=_env_or_default("VOICE_AGENT_ORCH_CMD", None),
        help=(
            "Optional command used to start the orchestrator that manages Mosquitto. "
            "Set VOICE_AGENT_ORCH_CMD or pass --orchestrator-cmd to enable it."
        ),
    )
    parser.add_argument(
        "--orchestrator-dir",
        default=_env_or_default("VOICE_AGENT_ORCH_CWD", str(Path.cwd())),
        help="Working directory for the orchestrator command.",
    )
    parser.add_argument(
        "--piper-http-cmd",
        default=defaults.default_piper_http_cmd,
        help=(
            "Optional command used to start a lightweight Piper HTTP wrapper service. "
            "Pass --piper-http-cmd to override."
        ),
    )
    parser.add_argument(
        "--piper-http-dir",
        default=_env_or_default("VOICE_AGENT_PIPER_HTTP_CWD", defaults.default_piper_http_dir),
        help="Working directory for the Piper HTTP wrapper service.",
    )
    parser.add_argument(
        "--qwen-http-cmd",
        default=defaults.default_qwen_http_cmd,
        help=(
            "Optional command used to start a Qwen TTS HTTP wrapper service. "
            "Pass --qwen-http-cmd to override."
        ),
    )
    parser.add_argument(
        "--qwen-http-dir",
        default=_env_or_default("VOICE_AGENT_QWEN_HTTP_CWD", defaults.default_qwen_http_dir),
        help="Working directory for the Qwen HTTP wrapper service.",
    )
    parser.add_argument(
        "--kokoro-http-cmd",
        default=defaults.default_kokoro_http_cmd,
        help=(
            "Optional command used to start a Kokoro TTS HTTP wrapper service. "
            "Pass --kokoro-http-cmd to override."
        ),
    )
    parser.add_argument(
        "--kokoro-http-dir",
        default=_env_or_default("VOICE_AGENT_KOKORO_HTTP_CWD", defaults.default_kokoro_http_dir),
        help="Working directory for the Kokoro HTTP wrapper service.",
    )
    parser.add_argument(
        "--desktop-runtime-cmd",
        default=_env_or_default("VOICE_AGENT_DESKTOP_RUNTIME_CMD", defaults.default_desktop_runtime_cmd),
        help=(
            "Optional command used to start the standalone desktop runtime panel "
            "(userpanel + audio agent outside Unity)."
        ),
    )
    parser.add_argument(
        "--desktop-runtime-dir",
        default=_env_or_default("VOICE_AGENT_DESKTOP_RUNTIME_CWD", defaults.default_desktop_runtime_dir),
        help="Working directory for the desktop runtime service.",
    )
    parser.add_argument(
        "--intent-cmd",
        default=_env_or_default("VOICE_AGENT_INTENT_CMD", defaults.default_intent_cmd),
        help=(
            "Optional command used to start the intent recognition service. "
            "Set VOICE_AGENT_INTENT_CMD or pass --intent-cmd to enable it."
        ),
    )
    parser.add_argument(
        "--intent-dir",
        default=_env_or_default("VOICE_AGENT_INTENT_CWD", defaults.default_intent_dir),
        help="Working directory for the intent recognition service.",
    )
    parser.add_argument(
        "--dialog-cmd",
        default=_env_or_default("VOICE_AGENT_DIALOG_CMD", defaults.default_dialog_cmd),
        help=(
            "Optional command used to start the dialog (LLM+TTS) service. "
            "Set VOICE_AGENT_DIALOG_CMD or pass --dialog-cmd to enable it."
        ),
    )
    parser.add_argument(
        "--dialog-dir",
        default=_env_or_default("VOICE_AGENT_DIALOG_CWD", defaults.default_dialog_dir),
        help="Working directory for the dialog (LLM+TTS) service.",
    )
    parser.add_argument(
        "--launcher-cmd",
        default=_env_or_default("VOICE_AGENT_LAUNCHER_CMD", defaults.default_launcher_cmd),
        help=(
            "Optional command used to start the local game launcher service that consumes "
            "robot/intent and opens/closes games."
        ),
    )
    parser.add_argument(
        "--launcher-dir",
        default=_env_or_default("VOICE_AGENT_LAUNCHER_CWD", defaults.default_launcher_dir),
        help="Working directory for the local game launcher service.",
    )
    parser.add_argument(
        "--telemetry-cmd",
        default=_env_or_default("VOICE_AGENT_TELEMETRY_CMD", defaults.default_telemetry_cmd),
        help=(
            "Optional command used to start the telemetry aggregation service "
            "(HTTP metrics + MQTT ingest)."
        ),
    )
    parser.add_argument(
        "--telemetry-dir",
        default=_env_or_default("VOICE_AGENT_TELEMETRY_CWD", defaults.default_telemetry_dir),
        help="Working directory for the telemetry service.",
    )
    parser.add_argument(
        "--no-telemetry",
        action="store_true",
        help="Do not start the telemetry service.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Optional .env style file whose variables are exported before launching the services.",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Do not block waiting for the processes to exit.",
    )
    parser.add_argument(
        "--no-cleanup-old-services",
        action="store_true",
        help="Do not kill existing repo-owned service process trees before launch.",
    )
    return parser


def _parse_commands(
    args: argparse.Namespace,
    *,
    windows: bool,
    parser: argparse.ArgumentParser,
) -> CommandSet:
    try:
        return CommandSet(
            hub=None if args.no_hub else (
                parse_command(args.hub_cmd, windows=windows)
                if args.hub_cmd
                else None
            ),
            voice=parse_command(args.voice_cmd, windows=windows),
            orchestrator=(
                parse_command(args.orchestrator_cmd, windows=windows)
                if args.orchestrator_cmd
                else None
            ),
            piper_http=(
                parse_command(args.piper_http_cmd, windows=windows)
                if args.piper_http_cmd
                else None
            ),
            qwen_http=(
                parse_command(args.qwen_http_cmd, windows=windows)
                if args.qwen_http_cmd
                else None
            ),
            kokoro_http=(
                parse_command(args.kokoro_http_cmd, windows=windows)
                if args.kokoro_http_cmd
                else None
            ),
            desktop_runtime=(
                parse_command(args.desktop_runtime_cmd, windows=windows)
                if args.desktop_runtime_cmd
                else None
            ),
            intent=(
                parse_command(args.intent_cmd, windows=windows)
                if args.intent_cmd
                else None
            ),
            dialog=(
                parse_command(args.dialog_cmd, windows=windows)
                if args.dialog_cmd
                else None
            ),
            telemetry=(
                None
                if args.no_telemetry
                else (
                    parse_command(args.telemetry_cmd, windows=windows)
                    if args.telemetry_cmd
                    else None
                )
            ),
            launcher=(
                parse_command(args.launcher_cmd, windows=windows)
                if args.launcher_cmd
                else None
            ),
        )
    except ValueError as exc:
        parser.error(str(exc))
        raise RuntimeError("unreachable")


def _resolve_directories(args: argparse.Namespace) -> DirectorySet:
    return DirectorySet(
        hub=Path(args.hub_dir).resolve(),
        voice=Path(args.voice_dir).resolve(),
        orchestrator=Path(args.orchestrator_dir).resolve(),
        piper_http=Path(args.piper_http_dir).resolve(),
        qwen_http=Path(args.qwen_http_dir).resolve(),
        kokoro_http=Path(args.kokoro_http_dir).resolve(),
        desktop_runtime=Path(args.desktop_runtime_dir).resolve(),
        intent=Path(args.intent_dir).resolve(),
        dialog=Path(args.dialog_dir).resolve(),
        telemetry=Path(args.telemetry_dir).resolve(),
        launcher=Path(args.launcher_dir).resolve(),
    )


def _validate_paths(
    *,
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    commands: CommandSet,
    dirs: DirectorySet,
) -> None:
    if commands.hub is None and not args.no_hub:
        parser.error(
            "No MQTT broker command resolved. Install Mosquitto, set VOICE_AGENT_BROKER_CMD, "
            "pass --hub-cmd, or use --no-hub with an external broker."
        )
    if commands.hub is not None and not dirs.hub.exists():
        parser.error(f"Hub directory does not exist: {dirs.hub}")
    if not dirs.voice.exists():
        parser.error(f"Voice service directory does not exist: {dirs.voice}")
    if commands.orchestrator is not None and not dirs.orchestrator.exists():
        parser.error(f"Orchestrator directory does not exist: {dirs.orchestrator}")
    if commands.piper_http is not None and not dirs.piper_http.exists():
        parser.error(f"Piper HTTP directory does not exist: {dirs.piper_http}")
    if commands.qwen_http is not None and not dirs.qwen_http.exists():
        parser.error(f"Qwen HTTP directory does not exist: {dirs.qwen_http}")
    if commands.kokoro_http is not None and not dirs.kokoro_http.exists():
        parser.error(f"Kokoro HTTP directory does not exist: {dirs.kokoro_http}")
    if commands.desktop_runtime is not None and not dirs.desktop_runtime.exists():
        parser.error(f"Desktop runtime directory does not exist: {dirs.desktop_runtime}")
    if commands.intent is not None and not dirs.intent.exists():
        parser.error(f"Intent service directory does not exist: {dirs.intent}")
    if commands.dialog is not None and not dirs.dialog.exists():
        parser.error(f"Dialog service directory does not exist: {dirs.dialog}")
    if commands.telemetry is not None and not dirs.telemetry.exists():
        parser.error(f"Telemetry service directory does not exist: {dirs.telemetry}")
    if commands.launcher is not None and not dirs.launcher.exists():
        parser.error(f"Launcher service directory does not exist: {dirs.launcher}")


def _build_runtime_env(args: argparse.Namespace, defaults: LauncherDefaults) -> Dict[str, str]:
    env = os.environ.copy()
    for key, value in defaults.extra_env.items():
        env[key] = value
    if defaults.openai_api_key:
        env["OPENAI_API_KEY"] = defaults.openai_api_key
    if defaults.openai_base_url:
        env["OPENAI_BASE_URL"] = defaults.openai_base_url
    if defaults.openai_transcribe_model:
        env["OPENAI_TRANSCRIBE_MODEL"] = defaults.openai_transcribe_model
    if defaults.openai_transcribe_prompt:
        env["OPENAI_TRANSCRIBE_PROMPT"] = defaults.openai_transcribe_prompt
    if defaults.intent_launch_triggers:
        env["INTENT_LAUNCH_TRIGGERS"] = json.dumps(defaults.intent_launch_triggers, ensure_ascii=False)
    if defaults.intent_exit_keywords:
        env["INTENT_EXIT_KEYWORDS"] = json.dumps(defaults.intent_exit_keywords, ensure_ascii=False)
    env["INTENT_USE_LLM_CLASSIFIER"] = "1" if defaults.intent_use_llm_classifier else "0"
    env["INTENT_USE_MOONSHINE_RECOGNIZER"] = (
        "1" if defaults.intent_use_moonshine_recognizer else "0"
    )
    if args.env_file is not None:
        apply_env_file(args.env_file, env)

    # Force voice service /tts to proxy to local Piper HTTP unless explicitly changed here.
    env["PIPER_HTTP_URL"] = "http://127.0.0.1:5005"
    env["KOKORO_HTTP_URL"] = "http://127.0.0.1:5007"
    # Avoid accidental redirection to remote Piper server.
    if "PIPER_SERVER_URL" in env:
        env.pop("PIPER_SERVER_URL", None)

    # IMPORTANT (AEC): Ensure dialog_service does NOT play audio locally via winsound.
    # TTS must be played through Unity so the AudioListener render reference is available for echo cancellation.
    env["DIALOG_SPEAK_AUDIO"] = "0"
    # Latency-first defaults (can still be overridden by user env/.env).
    env.setdefault("OLLAMA_MAX_TOKENS", "180")
    env.setdefault("OLLAMA_KEEP_ALIVE", "30m")
    env.setdefault("OLLAMA_MODEL", "qwen3.5:0.8b")
    env.setdefault("OLLAMA_THINK", "0")
    env.setdefault("OLLAMA_TEMPERATURE", "0.7")
    env.setdefault("OLLAMA_TOP_P", "0.8")
    env.setdefault("OLLAMA_TOP_K", "20")
    env.setdefault("DIALOG_ENABLE_CONTEXT_MEMORY", "1")
    env.setdefault("DIALOG_ENABLE_POLICY", "1")
    env.setdefault("DIALOG_HISTORY_TURNS", "8")
    env.setdefault("DIALOG_SUMMARY_MAX_CHARS", "420")
    env.setdefault("DIALOG_CONTEXT_MAX_CHARS", "900")
    env.setdefault("DIALOG_MEMORY_QUERY_RULE", "1")
    env.setdefault("DIALOG_MEMORY_QUERY_SEMANTIC", "1")
    env.setdefault("DIALOG_MEMORY_QUERY_THRESHOLD", "0.58")
    env.setdefault("DIALOG_ENABLE_VISION_QUERY", "1")
    env.setdefault("DIALOG_VISION_DESCRIBE_URL", "http://127.0.0.1:8787/api/vision/describe")
    env.setdefault(
        "DIALOG_VISION_QUERY_PROMPT",
        "Describe what you see in this camera frame in 2-4 concise sentences.",
    )
    env.setdefault("DIALOG_VISION_TIMEOUT_SECONDS", "12")
    # ASR defaults for NucBox M6 (no NVIDIA GPU): CPU-only faster-whisper tuning.
    env.setdefault("WHISPER_DEVICE", "cpu")
    env.setdefault("WHISPER_COMPUTE_TYPE", "int8")
    env.setdefault("WHISPER_MODEL_PATH", "Systran/faster-distil-whisper-large-v3")
    env.setdefault("WHISPER_CPU_THREADS", "6")
    env.setdefault("WHISPER_VAD_SILENCE_MS", "280")
    env.setdefault("WHISPER_VAD_MIN_SPEECH_MS", "140")
    env.setdefault("WHISPER_STREAM_OVERLAP_SECONDS", "0.6")
    env.setdefault("WHISPER_STREAM_CONTEXT_CHARS", "180")
    env.setdefault("WHISPER_LOW_CONFIDENCE_THRESHOLD", "-0.8")
    env.setdefault("WHISPER_RETRY_BEAM_BONUS", "1")
    env.setdefault("WHISPER_RETRY_MAX_BEAM", "6")
    env.setdefault("WHISPER_RETRY_TEMPERATURES", "0.0,0.2")
    env.setdefault("ASR_DEFAULT_LANGUAGE", "en")
    env.setdefault("ASR_FORCE_LANGUAGE", "en")
    env.setdefault("ASR_ENGLISH_ONLY", "1")
    explicit_transcribe_mode = _normalize_string(env.get("TRANSCRIBE_MODE"))
    env.setdefault("VOICE_PIPELINE_MODE", "direct_unified")
    env.setdefault("VOICE_CONVERSATION_PROFILE", "local")
    env.setdefault("VOICE_LOCAL_ASR_MODE", "moonshine-medium")
    env.setdefault("VOICE_CLOUD_ASR_MODE", "api")
    env.setdefault("VOICE_LOCAL_STREAMING_ASR_MODE", env.get("VOICE_LOCAL_ASR_MODE", "moonshine-medium"))
    env.setdefault("VOICE_CLOUD_STREAMING_ASR_MODE", env.get("VOICE_LOCAL_STREAMING_ASR_MODE", "moonshine-medium"))
    env.setdefault("VOICE_ASR_HOTWORD_STRATEGY", "commands_games_memory")
    env.setdefault("VOICE_ASR_STABLE_PARTIAL_REPEATS", "2")
    env.setdefault("VOICE_CLOUD_RESPONSE_PROVIDER", "openai")
    env.setdefault("VOICE_AGENT_TTS_BACKEND", "piper")
    env.setdefault("OPENAI_RESPONSE_MODEL", "gpt-4o-mini")
    env.setdefault("DOC_RAG_ENABLE", "1")
    env.setdefault("DOC_RAG_ROOT", str(defaults.repo_root / "docs" / "rag" / "bioadaptive_lab"))
    if explicit_transcribe_mode:
        env["TRANSCRIBE_MODE"] = explicit_transcribe_mode
    else:
        profile = (env.get("VOICE_CONVERSATION_PROFILE", "local") or "local").strip().lower()
        preferred_mode_key = "VOICE_CLOUD_ASR_MODE" if profile == "cloud" else "VOICE_LOCAL_ASR_MODE"
        preferred_mode_default = "api" if profile == "cloud" else "moonshine-medium"
        env["TRANSCRIBE_MODE"] = (env.get(preferred_mode_key, preferred_mode_default) or preferred_mode_default).strip() or preferred_mode_default
    env.setdefault("DIALOG_REPLY_COMPRESS", "1")
    env.setdefault("DIALOG_MAX_REPLY_SENTENCES", "3")
    env.setdefault("DIALOG_MAX_REPLY_CHARS", "0")
    env.setdefault("DIALOG_MAX_REPLY_WORDS", "0")
    env.setdefault("QWEN_TTS_SPEED_PROFILE", "fast")
    env.setdefault("QWEN_TTS_SPEAKER", "Ryan")
    env.setdefault("QWEN_TTS_INSTRUCT", "")
    env.setdefault("QWEN_TTS_DO_SAMPLE", "0")
    env.setdefault("KOKORO_TTS_VOICE", "af_heart")
    env.setdefault("KOKORO_TTS_LANG_CODE", "a")
    env.setdefault("KOKORO_TTS_SPEED", "1.0")
    # Do not hard-truncate reply text by default; this can sound like the last
    # 1-2 words are "swallowed" on longer sentences. Users can still opt-in.
    env.setdefault("QWEN_TTS_MAX_TEXT_CHARS", "0")
    env.setdefault("QWEN_TTS_FAST_SHORT_MAX_NEW_TOKENS", "240")
    env.setdefault("QWEN_TTS_WARMUP_TEXT", "Hello. I am ready.")
    env.setdefault("MQTT_HOST", "127.0.0.1")
    env.setdefault("MQTT_PORT", "1883")
    env.setdefault("PANEL_PORT", "8787")
    env.setdefault("VOICE_AGENT_PANEL_URL", "http://127.0.0.1:8787")
    env.setdefault(
        "DIALOG_USER_MEMORY_PATH",
        str(defaults.script_dir / "dialog_service" / "user_memory.json"),
    )
    env.setdefault("TELEMETRY_HOST", "0.0.0.0")
    env.setdefault("TELEMETRY_MQTT_TOPIC", "voiceagent/telemetry/#")
    env.setdefault("TELEMETRY_AUTO_SEED", "1")
    env.setdefault("TELEMETRY_SEED_USER", "demo_user")
    env.setdefault("TELEMETRY_SEED_DAYS", "21")
    env.setdefault("TELEMETRY_PORT", "8101")
    env.setdefault(
        "INTENT_MANIFEST_PATH",
        defaults.intent_manifest_path
        or str(defaults.script_dir / "intent_service" / "manifest.json"),
    )
    env.setdefault(
        "GAME_LAUNCHER_MANIFEST_PATH",
        defaults.game_manifest_path
        or defaults.intent_manifest_path
        or str(defaults.script_dir / "intent_service" / "manifest.json"),
    )

    doc_root = Path(str(env.get("DOC_RAG_ROOT", "") or "")).expanduser()
    docs_dir = _resolve_general_docs_dir(doc_root) if str(doc_root) else Path()
    if str(doc_root):
        env["DOC_RAG_ROOT"] = str(doc_root)
    if docs_dir and not _has_general_docs(doc_root):
        print(
            "[voice-agent] warning: DOC_RAG_ROOT is configured but no general docs were found in "
            f"{docs_dir}"
        )

    # Optional sample defaults so LAUNCH_GAME can work immediately on dev machines.
    sample_flappy = defaults.repo_root.parent / "Voice Flippy Bird" / "Flappy Bird.exe"
    if sample_flappy.exists():
        env.setdefault("VOICE_AGENT_CORNHOLE_EXE", str(sample_flappy))
        env.setdefault("VOICE_AGENT_CORNHOLE_CWD", str(sample_flappy.parent))
    env.setdefault("VOICE_AGENT_NOTEBOOK_EXE", "notepad.exe")
    return env


def _is_tcp_port_in_use(host: str, port: int, timeout_sec: float = 0.25) -> bool:
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
    try:
        with socket.create_connection((probe_host, port), timeout=timeout_sec):
            return True
    except OSError:
        return False


def _repo_service_cleanup_markers(defaults: LauncherDefaults) -> List[str]:
    script_dir = defaults.script_dir
    markers = [
        " -m uvicorn main:app ",
        " -m uvicorn desktop_runtime:app ",
        " -m uvicorn piper_http:app ",
        " -m uvicorn qwen_tts_http:app ",
        " -m uvicorn kokoro_tts_http:app ",
        str(script_dir / "intent_service" / "main.py"),
        str(script_dir / "dialog_service" / "main.py"),
        str(script_dir / "game_launcher" / "main.py"),
        str(script_dir / "telemetry_service" / "main.py"),
    ]
    return [marker.replace("/", "\\").strip().lower() for marker in markers if str(marker).strip()]


def _resolve_general_docs_dir(doc_root: Path) -> Path:
    root = Path(str(doc_root or "")).expanduser()
    nested = root / "docs"
    if nested.exists():
        return nested
    return root


def _has_general_docs(doc_root: Path) -> bool:
    docs_dir = _resolve_general_docs_dir(doc_root)
    if not docs_dir.exists():
        return False
    return any(docs_dir.rglob("*.md")) or any(docs_dir.rglob("*.qmd")) or any(docs_dir.rglob("*.txt"))


def _normalize_process_command_line(value: object) -> str:
    return str(value or "").strip().replace("/", "\\").lower()


def _select_repo_service_root_pids(
    processes: List[Dict[str, object]],
    *,
    repo_root: Path,
    markers: List[str],
    current_pid: int,
) -> List[int]:
    repo_root_text = str(repo_root.resolve()).replace("/", "\\").lower()
    selected: List[int] = []
    parent_by_pid: Dict[int, int] = {}
    for item in processes:
        try:
            pid = int(item.get("ProcessId") or 0)
        except Exception:
            pid = 0
        try:
            parent_by_pid[pid] = int(item.get("ParentProcessId") or 0)
        except Exception:
            parent_by_pid[pid] = 0
        if pid <= 0 or pid == current_pid:
            continue
        command_line = _normalize_process_command_line(item.get("CommandLine"))
        if not command_line or repo_root_text not in command_line:
            continue
        if not any(marker in command_line for marker in markers):
            continue
        selected.append(pid)
    selected_set = set(selected)
    root_pids = [pid for pid in selected_set if parent_by_pid.get(pid, 0) not in selected_set]
    return sorted(root_pids)


def _list_windows_processes() -> List[Dict[str, object]]:
    if os.name != "nt":
        return []
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | "
                "Select-Object ProcessId,ParentProcessId,CommandLine | "
                "ConvertTo-Json -Compress",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    raw = (result.stdout or "").strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except Exception:
        return []
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _cleanup_repo_service_processes(defaults: LauncherDefaults) -> None:
    if os.name != "nt":
        return
    markers = _repo_service_cleanup_markers(defaults)
    processes = _list_windows_processes()
    target_pids = _select_repo_service_root_pids(
        processes,
        repo_root=defaults.repo_root,
        markers=markers,
        current_pid=os.getpid(),
    )
    if not target_pids:
        return
    print(f"[voice-agent] cleaning {len(target_pids)} existing repo service process(es) before launch.")
    for pid in target_pids:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            print(f"[voice-agent] cleaned old service tree rooted at PID {pid}.")
        else:
            stderr = (result.stderr or result.stdout or "").strip()
            print(f"[voice-agent] warning: failed to clean PID {pid}: {stderr}")
    time.sleep(1.0)


def _probe_doc_rag_embedder(defaults: LauncherDefaults, env: Dict[str, str]) -> Tuple[bool, str]:
    dialog_dir = defaults.script_dir / "dialog_service"
    dialog_dir_text = str(dialog_dir)
    if dialog_dir_text not in sys.path:
        sys.path.insert(0, dialog_dir_text)
    try:
        module = importlib.import_module("onnx_embedder")
    except Exception as exc:
        return False, f"failed to import onnx_embedder: {exc}"

    embedder_cls = getattr(module, "OnnxTextEmbedder", None)
    if embedder_cls is None:
        return False, "OnnxTextEmbedder class unavailable"

    try:
        embedder = embedder_cls(
            embedder=(env.get("DOC_RAG_EMBEDDER", "bge") or "bge").strip().lower(),
            repo_id=(env.get("DOC_RAG_EMBEDDING_REPO_ID", "") or "").strip(),
            model_dir=(env.get("DOC_RAG_EMBEDDING_MODEL_DIR", "") or "").strip(),
            model_file=(env.get("DOC_RAG_EMBEDDING_MODEL_FILE", "") or "").strip(),
            tokenizer_file=(env.get("DOC_RAG_EMBEDDING_TOKENIZER_FILE", "") or "").strip(),
            max_length=max(16, int((env.get("DOC_RAG_EMBEDDING_MAX_LENGTH", "256") or "256").strip() or "256")),
            auto_download=((env.get("DOC_RAG_EMBEDDING_AUTO_DOWNLOAD", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}),
            cache_dir=(env.get("DOC_RAG_EMBEDDING_CACHE_DIR", "") or "").strip(),
            query_prefix=(env.get("DOC_RAG_QUERY_PREFIX", "") or "").strip(),
            doc_prefix=(env.get("DOC_RAG_DOC_PREFIX", "") or "").strip(),
        )
    except Exception as exc:
        return False, f"failed to initialize doc_rag embedder: {exc}"
    return bool(getattr(embedder, "ready", False)), str(getattr(embedder, "error", "") or "")


def _run_doc_rag_preflight(defaults: LauncherDefaults, env: Dict[str, str]) -> Tuple[bool, str]:
    if (env.get("DOC_RAG_ENABLE", "1") or "1").strip().lower() not in {"1", "true", "yes", "on"}:
        return True, ""
    doc_root = Path(str(env.get("DOC_RAG_ROOT", "") or "")).expanduser()
    docs_dir = _resolve_general_docs_dir(doc_root) if str(doc_root) else Path()
    if not docs_dir.exists():
        return True, ""
    if not _has_general_docs(doc_root):
        return True, ""
    ready, error = _probe_doc_rag_embedder(defaults, env)
    if ready:
        return True, ""
    return False, error or "doc_rag embedder unavailable"


def _build_handles(commands: CommandSet, dirs: DirectorySet, env: Dict[str, str]) -> List[ProcessHandle]:
    handles: List[ProcessHandle] = [ProcessHandle("voice service", commands.voice, dirs.voice)]
    pipeline_mode = (env.get("VOICE_PIPELINE_MODE", "direct_unified") or "direct_unified").strip().lower()
    use_legacy_dialog_stack = pipeline_mode == "legacy_mqtt"
    if commands.hub is not None:
        handles.insert(0, ProcessHandle("mqtt-broker", commands.hub, dirs.hub))
    if commands.orchestrator is not None:
        handles.append(ProcessHandle("orchestrator", commands.orchestrator, dirs.orchestrator))
    if commands.piper_http is not None:
        handles.append(ProcessHandle("piper-http", commands.piper_http, dirs.piper_http))
    if commands.qwen_http is not None:
        handles.append(ProcessHandle("qwen-http", commands.qwen_http, dirs.qwen_http))
    if commands.kokoro_http is not None:
        handles.append(ProcessHandle("kokoro-http", commands.kokoro_http, dirs.kokoro_http))
    if commands.desktop_runtime is not None:
        handles.append(
            ProcessHandle(
                "desktop-runtime",
                commands.desktop_runtime,
                dirs.desktop_runtime,
                restart_on_exit=True,
                max_restarts=3,
            )
        )
    if commands.intent is not None:
        handles.append(ProcessHandle("intent-service", commands.intent, dirs.intent))
        if not use_legacy_dialog_stack:
            print("[voice-agent] intent-service running in standby for legacy MQTT fallback.")
    if commands.dialog is not None:
        handles.append(ProcessHandle("dialog-service", commands.dialog, dirs.dialog))
        if not use_legacy_dialog_stack:
            print("[voice-agent] dialog-service running in standby for legacy MQTT fallback.")
    if commands.telemetry is not None:
        telemetry_host = env.get("TELEMETRY_HOST", "0.0.0.0").strip() or "0.0.0.0"
        try:
            telemetry_port = int(env.get("TELEMETRY_PORT", "8101"))
        except ValueError:
            telemetry_port = 8101

        if _is_tcp_port_in_use(telemetry_host, telemetry_port):
            print(
                "[voice-agent] telemetry-service skipped: "
                f"{telemetry_host}:{telemetry_port} already in use."
            )
        else:
            handles.append(ProcessHandle("telemetry-service", commands.telemetry, dirs.telemetry))
    if commands.launcher is not None:
        handles.append(ProcessHandle("game-launcher", commands.launcher, dirs.launcher))
    return handles


def _run_supervisor(handles: List[ProcessHandle], env: Dict[str, str], *, no_wait: bool) -> int:
    return run_process_supervisor(handles, env, no_wait=no_wait, log_prefix="voice-agent")


def _resolve_repo_root() -> Path:
    if bool(getattr(sys, "frozen", False)):
        exe_dir = Path(sys.executable).resolve().parent
        if exe_dir.name.lower() == "services" and exe_dir.parent.name.lower() == "runtime":
            return exe_dir.parent.parent
        return exe_dir
    return Path(__file__).resolve().parents[1]


def main(argv: Optional[List[str]] = None) -> int:
    requested_args = list(argv) if argv is not None else sys.argv[1:]
    if any(arg in {"-h", "--help"} for arg in requested_args):
        os.environ.setdefault("VOICE_AGENT_AUTO_BOOTSTRAP_VENV", "0")

    defaults = _build_defaults(_resolve_repo_root())
    if defaults.launcher_default_config_path.exists():
        print(f"[voice-agent] default config: {defaults.launcher_default_config_path}")
    else:
        print(
            "[voice-agent] default config not found, using built-in defaults: "
            f"{defaults.launcher_default_config_path}"
        )
    if defaults.launcher_config_path.exists():
        print(f"[voice-agent] user config: {defaults.launcher_config_path}")
    else:
        print(
            "[voice-agent] user config not found, using defaults + env: "
            f"{defaults.launcher_config_path}"
        )
    parser = _build_parser(defaults)
    args = parser.parse_args(argv)
    commands = _parse_commands(args, windows=os.name == "nt", parser=parser)
    dirs = _resolve_directories(args)
    _validate_paths(parser=parser, args=args, commands=commands, dirs=dirs)
    env = _build_runtime_env(args, defaults)
    if commands.intent is not None:
        print("[voice-agent] intent command:", " ".join(commands.intent))
    else:
        print("[voice-agent] intent command: <disabled>")
    print(
        "[voice-agent] INTENT_USE_LLM_CLASSIFIER="
        + str(env.get("INTENT_USE_LLM_CLASSIFIER", ""))
    )
    print(
        "[voice-agent] INTENT_USE_MOONSHINE_RECOGNIZER="
        + str(env.get("INTENT_USE_MOONSHINE_RECOGNIZER", ""))
    )
    print("[voice-agent] VOICE_PIPELINE_MODE=" + str(env.get("VOICE_PIPELINE_MODE", "")))
    print("[voice-agent] VOICE_CONVERSATION_PROFILE=" + str(env.get("VOICE_CONVERSATION_PROFILE", "")))
    print("[voice-agent] ASR_PYTHON=" + str(defaults.asr_python))
    print("[voice-agent] TTS_PYTHON=" + str(defaults.tts_python))
    print("[voice-agent] DOC_RAG_ENABLE=" + str(env.get("DOC_RAG_ENABLE", "")))
    print("[voice-agent] DOC_RAG_ROOT=" + str(env.get("DOC_RAG_ROOT", "")))
    doc_rag_ok, doc_rag_error = _run_doc_rag_preflight(defaults, env)
    if not doc_rag_ok:
        print("[voice-agent] error: DOC_RAG preflight failed: " + str(doc_rag_error))
        return 2
    if not args.no_cleanup_old_services:
        _cleanup_repo_service_processes(defaults)
    handles = _build_handles(commands, dirs, env)
    return _run_supervisor(handles, env, no_wait=args.no_wait)


if __name__ == "__main__":
    sys.exit(main())
