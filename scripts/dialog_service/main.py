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
    speak_audio: bool = True
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
        self.tts_voice: Optional[str] = None
        self.tts_model: Optional[str] = None

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
            url = f"{self.cfg.tts_api_url}{self.cfg.tts_endpoint}"
            # Piper HTTP 支持 GET /speak?text=... 直接返回 WAV；可附带 voice/model
            params = {"text": text}
            if self.tts_voice:
                params["voice"] = self.tts_voice
            if self.tts_model:
                params["model"] = self.tts_model
            resp = self.http.get(url, params=params)
            resp.raise_for_status()
            wav_bytes = resp.content
            try:
                self._publish_tts_state(True, corr_id, text)
                winsound.PlaySound(wav_bytes, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
            except Exception:
                pass
            finally:
                self._publish_tts_state(False, corr_id)
        except httpx.HTTPStatusError as exc:
            # 打印服务器返回的错误正文，便于快速定位 500 的真实原因
            body = ""
            try:
                body = exc.response.text if exc.response is not None else ""
            except Exception:
                body = ""
            preview = body[:400].replace("\n", "\\n")
            print(f"[dialog] TTS failed: {exc} | body={preview}")
        except Exception as exc:
            print(f"[dialog] TTS failed: {exc}")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except json.JSONDecodeError:
            print("[dialog] invalid json, ignored")
            return

        topic = msg.topic or ""

        # TTS 选项更新
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


