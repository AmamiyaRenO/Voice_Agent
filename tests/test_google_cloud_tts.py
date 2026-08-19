import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest
from fastapi import HTTPException

VOICE_SERVICE_DIR = Path(__file__).resolve().parents[1] / "python_voice_service"
if str(VOICE_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(VOICE_SERVICE_DIR))

import desktop_audio_agent
import desktop_runtime


class _LogStore:
    def __init__(self):
        self.entries = []

    def add(self, role, text, **kwargs):
        self.entries.append((role, text, kwargs))


class _Player:
    def __init__(self):
        self.audio = []
        self.begin_count = 0
        self.end_count = 0

    def begin_stream(self):
        self.begin_count += 1

    def enqueue_audio(self, audio, sample_rate):
        self.audio.append((audio, sample_rate))

    def end_stream(self):
        self.end_count += 1


def _agent():
    agent = desktop_audio_agent.DesktopAudioAgent.__new__(desktop_audio_agent.DesktopAudioAgent)
    agent.active_google_cloud_voice = ""
    agent.google_cloud_tts_language_code = "en-US"
    agent.active_tts_backend = "piper"
    agent.active_tts_model = ""
    agent.log_store = _LogStore()
    agent._player = _Player()
    agent._manual_task = None
    agent.cancel_current_turn = lambda **kwargs: None
    agent._sanitize_assistant_text = lambda text: str(text or "").strip()
    agent._remember_assistant_text = lambda text: None
    agent._wait_for_playback_drain = AsyncMock()
    return agent


def test_google_cloud_backend_normalization():
    assert desktop_audio_agent._normalize_tts_backend("google") == "google-cloud"
    assert desktop_audio_agent._normalize_tts_backend("google_cloud_tts") == "google-cloud"
    assert desktop_runtime._normalize_tts_backend("cloud-tts") == "google-cloud"


