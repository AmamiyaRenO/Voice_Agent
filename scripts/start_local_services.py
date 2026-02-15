#!/usr/bin/env python3
"""Utility to launch the local Voice Agent stack (MQTT + ASR/TTS helpers)."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from common.env_utils import apply_env_file
from common.process_supervisor import ProcessHandle, run_process_supervisor


@dataclass
class LauncherDefaults:
    repo_root: Path
    script_dir: Path
    service_dir: Path
    default_hub_cmd: Optional[str]
    asr_python: str
    tts_python: str
    default_voice_cmd: str
    default_piper_http_cmd: str
    default_qwen_http_cmd: str
    default_launcher_cmd: str


@dataclass
class CommandSet:
    hub: Optional[List[str]]
    voice: List[str]
    orchestrator: Optional[List[str]]
    piper_http: Optional[List[str]]
    qwen_http: Optional[List[str]]
    intent: Optional[List[str]]
    dialog: Optional[List[str]]
    launcher: Optional[List[str]]


@dataclass
class DirectorySet:
    hub: Path
    voice: Path
    orchestrator: Path
    piper_http: Path
    qwen_http: Path
    intent: Path
    dialog: Path
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
        if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
            token = token[1:-1]
        normalized.append(token)
    return normalized


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


def _resolve_default_hub_command(repo_root: Path) -> Optional[str]:
    """Resolve a default local MQTT broker command without Robot_opr dependency."""
    broker_cmd = os.environ.get("VOICE_AGENT_BROKER_CMD", "").strip()
    if broker_cmd:
        return broker_cmd

    mosquitto_exe = _detect_mosquitto_executable()
    if not mosquitto_exe:
        return None

    conf = repo_root / "scripts" / "mqtt" / "mosquitto.conf"
    if conf.exists():
        return f'"{mosquitto_exe}" -c "{conf}" -v'
    return f'"{mosquitto_exe}" -v'


def _resolve_python_executable(
    *,
    preferred_paths: List[Path],
    env_var: str,
    fallback: str,
) -> str:
    env_override = os.environ.get(env_var, "").strip()
    if env_override:
        candidate = Path(os.path.expandvars(env_override)).expanduser()
        if candidate.exists():
            return str(candidate)

    for path in preferred_paths:
        if path.exists():
            return str(path)

    return fallback


def _build_defaults(repo_root: Path) -> LauncherDefaults:
    script_dir = Path(__file__).resolve().parents[0]
    service_dir = repo_root / "python_voice_service"
    asr_python = _resolve_python_executable(
        preferred_paths=[
            service_dir / ".venv_asr" / "Scripts" / "python.exe",
            service_dir / ".venv" / "Scripts" / "python.exe",
        ],
        env_var="VOICE_AGENT_ASR_PYTHON",
        fallback=sys.executable,
    )
    tts_python = _resolve_python_executable(
        preferred_paths=[
            service_dir / ".venv_tts" / "Scripts" / "python.exe",
            service_dir / ".venv" / "Scripts" / "python.exe",
        ],
        env_var="VOICE_AGENT_TTS_PYTHON",
        fallback=asr_python,
    )
    return LauncherDefaults(
        repo_root=repo_root,
        script_dir=script_dir,
        service_dir=service_dir,
        default_hub_cmd=_resolve_default_hub_command(repo_root),
        asr_python=asr_python,
        tts_python=tts_python,
        default_voice_cmd=f"{asr_python} -m uvicorn main:app --host 0.0.0.0 --port 8000",
        default_piper_http_cmd=f"{tts_python} -m uvicorn piper_http:app --host 0.0.0.0 --port 5005",
        default_qwen_http_cmd=f"{tts_python} -m uvicorn qwen_tts_http:app --host 0.0.0.0 --port 5006",
        default_launcher_cmd=asr_python + " " + str(script_dir / "game_launcher" / "main.py"),
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
        default=os.environ.get(
            "VOICE_AGENT_VOICE_CMD",
            defaults.default_voice_cmd,
        ),
        help="Command used to start the Python voice service.",
    )
    parser.add_argument(
        "--voice-dir",
        default=os.environ.get(
            "VOICE_AGENT_VOICE_CWD", str(defaults.service_dir)
        ),
        help="Working directory for the Python voice service command.",
    )
    parser.add_argument(
        "--orchestrator-cmd",
        default=os.environ.get("VOICE_AGENT_ORCH_CMD"),
        help=(
            "Optional command used to start the orchestrator that manages Mosquitto. "
            "Set VOICE_AGENT_ORCH_CMD or pass --orchestrator-cmd to enable it."
        ),
    )
    parser.add_argument(
        "--orchestrator-dir",
        default=os.environ.get("VOICE_AGENT_ORCH_CWD", str(Path.cwd())),
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
        default=os.environ.get(
            "VOICE_AGENT_PIPER_HTTP_CWD", str(defaults.service_dir)
        ),
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
        default=os.environ.get(
            "VOICE_AGENT_QWEN_HTTP_CWD", str(defaults.service_dir)
        ),
        help="Working directory for the Qwen HTTP wrapper service.",
    )
    parser.add_argument(
        "--intent-cmd",
        default=os.environ.get(
            "VOICE_AGENT_INTENT_CMD",
            defaults.asr_python + " " + str(defaults.script_dir / "intent_service" / "main.py"),
        ),
        help=(
            "Optional command used to start the intent recognition service. "
            "Set VOICE_AGENT_INTENT_CMD or pass --intent-cmd to enable it."
        ),
    )
    parser.add_argument(
        "--intent-dir",
        default=os.environ.get(
            "VOICE_AGENT_INTENT_CWD", str(defaults.script_dir / "intent_service")
        ),
        help="Working directory for the intent recognition service.",
    )
    parser.add_argument(
        "--dialog-cmd",
        default=os.environ.get(
            "VOICE_AGENT_DIALOG_CMD",
            defaults.asr_python + " " + str(defaults.script_dir / "dialog_service" / "main.py"),
        ),
        help=(
            "Optional command used to start the dialog (LLM+TTS) service. "
            "Set VOICE_AGENT_DIALOG_CMD or pass --dialog-cmd to enable it."
        ),
    )
    parser.add_argument(
        "--dialog-dir",
        default=os.environ.get(
            "VOICE_AGENT_DIALOG_CWD", str(defaults.script_dir / "dialog_service")
        ),
        help="Working directory for the dialog (LLM+TTS) service.",
    )
    parser.add_argument(
        "--launcher-cmd",
        default=os.environ.get("VOICE_AGENT_LAUNCHER_CMD", defaults.default_launcher_cmd),
        help=(
            "Optional command used to start the local game launcher service that consumes "
            "robot/intent and opens/closes games."
        ),
    )
    parser.add_argument(
        "--launcher-dir",
        default=os.environ.get(
            "VOICE_AGENT_LAUNCHER_CWD", str(defaults.script_dir / "game_launcher")
        ),
        help="Working directory for the local game launcher service.",
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
        intent=Path(args.intent_dir).resolve(),
        dialog=Path(args.dialog_dir).resolve(),
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
    if commands.intent is not None and not dirs.intent.exists():
        parser.error(f"Intent service directory does not exist: {dirs.intent}")
    if commands.dialog is not None and not dirs.dialog.exists():
        parser.error(f"Dialog service directory does not exist: {dirs.dialog}")
    if commands.launcher is not None and not dirs.launcher.exists():
        parser.error(f"Launcher service directory does not exist: {dirs.launcher}")


def _build_runtime_env(args: argparse.Namespace, defaults: LauncherDefaults) -> Dict[str, str]:
    env = os.environ.copy()
    if args.env_file is not None:
        apply_env_file(args.env_file, env)

    # Force voice service /tts to proxy to local Piper HTTP unless explicitly changed here.
    env["PIPER_HTTP_URL"] = "http://127.0.0.1:5005"
    # Avoid accidental redirection to remote Piper server.
    if "PIPER_SERVER_URL" in env:
        env.pop("PIPER_SERVER_URL", None)

    # IMPORTANT (AEC): Ensure dialog_service does NOT play audio locally via winsound.
    # TTS must be played through Unity so the AudioListener render reference is available for echo cancellation.
    env["DIALOG_SPEAK_AUDIO"] = "0"
    # Latency-first defaults (can still be overridden by user env/.env).
    env.setdefault("OLLAMA_MAX_TOKENS", "180")
    env.setdefault("OLLAMA_KEEP_ALIVE", "30m")
    env.setdefault("OLLAMA_MODEL", "gemma3:4b")
    # ASR defaults for NucBox M6 (no NVIDIA GPU): CPU-only faster-whisper tuning.
    env.setdefault("WHISPER_DEVICE", "cpu")
    env.setdefault("WHISPER_COMPUTE_TYPE", "int8")
    env.setdefault("WHISPER_MODEL_PATH", "Systran/faster-whisper-large-v3-turbo")
    env.setdefault("WHISPER_CPU_THREADS", "6")
    env.setdefault("WHISPER_VAD_SILENCE_MS", "220")
    env.setdefault("WHISPER_VAD_MIN_SPEECH_MS", "120")
    env.setdefault("WHISPER_STREAM_OVERLAP_SECONDS", "0.4")
    env.setdefault("WHISPER_STREAM_CONTEXT_CHARS", "120")
    env.setdefault("WHISPER_LOW_CONFIDENCE_THRESHOLD", "-0.8")
    env.setdefault("WHISPER_RETRY_BEAM_BONUS", "1")
    env.setdefault("WHISPER_RETRY_MAX_BEAM", "6")
    env.setdefault("WHISPER_RETRY_TEMPERATURES", "0.0,0.2")
    env.setdefault("DIALOG_REPLY_COMPRESS", "1")
    env.setdefault("DIALOG_MAX_REPLY_SENTENCES", "3")
    env.setdefault("DIALOG_MAX_REPLY_CHARS", "0")
    env.setdefault("DIALOG_MAX_REPLY_WORDS", "0")
    env.setdefault("QWEN_TTS_SPEED_PROFILE", "fast")
    env.setdefault("QWEN_TTS_SPEAKER", "Ryan")
    env.setdefault("QWEN_TTS_INSTRUCT", "")
    env.setdefault("QWEN_TTS_DO_SAMPLE", "0")
    env.setdefault("QWEN_TTS_MAX_TEXT_CHARS", "90")
    env.setdefault("QWEN_TTS_FAST_SHORT_MAX_NEW_TOKENS", "240")
    env.setdefault("QWEN_TTS_WARMUP_TEXT", "Hello. I am ready.")
    env.setdefault("MQTT_HOST", "127.0.0.1")
    env.setdefault("MQTT_PORT", "1883")
    env.setdefault(
        "INTENT_MANIFEST_PATH",
        str(defaults.script_dir / "intent_service" / "manifest.json"),
    )
    env.setdefault(
        "GAME_LAUNCHER_MANIFEST_PATH",
        str(defaults.script_dir / "intent_service" / "manifest.json"),
    )

    # Optional sample defaults so LAUNCH_GAME can work immediately on dev machines.
    sample_flappy = defaults.repo_root.parent / "Voice Flippy Bird" / "Flappy Bird.exe"
    if sample_flappy.exists():
        env.setdefault("VOICE_AGENT_CORNHOLE_EXE", str(sample_flappy))
        env.setdefault("VOICE_AGENT_CORNHOLE_CWD", str(sample_flappy.parent))
    env.setdefault("VOICE_AGENT_NOTEBOOK_EXE", "notepad.exe")
    return env


def _build_handles(commands: CommandSet, dirs: DirectorySet) -> List[ProcessHandle]:
    handles: List[ProcessHandle] = [ProcessHandle("voice service", commands.voice, dirs.voice)]
    if commands.hub is not None:
        handles.insert(0, ProcessHandle("mqtt-broker", commands.hub, dirs.hub))
    if commands.orchestrator is not None:
        handles.append(ProcessHandle("orchestrator", commands.orchestrator, dirs.orchestrator))
    if commands.piper_http is not None:
        handles.append(ProcessHandle("piper-http", commands.piper_http, dirs.piper_http))
    if commands.qwen_http is not None:
        handles.append(ProcessHandle("qwen-http", commands.qwen_http, dirs.qwen_http))
    if commands.intent is not None:
        handles.append(ProcessHandle("intent-service", commands.intent, dirs.intent))
    if commands.dialog is not None:
        handles.append(ProcessHandle("dialog-service", commands.dialog, dirs.dialog))
    if commands.launcher is not None:
        handles.append(ProcessHandle("game-launcher", commands.launcher, dirs.launcher))
    return handles


def _run_supervisor(handles: List[ProcessHandle], env: Dict[str, str], *, no_wait: bool) -> int:
    return run_process_supervisor(handles, env, no_wait=no_wait, log_prefix="voice-agent")


def main(argv: Optional[List[str]] = None) -> int:
    defaults = _build_defaults(Path(__file__).resolve().parents[1])
    parser = _build_parser(defaults)
    args = parser.parse_args(argv)
    commands = _parse_commands(args, windows=os.name == "nt", parser=parser)
    dirs = _resolve_directories(args)
    _validate_paths(parser=parser, args=args, commands=commands, dirs=dirs)
    env = _build_runtime_env(args, defaults)
    handles = _build_handles(commands, dirs)
    return _run_supervisor(handles, env, no_wait=args.no_wait)


if __name__ == "__main__":
    sys.exit(main())
