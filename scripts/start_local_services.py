#!/usr/bin/env python3
"""Utility to launch the local messaging hub and voice model together."""

from __future__ import annotations

import argparse
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional


class ProcessHandle:
    def __init__(self, name: str, command: List[str], cwd: Optional[Path]) -> None:
        self.name = name
        self.command = command
        self.cwd = cwd
        self.process: Optional[subprocess.Popen] = None

    def start(self, env: Dict[str, str]) -> None:
        display_cmd = " ".join(shlex.quote(part) for part in self.command)
        work_dir = str(self.cwd) if self.cwd is not None else os.getcwd()
        print(f"[voice-agent] Starting {self.name}: {display_cmd} (cwd={work_dir})")
        self.process = subprocess.Popen(self.command, cwd=self.cwd, env=env)

    def terminate(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        print(f"[voice-agent] Stopping {self.name} (PID {self.process.pid})")
        self.process.terminate()

    def kill(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        print(f"[voice-agent] Killing {self.name} (PID {self.process.pid})")
        self.process.kill()

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        if self.process is None:
            return None
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def poll(self) -> Optional[int]:
        if self.process is None:
            return None
        return self.process.poll()


class GracefulTerminator:
    def __init__(self, handles: Iterable[ProcessHandle]) -> None:
        self.handles = list(handles)
        self._lock = threading.Lock()
        self._stopping = False

    def __call__(self, signum, frame) -> None:  # type: ignore[override]
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
        print(f"\n[voice-agent] Received signal {signum}, shutting down...")
        for handle in self.handles:
            handle.terminate()
        # Give the processes a few seconds to exit cleanly before forcing.
        deadline = time.monotonic() + 5.0
        for handle in self.handles:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            handle.wait(timeout=remaining)
        for handle in self.handles:
            if handle.poll() is None:
                handle.kill()


def parse_command(value: str, *, windows: bool) -> List[str]:
    if not value:
        raise ValueError("Command string cannot be empty")
    return shlex.split(value, posix=not windows)


def apply_env_file(path: Path, env: Dict[str, str]) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Environment file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                raise ValueError(f"Invalid line in env file {path}: {stripped}")
            key, value = stripped.split("=", 1)
            env[key.strip()] = value.strip()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Launch the messaging hub and local voice model in one command.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--hub-cmd",
        default=os.environ.get("VOICE_AGENT_HUB_CMD"),
        help=(
            "Command used to start the messaging hub. Set VOICE_AGENT_HUB_CMD "
            "or pass --hub-cmd explicitly."
        ),
    )
    parser.add_argument(
        "--hub-dir",
        default=os.environ.get("VOICE_AGENT_HUB_CWD", str(Path.cwd())),
        help="Working directory for the messaging hub command.",
    )
    parser.add_argument(
        "--voice-cmd",
        default=os.environ.get(
            "VOICE_AGENT_VOICE_CMD",
            "uvicorn main:app --host 0.0.0.0 --port 8000",
        ),
        help="Command used to start the Python voice service.",
    )
    parser.add_argument(
        "--voice-dir",
        default=os.environ.get(
            "VOICE_AGENT_VOICE_CWD", str(Path(__file__).resolve().parents[1] / "python_voice_service")
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
        "--env-file",
        type=Path,
        help="Optional .env style file whose variables are exported before launching the services.",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Do not block waiting for the processes to exit.",
    )

    args = parser.parse_args(argv)

    if not args.hub_cmd:
        parser.error("Missing --hub-cmd or VOICE_AGENT_HUB_CMD environment variable")

    windows = os.name == "nt"
    try:
        hub_command = parse_command(args.hub_cmd, windows=windows)
        voice_command = parse_command(args.voice_cmd, windows=windows)
        orchestrator_command = (
            parse_command(args.orchestrator_cmd, windows=windows)
            if args.orchestrator_cmd
            else None
        )
    except ValueError as exc:
        parser.error(str(exc))

    hub_dir = Path(args.hub_dir).resolve()
    voice_dir = Path(args.voice_dir).resolve()
    orchestrator_dir = Path(args.orchestrator_dir).resolve()

    if not hub_dir.exists():
        parser.error(f"Hub directory does not exist: {hub_dir}")
    if not voice_dir.exists():
        parser.error(f"Voice service directory does not exist: {voice_dir}")
    if orchestrator_command is not None and not orchestrator_dir.exists():
        parser.error(f"Orchestrator directory does not exist: {orchestrator_dir}")

    env = os.environ.copy()
    if args.env_file is not None:
        apply_env_file(args.env_file, env)

    hub_handle = ProcessHandle("messaging hub", hub_command, hub_dir)
    voice_handle = ProcessHandle("voice service", voice_command, voice_dir)
    handles = [hub_handle, voice_handle]
    if orchestrator_command is not None:
        handles.append(ProcessHandle("orchestrator", orchestrator_command, orchestrator_dir))

    terminator = GracefulTerminator(handles)
    signal.signal(signal.SIGINT, terminator)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, terminator)

    for handle in handles:
        handle.start(env)

    if args.no_wait:
        print("[voice-agent] Services launched in background mode.")
        return 0

    try:
        while True:
            for handle in handles:
                code = handle.poll()
                if code is not None:
                    print(f"[voice-agent] {handle.name} exited with code {code}.")
                    raise SystemExit(code)
            time.sleep(0.5)
    except KeyboardInterrupt:
        terminator(signal.SIGINT, None)
        return 130
    except SystemExit as exc:
        terminator(signal.SIGTERM if hasattr(signal, "SIGTERM") else signal.SIGINT, None)
        return int(exc.code)


if __name__ == "__main__":
    sys.exit(main())
