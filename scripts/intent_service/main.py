#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
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
    voice_text: str = "robot/voice/text"
    intent: str = "robot/intent"
    dialog_query: str = "robot/dialog/query"


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 1883
    topics: Topics = field(default_factory=Topics)
    require_wake_word: bool = False
    wake_words: List[str] = None  # type: ignore[assignment]
    exit_keywords: List[str] = None  # type: ignore[assignment]
    launch_triggers: List[str] = None  # type: ignore[assignment]
    source_label: str = "intent_service"
    manifest_path: Optional[str] = None
    fuzzy_threshold: int = 80
    dedupe_window_sec: float = 1.2

    def __post_init__(self) -> None:
        if self.wake_words is None:
            self.wake_words = [
                "hi rachel",
                "hey rachel",
                "hello rachel",
            ]
        if self.exit_keywords is None:
            self.exit_keywords = [
                "back home",
                "go home",
                "return home",
                "go back",
                "quit",
                "exit",
                "stop",
                "cancel",
                "close",
                "close game",
            ]
        if self.launch_triggers is None:
            self.launch_triggers = [
                "open ",
                "start ",
                "launch ",
                "play ",
                "begin ",
                "load ",
            ]


@dataclass
class RouteDecision:
    topic: Optional[str]
    payload: Optional[Dict[str, Any]]
    log_line: Optional[str]


def _apply_file_config(cfg: Config, cfg_path: Path) -> Config:
    data = load_yaml_file(cfg_path)
    mqtt_cfg = data.get("mqtt", {})
    topics = data.get("topics", {})
    rules = data.get("rules", {})

    cfg.host = os.environ.get("MQTT_HOST", mqtt_cfg.get("host", cfg.host))
    cfg.port = int(os.environ.get("MQTT_PORT", mqtt_cfg.get("port", cfg.port)))
    cfg.topics = Topics(
        voice_text=os.environ.get("VOICE_TEXT_TOPIC", topics.get("voice_text", cfg.topics.voice_text)),
        intent=os.environ.get("INTENT_TOPIC", topics.get("intent", cfg.topics.intent)),
        dialog_query=os.environ.get("DIALOG_QUERY_TOPIC", topics.get("dialog_query", cfg.topics.dialog_query)),
    )
    cfg.require_wake_word = bool(rules.get("require_wake_word", cfg.require_wake_word))
    cfg.wake_words = rules.get("wake_words", cfg.wake_words)
    cfg.exit_keywords = rules.get("exit_keywords", cfg.exit_keywords)
    cfg.launch_triggers = rules.get("launch_triggers", cfg.launch_triggers)
    cfg.source_label = data.get("source_label", cfg.source_label)

    env_manifest = os.environ.get("INTENT_MANIFEST_PATH")
    raw_manifest = env_manifest if env_manifest is not None else data.get("manifest_path")
    cfg.manifest_path = resolve_optional_path(raw_manifest, base_dir=cfg_path.parent)

    rules_threshold = rules.get("fuzzy_threshold")
    if rules_threshold is not None:
        cfg.fuzzy_threshold = int(rules_threshold)
    rules_dedupe = rules.get("dedupe_window_sec")
    if rules_dedupe is not None:
        cfg.dedupe_window_sec = float(rules_dedupe)
    cfg.launch_triggers = _apply_list_env_override(
        cfg.launch_triggers,
        os.environ.get("INTENT_LAUNCH_TRIGGERS"),
    )
    cfg.exit_keywords = _apply_list_env_override(
        cfg.exit_keywords,
        os.environ.get("INTENT_EXIT_KEYWORDS"),
    )
    return cfg


def _apply_env_only_config(cfg: Config) -> Config:
    cfg.host = os.environ.get("MQTT_HOST", cfg.host)
    cfg.port = int(os.environ.get("MQTT_PORT", cfg.port))
    cfg.topics = Topics(
        voice_text=os.environ.get("VOICE_TEXT_TOPIC", cfg.topics.voice_text),
        intent=os.environ.get("INTENT_TOPIC", cfg.topics.intent),
        dialog_query=os.environ.get("DIALOG_QUERY_TOPIC", cfg.topics.dialog_query),
    )
    env_manifest = os.environ.get("INTENT_MANIFEST_PATH")
    if env_manifest:
        cfg.manifest_path = resolve_optional_path(env_manifest)
    cfg.fuzzy_threshold = int(os.environ.get("FUZZY_THRESHOLD", cfg.fuzzy_threshold))
    cfg.dedupe_window_sec = float(os.environ.get("DEDUPE_WINDOW_SEC", cfg.dedupe_window_sec))
    cfg.launch_triggers = _apply_list_env_override(
        cfg.launch_triggers,
        os.environ.get("INTENT_LAUNCH_TRIGGERS"),
    )
    cfg.exit_keywords = _apply_list_env_override(
        cfg.exit_keywords,
        os.environ.get("INTENT_EXIT_KEYWORDS"),
    )
    return cfg


def load_config() -> Config:
    default_cfg = Path(__file__).resolve().with_name("config.yaml")
    cfg_path = Path(os.environ.get("INTENT_CONFIG", str(default_cfg))).expanduser()
    cfg = Config()
    if cfg_path.exists():
        return _apply_file_config(cfg, cfg_path)
    return _apply_env_only_config(cfg)


def normalize(text: str) -> str:
    return text.strip()


def has_wake_word(text: str, wake_words: List[str]) -> bool:
    lower = text.lower()
    return any(w.lower() in lower for w in wake_words)


def contains_any(text: str, keywords: List[str]) -> bool:
    lower = text.lower()
    return any(keyword.lower() in lower for keyword in keywords)


