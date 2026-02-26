#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import paho.mqtt.client as mqtt

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from common.config_utils import load_yaml_file, resolve_optional_path
from common.service_runtime import run_service_loop


@dataclass
class Topics:
    intent: str = "robot/intent"
    state: str = "robot/state"


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 1883
    topics: Topics = field(default_factory=Topics)
    manifest_path: str = str(Path(__file__).resolve().parents[1] / "intent_service" / "manifest.json")


@dataclass
class GameEntry:
    id: str
    name: str
    synonyms: List[str] = field(default_factory=list)
    exec: str = ""
    workdir: Optional[str] = None
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)


@dataclass
class IntentCommand:
    kind: str
    game_name: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class StartSpec:
    command: List[str]
    display_command: str
    cwd: Optional[str]
    env: Dict[str, str]


@dataclass
class ProcessExit:
    game_id: str
    code: Optional[int]
    expected: bool


_UNRESOLVED_WINDOWS_ENV = re.compile(r"%[^%]+%")
_UNRESOLVED_BASH_ENV_1 = re.compile(r"\$\{[^}]+\}")
_UNRESOLVED_BASH_ENV_2 = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")


def _expand(value: str) -> str:
    expanded = os.path.expandvars(str(value)).strip()
    # Treat unresolved env placeholders as "not configured".
    if _UNRESOLVED_WINDOWS_ENV.search(expanded):
        return ""
    if _UNRESOLVED_BASH_ENV_1.search(expanded) or _UNRESOLVED_BASH_ENV_2.search(expanded):
        return ""
    return expanded


def _apply_file_config(cfg: Config, cfg_path: Path) -> Config:
    data = load_yaml_file(cfg_path)
    mqtt_cfg = data.get("mqtt", {})
    topics = data.get("topics", {})
    manifest_path = data.get("manifest_path", cfg.manifest_path)
    cfg.host = str(mqtt_cfg.get("host", cfg.host))
    cfg.port = int(mqtt_cfg.get("port", cfg.port))
    cfg.topics = Topics(
        intent=str(topics.get("intent", cfg.topics.intent)),
        state=str(topics.get("state", cfg.topics.state)),
    )
    resolved = resolve_optional_path(str(manifest_path), base_dir=cfg_path.parent)
    if resolved:
        cfg.manifest_path = resolved
    return cfg


def _apply_env_overrides(cfg: Config) -> Config:
    cfg.host = os.environ.get("MQTT_HOST", cfg.host)
    cfg.port = int(os.environ.get("MQTT_PORT", cfg.port))
    cfg.topics = Topics(
        intent=os.environ.get("INTENT_TOPIC", cfg.topics.intent),
        state=os.environ.get("STATE_TOPIC", cfg.topics.state),
    )
    cfg.manifest_path = resolve_optional_path(
        os.environ.get("GAME_LAUNCHER_MANIFEST_PATH", cfg.manifest_path)
    ) or cfg.manifest_path
    return cfg


def load_config() -> Config:
    cfg = Config()
    cfg_path = Path(os.environ.get("GAME_LAUNCHER_CONFIG", str(Path(__file__).resolve().with_name("config.yaml"))))
    if cfg_path.exists():
        cfg = _apply_file_config(cfg, cfg_path)
    return _apply_env_overrides(cfg)


