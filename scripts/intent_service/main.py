#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import paho.mqtt.client as mqtt
import yaml
from rapidfuzz import fuzz


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


def load_config() -> Config:
    cfg_path = os.environ.get("INTENT_CONFIG", "config.yaml")
    cfg = Config()
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
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
        cfg.manifest_path = os.environ.get("INTENT_MANIFEST_PATH", data.get("manifest_path"))
        rules_threshold = rules.get("fuzzy_threshold")
        if rules_threshold is not None:
            cfg.fuzzy_threshold = int(rules_threshold)
        rules_dedupe = rules.get("dedupe_window_sec")
        if rules_dedupe is not None:
            cfg.dedupe_window_sec = float(rules_dedupe)
    else:
        # env-only mode
        cfg.host = os.environ.get("MQTT_HOST", cfg.host)
        cfg.port = int(os.environ.get("MQTT_PORT", cfg.port))
        cfg.topics = Topics(
            voice_text=os.environ.get("VOICE_TEXT_TOPIC", cfg.topics.voice_text),
            intent=os.environ.get("INTENT_TOPIC", cfg.topics.intent),
            dialog_query=os.environ.get("DIALOG_QUERY_TOPIC", cfg.topics.dialog_query),
        )
        cfg.manifest_path = os.environ.get("INTENT_MANIFEST_PATH")
        cfg.fuzzy_threshold = int(os.environ.get("FUZZY_THRESHOLD", cfg.fuzzy_threshold))
        cfg.dedupe_window_sec = float(os.environ.get("DEDUPE_WINDOW_SEC", cfg.dedupe_window_sec))
    return cfg


def normalize(s: str) -> str:
    return s.strip()


def has_wake_word(text: str, wake_words: List[str]) -> bool:
    t = text.lower()
    for w in wake_words:
        if w.lower() in t:
            return True
    return False


def contains_any(text: str, keywords: List[str]) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in keywords)


def extract_game_name(text: str, triggers: List[str]) -> Optional[str]:
    for trig in triggers:
        idx = text.lower().find(trig.lower())
        if idx >= 0:
            tail = text[idx + len(trig) :].strip()
            # strip trailing punctuation
            tail = re.sub(r"[。.!！?？\s]+$", "", tail)
            # strip leading filler
            tail = tail.lstrip("的 ")
            if tail:
                return tail
    return None


def new_corr_id() -> str:
    return uuid.uuid4().hex


class IntentService:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"intent-svc-{uuid.uuid4().hex[:8]}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self._stopping = False
        self._alias_to_name: Dict[str, str] = {}
        self._load_manifest_aliases()
        self._last_launch_name: Optional[str] = None
        self._last_launch_ts: float = 0.0

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

    def _load_manifest_aliases(self) -> None:
        path = self.cfg.manifest_path
        if not path:
            return
        try:
            if not os.path.exists(path):
                print(f"[intent] manifest not found: {path}")
                return
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            games = data.get("games", [])
            for g in games:
                name = str(g.get("name") or g.get("id") or "").strip()
                if not name:
                    continue
                self._alias_to_name[name.lower()] = name
                for alias in g.get("synonyms", []) or []:
                    a = str(alias).strip()
                    if a:
                        self._alias_to_name[a.lower()] = name
            print(f"[intent] loaded {len(self._alias_to_name)} aliases from manifest")
        except Exception as exc:
            print(f"[intent] failed to load manifest aliases: {exc}")

    # MQTT callbacks
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

        corr_id = payload.get("corr_id") or new_corr_id()

        # wake word gate
        if self.cfg.require_wake_word and not has_wake_word(text, self.cfg.wake_words):
            print(f"[intent] no wake word: {text}")
            return

        if contains_any(text, self.cfg.exit_keywords):
            out = {
                "type": "BACK_HOME",
                "source": self.cfg.source_label,
                "raw": {"text": text},
                "corr_id": corr_id,
            }
            self.client.publish(self.cfg.topics.intent, json.dumps(out))
            print(f"[intent] -> BACK_HOME {self.cfg.topics.intent}")
            return

        # 1) 优先从触发词后的短语提取
        game = extract_game_name(text, self.cfg.launch_triggers)

        # 2) 模糊匹配：对候选短语或整句在 manifest 别名中找最相近
        if self._alias_to_name:
            best_name: Optional[str] = None
            best_score = -1
            candidates = [game] if game else []
            if not candidates:
                candidates = [text]
            for cand in candidates:
                c = cand.strip()
                if not c:
                    continue
                for alias, canonical in self._alias_to_name.items():
                    score = fuzz.partial_ratio(c.lower(), alias.lower())
                    if score > best_score:
                        best_score = score
                        best_name = canonical
            if best_name is not None and best_score >= self.cfg.fuzzy_threshold:
                game = best_name
        if game:
            # 短时去重，避免连续多次相同触发
            now = time.time()
            if self._last_launch_name == game and (now - self._last_launch_ts) < self.cfg.dedupe_window_sec:
                print(f"[intent] skip duplicate LAUNCH_GAME '{game}'")
                return

            out = {
                "type": "LAUNCH_GAME",
                "game_name": game,
                "source": self.cfg.source_label,
                "raw": {"text": text},
                "corr_id": corr_id,
            }
            self.client.publish(self.cfg.topics.intent, json.dumps(out))
            print(f"[intent] -> LAUNCH_GAME '{game}' {self.cfg.topics.intent}")
            self._last_launch_name = game
            self._last_launch_ts = now
            return

        # default: dialog query
        out = {
            "type": "QUERY",
            "text": text,
            "source": self.cfg.source_label,
            "corr_id": corr_id,
        }
        self.client.publish(self.cfg.topics.dialog_query, json.dumps(out))
        print(f"[intent] -> QUERY {self.cfg.topics.dialog_query}")


def main() -> int:
    cfg = load_config()
    svc = IntentService(cfg)
    svc.start()

    def _term(signum, frame):  # type: ignore[override]
        print(f"[intent] signal {signum}, stopping...")
        svc.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _term)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _term)

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        _term(signal.SIGINT, None)
    return 0


if __name__ == "__main__":
    sys.exit(main())


