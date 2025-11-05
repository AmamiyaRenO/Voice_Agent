#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import httpx
import paho.mqtt.client as mqtt
import winsound


@dataclass
class Topics:
    dialog_query: str = "robot/dialog/query"
    dialog_answer: str = "robot/dialog/answer"
    tts_state: str = "robot/tts/state"


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 1883
    topics: Topics = field(default_factory=Topics)
    voice_api_url: str = "http://127.0.0.1:8000"
    tts_endpoint: str = "/tts"  # POST {text}
    respond_endpoint: str = "/respond"  # POST {text}
    speak_audio: bool = True
    source_label: str = "dialog_service"


def load_config() -> Config:
    cfg = Config()
    cfg.host = os.environ.get("MQTT_HOST", cfg.host)
    cfg.port = int(os.environ.get("MQTT_PORT", cfg.port))
    cfg.topics = Topics(
        dialog_query=os.environ.get("DIALOG_QUERY_TOPIC", cfg.topics.dialog_query),
        dialog_answer=os.environ.get("DIALOG_ANSWER_TOPIC", cfg.topics.dialog_answer),
    )
    cfg.voice_api_url = os.environ.get("VOICE_API_URL", cfg.voice_api_url).rstrip("/")
    cfg.speak_audio = os.environ.get("DIALOG_SPEAK_AUDIO", "1").lower() in {"1", "true", "yes", "on"}
    cfg.source_label = os.environ.get("DIALOG_SOURCE_LABEL", cfg.source_label)
    return cfg


class DialogService:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"dialog-svc-{uuid.uuid4().hex[:8]}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.http = httpx.Client(timeout=30.0)

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

    def _publish_answer(self, text: str, corr_id: Optional[str]) -> None:
        payload = {
            "type": "ANSWER",
            "text": text,
            "source": self.cfg.source_label,
            "corr_id": corr_id or uuid.uuid4().hex,
        }
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
        try:
            url = f"{self.cfg.voice_api_url}{self.cfg.tts_endpoint}"
            resp = self.http.post(url, json={"text": text})
            resp.raise_for_status()
            data = resp.json()
            audio_b64 = data.get("audio_wav_base64")
            if not isinstance(audio_b64, str) or not audio_b64:
                print("[dialog] TTS response missing audio data")
                return
            wav_bytes = base64.b64decode(audio_b64)
            try:
                self._publish_tts_state(True, corr_id, text)
                winsound.PlaySound(wav_bytes, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
            except Exception:
                # 忽略播放错误，避免影响主流程
                pass
            finally:
                self._publish_tts_state(False, corr_id)
        except Exception as exc:
            print(f"[dialog] TTS failed: {exc}")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except json.JSONDecodeError:
            print("[dialog] invalid json, ignored")
            return

        text = str(payload.get("text") or "").strip()
        if not text:
            return
        corr_id = payload.get("corr_id")

        try:
            url = f"{self.cfg.voice_api_url}{self.cfg.respond_endpoint}"
            resp = self.http.post(url, json={"text": text})
            resp.raise_for_status()
            data = resp.json()
            reply_text = (data.get("text") or "").strip()
        except Exception as exc:
            print(f"[dialog] LLM request failed: {exc}")
            return

        if not reply_text:
            return

        self._publish_answer(reply_text, corr_id)
        if self.cfg.speak_audio:
            self._tts_and_play(reply_text, corr_id)


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