class GameManifest:
    def __init__(self, path: str) -> None:
        self.path = path
        self.games: Dict[str, GameEntry] = {}
        self.alias_to_id: Dict[str, str] = {}
        self.reload()

    @staticmethod
    def _normalize_alias_key(value: str) -> str:
        # Keep launcher matching tolerant to trailing punctuation from ASR/LLM output,
        # e.g. "cornhole." / "disc golf.".
        key = (value or "").strip().lower()
        if not key:
            return ""
        key = key.strip(" \t\r\n.,!?;:'\"`~()[]{}<>，。！？；：")
        key = re.sub(r"\s+", " ", key)
        return key

    def reload(self) -> None:
        self.games.clear()
        self.alias_to_id.clear()
        manifest_path = Path(self.path)
        if not manifest_path.exists():
            print(f"[launcher] manifest not found: {manifest_path}")
            return
        try:
            with manifest_path.open("r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except Exception as exc:
            print(f"[launcher] failed to read manifest: {exc}")
            return

        for raw in data.get("games", []) or []:
            game_id = str(raw.get("id", "")).strip()
            name = str(raw.get("name", game_id)).strip()
            if not game_id:
                continue
            entry = GameEntry(
                id=game_id,
                name=name or game_id,
                synonyms=[str(s).strip() for s in (raw.get("synonyms") or []) if str(s).strip()],
                exec=str(raw.get("exec", "")).strip(),
                workdir=str(raw.get("workdir")).strip() if raw.get("workdir") is not None else None,
                args=[str(a) for a in (raw.get("args") or [])],
                env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
            )
            self.games[entry.id] = entry
            self._index_aliases(entry)

        print(f"[launcher] loaded {len(self.games)} games from {manifest_path}")

    def _index_aliases(self, entry: GameEntry) -> None:
        for key in [entry.id, entry.name, *entry.synonyms]:
            normalized = self._normalize_alias_key(key)
            if normalized:
                self.alias_to_id[normalized] = entry.id

    def resolve(self, spoken_name: str) -> Optional[GameEntry]:
        key = self._normalize_alias_key(spoken_name)
        if not key:
            return None
        game_id = self.alias_to_id.get(key)
        if not game_id:
            return None
        return self.games.get(game_id)


class IntentRouter:
    def parse(self, payload: Dict[str, Any]) -> IntentCommand:
        intent_type = str(payload.get("type") or "").strip().upper()
        if not intent_type:
            return IntentCommand(kind="ignore")
        if intent_type == "LAUNCH_GAME":
            game_name = str(payload.get("game_name") or "").strip()
            if not game_name:
                return IntentCommand(kind="invalid_launch")
            return IntentCommand(kind="launch", game_name=game_name)
        if intent_type in {"EXIT_GAME", "BACK_HOME", "QUIT"}:
            return IntentCommand(kind="stop", reason=intent_type)
        return IntentCommand(kind="ignore")


class GameProcessManager:
    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._current_game: Optional[GameEntry] = None
        self._stopping = False

    @property
    def current_game_id(self) -> Optional[str]:
        return self._current_game.id if self._current_game else None

    def has_process(self) -> bool:
        return self._proc is not None

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def prepare_start(self, game: GameEntry) -> Optional[StartSpec]:
        exec_path = _expand(game.exec) if game.exec else ""
        if not exec_path:
            return None

        args = [a for a in (_expand(a) for a in game.args) if a]
        command = [exec_path, *args]
        cwd: Optional[str] = None
        if game.workdir:
            cwd_candidate = _expand(game.workdir)
            cwd = cwd_candidate if cwd_candidate else None
        elif Path(exec_path).is_absolute():
            cwd = str(Path(exec_path).parent)

        env = os.environ.copy()
        for key, value in game.env.items():
            env[str(key)] = _expand(value)
        return StartSpec(
            command=command,
            display_command=" ".join(command),
            cwd=cwd,
            env=env,
        )

    def start(self, game: GameEntry, spec: StartSpec) -> Optional[str]:
        try:
            self._proc = subprocess.Popen(spec.command, cwd=spec.cwd, env=spec.env)
            self._current_game = game
            self._stopping = False
            return None
        except Exception as exc:
            self._proc = None
            self._current_game = None
            self._stopping = False
            return str(exc)

    def poll_exit(self) -> Optional[ProcessExit]:
        if self._proc is None:
            return None
        code = self._proc.poll()
        if code is None:
            return None

        game_id = self.current_game_id or "unknown"
        expected = self._stopping
        self._proc = None
        self._current_game = None
        self._stopping = False
        return ProcessExit(game_id=game_id, code=code, expected=expected)

    def stop(self) -> Optional[ProcessExit]:
        if self._proc is None:
            return None

        proc = self._proc
        self._stopping = True
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            proc.terminate()
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                proc.kill()
        return self.poll_exit()


class GameLauncherService:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.manifest = GameManifest(cfg.manifest_path)
        self.router = IntentRouter()
        self.process = GameProcessManager()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"game-launcher-{uuid.uuid4().hex[:8]}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def start(self) -> None:
        print(f"[launcher] connecting to mqtt {self.cfg.host}:{self.cfg.port}")
        self.client.connect(self.cfg.host, self.cfg.port, keepalive=20)
        self.client.loop_start()

    def stop(self) -> None:
        self._stop_current(reason="service_stop")
        try:
            self.client.loop_stop()
        finally:
            self.client.disconnect()

    def poll(self) -> None:
        exit_info = self.process.poll_exit()
        if exit_info is None:
            return
        self._handle_process_exit(exit_info)

    def _publish_state(self, mode: str, detail: str = "", game_id: Optional[str] = None) -> None:
        payload = {
            "mode": mode,
            "game_id": game_id or self.process.current_game_id,
            "detail": detail,
            "source": "game_launcher",
            "ts": time.time(),
        }
        self.client.publish(self.cfg.topics.state, json.dumps(payload))

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        print(f"[launcher] connected rc={reason_code}")
        client.subscribe(self.cfg.topics.intent)
        print(f"[launcher] subscribed {self.cfg.topics.intent}")
        self._publish_state("IDLE")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except json.JSONDecodeError:
            print("[launcher] invalid json, ignored")
            return

        command = self.router.parse(payload)
        if command.kind == "ignore":
            return
        if command.kind == "invalid_launch":
            print("[launcher] LAUNCH_GAME missing game_name")
            return
        if command.kind == "launch":
            self._handle_launch(command.game_name or "")
            return
        if command.kind == "stop":
            self._stop_current(reason=command.reason or "stop")

    def _handle_launch(self, spoken_name: str) -> None:
        # Reload on every launch so manifest edits from User Panel take effect immediately.
        self.manifest.reload()
        game = self.manifest.resolve(spoken_name)
        if game is None:
            detail = f"unknown game: {spoken_name} (manifest={self.manifest.path})"
            print(f"[launcher] {detail}")
            self._publish_state("ERROR", detail)
            return

        if self.process.is_running() and self.process.current_game_id == game.id:
            print(f"[launcher] game already running: {game.id}")
            self._publish_state("RUNNING", "already running", game_id=game.id)
            return

        if self.process.is_running():
            self._stop_current(reason="switch_game")

        self._start_game(game)

    def _start_game(self, game: GameEntry) -> None:
        spec = self.process.prepare_start(game)
        if spec is None:
            detail = f"game '{game.id}' has no exec configured in manifest"
            print(f"[launcher] {detail}")
            self._publish_state("ERROR", detail, game_id=game.id)
            return

        self._publish_state("STARTING", game_id=game.id)
        error = self.process.start(game, spec)
        if error is None:
            print(f"[launcher] started {game.id}: {spec.display_command}")
            self._publish_state("RUNNING", game_id=game.id)
            return

        detail = f"failed to start {game.id}: {error}"
        print(f"[launcher] {detail}")
        self._publish_state("ERROR", detail, game_id=game.id)

    def _stop_current(self, reason: str) -> None:
        if not self.process.has_process():
            self._publish_state("IDLE", "already idle")
            return

        game_id = self.process.current_game_id or "unknown"
        print(f"[launcher] stopping {game_id} (reason={reason})")
        self._publish_state("STOPPING", reason, game_id=game_id)

        try:
            exit_info = self.process.stop()
        except Exception as exc:
            print(f"[launcher] stop error: {exc}")
            return

        if exit_info is not None:
            self._handle_process_exit(exit_info)

    def _handle_process_exit(self, exit_info: ProcessExit) -> None:
        if exit_info.expected:
            print(f"[launcher] game stopped: {exit_info.game_id} (code={exit_info.code})")
            self._publish_state("IDLE", f"stopped {exit_info.game_id}")
            return

        detail = f"game exited unexpectedly: {exit_info.game_id} (code={exit_info.code})"
        print(f"[launcher] {detail}")
        self._publish_state("ERROR", detail)


def main() -> int:
    cfg = load_config()
    svc = GameLauncherService(cfg)
    return run_service_loop(
        service_name="launcher",
        start=svc.start,
        stop=svc.stop,
        poll=svc.poll,
        interval_sec=0.3,
    )


if __name__ == "__main__":
    sys.exit(main())
