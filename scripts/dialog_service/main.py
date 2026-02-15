#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
import re
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import httpx
import paho.mqtt.client as mqtt

_WHITESPACE_RE = re.compile(r"\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[\.\!\?])\s+")
_TRAILING_CONNECTOR_RE = re.compile(
    r"(?:\b(?:and|or|but|to|of|with|for|in|on|at|through|about|into|from)\b[\s,;:]*)+$",
    re.IGNORECASE,
)


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
    return cfg


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


def _compress_reply_for_latency(text: str, max_sentences: int, max_chars: int) -> str:
    normalized = _WHITESPACE_RE.sub(" ", (text or "").strip())
    if not normalized:
        return ""

    compact = normalized
    if max_sentences > 0:
        parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(compact) if p and p.strip()]
        if parts:
            compact = " ".join(parts[:max_sentences]).strip()

    if max_chars > 0 and len(compact) > max_chars:
        search_start = max(16, max_chars - 20)
        split = -1
        punctuation = ".!?;: "
        for i in range(max_chars, search_start - 1, -1):
            if compact[i] in punctuation:
                split = i + 1
                break
        if split <= 0:
            split = max_chars
        compact = compact[:split].strip()

    return compact


def _trim_trailing_connectors(text: str) -> str:
    if not text:
        return ""
    trimmed = _TRAILING_CONNECTOR_RE.sub("", text).strip()
    return trimmed if trimmed else text.strip()


def _compress_reply_by_words(text: str, max_words: int) -> str:
    if max_words <= 0:
        return (text or "").strip()
    words = [w for w in (text or "").strip().split(" ") if w]
    if len(words) <= max_words:
        return (text or "").strip()
    return " ".join(words[:max_words]).strip()


class DialogService:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"dialog-svc-{uuid.uuid4().hex[:8]}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.http = httpx.Client(timeout=30.0)
        self.tts_voice: Optional[str] = None
        self.tts_model: Optional[str] = None
        # Keep compression for latency, but defaults must not truncate sentence semantics.
        self.reply_compress = _env_bool("DIALOG_REPLY_COMPRESS", True)
        self.reply_max_sentences = _env_int("DIALOG_MAX_REPLY_SENTENCES", 2, floor=0)
        self.reply_max_chars = _env_int("DIALOG_MAX_REPLY_CHARS", 0, floor=0)
        self.reply_max_words = _env_int("DIALOG_MAX_REPLY_WORDS", 0, floor=0)
        if os.environ.get("DIALOG_SPEAK_AUDIO"):
            print("[dialog] NOTE: DIALOG_SPEAK_AUDIO is set but will be ignored (forced off for AEC).")

    def start(self) -> None:
        print(f"[dialog] connecting to mqtt {self.cfg.host}:{self.cfg.port}")
        self.client.connect(self.cfg.host, self.cfg.port, keepalive=20)
        self.client.loop_start()

    def stop(self) -> None:
        try:
            self.client.loop_stop()
        finally:
            self.client.disconnect()
            self.http.close()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        print(f"[dialog] connected rc={reason_code}")
        client.subscribe(self.cfg.topics.dialog_query)
        print(f"[dialog] subscribed {self.cfg.topics.dialog_query}")
        client.subscribe(self.cfg.topics.tts_options)
        print(f"[dialog] subscribed {self.cfg.topics.tts_options}")

    def _publish_answer(self, text: str, corr_id: Optional[str]) -> None:
        self._publish_answer_ex(text=text, corr_id=corr_id, tts_speaker=None)

    def _publish_answer_ex(
        self,
        *,
        text: str,
        corr_id: Optional[str],
        tts_speaker: Optional[str],
    ) -> None:
        payload = {
            "type": "ANSWER",
            "text": text,
            "source": self.cfg.source_label,
            "corr_id": corr_id or uuid.uuid4().hex,
        }
        if tts_speaker:
            payload["tts_speaker"] = tts_speaker
        self.client.publish(self.cfg.topics.dialog_answer, json.dumps(payload))

    def _publish_tts_state(self, speaking: bool, corr_id: Optional[str], text: Optional[str] = None) -> None:
        try:
            payload = {
                "speaking": speaking,
                "source": self.cfg.source_label,
                "corr_id": corr_id or uuid.uuid4().hex,
                "ts": time.time(),
            }
            if speaking and text:
                payload["text"] = text
            self.client.publish(self.cfg.topics.tts_state, json.dumps(payload))
        except Exception:
            pass

    def _tts_and_play(self, text: str, corr_id: Optional[str]) -> None:
        # Local playback disabled (Unity will play audio using robot/dialog/answer).
        return

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except json.JSONDecodeError:
            print("[dialog] invalid json, ignored")
            return

        topic = msg.topic or ""

        # TTS 閫夐」鏇存柊
        if topic == self.cfg.topics.tts_options:
            voice = str(payload.get("voice") or "").strip()
            model = str(payload.get("model") or "").strip()
            self.tts_voice = voice or None
            self.tts_model = model or None
            print(f"[dialog] tts options updated voice='{self.tts_voice}' model='{self.tts_model}'")
            return
        text = str(payload.get("text") or "").strip()
        if not text:
            return
        corr_id = payload.get("corr_id")

        try:
            url = f"{self.cfg.respond_api_url}{self.cfg.respond_endpoint}"
            body = {"text": text}
            print("[dialog] respond uses backend runtime/default system prompt")
            resp = self.http.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
            reply_text = (data.get("text") or "").strip()
        except Exception as exc:
            print(f"[dialog] LLM request failed: {exc}")
            return

        if not reply_text:
            return

        answer_text = self._extract_answer_text(reply_text)
        if self.reply_compress:
            answer_text = _compress_reply_for_latency(
                answer_text,
                max_sentences=self.reply_max_sentences,
                max_chars=self.reply_max_chars,
            )
            answer_text = _compress_reply_by_words(answer_text, self.reply_max_words)
            answer_text = _trim_trailing_connectors(answer_text)
            if answer_text and answer_text[-1] not in ".!?":
                answer_text = f"{answer_text}."
        if not answer_text:
            return
        # Prefer the TTS speaker selected by the UI (tts_options topic), if any.
        tts_speaker = self.tts_voice or None

        self._publish_answer_ex(
            text=answer_text,
            corr_id=corr_id,
            tts_speaker=tts_speaker,
        )
        if self.cfg.speak_audio:
            self._tts_and_play(reply_text, corr_id)

    @staticmethod
    def _extract_answer_text(reply_text: str) -> str:
        raw = (reply_text or "").strip()
        if not raw:
            return ""
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                text = str(obj.get("text") or "").strip()
                if text:
                    return text
        except Exception:
            pass
        # Common fallback when models output a quoted answer as first line.
        try:
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            if len(lines) >= 1:
                t0 = json.loads(lines[0]) if lines[0].startswith('"') else lines[0]
                if isinstance(t0, str) and t0.strip():
                    return t0.strip()
        except Exception:
            pass
        return raw


def main() -> int:
    cfg = load_config()
    svc = DialogService(cfg)
    svc.start()

    def _term(signum, frame):  # type: ignore[override]
        print(f"[dialog] signal {signum}, stopping...")
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