def _apply_list_env_override(current: Optional[List[str]], raw: Optional[str]) -> List[str]:
    base = [str(item).strip() for item in (current or []) if str(item).strip()]
    text = (raw or "").strip()
    if not text:
        return base

    parsed: List[str] = []
    try:
        node = json.loads(text)
        if isinstance(node, list):
            parsed = [str(item).strip() for item in node if str(item).strip()]
    except Exception:
        merged = text.replace("\r\n", "\n").replace(";", ",").replace("\n", ",")
        parsed = [part.strip() for part in merged.split(",") if part.strip()]

    return parsed if parsed else base


def extract_game_name(text: str, triggers: List[str]) -> Optional[str]:
    lower = text.lower()
    for trigger in triggers:
        raw_trigger = str(trigger).strip()
        if not raw_trigger:
            continue
        pattern = re.compile(rf"(?<!\w){re.escape(raw_trigger.lower())}(?!\w)")
        match = pattern.search(lower)
        if not match:
            continue
        tail = text[match.end() :].strip()
        tail = tail.lstrip(":,.- \t")
        tail = re.sub(r"^(the|a|an)\s+", "", tail, flags=re.IGNORECASE)
        tail = re.sub(r"[.!?\u3002\uff01\uff1f\s]+$", "", tail)
        tail = tail.lstrip("\u7684 ")
        if tail:
            return tail
    return None


def new_corr_id() -> str:
    return uuid.uuid4().hex


class ManifestAliasResolver:
    def __init__(self, manifest_path: Optional[str]) -> None:
        self.manifest_path = manifest_path
        self.alias_to_name: Dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        path = self.manifest_path
        if not path:
            return
        try:
            if not os.path.exists(path):
                print(f"[intent] manifest not found: {path}")
                return
            with open(path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
            for game in data.get("games", []) or []:
                name = str(game.get("name") or game.get("id") or "").strip()
                if not name:
                    continue
                self.alias_to_name[name.lower()] = name
                for alias in game.get("synonyms", []) or []:
                    text = str(alias).strip()
                    if text:
                        self.alias_to_name[text.lower()] = name
            print(f"[intent] loaded {len(self.alias_to_name)} aliases from manifest")
        except Exception as exc:
            print(f"[intent] failed to load manifest aliases: {exc}")

    def canonical_name(self, candidate: str) -> Optional[str]:
        return self.alias_to_name.get(candidate.strip().lower())


class IntentRouterEngine:
    def __init__(self, cfg: Config, resolver: ManifestAliasResolver) -> None:
        self.cfg = cfg
        self.resolver = resolver

    def route(self, text: str, corr_id: str) -> RouteDecision:
        if self.cfg.require_wake_word and not has_wake_word(text, self.cfg.wake_words):
            return RouteDecision(topic=None, payload=None, log_line=f"[intent] no wake word: {text}")

        if contains_any(text, self.cfg.exit_keywords):
            payload = {
                "type": "BACK_HOME",
                "source": self.cfg.source_label,
                "raw": {"text": text},
                "corr_id": corr_id,
            }
            return RouteDecision(
                topic=self.cfg.topics.intent,
                payload=payload,
                log_line=f"[intent] -> BACK_HOME {self.cfg.topics.intent}",
            )

        game = extract_game_name(text, self.cfg.launch_triggers)
        candidate = (game or text).strip()
        if self.resolver.alias_to_name:
            canonical = self.resolver.canonical_name(candidate)
            if canonical:
                game = canonical

        if game:
            payload = {
                "type": "LAUNCH_GAME",
                "game_name": game,
                "source": self.cfg.source_label,
                "raw": {"text": text},
                "corr_id": corr_id,
            }
            return RouteDecision(
                topic=self.cfg.topics.intent,
                payload=payload,
                log_line=f"[intent] -> LAUNCH_GAME '{game}' {self.cfg.topics.intent}",
            )

        payload = {
            "type": "QUERY",
            "text": text,
            "source": self.cfg.source_label,
            "corr_id": corr_id,
        }
        return RouteDecision(
            topic=self.cfg.topics.dialog_query,
            payload=payload,
            log_line=f"[intent] -> QUERY {self.cfg.topics.dialog_query}",
        )


class IntentService:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"intent-svc-{uuid.uuid4().hex[:8]}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self._stopping = False
        self._resolver = ManifestAliasResolver(cfg.manifest_path)
        self._router = IntentRouterEngine(cfg, self._resolver)

    def start(self) -> None:
        print(f"[intent] connecting to mqtt {self.cfg.host}:{self.cfg.port}")
        self.client.connect(self.cfg.host, self.cfg.port, keepalive=20)
        self.client.loop_start()

    def stop(self) -> None:
        self._stopping = True
        try:
            self.client.loop_stop()
        finally:
            self.client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        print(f"[intent] connected rc={reason_code}")
        client.subscribe(self.cfg.topics.voice_text)
        print(f"[intent] subscribed {self.cfg.topics.voice_text}")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except json.JSONDecodeError:
            print("[intent] invalid json, ignored")
            return

        topic = getattr(msg, "topic", "") or ""
        if topic != self.cfg.topics.voice_text:
            return

        text = normalize(str(payload.get("text") or ""))
        if not text:
            return

        corr_id = str(payload.get("corr_id") or new_corr_id())
        decision = self._router.route(text, corr_id)
        if decision.log_line:
            print(decision.log_line)
        if decision.topic and decision.payload is not None:
            self.client.publish(decision.topic, json.dumps(decision.payload))


def main() -> int:
    cfg = load_config()
    svc = IntentService(cfg)
    return run_service_loop(
        service_name="intent",
        start=svc.start,
        stop=svc.stop,
        interval_sec=0.5,
    )


if __name__ == "__main__":
    sys.exit(main())
