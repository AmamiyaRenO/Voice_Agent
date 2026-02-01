from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Any, Dict, Optional

import paho.mqtt.client as mqtt
import requests


@dataclass(frozen=True)
class MqttConfig:
    host: str = "127.0.0.1"
    port: int = 1883
    username: Optional[str] = None
    password: Optional[str] = None
    qos: int = 1
    retain: bool = False


class VoiceAgentClient:
    """Python SDK for controlling the Robot Voice Agent via HTTP + MQTT."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        tts_port: int = 5005,
        panel_port: int = 8787,
        mqtt_config: Optional[MqttConfig] = None,
        mqtt_client: Optional[mqtt.Client] = None,
        http_session: Optional[requests.Session] = None,
    ) -> None:
        self._host = host
        self._tts_port = tts_port
        self._panel_port = panel_port
        self._mqtt_config = mqtt_config or MqttConfig(host=host)
        self._mqtt_client = mqtt_client
        self._http = http_session or requests.Session()

        self.default_face_seconds = 3.0
        self.default_servo_seconds = 2.0
        self.default_slow_seconds = 3.0
        self.default_speed_percent = 30
        self.default_led_breath_seconds = 2.0
        self.default_led_on_seconds = 1.0

        self.face_topic = "robot/pi/face/cmd"
        self.servo_topic = "robot/pi/servo/cmd"
        self.led_topic = "robot/pi/led/cmd"
        self.intent_topic = "robot/intent"
        self.dialog_style_topic = "robot/dialog/style"
        self.tts_options_topic = "robot/tts/options"

    @property
    def piper_url(self) -> str:
        return f"http://{self._host}:{self._tts_port}/speak"

    @property
    def panel_speak_url(self) -> str:
        # Mirrors UserTestControlPanel: POST /api/speak
        return f"http://{self._host}:{self._panel_port}/api/speak"

    def connect_mqtt(self, start_loop: bool = True) -> None:
        if self._mqtt_client is None:
            self._mqtt_client = mqtt.Client()
            if self._mqtt_config.username:
                self._mqtt_client.username_pw_set(
                    self._mqtt_config.username,
                    self._mqtt_config.password,
                )
        self._mqtt_client.connect(self._mqtt_config.host, self._mqtt_config.port)
        if start_loop:
            self._mqtt_client.loop_start()

    def disconnect_mqtt(self, stop_loop: bool = True) -> None:
        if self._mqtt_client is None:
            return
        if stop_loop:
            self._mqtt_client.loop_stop()
        self._mqtt_client.disconnect()

    def speak(
        self,
        text: str,
        voice: Optional[str] = None,
        model: Optional[str] = None,
        speed: float = 1.0,
        volume: float = 1.0,
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        """
        Trigger audible speech via Unity UserTestControlPanel (/api/speak).

        This is intentionally aligned with the UserTestControlPanel's speak payload shape:
        text / voice / model / speed / volume.

        Notes:
        - Unity must be running with UserTestControlPanel listening on panel_port.
        - UserTestControlPanel must have VoiceGameLauncher assigned to actually play audio.
        """
        if not text or not text.strip():
            raise ValueError("text is required")

        payload: Dict[str, Any] = {
            "text": text,
            "voice": voice,
            "model": model,
            "speed": speed,
            "volume": volume,
        }
        filtered = {k: v for k, v in payload.items() if v is not None}
        r = self._http.post(self.panel_speak_url, json=filtered, timeout=timeout)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"raw": r.text}

    def synthesize_wav(
        self,
        text: str,
        voice: Optional[str] = None,
        model: Optional[str] = None,
        config: Optional[str] = None,
        speed: float = 1.0,
        volume: float = 1.0,
        use_post: bool = False,
        timeout: float = 10.0,
    ) -> bytes:
        """
        Call the Piper HTTP service directly and return WAV bytes.

        Note: Piper HTTP in this repo accepts text/model/config. It does not play audio.
        Parameters voice/speed/volume are included to mirror UserTestControlPanel;
        they may be ignored by the server depending on implementation.
        """
        if not text or not text.strip():
            raise ValueError("text is required")
        payload: Dict[str, Any] = {
            "text": text,
            "voice": voice,
            "model": model,
            "config": config,
            "speed": speed,
            "volume": volume,
        }
        filtered = {k: v for k, v in payload.items() if v is not None}
        if use_post:
            response = self._http.post(self.piper_url, json=filtered, timeout=timeout)
        else:
            response = self._http.get(self.piper_url, params=filtered, timeout=timeout)
        response.raise_for_status()
        return response.content

    def set_tts_options(self, voice: Optional[str] = None, model: Optional[str] = None) -> None:
        payload: Dict[str, Any] = {}
        if voice:
            payload["voice"] = voice
        if model:
            payload["model"] = model
        if not payload:
            raise ValueError("voice or model must be provided")
        self._publish(self.tts_options_topic, payload)

    def set_dialog_style(self, style: str) -> None:
        if not style or not style.strip():
            raise ValueError("style is required")
        self._publish(self.dialog_style_topic, {"style": style})

    def launch_game(self, game_name: str, source: str = "python_sdk") -> None:
        if not game_name or not game_name.strip():
            raise ValueError("game_name is required")
        self._publish(
            self.intent_topic,
            {"type": "LAUNCH_GAME", "game_name": game_name, "source": source},
        )

    def exit_game(self, source: str = "python_sdk") -> None:
        self._publish(self.intent_topic, {"type": "EXIT_GAME", "source": source})

    def face_preset(self, preset: str, duration: Optional[float] = None) -> None:
        if not preset or not preset.strip():
            raise ValueError("preset is required")
        if preset.strip().lower() == "idle" and duration is None:
            value = "idle"
        else:
            seconds = duration if duration and duration > 0 else self.default_face_seconds
            value = f"{preset}:{seconds}"
        self._publish(self.face_topic, {"action": "face", "value": value})

    def face_custom(self, value: str) -> None:
        if not value or not value.strip():
            raise ValueError("value is required")
        self._publish(self.face_topic, {"action": "face", "value": value})

    def face_happy(self, duration: Optional[float] = None) -> None:
        self.face_preset("happy", duration)

    def face_neutral(self, duration: Optional[float] = None) -> None:
        self.face_preset("neutral", duration)

    def face_sad(self, duration: Optional[float] = None) -> None:
        self.face_preset("sad", duration)

    def face_very_sad(self, duration: Optional[float] = None) -> None:
        self.face_preset("verySad", duration)

    def face_excited(self, duration: Optional[float] = None) -> None:
        self.face_preset("excited", duration)

    def face_idle(self) -> None:
        self.face_preset("idle", None)

    def servo_open(self, seconds: Optional[float] = None, speed: Optional[int] = None) -> None:
        duration = seconds if seconds and seconds > 0 else self.default_servo_seconds
        self._publish_servo(f"open:{duration}", speed)

    def servo_close(self, seconds: Optional[float] = None, speed: Optional[int] = None) -> None:
        duration = seconds if seconds and seconds > 0 else self.default_servo_seconds
        self._publish_servo(f"close:{duration}", speed)

    def servo_open_hold(self, speed: Optional[int] = None) -> None:
        self._publish_servo("open:0", speed)

    def servo_close_hold(self, speed: Optional[int] = None) -> None:
        self._publish_servo("close:0", speed)

    def servo_center_hold(self, speed: Optional[int] = None) -> None:
        self._publish_servo("center:0", speed)

    def servo_stop(self) -> None:
        self._publish_servo("stop", None)

    def servo_open_slow(self, speed: Optional[int] = None, seconds: Optional[float] = None) -> None:
        duration = seconds if seconds and seconds > 0 else self.default_slow_seconds
        self._publish_servo(f"open:{duration}", speed or self.default_speed_percent)

    def servo_close_slow(self, speed: Optional[int] = None, seconds: Optional[float] = None) -> None:
        duration = seconds if seconds and seconds > 0 else self.default_slow_seconds
        self._publish_servo(f"close:{duration}", speed or self.default_speed_percent)

    def led_breathe(
        self,
        color: str = "#00BFFF",
        brightness: float = 1.0,
        period: float = 1.5,
        duration: Optional[float] = None,
    ) -> None:
        seconds = duration if duration and duration > 0 else self.default_led_breath_seconds
        value = f"breathe:{color}:{seconds}:{max(0.0, min(brightness, 1.0))}:{period}"
        self._publish(self.led_topic, {"action": "led", "value": value})

    def led_solid(
        self,
        color: str = "#FFFFFF",
        brightness: float = 1.0,
        duration: Optional[float] = None,
    ) -> None:
        seconds = duration if duration and duration > 0 else self.default_led_on_seconds
        value = f"on:{color}:{seconds}:{max(0.0, min(brightness, 1.0))}"
        self._publish(self.led_topic, {"action": "led", "value": value})

    def led_random(self, duration: Optional[float] = None) -> None:
        seconds = duration if duration and duration > 0 else self.default_led_on_seconds
        color = "#{:02X}{:02X}{:02X}".format(
            random.randint(0, 255),
            random.randint(0, 255),
            random.randint(0, 255),
        )
        value = f"on:{color}:{seconds}:1.0"
        self._publish(self.led_topic, {"action": "led", "value": value})

    def led_off(self) -> None:
        self._publish(self.led_topic, {"action": "led", "value": "off"})

    def publish_raw(self, topic: str, payload: Dict[str, Any]) -> None:
        self._publish(topic, payload)

    def _publish_servo(self, value: str, speed: Optional[int]) -> None:
        payload: Dict[str, Any] = {"action": "servo", "value": value}
        if speed is not None:
            payload["speed"] = speed
        self._publish(self.servo_topic, payload)

    def _publish(self, topic: str, payload: Dict[str, Any]) -> None:
        if self._mqtt_client is None:
            self.connect_mqtt()
        assert self._mqtt_client is not None
        data = json.dumps(payload, ensure_ascii=False)
        self._mqtt_client.publish(topic, data, qos=self._mqtt_config.qos, retain=self._mqtt_config.retain)
