from __future__ import annotations

import os
import shlex
import signal
import subprocess
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

    def start(self, env: Dict[str, str], *, log_prefix: str = "voice-agent") -> None:
        display_cmd = " ".join(shlex.quote(part) for part in self.command)
        work_dir = str(self.cwd) if self.cwd is not None else os.getcwd()
        print(f"[{log_prefix}] Starting {self.name}: {display_cmd} (cwd={work_dir})")
        self.process = subprocess.Popen(self.command, cwd=self.cwd, env=env)

    def terminate(self, *, log_prefix: str = "voice-agent") -> None:
        if self.process is None or self.process.poll() is not None:
            return
        print(f"[{log_prefix}] Stopping {self.name} (PID {self.process.pid})")
        self.process.terminate()

    def kill(self, *, log_prefix: str = "voice-agent") -> None:
        if self.process is None or self.process.poll() is not None:
            return
        print(f"[{log_prefix}] Killing {self.name} (PID {self.process.pid})")
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
    def __init__(self, handles: Iterable[ProcessHandle], *, log_prefix: str = "voice-agent") -> None:
        self.handles = list(handles)
        self.log_prefix = log_prefix
        self._lock = threading.Lock()
        self._stopping = False

    def __call__(self, signum, frame) -> None:  # type: ignore[override]
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
        print(f"\n[{self.log_prefix}] Received signal {signum}, shutting down...")
        for handle in self.handles:
            handle.terminate(log_prefix=self.log_prefix)
        # Give the processes a few seconds to exit cleanly before forcing.
        deadline = time.monotonic() + 5.0
        for handle in self.handles:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            handle.wait(timeout=remaining)
        for handle in self.handles:
            if handle.poll() is None:
                handle.kill(log_prefix=self.log_prefix)


def run_process_supervisor(
    handles: List[ProcessHandle],
    env: Dict[str, str],
    *,
    no_wait: bool = False,
    log_prefix: str = "voice-agent",
) -> int:
    terminator = GracefulTerminator(handles, log_prefix=log_prefix)
    signal.signal(signal.SIGINT, terminator)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, terminator)

    for handle in handles:
        handle.start(env, log_prefix=log_prefix)

    if no_wait:
        print(f"[{log_prefix}] Services launched in background mode.")
        return 0

    try:
        while True:
            for handle in handles:
                code = handle.poll()
                if code is not None:
                    print(f"[{log_prefix}] {handle.name} exited with code {code}.")
                    raise SystemExit(code)
            time.sleep(0.5)
    except KeyboardInterrupt:
        terminator(signal.SIGINT, None)
        return 130
    except SystemExit as exc:
        terminator(signal.SIGTERM if hasattr(signal, "SIGTERM") else signal.SIGINT, None)
        return int(exc.code)
