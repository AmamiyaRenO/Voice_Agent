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
from typing import Optional, Tuple

import httpx
import paho.mqtt.client as mqtt


@dataclass
class Topics:
    dialog_query: str = "robot/dialog/query"
    dialog_answer: str = "robot/dialog/answer"
    tts_state: str = "robot/tts/state"
    tts_options: str = "robot/tts/options"
    style: str = "robot/dialog/style"


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
        style=os.environ.get("DIALOG_STYLE_TOPIC", cfg.topics.style),
    )
    # Backward compatibility: VOICE_API_URL used to serve both; now prefer RESPOND_API_URL and TTS_API_URL/PIPER_HTTP_URL
    cfg.respond_api_url = os.environ.get("RESPOND_API_URL", os.environ.get("VOICE_API_URL", cfg.respond_api_url)).rstrip("/")
    cfg.tts_api_url = os.environ.get("TTS_API_URL", os.environ.get("PIPER_HTTP_URL", cfg.tts_api_url)).rstrip("/")
    # Force-disable local playback (even if env is set) to avoid double audio.
    cfg.speak_audio = False
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
        self.current_style: Optional[str] = None
        self.current_system_prompt: Optional[str] = None
        if os.environ.get("DIALOG_SPEAK_AUDIO"):
            print("[dialog] NOTE: DIALOG_SPEAK_AUDIO is set but will be ignored (forced off for AEC).")

    @staticmethod
    def _style_to_prompt(style: str) -> Optional[str]:
        s = (style or "").strip().lower()
        if not s:
            return None
        if s in {"supportive", "coach", "friendly"}:
            return (
                "You are Rachel, a supportive rehabilitation and exercise coach. "
                "Be encouraging, concise, and proactive. Use simple sentences. "
                "Guide the user step by step and celebrate small progress."
            )
        if s in {"minimalist", "short", "brief"}:
            return (
                "You are Rachel the coach. Reply in very short, minimal sentences. "
                "Only the essential guidance, no small talk. Max two sentences."
            )
        if s in {"energetic", "enthusiastic", "cheerful"}:
            return (
                "You are Rachel, an energetic and motivating fitness coach. "
                "Be upbeat and positive. Keep responses concise but spirited."
            )
        # Fallback: treat the style string itself as a custom system prompt if it's long
        if len(s) > 12:
            return style
        return None

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
        client.subscribe(self.cfg.topics.style)
        print(f"[dialog] subscribed {self.cfg.topics.style}")

    def _publish_answer(self, text: str, corr_id: Optional[str]) -> None:
        self._publish_answer_ex(text=text, corr_id=corr_id, tts_instruct=None, tts_speaker=None)

    def _publish_answer_ex(
        self,
        *,
        text: str,
        corr_id: Optional[str],
        tts_instruct: Optional[str],
        tts_speaker: Optional[str],
    ) -> None:
        payload = {
            "type": "ANSWER",
            "text": text,
            "source": self.cfg.source_label,
            "corr_id": corr_id or uuid.uuid4().hex,
        }
        if tts_instruct:
            payload["tts_instruct"] = tts_instruct
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

        # TTS 选项更新
        if topic == self.cfg.topics.tts_options:
            voice = str(payload.get("voice") or "").strip()
            model = str(payload.get("model") or "").strip()
            self.tts_voice = voice or None
            self.tts_model = model or None
            print(f"[dialog] tts options updated voice='{self.tts_voice}' model='{self.tts_model}'")
            return
        # 风格/提示词更新
        if topic == self.cfg.topics.style:
            style = str(payload.get("style") or payload.get("value") or payload or "").strip()
            prompt = self._style_to_prompt(style)
            self.current_style = style or None
            self.current_system_prompt = prompt
            print(f"[dialog] style updated style='{self.current_style}' prompt={'set' if self.current_system_prompt else 'unset'}")
            return

        text = str(payload.get("text") or "").strip()
        if not text:
            return
        corr_id = payload.get("corr_id")

        try:
            url = f"{self.cfg.respond_api_url}{self.cfg.respond_endpoint}"
            system_prompt = self.current_system_prompt or None
            # Require structured output so we can attach an emotion/style instruction for TTS.
            # Keep it short to reduce LLM latency and avoid runaway JSON.
            structured_rule = (
                "Return ONLY valid JSON with keys: "
                "\"text\" and \"tts_instruct\".\n"
                "- text: what you say to the user (1-2 sentences, concise)\n"
                "- tts_instruct: ONE short sentence describing speaking style/emotion/prosody "
                "(e.g. \"Warm, encouraging coach tone; clear pauses; slightly upbeat.\")\n"
                "No markdown, no extra keys, no extra commentary."
            )
            if system_prompt:
                system_prompt = system_prompt.strip() + "\n\n" + structured_rule
            else:
                # Default persona if no style prompt was configured.
                base = (
                    "You are Rachel, a supportive rehabilitation and exercise coach. "
                    "Be encouraging, concise, and proactive. Use simple sentences. "
                    "Keep responses short (1–2 sentences) so they sound natural when spoken."
                )
                system_prompt = base + "\n\n" + structured_rule

            body = {"text": text, "system": system_prompt}
            resp = self.http.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
            reply_text = (data.get("text") or "").strip()
        except Exception as exc:
            print(f"[dialog] LLM request failed: {exc}")
            return

        if not reply_text:
            return

        answer_text, tts_instruct = self._try_parse_structured_reply(reply_text)
        # Prefer the TTS speaker selected by the UI (tts_options topic), if any.
        tts_speaker = self.tts_voice or None

        self._publish_answer_ex(
            text=answer_text,
            corr_id=corr_id,
            tts_instruct=tts_instruct,
            tts_speaker=tts_speaker,
        )
        if self.cfg.speak_audio:
            self._tts_and_play(reply_text, corr_id)

    @staticmethod
    def _try_parse_structured_reply(reply_text: str) -> Tuple[str, Optional[str]]:
        raw = (reply_text or "").strip()
        if not raw:
            return "", None
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                text = str(obj.get("text") or "").strip()
                instruct = str(obj.get("tts_instruct") or "").strip()
                if text:
                    return text, (instruct or None)
        except Exception:
            pass
        # Common fallback when models output two JSON strings on separate lines:
        # "Answer text...."
        # "Warm, gentle style..."
        try:
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            if len(lines) >= 2:
                t0 = json.loads(lines[0]) if lines[0].startswith('"') else lines[0]
                t1 = json.loads(lines[1]) if lines[1].startswith('"') else lines[1]
                if isinstance(t0, str) and t0.strip():
                    return t0.strip(), (t1.strip() if isinstance(t1, str) and t1.strip() else None)
        except Exception:
            pass
        # Fallback: treat whole reply as text with no instruct.
        return raw, None


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