def test_google_cloud_model_is_inferred_only_for_gemini_tts_voice(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_TTS_MODEL", raising=False)

    assert desktop_audio_agent._google_cloud_model_for_voice("Achernar") == "gemini-2.5-flash-tts"
    assert desktop_audio_agent._google_cloud_model_for_voice("en-US-Neural2-F") == ""
    assert desktop_audio_agent._google_cloud_model_for_voice("en-US-Chirp3-HD-Achernar") == ""


def test_google_cloud_gemini_tts_model_can_be_overridden(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_TTS_MODEL", "gemini-2.5-pro-tts")

    assert desktop_audio_agent._google_cloud_model_for_voice("Kore") == "gemini-2.5-pro-tts"


def test_google_cloud_voice_options_exposes_error_without_failing():
    agent = _agent()

    def fail(api_key=None):
        raise RuntimeError("credentials missing")

    agent._list_google_cloud_voices_sync = fail
    result = asyncio.run(agent.get_google_cloud_voice_options())

    assert result["ready"] is False
    assert result["voices"] == []
    assert "credentials missing" in result["error"]


def test_google_cloud_voice_options_returns_dynamic_list():
    agent = _agent()
    agent.active_google_cloud_voice = "en-US-Neural2-F"
    agent._list_google_cloud_voices_sync = lambda api_key=None: ["en-US-Neural2-A", "en-US-Neural2-F"]

    result = asyncio.run(agent.get_google_cloud_voice_options())

    assert result["ready"] is True
    assert result["current"] == "en-US-Neural2-F"
    assert result["voices"] == ["en-US-Neural2-A", "en-US-Neural2-F"]


def test_google_cloud_playback_uses_synthesized_audio(monkeypatch):
    agent = _agent()
    agent._synthesize_google_cloud_text_sync = lambda text, voice, api_key=None: b"wav"
    monkeypatch.setattr(
        desktop_audio_agent,
        "_decode_wav_bytes",
        lambda payload: (np.asarray([0.1, -0.1], dtype=np.float32), 24000),
    )

    asyncio.run(
        agent._play_google_cloud_text(
            text="Hello",
            voice="en-US-Neural2-F",
            source="test",
            log_message=True,
            wait_for_drain=False,
        )
    )

    assert len(agent._player.audio) == 1
    assert agent._player.audio[0][1] == 24000
    assert agent.log_store.entries[0][1] == "Hello"


def test_google_cloud_manual_speak_propagates_error_without_local_fallback():
    agent = _agent()
    agent._play_google_cloud_text = AsyncMock(side_effect=RuntimeError("quota exceeded"))
    agent._play_tts_text = AsyncMock()

    with pytest.raises(RuntimeError, match="quota exceeded"):
        asyncio.run(agent.manual_speak(text="Hello", backend="google-cloud"))

    agent._play_tts_text.assert_not_called()


def test_google_cloud_client_prefers_api_key(monkeypatch):
    agent = _agent()
    captured = {}

    class _Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        agent,
        "_google_cloud_tts_module",
        lambda: SimpleNamespace(TextToSpeechClient=_Client),
    )

    agent._google_cloud_tts_client("inspector-key")

    assert captured["client_options"] == {"api_key": "inspector-key"}


def test_google_cloud_synthesis_adds_model_for_gemini_tts_voice(monkeypatch):
    agent = _agent()
    captured = {}

    class _TextToSpeech:
        class AudioEncoding:
            LINEAR16 = "LINEAR16"

        @staticmethod
        def SynthesisInput(**kwargs):
            return kwargs

        @staticmethod
        def VoiceSelectionParams(**kwargs):
            captured["voice"] = kwargs
            return kwargs

        @staticmethod
        def AudioConfig(**kwargs):
            return kwargs

    class _Client:
        @staticmethod
        def synthesize_speech(**kwargs):
            captured["request"] = kwargs
            return SimpleNamespace(audio_content=b"wav")

    monkeypatch.delenv("GOOGLE_CLOUD_TTS_MODEL", raising=False)
    monkeypatch.setattr(agent, "_google_cloud_tts_module", lambda: _TextToSpeech)
    monkeypatch.setattr(agent, "_google_cloud_tts_client", lambda api_key=None: _Client())

    result = agent._synthesize_google_cloud_text_sync("Hello", "Achernar")

    assert result == b"wav"
    assert captured["voice"] == {
        "language_code": "en-US",
        "name": "Achernar",
        "model_name": "gemini-2.5-flash-tts",
    }


def test_google_cloud_synthesis_does_not_add_model_for_classic_voice(monkeypatch):
    agent = _agent()
    captured = {}

    class _TextToSpeech:
        class AudioEncoding:
            LINEAR16 = "LINEAR16"

        SynthesisInput = staticmethod(lambda **kwargs: kwargs)
        AudioConfig = staticmethod(lambda **kwargs: kwargs)

        @staticmethod
        def VoiceSelectionParams(**kwargs):
            captured.update(kwargs)
            return kwargs

    class _Client:
        synthesize_speech = staticmethod(lambda **kwargs: SimpleNamespace(audio_content=b"wav"))

    monkeypatch.setattr(agent, "_google_cloud_tts_module", lambda: _TextToSpeech)
    monkeypatch.setattr(agent, "_google_cloud_tts_client", lambda api_key=None: _Client())

    agent._synthesize_google_cloud_text_sync("Hello", "en-US-Neural2-F")

    assert "model_name" not in captured


def test_google_cloud_api_speak_exposes_provider_error(monkeypatch):
    runtime_agent = SimpleNamespace(
        active_tts_backend="piper",
        active_tts_model="",
        manual_speak=AsyncMock(side_effect=RuntimeError("quota exceeded")),
    )
    monkeypatch.setattr(desktop_runtime, "audio_agent", runtime_agent)

    class _Request:
        async def json(self):
            return {"text": "Hello", "backend": "google-cloud"}

    with pytest.raises(HTTPException) as error:
        asyncio.run(desktop_runtime.api_speak(_Request()))

    assert error.value.status_code == 502
    assert error.value.detail == "Google Cloud TTS failed: quota exceeded"
