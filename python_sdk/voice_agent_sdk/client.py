from __future__ import annotations

from typing import Any, Dict, Optional

import requests


class VoiceAgentClient:
    """HTTP SDK that mirrors the Assets full-control panel surface."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        panel_port: int = 8787,
        http_session: Optional[requests.Session] = None,
    ) -> None:
        self._host = host
        self._panel_port = panel_port
        self._http = http_session or requests.Session()

    @property
    def panel_base_url(self) -> str:
        return f"http://{self._host}:{self._panel_port}"

    def get_logs(self, timeout: float = 10.0) -> Dict[str, Any]:
        return self._request_json("GET", "/api/logs", timeout=timeout)

    def get_tts_options(self, timeout: float = 10.0) -> Dict[str, Any]:
        return self._request_json("GET", "/api/voice/options", timeout=timeout)

    def get_kokoro_options(self, timeout: float = 10.0) -> Dict[str, Any]:
        return self._request_json("GET", "/api/kokoro/options", timeout=timeout)

    def speak(
        self,
        text: str,
        voice: Optional[str] = None,
        model: Optional[str] = None,
        speed: float = 1.0,
        volume: float = 1.0,
        backend: Optional[str] = None,
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        self._require_text(text)
        payload: Dict[str, Any] = {
            "text": text.strip(),
            "voice": voice,
            "model": model,
            "speed": speed,
            "volume": volume,
            "backend": backend,
        }
        return self._request_json("POST", "/api/speak", payload=self._filter_none(payload), timeout=timeout)

    def set_voice(self, voice: str, timeout: float = 10.0) -> Dict[str, Any]:
        return self._voice_action("set", {"voice": self._require_value(voice, "voice")}, timeout=timeout)

    def set_tts_model(self, model: str, timeout: float = 10.0) -> Dict[str, Any]:
        return self._voice_action("set_model", {"model": self._require_value(model, "model")}, timeout=timeout)

    def set_tts_backend(self, backend: str, timeout: float = 10.0) -> Dict[str, Any]:
        return self._voice_action("set_backend", {"backend": self._require_value(backend, "backend")}, timeout=timeout)

    def set_kokoro_voice(self, voice: str, timeout: float = 10.0) -> Dict[str, Any]:
        return self._voice_action("set_kokoro_voice", {"voice": self._require_value(voice, "voice")}, timeout=timeout)

    def kokoro_speak(self, text: str, voice: Optional[str] = None, timeout: float = 30.0) -> Dict[str, Any]:
        self._require_text(text)
        payload = {"text": text.strip(), "voice": voice}
        return self._request_json("POST", "/api/kokoro/speak", payload=self._filter_none(payload), timeout=timeout)

    def get_llm_prompt(self, timeout: float = 10.0) -> Dict[str, Any]:
        return self._request_json("GET", "/api/llm/prompt", timeout=timeout)

    def set_llm_prompt(self, prompt: str, timeout: float = 10.0) -> Dict[str, Any]:
        return self._request_json(
            "POST",
            "/api/llm/prompt",
            payload={"prompt": self._require_value(prompt, "prompt")},
            timeout=timeout,
        )

    def reset_llm_prompt(self, timeout: float = 10.0) -> Dict[str, Any]:
        return self._request_json("POST", "/api/llm/prompt", payload={"reset": True}, timeout=timeout)

    def get_runtime_config(self, timeout: float = 10.0) -> Dict[str, Any]:
        return self._request_json("GET", "/api/runtime/config", timeout=timeout)

    def set_local_model(self, model: str, timeout: float = 10.0) -> Dict[str, Any]:
        return self._request_json(
            "POST",
            "/api/runtime/config",
            payload={"ollama_model": self._require_value(model, "model")},
            timeout=timeout,
        )

    def get_asr_status(self, timeout: float = 10.0) -> Dict[str, Any]:
        return self._request_json("GET", "/api/asr", timeout=timeout)

    def set_asr_mode(self, mode: str, timeout: float = 10.0) -> Dict[str, Any]:
        return self._request_json(
            "POST",
            "/api/asr",
            payload={"action": "set_mode", "mode": self._require_value(mode, "mode")},
            timeout=timeout,
        )

    def set_backend_asr_mode(self, mode: str, timeout: float = 10.0) -> Dict[str, Any]:
        return self._request_json(
            "POST",
            "/api/asr/backend",
            payload={"action": "set_mode", "mode": self._require_value(mode, "mode")},
            timeout=timeout,
        )

    def start_listening(self, timeout: float = 10.0) -> Dict[str, Any]:
        return self._request_json("POST", "/api/asr", payload={"action": "start_listening"}, timeout=timeout)

    def pause_listening(self, timeout: float = 10.0) -> Dict[str, Any]:
        return self._request_json("POST", "/api/asr", payload={"action": "pause_listening"}, timeout=timeout)

    def describe_current_camera(
        self,
        prompt: str,
        model: Optional[str] = None,
        timeout: float = 45.0,
    ) -> Dict[str, Any]:
        payload = {"prompt": self._require_value(prompt, "prompt"), "model": model}
        return self._request_json("POST", "/api/vision/describe", payload=self._filter_none(payload), timeout=timeout)

    def launch_game(self, name: str, timeout: float = 10.0) -> Dict[str, Any]:
        return self._request_json(
            "POST",
            "/api/game",
            payload={"action": "launch", "name": self._require_value(name, "name")},
            timeout=timeout,
        )

    def exit_game(self, timeout: float = 10.0) -> Dict[str, Any]:
        return self._request_json("POST", "/api/game", payload={"action": "exit"}, timeout=timeout)

    def face_preset(self, mode: str, seconds: float = 3.0, timeout: float = 10.0) -> Dict[str, Any]:
        return self._request_json(
            "POST",
            "/api/face",
            payload={"mode": self._require_value(mode, "mode"), "seconds": seconds},
            timeout=timeout,
        )

    def face_custom(self, value: str, seconds: float = 3.0, timeout: float = 10.0) -> Dict[str, Any]:
        return self._request_json(
            "POST",
            "/api/face",
            payload={"mode": "custom", "value": self._require_value(value, "value"), "seconds": seconds},
            timeout=timeout,
        )

    def face_happy(self, seconds: float = 3.0, timeout: float = 10.0) -> Dict[str, Any]:
        return self.face_preset("happy", seconds=seconds, timeout=timeout)

    def face_neutral(self, seconds: float = 3.0, timeout: float = 10.0) -> Dict[str, Any]:
        return self.face_preset("neutral", seconds=seconds, timeout=timeout)

    def face_sad(self, seconds: float = 3.0, timeout: float = 10.0) -> Dict[str, Any]:
        return self.face_preset("sad", seconds=seconds, timeout=timeout)

    def face_very_sad(self, seconds: float = 3.0, timeout: float = 10.0) -> Dict[str, Any]:
        return self.face_preset("verySad", seconds=seconds, timeout=timeout)

    def face_excited(self, seconds: float = 3.0, timeout: float = 10.0) -> Dict[str, Any]:
        return self.face_preset("excited", seconds=seconds, timeout=timeout)

    def led_breathe(
        self,
        color: str = "#00BFFF",
        brightness: float = 0.8,
        period: float = 2.0,
        duration: float = 0.0,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        payload = {
            "mode": "breathe",
            "color": color,
            "brightness": brightness,
            "period": period,
            "duration": duration,
        }
        return self._request_json("POST", "/api/led", payload=payload, timeout=timeout)

    def led_solid(
        self,
        color: str = "#FFFFFF",
        brightness: float = 0.8,
        duration: float = 0.0,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        payload = {
            "mode": "solid",
            "color": color,
            "brightness": brightness,
            "duration": duration,
        }
        return self._request_json("POST", "/api/led", payload=payload, timeout=timeout)

    def led_random(self, duration: float = 0.0, timeout: float = 10.0) -> Dict[str, Any]:
        return self._request_json("POST", "/api/led", payload={"mode": "random", "duration": duration}, timeout=timeout)

    def led_off(self, timeout: float = 10.0) -> Dict[str, Any]:
        return self._request_json("POST", "/api/led", payload={"mode": "off"}, timeout=timeout)

    def flower_open(self, timeout: float = 10.0) -> Dict[str, Any]:
        return self._flower_action("open", timeout=timeout)

    def flower_close(self, timeout: float = 10.0) -> Dict[str, Any]:
        return self._flower_action("close", timeout=timeout)

    def flower_stop(self, timeout: float = 10.0) -> Dict[str, Any]:
        return self._flower_action("stop", timeout=timeout)

    def flower_open_slow(self, timeout: float = 10.0) -> Dict[str, Any]:
        return self._flower_action("open_slow", timeout=timeout)

    def flower_close_slow(self, timeout: float = 10.0) -> Dict[str, Any]:
        return self._flower_action("close_slow", timeout=timeout)

    def _flower_action(self, action: str, timeout: float) -> Dict[str, Any]:
        return self._request_json("POST", "/api/flower", payload={"action": action}, timeout=timeout)

    def _voice_action(self, action: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        body = {"action": action}
        body.update(payload)
        return self._request_json("POST", "/api/voice", payload=body, timeout=timeout)

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        url = f"{self.panel_base_url}{path}"
        response = self._http.request(method.upper(), url, json=payload, timeout=timeout)
        response.raise_for_status()
        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text}
        return data if isinstance(data, dict) else {"data": data}

    @staticmethod
    def _filter_none(payload: Dict[str, Any]) -> Dict[str, Any]:
        return {key: value for key, value in payload.items() if value is not None}

    @staticmethod
    def _require_text(text: str) -> str:
        if not text or not text.strip():
            raise ValueError("text is required")
        return text.strip()

    @staticmethod
    def _require_value(value: str, name: str) -> str:
        if not value or not str(value).strip():
            raise ValueError(f"{name} is required")
        return str(value).strip()
