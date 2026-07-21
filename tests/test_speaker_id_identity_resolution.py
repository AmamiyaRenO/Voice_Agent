import asyncio
import importlib.util
import json
import math
import sys
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
PYTHON_VOICE_DIR = ROOT / "python_voice_service"
SCRIPTS_DIR = ROOT / "scripts"
INTENT_DIR = SCRIPTS_DIR / "intent_service"
DIALOG_DIR = SCRIPTS_DIR / "dialog_service"


def _load_module(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load module spec: {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _unit_vector(x: float, y: float) -> np.ndarray:
    return np.asarray([x, y], dtype=np.float32)


def test_speaker_id_commit_reload_and_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    speaker_id = _load_module("speaker_id_unit_module", PYTHON_VOICE_DIR / "speaker_id.py")
    profiles_path = tmp_path / "speaker_profiles.json"
    config = speaker_id.SpeakerIdConfig(
        enabled=True,
        model_path=str(tmp_path / "missing.onnx"),
        profiles_path=str(profiles_path),
        enroll_min_clips=3,
    )
    service = speaker_id.SpeakerIdService(config)
    service._session = object()
    service._input_name = "feats"
    service.error = ""

    raw_embeddings = [
        np.asarray([1.0, 0.0, 0.0], dtype=np.float32),
        np.asarray([0.0, 2.0, 0.0], dtype=np.float32),
        np.asarray([1.0, 1.0, 0.0], dtype=np.float32),
    ]
    embedding_iter = iter(raw_embeddings)

    def fake_embedding_for_audio(_audio, _sample_rate):
        return next(embedding_iter), 2.0

    monkeypatch.setattr(service, "_embedding_for_audio", fake_embedding_for_audio)
    dummy_audio = np.ones(32000, dtype=np.float32)

    for _ in range(3):
        pending = service.add_pending_clip("user-1", dummy_audio, 16000)
        assert pending["user_id"] == "user-1"

    summary = service.commit_pending_clips("user-1")
    assert summary["has_profile"] is True
    assert summary["clip_count"] == 3
    assert profiles_path.exists()

    reloaded = speaker_id.SpeakerIdService(config)
    profile = reloaded.user_summary("user-1")
    assert profile["has_profile"] is True
    assert profile["clip_count"] == 3

    expected = speaker_id._normalize_embedding(
        np.mean(
            np.stack([speaker_id._normalize_embedding(item) for item in raw_embeddings], axis=0),
            axis=0,
        )
    )
    assert np.allclose(reloaded._profile_centroid("user-1"), expected, atol=1e-5)

    cleared = reloaded.clear_profile("user-1")
    assert cleared["has_profile"] is False
    payload = json.loads(profiles_path.read_text(encoding="utf-8"))
    assert payload["users"] == {}


def test_speaker_id_commits_guest_clips_into_existing_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    speaker_id = _load_module("speaker_id_guest_commit_module", PYTHON_VOICE_DIR / "speaker_id.py")
    config = speaker_id.SpeakerIdConfig(
        enabled=True,
        model_path=str(tmp_path / "missing.onnx"),
        profiles_path=str(tmp_path / "speaker_profiles.json"),
        enroll_min_clips=3,
    )
    service = speaker_id.SpeakerIdService(config)
    service._session = object()
    service._input_name = "feats"
    service.error = ""
    service._profiles = {
        "user_001": {
            "centroid": speaker_id._normalize_embedding(np.asarray([1.0, 0.0], dtype=np.float32)),
            "clip_count": 2,
            "created_ts": 1.0,
        }
    }
    embeddings = iter(
        [
            np.asarray([0.9, 0.1], dtype=np.float32),
            np.asarray([1.0, 0.1], dtype=np.float32),
            np.asarray([0.9, -0.1], dtype=np.float32),
        ]
    )
    monkeypatch.setattr(service, "_embedding_for_audio", lambda _audio, _rate: (next(embeddings), 2.0))
    for _ in range(3):
        service.add_pending_clip("__guest__", np.ones(32000, dtype=np.float32), 16000)

    summary = service.commit_pending_clips_as("__guest__", "user_001")

    assert summary["has_profile"] is True
    assert summary["clip_count"] == 5
    assert service.pending_summary("__guest__")["pending_clip_count"] == 0


def test_desktop_audio_agent_participant_status_exposes_guest_learning_and_possible_match():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_participant_status_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._active_user_id = ""
    agent._last_speaker_match = {
        "matched": False,
        "top1_user_id": "user_008",
        "top1_score": 0.35,
    }
    agent._guest_learning_last_error = ""
    agent._speaker_id = SimpleNamespace(config=SimpleNamespace(enroll_min_clips=3))
    payload = {
        "users": [
            {
                "user_id": "__guest__",
                "pending": {"pending_clip_count": 2, "required_clip_count": 3, "can_commit": False},
            }
        ]
    }

    participant = agent._participant_status(payload)

    assert participant["state"] == "possible_match"
    assert participant["possible_user_id"] == "user_008"
    assert participant["learning_clip_count"] == 2
    assert participant["ready_to_confirm"] is False


def test_desktop_audio_agent_participant_status_is_disabled_when_speaker_id_is_off():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_participant_disabled_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._active_user_id = ""
    agent._last_speaker_match = {}
    agent._guest_learning_last_error = ""
    agent._speaker_id = SimpleNamespace(enabled=False, config=SimpleNamespace(enroll_min_clips=3))
    agent._auto_identity_handler = object()

    participant = agent._participant_status({"users": []})

    assert participant["state"] == "disabled"
    assert participant["confirmation_required"] is False
    assert participant["automatic_identity_enabled"] is False
    assert participant["auto_learning_enabled"] is False


def test_desktop_audio_agent_extracts_participant_name_from_intro_and_short_answer():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_name_extract_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )

    assert audio_agent_module.DesktopAudioAgent._extract_participant_name(
        "My name is Alex Chen",
        allow_short_answer=False,
    ) == "Alex Chen"
    assert audio_agent_module.DesktopAudioAgent._extract_participant_name(
        "Taylor",
        allow_short_answer=True,
    ) == "Taylor"
    assert audio_agent_module.DesktopAudioAgent._extract_participant_name(
        "open cornhole",
        allow_short_answer=True,
    ) == ""
    assert audio_agent_module.DesktopAudioAgent._extract_participant_name(
        "I'm lifting.",
        allow_short_answer=True,
    ) == ""
    assert audio_agent_module.DesktopAudioAgent._extract_participant_name(
        "I'm Taylor.",
        allow_short_answer=False,
    ) == ""


def test_desktop_audio_agent_name_answer_automatically_binds_profile():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_auto_name_bind_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._participant_confirmed_user_id = ""
    agent._identity_awaiting_name = True
    agent._guest_turns = deque(maxlen=12)
    agent._active_user_id = "user_005"
    agent._active_user_last_seen_at = time.time()
    agent._last_speaker_match = {}
    logs = []
    agent.log_store = SimpleNamespace(add=lambda *args, **kwargs: logs.append((args, kwargs)))

    async def fake_handler(**kwargs):
        assert kwargs["name"] == "Taylor"
        agent._participant_confirmed_user_id = "user_005"
        return {"user_id": "user_005", "message": "Using Taylor's existing profile."}

    agent._auto_identity_handler = fake_handler

    user_id, resolution = asyncio.run(agent._maybe_resolve_identity_from_text("Taylor"))

    assert user_id == "user_005"
    assert resolution == "operator_confirmed"
    assert agent._identity_awaiting_name is False
    assert list(agent._guest_turns)[0]["text"] == "Taylor"
    assert any("Using Taylor's existing profile." in args[1] for args, _kwargs in logs)


def test_desktop_audio_agent_switches_to_guest_after_repeated_current_user_mismatch(monkeypatch: pytest.MonkeyPatch):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_auto_switch_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    monkeypatch.setattr(audio_agent_module, "DEFAULT_SPEAKER_ID_AUTO_SWITCH_MISMATCH_REPEATS", 2)
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._speaker_id = SimpleNamespace(config=SimpleNamespace(match_threshold=0.38, match_margin=0.08))
    agent._participant_confirmed_user_id = "user_001"
    agent._active_user_id = "user_001"
    agent._active_user_last_seen_at = time.time()
    agent._last_speaker_match = {}
    agent._confirmed_user_mismatch_count = 0
    logs = []
    agent.log_store = SimpleNamespace(add=lambda *args, **kwargs: logs.append((args, kwargs)))
    mismatch = audio_agent_module.SpeakerMatchResult(
        matched=False,
        top1_user_id="user_002",
        top1_score=0.25,
        reason="below_score_threshold",
    )

    agent._update_last_speaker_match(mismatch, source="test")
    assert agent._participant_confirmed_user_id == "user_001"
    agent._update_last_speaker_match(mismatch, source="test")

    assert agent._participant_confirmed_user_id == ""
    assert agent._active_user_id == ""
    assert any("different participant voice" in args[1] for args, _kwargs in logs)


def test_desktop_audio_agent_name_question_cancels_current_native_reply():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_identity_exclusive_turn_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._participant_confirmed_user_id = ""
    agent._participant_continue_as_guest = False
    agent._auto_identity_candidate_id = ""
    agent._auto_identity_candidate_count = 0
    agent._identity_awaiting_name = False
    agent._identity_question_asked_at = 0.0
    agent.current_asr_mode = audio_agent_module.STREAMING_ASR_MODE_GEMINI_LIVE
    agent.gemini_live_native_response_enabled = True
    cancelled = []
    spoken = []
    agent.cancel_current_turn = lambda **kwargs: cancelled.append(kwargs)

    async def fake_speak(**kwargs):
        spoken.append(kwargs)

    agent.manual_speak = fake_speak
    agent.active_tts_backend = "piper"

    asyncio.run(agent._auto_resolve_or_ask_name())

    assert agent._identity_awaiting_name is True
    assert agent._identity_prompt_suppress_native_output is True
    assert cancelled == [{"reason": "auto_identity_question", "capture_barge_in": False}]
    assert spoken[0]["text"] == "I don't think we've met yet. What's your name?"


def test_desktop_audio_agent_unclear_name_retries_without_leaving_identity_flow():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_identity_name_retry_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._participant_confirmed_user_id = ""
    agent._identity_awaiting_name = True
    agent._identity_name_retry_count = 0
    agent._identity_name_last_retry_at = 0.0
    agent.current_asr_mode = audio_agent_module.STREAMING_ASR_MODE_GEMINI_LIVE
    agent.gemini_live_native_response_enabled = True
    agent.active_tts_backend = "piper"
    cancelled = []
    spoken = []
    logs = []
    agent.cancel_current_turn = lambda **kwargs: cancelled.append(kwargs)
    agent.log_store = SimpleNamespace(add=lambda *args, **kwargs: logs.append((args, kwargs)))

    async def fake_speak(**kwargs):
        spoken.append(kwargs)

    agent.manual_speak = fake_speak

    user_id, resolution = asyncio.run(agent._maybe_resolve_identity_from_text("I'm"))

    assert (user_id, resolution) == ("", "none")
    assert agent._identity_awaiting_name is True
    assert agent._identity_name_retry_count == 1
    assert agent._identity_prompt_suppress_native_output is True
    assert cancelled == [{"reason": "auto_identity_name_retry", "capture_barge_in": False}]
    assert spoken[0]["text"] == "Sorry, I didn't catch that. Please say only your name."
    assert any("recognition was unclear" in args[1] for args, _kwargs in logs)


def test_desktop_audio_agent_gemini_unclear_name_does_not_continue_normal_reply():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_identity_name_consumed_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._listening = True
    agent._identity_awaiting_name = True
    agent._gemini_live_last_input_text = ""
    agent._last_final_event_at = 0.0
    agent._last_gemini_tool_command_at = 0.0
    agent._participant_confirmed_user_id = ""
    agent.gemini_live_native_response_enabled = True
    agent._command_grammar = SimpleNamespace(
        canonicalize=lambda text, allow_bare_game=False: SimpleNamespace(
            canonical_text=text,
            route_type="QUERY",
            game_name="",
            confidence=0.0,
        )
    )
    logs = []
    agent.log_store = SimpleNamespace(add=lambda *args, **kwargs: logs.append((args, kwargs)))
    agent._record_user_transcript_event = lambda _text: None
    agent._remember_guest_turn_text = lambda *_args, **_kwargs: None

    async def fake_identity(_text):
        return "", "none"

    async def unexpected_speaker_resolution(**_kwargs):
        raise AssertionError("speaker resolution should not run during the name retry turn")

    agent._maybe_resolve_identity_from_text = fake_identity
    agent._resolve_recent_speaker_user = unexpected_speaker_resolution

    asyncio.run(agent._handle_gemini_live_user_turn("I'm"))

    assert len(logs) == 1
    assert logs[0][0][0] == "user"
    assert logs[0][0][1] == "I'm"


def test_desktop_audio_agent_identity_prompt_survives_gemini_turn_complete():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_identity_prompt_playback_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    player_calls = []
    agent._player = SimpleNamespace(
        end_stream=lambda: player_calls.append("end_stream"),
        clear=lambda: player_calls.append("clear"),
    )
    agent._identity_prompt_suppress_native_output = True
    agent._gemini_live_output_open = True
    agent._gemini_live_input_text = ""
    agent._assistant_buffer_text = "old Gemini reply"
    agent._assistant_corr_id = "old-turn"
    agent._gemini_live_output_text = "old Gemini reply"
    agent._gemini_live_logged_output_text = ""

    asyncio.run(agent._handle_gemini_live_server_content(SimpleNamespace(turn_complete=True)))

    assert player_calls == ["end_stream"]
    assert agent._identity_prompt_suppress_native_output is False
    assert agent._gemini_live_output_open is False


def test_speaker_id_match_threshold_margin_and_min_duration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    speaker_id = _load_module("speaker_id_match_module", PYTHON_VOICE_DIR / "speaker_id.py")
    config = speaker_id.SpeakerIdConfig(
        enabled=True,
        model_path=str(tmp_path / "missing.onnx"),
        profiles_path=str(tmp_path / "speaker_profiles.json"),
        match_threshold=0.72,
        match_margin=0.05,
        min_match_seconds=1.2,
    )
    service = speaker_id.SpeakerIdService(config)
    service._session = object()
    service._input_name = "feats"
    service.error = ""
    dummy_audio = np.ones(32000, dtype=np.float32)

    monkeypatch.setattr(
        service,
        "_embedding_for_audio",
        lambda _audio, _sample_rate: (np.asarray([1.0, 0.0], dtype=np.float32), 1.0),
    )
    too_short = service.match_audio(dummy_audio, 16000)
    assert too_short.matched is False
    assert too_short.reason == "too_short"

    service._profiles = {
        "alice": {"centroid": speaker_id._normalize_embedding(_unit_vector(0.71, math.sqrt(1.0 - 0.71**2)))},
        "bob": {"centroid": speaker_id._normalize_embedding(_unit_vector(0.20, math.sqrt(1.0 - 0.20**2)))},
    }
    monkeypatch.setattr(
        service,
        "_embedding_for_audio",
        lambda _audio, _sample_rate: (np.asarray([1.0, 0.0], dtype=np.float32), 2.0),
    )
    below_threshold = service.match_audio(dummy_audio, 16000)
    assert below_threshold.matched is False
    assert below_threshold.top1_score < config.match_threshold
    assert below_threshold.reason == "below_score_threshold"
    assert below_threshold.top1_user_id == "alice"
    assert below_threshold.top2_user_id == "bob"

    service._profiles = {
        "alice": {"centroid": speaker_id._normalize_embedding(_unit_vector(0.95, math.sqrt(1.0 - 0.95**2)))},
        "bob": {"centroid": speaker_id._normalize_embedding(_unit_vector(0.92, math.sqrt(1.0 - 0.92**2)))},
    }
    below_margin = service.match_audio(dummy_audio, 16000)
    assert below_margin.matched is False
    assert below_margin.top1_score >= config.match_threshold
    assert below_margin.margin < config.match_margin
    assert below_margin.reason == "below_margin_threshold"

    service._profiles = {
        "alice": {"centroid": speaker_id._normalize_embedding(_unit_vector(0.96, math.sqrt(1.0 - 0.96**2)))},
        "bob": {"centroid": speaker_id._normalize_embedding(_unit_vector(0.20, math.sqrt(1.0 - 0.20**2)))},
    }
    matched = service.match_audio(dummy_audio, 16000)
    assert matched.matched is True
    assert matched.user_id == "alice"
    assert matched.reason == "matched"
    assert matched.top1_user_id == "alice"
    assert matched.top2_user_id == "bob"


def test_intent_service_preserves_identity_resolution_none():
    pytest.importorskip("paho.mqtt.client")
    pytest.importorskip("yaml")

    if str(INTENT_DIR) not in sys.path:
        sys.path.insert(0, str(INTENT_DIR))
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))

    intent_main = _load_module("intent_identity_resolution_main", INTENT_DIR / "main.py")
    cfg = intent_main.Config(
        require_wake_word=False,
        use_llm_classifier=False,
        use_moonshine_intent_recognizer=False,
    )
    service = intent_main.IntentService(cfg)

    class DummyUserMemory:
        def __init__(self):
            self.resolve_calls = 0

        def resolve_user(self, _identity_key):
            self.resolve_calls += 1
            raise AssertionError("resolve_user should not run for identity_resolution=none")

        def get_game_reference(self, _user_id):
            raise AssertionError("get_game_reference should not run for identity_resolution=none")

    class DummyRouter:
        def __init__(self):
            self.last_context_game_name = None

        def route(self, text, corr_id, context_game_name=""):
            self.last_context_game_name = context_game_name
            return SimpleNamespace(
                log_line="",
                topic=cfg.topics.intent,
                payload={"type": "BACK_HOME", "text": text, "corr_id": corr_id},
            )

        def close(self):
            return None

    published = []

    class DummyClient:
        def publish(self, topic, payload):
            published.append((topic, payload))

    class DummyMsg:
        topic = cfg.topics.voice_text
        payload = json.dumps(
            {
                "text": "back home",
                "corr_id": "corr-none",
                "identity_resolution": "none",
            }
        ).encode("utf-8")

    dummy_memory = DummyUserMemory()
    dummy_router = DummyRouter()
    service.client = DummyClient()
    service._user_memory = dummy_memory
    service._router = dummy_router
    service._on_message(None, None, DummyMsg())
    service._router.close()

    assert dummy_memory.resolve_calls == 0
    assert dummy_router.last_context_game_name == ""
    assert len(published) == 1
    topic, payload = published[0]
    node = json.loads(payload)
    assert topic == cfg.topics.intent
    assert node["identity_resolution"] == "none"


def test_dialog_service_skips_memory_when_identity_resolution_none():
    pytest.importorskip("paho.mqtt.client")

    if str(DIALOG_DIR) not in sys.path:
        sys.path.insert(0, str(DIALOG_DIR))

    dialog_cfg = _load_module("dialog_identity_resolution_cfg", DIALOG_DIR / "dialog_config.py")
    dialog_impl = _load_module("dialog_identity_resolution_impl", DIALOG_DIR / "dialog_service_impl.py")
    cfg = dialog_cfg.Config(enable_user_memory=False, enable_vision_query=False)
    service = dialog_impl.DialogService(cfg)
    service.reply_compress = False
    service.game_catalog = None

    class DummyMemory:
        def __init__(self):
            self.resolve_calls = 0
            self.remember_calls = 0
            self.context_calls = 0

        def resolve_user(self, _identity_key):
            self.resolve_calls += 1
            raise AssertionError("resolve_user should not run for identity_resolution=none")

        def remember_utterance(self, _user_id, _text):
            self.remember_calls += 1
            raise AssertionError("remember_utterance should not run for identity_resolution=none")

        def build_memory_context(self, _user_id, query_text=""):
            self.context_calls += 1
            raise AssertionError("build_memory_context should not run for identity_resolution=none")

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"text": "Hello there."}

    class DummyHttp:
        def __init__(self):
            self.url = ""
            self.json_payload = {}

        def post(self, url, json):
            self.url = url
            self.json_payload = dict(json)
            return DummyResponse()

    published = []

    class DummyClient:
        def publish(self, topic, payload):
            published.append((topic, payload))

    class DummyMsg:
        topic = cfg.topics.dialog_query
        payload = json.dumps(
            {
                "text": "hello",
                "corr_id": "corr-dialog-none",
                "identity_resolution": "none",
            }
        ).encode("utf-8")

    dummy_memory = DummyMemory()
    dummy_http = DummyHttp()
    service.user_memory = dummy_memory
    service.http = dummy_http
    service.client = DummyClient()
    service._on_message(None, None, DummyMsg())

    assert dummy_memory.resolve_calls == 0
    assert dummy_memory.remember_calls == 0
    assert dummy_memory.context_calls == 0
    assert dummy_http.json_payload["text"] == "hello"
    assert "user_id" not in dummy_http.json_payload
    assert "memory_context" not in dummy_http.json_payload
    assert len(published) == 1
    topic, payload = published[0]
    node = json.loads(payload)
    assert topic == cfg.topics.dialog_answer
    assert node["text"] == "Hello there."


def test_desktop_audio_agent_requeues_invalid_enrollment_clip():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module("desktop_audio_agent_enrollment_module", PYTHON_VOICE_DIR / "desktop_audio_agent.py")
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    log_lines = []
    call_count = {"value": 0}

    def fake_add_pending_clip(user_id, _audio, _sample_rate):
        call_count["value"] += 1
        if call_count["value"] == 1:
            raise ValueError("enrollment clip is too short")
        return {"user_id": user_id, "pending_clip_count": 1}

    async def _exercise() -> None:
        future = asyncio.get_running_loop().create_future()
        agent._loop = asyncio.get_running_loop()
        agent._speaker_enrollment_requests = deque([("user_001", future, 0.0)])
        agent._speaker_enrollment_lock = threading.Lock()
        agent._speaker_enrollment_suppress_until = 0.0
        agent._last_enrollment_suppression_log_at = 0.0
        agent.log_store = SimpleNamespace(add=lambda *_args, **_kwargs: log_lines.append((_args, _kwargs)))
        agent._speaker_id = SimpleNamespace(add_pending_clip=fake_add_pending_clip)

        segment = audio_agent_module.CapturedSpeechSegment(
            audio=np.ones(32000, dtype=np.float32),
            sample_rate=16000,
            started_at=0.0,
            ended_at=2.0,
            speech_seconds=2.0,
        )

        await agent._consume_next_enrollment_segment(segment)
        assert not future.done()
        assert len(agent._speaker_enrollment_requests) == 1
        assert agent._speaker_enrollment_requests[0][0] == "user_001"

        await agent._consume_next_enrollment_segment(segment)
        assert future.done() is True
        assert future.result()["user_id"] == "user_001"
        assert agent._speaker_enrollment_suppress_until > 0.0

    asyncio.run(_exercise())

    assert call_count["value"] == 2
    assert any("ignored invalid enrollment clip" in args[1] for args, _kwargs in log_lines)


def test_desktop_audio_agent_auto_voice_learning_rejects_noise_without_clear_transcript():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_auto_learning_noise_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._recent_credible_transcripts = deque()

    sample_rate = 16000
    rng = np.random.default_rng(7)
    noise = rng.normal(0.0, 0.02, sample_rate * 2).astype(np.float32)
    segment = audio_agent_module.CapturedSpeechSegment(
        audio=noise,
        sample_rate=sample_rate,
        started_at=100.0,
        ended_at=102.0,
        speech_seconds=2.0,
    )

    useful, reason = agent._guest_learning_segment_quality(segment)

    assert useful is False
    assert "transcript" in reason.lower() or "background noise" in reason.lower()


def test_desktop_audio_agent_auto_voice_learning_accepts_dynamic_audio_with_clear_transcript():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_auto_learning_voice_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._recent_credible_transcripts = deque([(102.2, "Hello, my name is Leo.")])

    sample_rate = 16000
    time_axis = np.arange(sample_rate * 2, dtype=np.float32) / sample_rate
    envelope = np.tile(
        np.concatenate(
            [
                np.full(sample_rate // 4, 0.15, dtype=np.float32),
                np.full(sample_rate // 4, 1.0, dtype=np.float32),
            ]
        ),
        4,
    )
    voice_like_audio = (0.08 * envelope * np.sin(2.0 * np.pi * 180.0 * time_axis)).astype(np.float32)
    segment = audio_agent_module.CapturedSpeechSegment(
        audio=voice_like_audio,
        sample_rate=sample_rate,
        started_at=100.0,
        ended_at=102.0,
        speech_seconds=2.0,
    )

    useful, reason = agent._guest_learning_segment_quality(segment)

    assert useful is True
    assert reason == ""


def test_desktop_audio_agent_auto_voice_learning_accepts_gemini_confirmed_speech_without_usable_transcript():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_auto_learning_gemini_confirmation_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._recent_credible_transcripts = deque()
    agent._recent_speech_confirmations = deque([102.2])

    sample_rate = 16000
    time_axis = np.arange(sample_rate, dtype=np.float32) / sample_rate
    envelope = np.concatenate(
        [
            np.full(sample_rate // 2, 0.2, dtype=np.float32),
            np.full(sample_rate // 2, 1.0, dtype=np.float32),
        ]
    )
    voice_like_audio = (0.08 * envelope * np.sin(2.0 * np.pi * 180.0 * time_axis)).astype(np.float32)
    segment = audio_agent_module.CapturedSpeechSegment(
        audio=voice_like_audio,
        sample_rate=sample_rate,
        started_at=101.0,
        ended_at=102.0,
        speech_seconds=1.0,
    )

    useful, reason = agent._guest_learning_segment_quality(segment)

    assert useful is True
    assert reason == ""


def test_desktop_audio_agent_auto_voice_learning_ignores_noise_transcripts():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_auto_learning_transcript_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )

    assert audio_agent_module.DesktopAudioAgent._is_credible_voice_learning_transcript("<noise>") is False
    assert audio_agent_module.DesktopAudioAgent._is_credible_voice_learning_transcript("[silence]") is False
    assert audio_agent_module.DesktopAudioAgent._is_credible_voice_learning_transcript("um") is False
    assert audio_agent_module.DesktopAudioAgent._is_credible_voice_learning_transcript("My name is Leo") is True


def test_audio_frontend_does_not_treat_low_level_noise_after_silence_as_speech():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_frontend_noise_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    processor = audio_agent_module.AudioFrontEndProcessor(sample_rate_hz=16000, frame_size=320)
    silence = np.zeros(320, dtype=np.float32)
    for _ in range(audio_agent_module.DEFAULT_FRONTEND_NOISE_BOOTSTRAP_FRAMES + 2):
        processor.process(silence)

    time_axis = np.arange(320, dtype=np.float32) / 16000.0
    low_noise = (0.001 * np.sin(2.0 * np.pi * 600.0 * time_axis)).astype(np.float32)
    processor.process(low_noise)

    assert not bool(processor.status().speech_active)


def test_speaker_capture_discards_active_segment_when_assistant_starts_speaking():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_speaker_capture_assistant_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._speaker_capture_enabled = lambda: True
    agent.is_assistant_speaking = lambda: True
    agent._speaker_capture_suppress_until = 0.0
    agent._speaker_segment_active = True
    agent._speaker_segment_frames = [np.ones(320, dtype=np.float32)]
    agent._speaker_segment_started_at = time.time() - 1.0
    agent._speaker_segment_preroll_frames = deque([np.ones(320, dtype=np.float32)])

    agent._update_speaker_capture(np.ones(320, dtype=np.float32), speech_active=True)

    assert agent._speaker_segment_active is False
    assert agent._speaker_segment_frames == []
    assert list(agent._speaker_segment_preroll_frames) == []
    assert agent._speaker_capture_suppress_until > time.time()


def test_speaker_capture_ignores_echo_during_assistant_tail_window():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_speaker_capture_tail_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._speaker_capture_enabled = lambda: True
    agent.is_assistant_speaking = lambda: False
    agent._speaker_capture_suppress_until = time.time() + 1.0
    agent._speaker_segment_active = False
    agent._speaker_segment_frames = []
    agent._speaker_segment_started_at = 0.0
    agent._speaker_segment_preroll_frames = deque([np.ones(320, dtype=np.float32)])

    agent._update_speaker_capture(np.ones(320, dtype=np.float32), speech_active=True)

    assert agent._speaker_segment_active is False
    assert list(agent._speaker_segment_preroll_frames) == []


def test_desktop_audio_agent_suppresses_transcript_during_enrollment_cooldown():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module("desktop_audio_agent_transcript_module", PYTHON_VOICE_DIR / "desktop_audio_agent.py")
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    submitted = []
    log_lines = []

    async def fake_submit_user_turn(**kwargs):
        submitted.append(kwargs)

    agent._speaker_enrollment_requests = deque()
    agent._speaker_enrollment_lock = threading.Lock()
    agent._speaker_enrollment_suppress_until = time.time() + 5.0
    agent._last_enrollment_suppression_log_at = 0.0
    agent.log_store = SimpleNamespace(add=lambda *_args, **_kwargs: log_lines.append((_args, _kwargs)))
    agent._cancel_partial_commit = lambda: None
    agent._last_final_event_at = 0.0
    agent._last_partial_text = ""
    agent._last_stable_partial_text = ""
    agent._listening = True
    agent.is_assistant_speaking = lambda: False
    agent._should_ignore_live_captions_echo = lambda _text: False
    agent._command_grammar = SimpleNamespace(
        canonicalize=lambda text, **_kwargs: SimpleNamespace(
            canonical_text=text,
            route_type="QUERY",
            game_name="",
            confidence=0.0,
        )
    )
    agent._submit_user_turn = fake_submit_user_turn

    asyncio.run(
        agent._handle_external_transcript_final(
            "please remember this",
            source="live_captions",
            user_id="user_001",
            identity_resolution="auto",
        )
    )

    assert submitted == []
    assert any("ignored transcript from live_captions" in args[1] for args, _kwargs in log_lines)


def test_desktop_audio_agent_relaxes_short_live_captions_queries():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module("desktop_audio_agent_live_caption_confidence_module", PYTHON_VOICE_DIR / "desktop_audio_agent.py")

    assert audio_agent_module._estimate_transcript_confidence(
        text="What's up, Rachel?",
        grammar_route="QUERY",
        grammar_confidence=0.0,
        avg_logprob=None,
        transcript_source=audio_agent_module.TRANSCRIPT_SOURCE_FINAL,
        input_source="live_captions",
    ) == audio_agent_module.TRANSCRIPT_CONFIDENCE_MEDIUM

    assert audio_agent_module._estimate_transcript_confidence(
        text="What's up, Rachel?",
        grammar_route="QUERY",
        grammar_confidence=0.0,
        avg_logprob=None,
        transcript_source=audio_agent_module.TRANSCRIPT_SOURCE_FINAL,
        input_source="api",
    ) == audio_agent_module.TRANSCRIPT_CONFIDENCE_LOW


def test_api_asr_bare_game_name_reaches_command_grammar(tmp_path: Path):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module("desktop_audio_agent_api_bare_game_module", PYTHON_VOICE_DIR / "desktop_audio_agent.py")
    grammar_module = _load_module("command_grammar_api_bare_game_module", PYTHON_VOICE_DIR / "command_grammar.py")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "games": [
                    {"id": "air_hockey", "name": "Air Hockey", "synonyms": ["air hockey"]},
                    {"id": "cornhole", "name": "Bean Bag Toss", "synonyms": ["cornhole", "corn hole"]},
                ]
            }
        ),
        encoding="utf-8",
    )

    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    submitted = []

    async def fake_submit_user_turn(**kwargs):
        submitted.append(kwargs)

    agent._command_grammar = grammar_module.CommandGrammarMatcher.from_sources(
        launch_triggers=["open", "start", "play"],
        exit_keywords=["back home"],
        manifest_path=str(manifest_path),
    )
    agent._cancel_partial_commit = lambda: None
    agent._last_final_event_at = 0.0
    agent._last_partial_text = ""
    agent._last_stable_partial_text = ""
    agent._last_partial_text_changed_at = 0.0
    agent._last_stable_partial_text_changed_at = 0.0
    agent._listening = True
    agent.is_assistant_speaking = lambda: False
    agent._suppress_transcript_during_enrollment = lambda source, text=None: False
    agent._should_ignore_live_captions_echo = lambda _text: False
    agent._record_user_transcript_event = lambda _text: None
    agent._submit_user_turn = fake_submit_user_turn
    agent.log_store = SimpleNamespace(add=lambda *_args, **_kwargs: None)

    asyncio.run(agent._handle_external_transcript_final("Air Hockey", source="api_openai"))

    assert len(submitted) == 1
    assert submitted[0]["text"] == "open Air Hockey"
    assert submitted[0]["grammar_route"] == "LAUNCH_GAME"
    assert submitted[0]["grammar_game_name"] == "Air Hockey"
    assert submitted[0]["input_source"] == "api_openai"


def test_sanitize_tts_text_strips_keycap_and_symbol_emoji():
    if str(DIALOG_DIR) not in sys.path:
        sys.path.insert(0, str(DIALOG_DIR))

    text_utils_module = _load_module("dialog_text_utils_sanitize_module", DIALOG_DIR / "text_utils.py")

    assert text_utils_module.sanitize_tts_text("Hi 1️⃣ 😊 ™️ there") == "Hi there"


def test_desktop_audio_agent_partial_fallback_skips_incomplete_query(monkeypatch: pytest.MonkeyPatch):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module("desktop_audio_agent_partial_incomplete_module", PYTHON_VOICE_DIR / "desktop_audio_agent.py")
    monkeypatch.setattr(audio_agent_module, "DEFAULT_PARTIAL_COMMIT_DELAY_SECONDS", 0.0)
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    submitted = []
    log_lines = []

    async def fake_submit_user_turn(**kwargs):
        submitted.append(kwargs)

    agent.current_asr_mode = "moonshine"
    agent._listening = True
    agent.is_assistant_speaking = lambda: False
    agent._suppress_transcript_during_enrollment = lambda source, text=None: False
    agent._speech_active_last = False
    agent._speech_ended_at = 10.0
    agent._partial_commit_anchor_at = 10.0
    agent._last_final_event_at = 0.0
    agent._last_partial_text = "help me with"
    agent._last_stable_partial_text = "help me with"
    agent._last_partial_text_changed_at = 10.0
    agent._last_stable_partial_text_changed_at = 10.0
    agent._command_grammar = SimpleNamespace(
        canonicalize=lambda text, **_kwargs: SimpleNamespace(
            canonical_text=text,
            route_type="QUERY",
            game_name="",
            confidence=0.0,
        )
    )
    agent._last_reported_transcript_text = ""
    agent._last_reported_transcript_at = 0.0
    agent.log_store = SimpleNamespace(add=lambda *_args, **_kwargs: log_lines.append((_args, _kwargs)))
    agent._submit_user_turn = fake_submit_user_turn

    asyncio.run(agent._commit_partial_after_delay(10.0, 10.0))

    assert submitted == []
    assert log_lines == []


def test_desktop_audio_agent_partial_fallback_respects_later_final(monkeypatch: pytest.MonkeyPatch):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module("desktop_audio_agent_partial_final_guard_module", PYTHON_VOICE_DIR / "desktop_audio_agent.py")
    monkeypatch.setattr(audio_agent_module, "DEFAULT_PARTIAL_COMMIT_DELAY_SECONDS", 0.0)
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    submitted = []

    async def fake_submit_user_turn(**kwargs):
        submitted.append(kwargs)

    agent.current_asr_mode = "moonshine"
    agent._listening = True
    agent.is_assistant_speaking = lambda: False
    agent._suppress_transcript_during_enrollment = lambda source, text=None: False
    agent._speech_active_last = False
    agent._speech_ended_at = 10.0
    agent._partial_commit_anchor_at = 10.0
    agent._last_final_event_at = 10.1
    agent._last_partial_text = "Can you hear me"
    agent._last_stable_partial_text = "Can you hear me"
    agent._last_partial_text_changed_at = 10.0
    agent._last_stable_partial_text_changed_at = 10.0
    agent._command_grammar = SimpleNamespace(
        canonicalize=lambda text, **_kwargs: SimpleNamespace(
            canonical_text=text,
            route_type="QUERY",
            game_name="",
            confidence=0.0,
        )
    )
    agent._last_reported_transcript_text = ""
    agent._last_reported_transcript_at = 0.0
    agent._last_final_transcript = ""
    agent._last_final_transcript_seq = 0
    agent.log_store = SimpleNamespace(add=lambda *_args, **_kwargs: None)
    agent._submit_user_turn = fake_submit_user_turn

    asyncio.run(agent._commit_partial_after_delay(10.0, 10.0))

    assert submitted == []


def test_desktop_audio_agent_partial_fallback_commits_balanced_query(monkeypatch: pytest.MonkeyPatch):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module("desktop_audio_agent_partial_balanced_query_module", PYTHON_VOICE_DIR / "desktop_audio_agent.py")
    monkeypatch.setattr(audio_agent_module, "DEFAULT_PARTIAL_COMMIT_DELAY_SECONDS", 0.0)
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    submitted = []

    async def fake_submit_user_turn(**kwargs):
        submitted.append(kwargs)

    agent.current_asr_mode = "moonshine"
    agent._listening = True
    agent.is_assistant_speaking = lambda: False
    agent._suppress_transcript_during_enrollment = lambda source, text=None: False
    agent._speech_active_last = False
    agent._speech_ended_at = 10.0
    agent._partial_commit_anchor_at = 10.0
    agent._last_final_event_at = 0.0
    agent._last_partial_text = "Can you hear me"
    agent._last_stable_partial_text = "Can you hear me"
    agent._last_partial_text_changed_at = 10.0
    agent._last_stable_partial_text_changed_at = 10.0
    agent._command_grammar = SimpleNamespace(
        canonicalize=lambda text, **_kwargs: SimpleNamespace(
            canonical_text=text,
            route_type="QUERY",
            game_name="",
            confidence=0.0,
        )
    )
    agent._last_reported_transcript_text = ""
    agent._last_reported_transcript_at = 0.0
    agent._last_final_transcript = ""
    agent._last_final_transcript_seq = 0
    agent.log_store = SimpleNamespace(add=lambda *_args, **_kwargs: None)
    agent._submit_user_turn = fake_submit_user_turn

    asyncio.run(agent._commit_partial_after_delay(10.0, 10.0))

    assert len(submitted) == 1
    assert submitted[0]["text"] == "Can you hear me"
    assert submitted[0]["transcript_source"] == audio_agent_module.TRANSCRIPT_SOURCE_STABLE_PARTIAL_FALLBACK


def test_desktop_audio_agent_play_piper_text_sanitizes_assistant_log():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module("desktop_audio_agent_piper_sanitize_module", PYTHON_VOICE_DIR / "desktop_audio_agent.py")
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    logs = []

    class _DummyStreamResponse:
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            if False:
                yield b""

    class _DummyClient:
        def stream(self, *_args, **_kwargs):
            return _DummyStreamResponse()

    agent._client = _DummyClient()
    agent._player = SimpleNamespace(
        begin_stream=lambda: None,
        enqueue_pcm16=lambda *_args, **_kwargs: None,
        end_stream=lambda: None,
    )
    agent._recent_assistant_texts = deque()
    agent._last_assistant_spoke_at = 0.0
    agent.output_sample_rate = 22050
    agent.piper_base_url = "http://example.test"
    agent.log_store = SimpleNamespace(add=lambda *_args, **_kwargs: logs.append((_args, _kwargs)))
    agent._wait_for_playback_drain = lambda: asyncio.sleep(0)

    asyncio.run(
        agent._play_piper_text(
            text="Hi 1️⃣ 😊 ™️ there",
            voice=None,
            model=None,
            instruct=None,
            source="test",
            log_message=True,
            wait_for_drain=False,
        )
    )

    assert logs[0][0][1] == "Hi there"


def test_voice_service_skips_uncertain_turn_guard_for_live_captions_final():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("faster_whisper")

    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))

    voice_main = _load_module("voice_service_live_caption_uncertainty_module", PYTHON_VOICE_DIR / "main.py")

    live_captions_payload = voice_main.ConversationTurnRequest(
        text="What's up, Rachel?",
        source="desktop_audio:live_captions",
        transcript_source="final",
        transcript_confidence="low",
    )
    assert voice_main._should_clarify_uncertain_turn(live_captions_payload, "QUERY") is False

    api_payload = voice_main.ConversationTurnRequest(
        text="What's up, Rachel?",
        source="desktop_audio:api",
        transcript_source="final",
        transcript_confidence="low",
    )
    assert voice_main._should_clarify_uncertain_turn(api_payload, "QUERY") is True


def test_gemini_live_input_transcription_accumulates_delta_chunks():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    audio_agent_module = _load_module("desktop_audio_agent_gemini_input_module", PYTHON_VOICE_DIR / "desktop_audio_agent.py")
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._gemini_live_input_text = ""
    agent._last_partial_text = ""
    agent._last_stable_partial_text = ""
    submitted = []

    async def fake_user_turn(text):
        submitted.append(text)

    agent._handle_gemini_live_user_turn = fake_user_turn

    asyncio.run(agent._handle_gemini_live_input_transcription(SimpleNamespace(text="I want", finished=False)))
    asyncio.run(agent._handle_gemini_live_input_transcription(SimpleNamespace(text="to play", finished=False)))
    asyncio.run(agent._handle_gemini_live_input_transcription(SimpleNamespace(text="air hockey", finished=True)))

    assert submitted == ["I want to play air hockey"]
    assert agent._gemini_live_input_text == ""
    assert agent._last_partial_text == ""
    assert agent._last_stable_partial_text == ""


def test_gemini_live_input_transcription_keeps_cumulative_updates_clean():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    audio_agent_module = _load_module(
        "desktop_audio_agent_gemini_input_cumulative_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._gemini_live_input_text = ""
    agent._last_partial_text = ""
    agent._last_stable_partial_text = ""
    submitted = []

    async def fake_user_turn(text):
        submitted.append(text)

    agent._handle_gemini_live_user_turn = fake_user_turn

    asyncio.run(agent._handle_gemini_live_input_transcription(SimpleNamespace(text="I want", finished=False)))
    asyncio.run(
        agent._handle_gemini_live_input_transcription(
            SimpleNamespace(text="I want to play air hockey", finished=True)
        )
    )

    assert submitted == ["I want to play air hockey"]


def test_gemini_live_input_transcription_replaces_non_english_text_in_english_mode():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    audio_agent_module = _load_module(
        "desktop_audio_agent_gemini_wake_alias_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._gemini_live_input_text = ""
    agent._last_partial_text = ""
    agent._last_stable_partial_text = ""
    agent.gemini_live_force_english_transcripts = True
    submitted = []

    async def fake_user_turn(text):
        submitted.append(text)

    agent._handle_gemini_live_user_turn = fake_user_turn

    asyncio.run(agent._handle_gemini_live_input_transcription(SimpleNamespace(text="太日球了。", finished=True)))

    assert submitted == ["Speech recognized."]


def test_gemini_live_input_transcription_replaces_short_non_english_text_in_english_mode():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    audio_agent_module = _load_module(
        "desktop_audio_agent_gemini_short_non_english_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._gemini_live_input_text = ""
    agent._last_partial_text = ""
    agent._last_stable_partial_text = ""
    agent.gemini_live_force_english_transcripts = True
    submitted = []

    async def fake_user_turn(text):
        submitted.append(text)

    agent._handle_gemini_live_user_turn = fake_user_turn

    asyncio.run(agent._handle_gemini_live_input_transcription(SimpleNamespace(text="嘿球。", finished=True)))

    assert submitted == ["Speech recognized."]


def test_gemini_live_partial_transcription_never_displays_non_english_text_in_english_mode():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    audio_agent_module = _load_module(
        "desktop_audio_agent_gemini_non_english_partial_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._gemini_live_input_text = ""
    agent._last_partial_text = ""
    agent._last_stable_partial_text = ""
    agent.gemini_live_force_english_transcripts = True

    asyncio.run(agent._handle_gemini_live_input_transcription(SimpleNamespace(text="嘿球。", finished=False)))

    assert agent._last_partial_text == "Speech recognized."
    assert agent._last_stable_partial_text == "Speech recognized."


def test_gemini_live_turn_complete_normalizes_buffered_non_english_transcript():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    audio_agent_module = _load_module(
        "desktop_audio_agent_gemini_turn_complete_english_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._gemini_live_input_text = "嘿球。"
    agent._last_partial_text = "Speech recognized."
    agent._last_stable_partial_text = "Speech recognized."
    agent.gemini_live_force_english_transcripts = True
    agent._identity_prompt_suppress_native_output = False
    agent.gemini_live_native_response_enabled = False
    submitted = []

    async def fake_user_turn(text):
        submitted.append(text)

    agent._handle_gemini_live_user_turn = fake_user_turn

    asyncio.run(agent._handle_gemini_live_server_content(SimpleNamespace(turn_complete=True)))

    assert submitted == ["Speech recognized."]
    assert agent._gemini_live_input_text == ""


def test_gemini_live_input_transcription_replaces_long_non_english_text():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    audio_agent_module = _load_module(
        "desktop_audio_agent_gemini_long_non_english_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._gemini_live_input_text = ""
    agent._last_partial_text = ""
    agent._last_stable_partial_text = ""
    agent.gemini_live_force_english_transcripts = True
    submitted = []

    async def fake_user_turn(text):
        submitted.append(text)

    agent._handle_gemini_live_user_turn = fake_user_turn

    asyncio.run(
        agent._handle_gemini_live_input_transcription(
            SimpleNamespace(text="这是一句明确而且较长的中文内容。", finished=True)
        )
    )

    assert submitted == ["Speech recognized."]


def test_gemini_live_unavailable_english_transcript_is_not_saved_to_memory():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    audio_agent_module = _load_module(
        "desktop_audio_agent_gemini_unavailable_transcript_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    logs = []
    recorded = []
    remembered = []
    agent._listening = True
    agent._gemini_live_last_input_text = ""
    agent._last_final_event_at = 0.0
    agent.log_store = SimpleNamespace(add=lambda *args, **kwargs: logs.append((args, kwargs)))
    agent._record_user_transcript_event = lambda text: recorded.append(text)
    agent._remember_guest_turn_text = lambda text, **_kwargs: remembered.append(text)

    asyncio.run(agent._handle_gemini_live_user_turn("Speech recognized."))

    assert recorded == []
    assert remembered == []
    assert logs[0][0][1] == "Speech recognized."


def test_gemini_live_output_transcription_accumulates_delta_chunks():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    audio_agent_module = _load_module("desktop_audio_agent_gemini_output_module", PYTHON_VOICE_DIR / "desktop_audio_agent.py")
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._assistant_buffer_text = ""
    agent._gemini_live_output_text = ""
    agent._gemini_live_logged_output_text = ""
    agent._recent_assistant_texts = deque(maxlen=8)
    logs = []
    agent.log_store = SimpleNamespace(add=lambda *args, **kwargs: logs.append((args, kwargs)))

    agent._handle_gemini_live_output_transcription(SimpleNamespace(text="Let's play", finished=False))
    agent._handle_gemini_live_output_transcription(SimpleNamespace(text="air hockey", finished=True))

    assert agent._gemini_live_output_text == "Let's play air hockey"
    assert logs[-1][0][1] == "Let's play air hockey"


def test_voice_service_session_only_memory_payload_is_session_first():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("faster_whisper")

    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))

    voice_main = _load_module("voice_service_session_memory_module", PYTHON_VOICE_DIR / "main.py")
    voice_main._ANONYMOUS_SESSION_STORE = voice_main._AnonymousSessionStore()
    voice_main._ANONYMOUS_SESSION_STORE.remember_turn("user", "Can you hear me?")
    voice_main._ANONYMOUS_SESSION_STORE.remember_turn("user", "Can you recommend something?")

    payload = voice_main._ANONYMOUS_SESSION_STORE.build_memory_payload("What do you know about me?")

    assert payload["result_kind"] == "session_only_summary"
    assert "personal history" not in payload["text"].lower()
    assert "conversation" in payload["text"].lower()


def test_voice_service_structured_renderer_falls_back_when_required_game_name_is_missing(monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("faster_whisper")

    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))

    voice_main = _load_module("voice_service_structured_render_module", PYTHON_VOICE_DIR / "main.py")

    async def fake_render(_user_text, _payload):
        return "A good one to try is something new."

    monkeypatch.setattr(voice_main, "_generate_structured_spoken_reply", fake_render)
    reply = asyncio.run(
        voice_main._spoken_reply_from_payload(
            "Can you recommend another game?",
            {
                "type": "game_recommend",
                "primary_game_name": "Disc Golf",
                "game_name": "Disc Golf",
                "candidate_games": ["Disc Golf", "Bean Bag Toss"],
                "allowed_game_names": ["Disc Golf", "Bean Bag Toss"],
                "required_terms": ["Disc Golf"],
                "reason_text": "it fits what you like",
                "text": "A good one to try next is Disc Golf.",
                "max_sentences": 2,
            },
            all_game_names=["Disc Golf", "Bean Bag Toss"],
        )
    )

    assert "Disc Golf" in reply
    assert "something new" not in reply


def test_desktop_audio_agent_live_captions_prefers_recent_strict_segment_over_older_fallback():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module("desktop_audio_agent_live_captions_module", PYTHON_VOICE_DIR / "desktop_audio_agent.py")
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    now = 100.0

    recent_noise = audio_agent_module.CapturedSpeechSegment(
        audio=np.asarray([0.0], dtype=np.float32),
        sample_rate=16000,
        started_at=97.5,
        ended_at=99.0,
        speech_seconds=1.5,
    )
    older_voice = audio_agent_module.CapturedSpeechSegment(
        audio=np.asarray([1.0], dtype=np.float32),
        sample_rate=16000,
        started_at=87.0,
        ended_at=90.0,
        speech_seconds=3.0,
    )

    def fake_match_audio(audio, _sample_rate):
        marker = float(np.asarray(audio, dtype=np.float32).reshape(-1)[0])
        if marker >= 0.5:
            return audio_agent_module.SpeakerMatchResult(
                user_id="user_003",
                matched=True,
                score=0.91,
                margin=0.13,
                top1_score=0.91,
                top2_score=0.78,
                candidate_count=2,
                duration_seconds=3.0,
                reason="matched",
            )
        return audio_agent_module.SpeakerMatchResult(
            matched=False,
            score=0.18,
            top1_score=0.18,
            top2_score=0.09,
            candidate_count=2,
            duration_seconds=1.5,
            reason="below_threshold",
        )

    agent._speaker_id = SimpleNamespace(enabled=True, match_audio=fake_match_audio)
    agent._recent_speaker_segments = deque([older_voice, recent_noise])
    agent._last_speaker_match = {}
    agent._active_user_id = ""

    user_id, identity_resolution = asyncio.run(
        agent._resolve_recent_speaker_user(source="live_captions", observed_at=now)
    )

    assert user_id == ""
    assert identity_resolution == "none"
    assert agent._active_user_id == ""
    assert agent._last_speaker_match["matched"] is False
    assert agent._last_speaker_match["segment_used_fallback_window"] is False
    assert agent._last_speaker_match["segment_selection_window"] == "strict"
    assert agent._last_speaker_match["candidate_count"] == 2
    assert agent._last_speaker_match["profile_candidate_count"] == 2
    assert agent._last_speaker_match["segment_candidate_count"] == 2
    assert agent._last_speaker_match["strict_segment_candidate_count"] == 1
    assert agent._last_speaker_match["fallback_segment_candidate_count"] == 1
    assert older_voice.caption_uses == 0
    assert recent_noise.caption_uses == 1


def test_desktop_audio_agent_live_captions_match_requires_operator_confirmation():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_live_captions_recent_fallback_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    now = 100.0

    older_voice = audio_agent_module.CapturedSpeechSegment(
        audio=np.asarray([1.0], dtype=np.float32),
        sample_rate=16000,
        started_at=89.0,
        ended_at=91.5,
        speech_seconds=2.5,
    )

    agent._speaker_id = SimpleNamespace(
        enabled=True,
        match_audio=lambda _audio, _sample_rate: audio_agent_module.SpeakerMatchResult(
            user_id="user_003",
            matched=True,
            score=0.91,
            margin=0.13,
            top1_score=0.91,
            top2_score=0.78,
            candidate_count=2,
            duration_seconds=2.5,
            reason="matched",
        ),
    )
    agent._recent_speaker_segments = deque([older_voice])
    agent._last_speaker_match = {}
    agent._active_user_id = ""

    user_id, identity_resolution = asyncio.run(
        agent._resolve_recent_speaker_user(source="live_captions", observed_at=now)
    )

    assert user_id == ""
    assert identity_resolution == "none"
    assert agent._active_user_id == ""
    assert agent._last_speaker_match["matched"] is True
    assert agent._last_speaker_match["segment_used_fallback_window"] is True
    assert agent._last_speaker_match["segment_selection_window"] == "fallback"
    assert agent._last_speaker_match["segment_candidate_count"] == 1
    assert agent._last_speaker_match["strict_segment_candidate_count"] == 0
    assert agent._last_speaker_match["fallback_segment_candidate_count"] == 1
    assert agent._last_speaker_match["candidate_count"] == 2
    assert agent._last_speaker_match["profile_candidate_count"] == 2
    assert older_voice.caption_uses == 1


def test_desktop_audio_agent_keep_guest_discards_pending_voice_learning():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_keep_guest_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._active_user_id = "user_001"
    agent._active_user_last_seen_at = time.time()
    agent._last_speaker_match = {"matched": True}
    agent._guest_learning_last_error = ""
    agent._guest_turns = deque([{"role": "user", "text": "hello"}])
    agent._speaker_id = SimpleNamespace(clear_pending=lambda user_id: {"user_id": user_id, "pending_clip_count": 0})

    summary = asyncio.run(agent.keep_guest_participant())

    assert summary["pending_clip_count"] == 0
    assert agent._active_user_id == ""
    assert agent._participant_continue_as_guest is True
    assert list(agent._guest_turns) == []


def test_desktop_audio_agent_clear_current_speaker_profile_restarts_guest_identity_flow():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_clear_current_profile_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._active_user_id = "user_001"
    agent._active_user_last_seen_at = time.time()
    agent._last_speaker_match = {"matched": True}
    agent._participant_confirmed_user_id = "user_001"
    agent._participant_continue_as_guest = True
    agent._identity_awaiting_name = True
    agent._identity_question_asked_at = time.time()
    agent._auto_identity_candidate_id = "user_001"
    agent._auto_identity_candidate_count = 2
    agent._confirmed_user_mismatch_count = 2
    agent._guest_turns = deque([{"role": "user", "text": "hello"}])
    calls = []
    agent._speaker_id = SimpleNamespace(
        clear_profile=lambda user_id: calls.append(("profile", user_id)) or {"user_id": user_id, "has_profile": False},
        clear_pending=lambda user_id: calls.append(("pending", user_id)) or {"user_id": user_id, "pending_clip_count": 0},
    )

    summary = asyncio.run(agent.clear_speaker_profile(user_id="user_001"))

    assert summary["has_profile"] is False
    assert calls == [("profile", "user_001"), ("pending", audio_agent_module.GUEST_SPEAKER_ID)]
    assert agent._active_user_id == ""
    assert agent._participant_confirmed_user_id == ""
    assert agent._identity_awaiting_name is False
    assert agent._participant_continue_as_guest is False
    assert list(agent._guest_turns) == []


def test_desktop_audio_agent_clear_noncurrent_speaker_profile_removes_stale_match_state():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_clear_stale_profile_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._active_user_id = ""
    agent._active_user_last_seen_at = 0.0
    agent._last_speaker_match = {"top1_user_id": "user_002", "top1_score": 0.9}
    agent._participant_confirmed_user_id = ""
    agent._participant_continue_as_guest = False
    agent._identity_awaiting_name = False
    agent._identity_question_asked_at = 0.0
    agent._auto_identity_candidate_id = "user_002"
    agent._auto_identity_candidate_count = 2
    agent._auto_identity_candidate_last_at = time.time()
    agent._confirmed_user_mismatch_count = 0
    agent._recent_speaker_segments = deque([SimpleNamespace()])
    agent._guest_turns = deque([{"role": "user", "text": "hello"}])
    calls = []
    agent._speaker_id = SimpleNamespace(
        clear_profile=lambda user_id: calls.append(("profile", user_id)) or {"user_id": user_id, "has_profile": False},
        clear_pending=lambda user_id: calls.append(("pending", user_id)) or {"user_id": user_id, "pending_clip_count": 0},
    )

    asyncio.run(agent.clear_speaker_profile(user_id="user_002"))

    assert agent._last_speaker_match == {}
    assert agent._auto_identity_candidate_id == ""
    assert agent._auto_identity_candidate_count == 0
    assert list(agent._recent_speaker_segments) == []
    assert "user_002" in agent._deleted_speaker_user_ids
    assert calls == [("profile", "user_002"), ("pending", audio_agent_module.GUEST_SPEAKER_ID)]


def test_desktop_audio_agent_live_captions_blocks_stale_fallback_segment():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_live_captions_stale_fallback_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    now = 100.0

    stale_voice = audio_agent_module.CapturedSpeechSegment(
        audio=np.asarray([1.0], dtype=np.float32),
        sample_rate=16000,
        started_at=82.0,
        ended_at=84.5,
        speech_seconds=2.5,
    )

    agent._speaker_id = SimpleNamespace(
        enabled=True,
        match_audio=lambda _audio, _sample_rate: (_ for _ in ()).throw(AssertionError("stale fallback should not match")),
    )
    agent._recent_speaker_segments = deque([stale_voice])
    agent._last_speaker_match = {}
    agent._active_user_id = "user_003"

    user_id, identity_resolution = asyncio.run(
        agent._resolve_recent_speaker_user(source="live_captions", observed_at=now)
    )

    assert user_id == ""
    assert identity_resolution == "none"
    assert agent._active_user_id == ""
    assert agent._last_speaker_match["matched"] is False
    assert agent._last_speaker_match["reason"] == "stale_fallback_segment"
    assert agent._last_speaker_match["segment_used_fallback_window"] is True
    assert agent._last_speaker_match["segment_candidate_count"] == 1
    assert agent._last_speaker_match["strict_segment_candidate_count"] == 0
    assert agent._last_speaker_match["fallback_segment_candidate_count"] == 1
    assert agent._last_speaker_match["segment_age_seconds"] == pytest.approx(15.5, abs=1e-4)
    assert stale_voice.caption_uses == 0


def test_desktop_audio_agent_uses_recent_active_user_when_segment_missing():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_active_user_fallback_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    now = 100.0
    agent._speaker_id = SimpleNamespace(enabled=True)
    agent._recent_speaker_segments = deque()
    agent._last_speaker_match = {}
    agent._active_user_id = "user_008"
    agent._active_user_last_seen_at = now - 4.0

    user_id, identity_resolution = asyncio.run(
        agent._resolve_recent_speaker_user(source="gemini_live", observed_at=now)
    )

    assert user_id == "user_008"
    assert identity_resolution == "active_fallback"
    assert agent._last_speaker_match["matched"] is True
    assert agent._last_speaker_match["reason"] == "active_user_fallback_no_recent_segment"
    assert agent._last_speaker_match["fallback_user_id"] == "user_008"


def test_desktop_audio_agent_gemini_live_user_turn_reaches_conversation_memory():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_gemini_memory_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    submitted = []

    async def fake_resolve_recent_speaker_user(**_kwargs):
        return "user_008", "auto"

    async def fake_submit_user_turn(**kwargs):
        submitted.append(kwargs)

    agent._listening = True
    agent._gemini_live_last_input_text = ""
    agent._last_final_event_at = 0.0
    agent._last_user_submit_text = ""
    agent._last_user_submit_at = 0.0
    agent._command_grammar = SimpleNamespace(
        canonicalize=lambda text, allow_bare_game=False: SimpleNamespace(
            canonical_text=text,
            route_type="QUERY",
            game_name="",
            confidence=0.0,
        )
    )
    agent._record_user_transcript_event = lambda _text: None
    agent._resolve_recent_speaker_user = fake_resolve_recent_speaker_user
    agent._submit_user_turn = fake_submit_user_turn

    asyncio.run(agent._handle_gemini_live_user_turn("please remember that I like air hockey"))

    assert len(submitted) == 1
    assert submitted[0]["text"] == "please remember that I like air hockey"
    assert submitted[0]["user_id"] == "user_008"
    assert submitted[0]["identity_resolution"] == "auto"
    assert submitted[0]["input_source"] == "gemini_live"


def test_desktop_audio_agent_gemini_live_turn_complete_flushes_partial_input():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_gemini_turn_complete_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    turns = []

    async def fake_handle_user_turn(text):
        turns.append(text)

    agent._gemini_live_input_text = "can you hear me"
    agent._last_partial_text = "can you hear me"
    agent._last_stable_partial_text = "can you hear me"
    agent.gemini_live_native_response_enabled = False
    agent._handle_gemini_live_user_turn = fake_handle_user_turn

    asyncio.run(agent._handle_gemini_live_server_content(SimpleNamespace(turn_complete=True)))

    assert turns == ["can you hear me"]
    assert agent._gemini_live_input_text == ""
    assert agent._last_partial_text == ""
    assert agent._last_stable_partial_text == ""


def test_desktop_audio_agent_gemini_live_suppresses_assistant_echo(monkeypatch: pytest.MonkeyPatch):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_gemini_echo_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    calls = []

    class FakeLoop:
        def call_soon_threadsafe(self, callback, *args):
            calls.append((callback, args))

    monkeypatch.setattr(audio_agent_module, "DEFAULT_GEMINI_LIVE_SUPPRESS_DURING_ASSISTANT", True)
    agent._loop = FakeLoop()
    agent._last_assistant_spoke_at = 0.0
    agent.is_assistant_speaking = lambda: True

    agent._handle_gemini_live_frame(np.ones(160, dtype=np.float32) * 0.1)

    assert calls == []


def test_desktop_audio_agent_google_command_asr_sends_phrase_hints(monkeypatch: pytest.MonkeyPatch):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_google_command_hints_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    grammar_module = _load_module("command_grammar_google_command_hints_module", PYTHON_VOICE_DIR / "command_grammar.py")
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent.capture_sample_rate = 16000
    agent._hotword_pack = audio_agent_module.HotwordPack(
        entries=[
            audio_agent_module.HotwordEntry(
                phrase="cornhole",
                aliases=["corn hole", "Bean Bag Toss"],
            )
        ]
    )
    agent._command_grammar = grammar_module.CommandGrammarMatcher.from_sources(
        launch_triggers=[],
        exit_keywords=[],
        manifest_path=str(INTENT_DIR / "manifest.json"),
    )
    requests = []

    class FakeResponse:
        status_code = 200
        content = b"{}"

        def json(self):
            return {"results": [{"alternatives": [{"transcript": "open corn hole", "confidence": 0.91}]}]}

    class FakeClient:
        async def post(self, url, **kwargs):
            requests.append((url, kwargs))
            return FakeResponse()

    agent._client = FakeClient()
    monkeypatch.setattr(audio_agent_module, "_google_cloud_speech_api_key", lambda: "test-key")

    transcript = asyncio.run(agent._transcribe_command_with_google_cloud(np.ones(1600, dtype=np.float32) * 0.1))

    assert transcript == "open corn hole"
    phrases = requests[0][1]["json"]["config"]["speechContexts"][0]["phrases"]
    assert "cornhole" in phrases
    assert "corn hole" in phrases
    assert "open corn hole" in phrases
    assert requests[0][1]["params"]["key"] == "test-key"


def test_desktop_audio_agent_google_command_asr_dispatches_game_launch():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_google_command_dispatch_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    grammar_module = _load_module("command_grammar_google_command_dispatch_module", PYTHON_VOICE_DIR / "command_grammar.py")
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    published = []
    logs = []
    recorded = []

    async def fake_transcribe(_audio):
        return "open corn hole"

    async def fake_publish(topic, payload):
        published.append((topic, payload))

    agent._listening = True
    agent.command_asr_enabled = True
    agent.command_asr_provider = "google-cloud"
    agent.current_asr_mode = audio_agent_module.STREAMING_ASR_MODE_GEMINI_LIVE
    agent._command_grammar = grammar_module.CommandGrammarMatcher.from_sources(
        launch_triggers=[],
        exit_keywords=[],
        manifest_path=str(INTENT_DIR / "manifest.json"),
    )
    agent._command_asr_status = ""
    agent._last_error = ""
    agent._last_native_command_dispatch_text = ""
    agent._last_native_command_dispatch_at = 0.0
    agent.log_store = SimpleNamespace(add=lambda *args, **kwargs: logs.append((args, kwargs)))
    agent._record_user_transcript_event = lambda text: recorded.append(text)
    agent._transcribe_command_with_google_cloud = fake_transcribe
    agent._publish_mqtt = fake_publish

    asyncio.run(agent._run_command_asr_turn(np.ones(1600, dtype=np.float32) * 0.1))

    assert recorded == ["open Bean Bag Toss"]
    assert published[0][0] == "robot/intent"
    assert published[0][1]["type"] == "LAUNCH_GAME"
    assert published[0][1]["game_name"] == "Bean Bag Toss"
    assert published[0][1]["source"] == "google_cloud_command_asr"
    assert published[0][1]["raw_text"] == "open corn hole"


def test_desktop_audio_agent_gemini_live_declares_command_tools():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_gemini_tool_declare_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    grammar_module = _load_module("command_grammar_gemini_tool_declare_module", PYTHON_VOICE_DIR / "command_grammar.py")
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent.gemini_live_command_tools_enabled = True
    agent._command_grammar = grammar_module.CommandGrammarMatcher.from_sources(
        launch_triggers=[],
        exit_keywords=[],
        manifest_path=str(INTENT_DIR / "manifest.json"),
    )

    tools = agent._gemini_live_command_tools()

    declarations = tools[0].function_declarations
    assert declarations[0].name == "launch_game"
    assert declarations[0].parameters_json_schema["properties"]["game_name"]["enum"] == [
        "Bean Bag Toss",
        "Disc Golf",
    ]
    assert declarations[1].name == "go_home"
    assert "cornhole" in agent._gemini_live_system_instruction().lower()


def test_desktop_audio_agent_gemini_live_declares_local_knowledge_tool():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_gemini_local_knowledge_declare_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent.gemini_live_command_tools_enabled = False
    agent.gemini_live_local_knowledge_enabled = True

    tools = agent._gemini_live_command_tools()

    declarations = tools[0].function_declarations
    assert declarations[0].name == "search_local_knowledge"
    assert "local knowledge" in agent._gemini_live_system_instruction().lower()


def test_desktop_audio_agent_gemini_live_declares_current_participant_tool():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_gemini_participant_identity_declare_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent.gemini_live_command_tools_enabled = False
    agent.gemini_live_local_knowledge_enabled = True
    agent._auto_identity_handler = lambda **_kwargs: {}

    tools = agent._gemini_live_command_tools()

    declarations = tools[0].function_declarations
    assert declarations[0].name == "get_current_participant"
    assert declarations[1].name == "search_participant_history"
    assert declarations[2].name == "search_local_knowledge"
    instruction = agent._gemini_live_system_instruction().lower()
    assert "do not use search_local_knowledge for the current participant's identity" in instruction


def test_desktop_audio_agent_identity_query_uses_current_profile_not_local_knowledge(monkeypatch, tmp_path: Path):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_gemini_participant_identity_call_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    memory_path = tmp_path / "user_memory.json"
    memory_path.write_text(
        json.dumps({"profiles": {"user_012": {"display_name": "Leo"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("DIALOG_USER_MEMORY_PATH", str(memory_path))
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._active_user_fallback = lambda: ("user_012", "operator_confirmed")

    result = asyncio.run(
        agent._execute_gemini_live_function_call(
            name="search_local_knowledge",
            args={"query": "What is my name?"},
        )
    )

    assert result["identified"] is True
    assert result["display_name"] == "Leo"
    assert result["spoken_text"] == "Your name is Leo."


def test_desktop_audio_agent_identity_query_reports_unknown_without_profile():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_gemini_participant_identity_unknown_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._active_user_fallback = lambda: ("", "none")

    result = asyncio.run(
        agent._execute_gemini_live_function_call(
            name="get_current_participant",
            args={},
        )
    )

    assert result["identified"] is False
    assert result["spoken_text"] == "I haven't identified you yet."


def test_desktop_audio_agent_gemini_live_local_knowledge_tool_calls_endpoint():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_gemini_local_knowledge_call_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    calls = []

    class FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "matched": True,
                "spoken_text": "Bean Bag Toss is the cornhole-style throwing game.",
                "doc_confidence": 0.82,
                "stage1_result": "doc_candidate",
                "stage2_result": "doc_answer",
                "doc_snippets": ["Bean Bag Toss uses bean bags and targets."],
                "doc_source_ids": ["games.md#1"],
            }

    class FakeClient:
        async def post(self, url, json=None, timeout=None):
            calls.append((url, json, timeout))
            return FakeResponse()

    agent.asr_base_url = "http://127.0.0.1:8000"
    agent._client = FakeClient()
    agent._active_user_fallback = lambda: ("user_001", "active_fallback")
    agent.log_store = SimpleNamespace(add=lambda *_args, **_kwargs: None)

    result = asyncio.run(
        agent._execute_gemini_live_function_call(
            name="search_local_knowledge",
            args={"query": "What is Bean Bag Toss?", "heard_text": "what is bean bag toss"},
        )
    )

    assert result["status"] == "ok"
    assert result["matched"] is True
    assert result["spoken_text"] == "Bean Bag Toss is the cornhole-style throwing game."
    assert result["user_id"] == "user_001"
    assert calls[0][0].endswith("/conversation/local-knowledge/query")
    assert calls[0][1]["text"] == "What is Bean Bag Toss?"
    assert calls[0][1]["user_id"] == "user_001"


def test_local_knowledge_query_endpoint_returns_probe_payload(monkeypatch: pytest.MonkeyPatch):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    api_module = _load_module(
        "api_routes_local_knowledge_endpoint_module",
        PYTHON_VOICE_DIR / "api_routes.py",
    )
    service_models = _load_module(
        "service_models_local_knowledge_endpoint_module",
        PYTHON_VOICE_DIR / "service_models.py",
    )

    class FakeProbe:
        stage1_result = "doc_candidate"
        stage2_result = "doc_answer"
        fallback_reason = ""
        doc_confidence = 0.82
        response_text = "Bean Bag Toss is a cornhole-style tossing game."
        payload = {
            "type": "doc_answer",
            "doc_snippets": ["Bean Bag Toss uses bean bags and a target board."],
            "doc_source_ids": ["games.md#bean-bag-toss"],
        }

        @staticmethod
        def telemetry():
            return {"stage1_result": "doc_candidate"}

    class FakeRag:
        ready = True
        error = ""

        @staticmethod
        def probe(text, **_kwargs):
            assert text == "What is Bean Bag Toss?"
            return FakeProbe()

    class FakeSessionStore:
        @staticmethod
        def capability_state(_user_id):
            return None

        @staticmethod
        def is_game_suppressed(_user_id):
            return False

        @staticmethod
        def game_state(_user_id):
            return None

    class FakeRuntime:
        local_docs_rag = FakeRag()
        session_store = FakeSessionStore()

        @staticmethod
        def ensure_ready():
            return None

        @staticmethod
        def _profile_snapshot(user_id=None):
            return {"user_id": user_id or ""}

    async def fake_get_runtime():
        return FakeRuntime()

    monkeypatch.setattr(api_module, "_get_unified_conversation_runtime", fake_get_runtime)
    request = service_models.LocalKnowledgeQueryRequest(
        text="What is Bean Bag Toss?",
        user_id="user_001",
        source="test",
        render=False,
    )

    response = asyncio.run(api_module.conversation_local_knowledge_query(request))

    assert response.status == "ok"
    assert response.matched is True
    assert response.spoken_text == "Bean Bag Toss is a cornhole-style tossing game."
    assert response.doc_snippets == ["Bean Bag Toss uses bean bags and a target board."]
    assert response.doc_source_ids == ["games.md#bean-bag-toss"]


def test_desktop_audio_agent_gemini_live_tool_launch_replaces_bad_transcript():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_gemini_tool_launch_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    grammar_module = _load_module("command_grammar_gemini_tool_launch_module", PYTHON_VOICE_DIR / "command_grammar.py")
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    published = []

    async def fake_publish(topic, payload):
        published.append((topic, payload))

    agent._command_grammar = grammar_module.CommandGrammarMatcher.from_sources(
        launch_triggers=[],
        exit_keywords=[],
        manifest_path=str(INTENT_DIR / "manifest.json"),
    )
    agent._last_gemini_tool_command_text = ""
    agent._last_gemini_tool_command_at = 0.0
    agent._last_native_command_dispatch_text = ""
    agent._last_native_command_dispatch_at = 0.0
    agent._last_reported_transcript_text = ""
    agent._last_reported_transcript_at = 0.0
    agent._last_final_transcript = ""
    agent._last_final_transcript_seq = 0
    agent.log_store = audio_agent_module.ConversationLogStore(limit=10)
    agent.log_store.add(
        "user",
        "ก็ คง ข้อ 6",
        speaker="User",
        source="desktop_audio",
        metadata="source=desktop_audio:gemini_live | native_response=1",
    )
    agent._publish_mqtt = fake_publish

    result = asyncio.run(
        agent._execute_gemini_live_function_call(
            name="launch_game",
            args={"game_name": "Bean Bag Toss", "heard_text": "open cornhole"},
        )
    )

    assert result["status"] == "ok"
    assert result["spoken_ack"] == "Opening Bean Bag Toss."
    assert published[0][1]["type"] == "LAUNCH_GAME"
    assert published[0][1]["game_name"] == "Bean Bag Toss"
    assert published[0][1]["source"] == "gemini_live_tool"
    entries = agent.log_store.snapshot()
    assert len(entries) == 1
    assert entries[0]["role"] == "user"
    assert entries[0]["message"] == "open Bean Bag Toss"
    assert "gemini_live_tool" in entries[0]["metadata"]


def test_desktop_audio_agent_gemini_live_bad_transcript_ignored_after_tool():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_gemini_bad_transcript_filter_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    submitted = []
    logs = []

    async def fake_resolve_recent_speaker_user(**_kwargs):
        return "", "none"

    async def fake_submit_user_turn(**kwargs):
        submitted.append(kwargs)

    agent._listening = True
    agent.gemini_live_native_response_enabled = True
    agent._gemini_live_last_input_text = ""
    agent._last_final_event_at = 0.0
    agent._last_gemini_tool_command_at = time.time() - 4.0
    agent._last_partial_text = ""
    agent._last_stable_partial_text = ""
    agent._command_grammar = SimpleNamespace(
        canonicalize=lambda text, allow_bare_game=False: SimpleNamespace(
            canonical_text=text,
            route_type="QUERY",
            game_name="",
            confidence=0.0,
        )
    )
    agent.log_store = SimpleNamespace(add=lambda *args, **kwargs: logs.append((args, kwargs)))
    agent._record_user_transcript_event = lambda _text: None
    agent._resolve_recent_speaker_user = fake_resolve_recent_speaker_user
    agent._submit_user_turn = fake_submit_user_turn

    asyncio.run(agent._handle_gemini_live_user_turn("ก็ คง ข้อ 6"))

    assert submitted == []
    assert logs[0][0][0] == "system"
    assert "ignored Gemini transcript" in logs[0][0][1]


def test_api_routes_gemini_cloud_provider_does_not_normalize_to_openai(monkeypatch: pytest.MonkeyPatch):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    api_routes = _load_module("api_routes_gemini_provider_module", PYTHON_VOICE_DIR / "api_routes.py")
    monkeypatch.setenv("VOICE_CONVERSATION_PROFILE", "cloud")
    monkeypatch.setenv("VOICE_CLOUD_RESPONSE_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(api_routes, "google_genai", object())
    monkeypatch.setattr(api_routes, "google_genai_types", object())

    assert api_routes._conversation_cloud_response_provider() == "gemini"
    assert api_routes._conversation_effective_response_provider("cloud") == "gemini"


def test_api_routes_cloud_provider_does_not_fallback_to_local_when_unready(monkeypatch: pytest.MonkeyPatch):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    api_routes = _load_module("api_routes_cloud_no_fallback_module", PYTHON_VOICE_DIR / "api_routes.py")
    monkeypatch.setenv("VOICE_CONVERSATION_PROFILE", "cloud")
    monkeypatch.setenv("VOICE_CLOUD_RESPONSE_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_KEY", raising=False)
    monkeypatch.setattr(api_routes, "google_genai", None)
    monkeypatch.setattr(api_routes, "google_genai_types", None)

    snapshot = api_routes._conversation_config_snapshot()

    assert snapshot["cloud_response_provider"] == "gemini"
    assert snapshot["effective_response_provider"] == "gemini"
    assert snapshot["gemini_configured"] is False
    assert snapshot["cloud_ready"] is False


def test_desktop_audio_agent_input_device_details_prefers_stream_device(monkeypatch: pytest.MonkeyPatch):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_input_device_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent._input_stream_lock = threading.Lock()
    agent._input_stream = SimpleNamespace(device=(5, 7))
    agent._selected_input_device_index = -1
    agent._selected_input_device_name = ""
    agent._selected_input_device_hostapi = ""
    agent._selected_input_device_source = ""

    fake_sd = SimpleNamespace(
        query_devices=lambda index: {"name": f"Mic {index}", "hostapi": 2},
        query_hostapis=lambda index=None: {"name": "Windows WASAPI"} if index == 2 else [],
        default=SimpleNamespace(device=(1, 2)),
    )
    monkeypatch.setattr(audio_agent_module, "sd", fake_sd)

    index, name, hostapi, source = agent._input_device_details()

    assert index == 5
    assert name == "Mic 5"
    assert hostapi == "Windows WASAPI"
    assert source == "stream_runtime"


def test_desktop_audio_agent_resolve_preferred_input_device_prefers_windows_wasapi(monkeypatch: pytest.MonkeyPatch):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_preferred_input_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)

    hostapis = [
        {"name": "MME", "default_input_device": 1},
        {"name": "Windows WASAPI", "default_input_device": 4},
    ]
    devices = {
        1: {"name": "Laptop Mic", "hostapi": 0, "max_input_channels": 1},
        4: {"name": "Webcam Mic", "hostapi": 1, "max_input_channels": 1},
    }
    fake_sd = SimpleNamespace(
        query_devices=lambda index=None: devices[index] if index is not None else list(devices.values()),
        query_hostapis=lambda index=None: hostapis[index] if index is not None else hostapis,
        default=SimpleNamespace(device=(1, 9)),
    )
    monkeypatch.setattr(audio_agent_module, "sd", fake_sd)
    monkeypatch.setattr(audio_agent_module, "DEFAULT_INPUT_DEVICE_NAME", "")
    monkeypatch.setattr(audio_agent_module, "DEFAULT_INPUT_DEVICE_INDEX", "")
    monkeypatch.setattr(audio_agent_module.os, "name", "nt", raising=False)

    index, name, hostapi, source = agent._resolve_preferred_input_device()

    assert index == 4
    assert name == "Webcam Mic"
    assert hostapi == "Windows WASAPI"
    assert source == "windows_default_wasapi"


def test_desktop_audio_agent_preferred_input_stream_config_uses_device_sample_rate(monkeypatch: pytest.MonkeyPatch):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_stream_config_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    agent.capture_sample_rate = 16000
    agent.input_blocksize = 160
    monkeypatch.setattr(
        agent,
        "_resolve_preferred_input_device",
        lambda: (4, "Webcam Mic", "Windows WASAPI", "windows_default_wasapi"),
    )
    monkeypatch.setattr(
        agent,
        "_device_details_for_index",
        lambda _index: (4, "Webcam Mic", "Windows WASAPI", 48000.0),
    )

    index, name, hostapi, source, sample_rate, blocksize = agent._preferred_input_stream_config()

    assert index == 4
    assert name == "Webcam Mic"
    assert hostapi == "Windows WASAPI"
    assert source == "windows_default_wasapi"
    assert sample_rate == 48000.0
    assert blocksize == 480


def test_desktop_audio_agent_input_callback_resamples_to_capture_rate():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_input_callback_resample_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    pushed = []
    agent._running = True
    agent._listening = True
    agent.current_asr_mode = "live-captions"
    agent.is_assistant_speaking = lambda: False
    agent._input_stream_sample_rate = 48000.0
    agent.capture_sample_rate = 16000
    agent._input_buffer = SimpleNamespace(push=lambda samples: pushed.append(np.asarray(samples, dtype=np.float32)))
    agent._last_error = ""

    indata = np.ones((480, 1), dtype=np.float32)
    agent._input_callback(indata, 480, None, None)

    assert len(pushed) == 1
    assert pushed[0].size == pytest.approx(160, abs=2)


def test_desktop_audio_agent_publish_face_normalizes_panel_payload():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_publish_face_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    published = []

    async def fake_publish(topic, payload):
        published.append((topic, payload))

    agent._publish_mqtt = fake_publish

    asyncio.run(agent.publish_face({"mode": "happy", "seconds": 3}))

    assert published == [("robot/pi/face/cmd", {"action": "face", "value": "happy:3"})]


def test_desktop_audio_agent_publish_led_normalizes_panel_payload():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_publish_led_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    published = []

    async def fake_publish(topic, payload):
        published.append((topic, payload))

    agent._publish_mqtt = fake_publish

    asyncio.run(
        agent.publish_led(
            {
                "mode": "breathe",
                "color": "#00BFFF",
                "brightness": 0.8,
                "period": 2.5,
                "duration": 3,
            }
        )
    )

    assert published == [
        (
            "robot/pi/led/cmd",
            {"action": "led", "value": "breathe:#00BFFF:3:0.8:2.5"},
        )
    ]


def test_desktop_audio_agent_publish_flower_normalizes_panel_payload():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_publish_flower_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    agent = audio_agent_module.DesktopAudioAgent.__new__(audio_agent_module.DesktopAudioAgent)
    published = []

    async def fake_publish(topic, payload):
        published.append((topic, payload))

    agent._publish_mqtt = fake_publish

    asyncio.run(agent.publish_flower({"action": "open_hold"}))

    assert published == [("robot/pi/servo/cmd", {"action": "servo", "value": "open"})]


def test_live_captions_source_defaults_to_hide_after_mic_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    monkeypatch.delenv("LIVE_CAPTIONS_MINIMIZE_WINDOW", raising=False)
    audio_agent_module = _load_module(
        "desktop_audio_agent_live_captions_hide_default_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )

    exe_path = tmp_path / "EnableLcMic.exe"
    exe_path.write_text("stub", encoding="utf-8")
    output_dir = tmp_path / "captions"
    launched = {"args": None}

    class DummyProcess:
        def poll(self):
            return None

        @property
        def stdout(self):
            return None

        @property
        def stderr(self):
            return None

    class DummyThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return None

    def fake_popen(args, **kwargs):
        launched["args"] = list(args)
        return DummyProcess()

    monkeypatch.setattr(audio_agent_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(audio_agent_module.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(audio_agent_module.threading, "Thread", DummyThread)

    source = audio_agent_module.LiveCaptionsTranscriptSource(
        exe_path=str(exe_path),
        output_dir=str(output_dir),
        on_caption=lambda *_args: None,
        on_error=lambda *_args: None,
    )
    source.start()

    assert audio_agent_module.DEFAULT_LIVE_CAPTIONS_MINIMIZE_WINDOW is True
    assert launched["args"] is not None
    assert "--headless" in launched["args"]
    assert "--show-live-captions" not in launched["args"]


def test_live_captions_source_exit_error_includes_recent_stderr_tail(tmp_path: Path):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_live_captions_exit_error_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )

    source = audio_agent_module.LiveCaptionsTranscriptSource(
        exe_path=str(tmp_path / "EnableLcMic.exe"),
        output_dir=str(tmp_path / "captions"),
        on_caption=lambda *_args: None,
        on_error=lambda *_args: None,
    )
    source._stderr_tail.extend(
        [
            "first line",
            "second line",
            "at MS.Internal.WindowsBase.NativeMethodsSetLastError.SetWindowLongPtrWndProc(...)",
            "final line",
        ]
    )

    message = source._build_exit_error_message(3221226525)

    assert "3221226525" in message
    assert "first line" not in message
    assert "second line" in message
    assert "at MS.Internal.WindowsBase.NativeMethodsSetLastError.SetWindowLongPtrWndProc(...)" in message
    assert "final line" in message


def test_live_captions_source_retries_visible_window_after_quick_hidden_crash(tmp_path: Path):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    audio_agent_module = _load_module(
        "desktop_audio_agent_live_captions_retry_module",
        PYTHON_VOICE_DIR / "desktop_audio_agent.py",
    )
    errors = []
    launches = []

    source = audio_agent_module.LiveCaptionsTranscriptSource(
        exe_path=str(tmp_path / "EnableLcMic.exe"),
        output_dir=str(tmp_path / "captions"),
        on_caption=lambda *_args: None,
        on_error=lambda message: errors.append(message),
    )
    source._launch_started_at = time.monotonic()
    source._current_show_live_captions = False
    source._visible_retry_attempted = False
    source._launch_process = lambda *, show_live_captions, restart_reason: launches.append(
        (show_live_captions, restart_reason)
    )

    restarted = source._restart_visible_fallback("live captions listener exited with code 3221226525")

    assert restarted is True
    assert launches == [(True, "retrying live captions with visible window")]
    assert errors
    assert "retrying with visible Live Captions window" in errors[-1]


def test_local_service_templates_default_to_live_captions_streaming_modes():
    default_payload = json.loads((ROOT / "scripts" / "local_services.default.json").read_text(encoding="utf-8-sig"))
    sample_payload = json.loads((ROOT / "scripts" / "local_services.user.sample.json").read_text(encoding="utf-8-sig"))

    assert default_payload["env"]["VOICE_LOCAL_STREAMING_ASR_MODE"] == "live-captions"
    assert default_payload["env"]["VOICE_CLOUD_STREAMING_ASR_MODE"] == "live-captions"
    assert sample_payload["env"]["VOICE_LOCAL_STREAMING_ASR_MODE"] == "live-captions"
    assert sample_payload["env"]["VOICE_CLOUD_STREAMING_ASR_MODE"] == "live-captions"


def test_desktop_runtime_clear_speaker_profile_for_user(tmp_path: Path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")

    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    runtime_module = _load_module("desktop_runtime_speaker_cleanup_module", PYTHON_VOICE_DIR / "desktop_runtime.py")
    memory_path = tmp_path / "user_memory.json"
    speaker_profiles_path = tmp_path / "speaker_profiles.json"
    memory_path.write_text(json.dumps({"version": 1, "next_user_index": 1, "identity_map": {}, "profiles": {}}, indent=2), encoding="utf-8")
    speaker_profiles_path.write_text(
        json.dumps(
            {
                "version": 1,
                "users": {
                    "user_001": {"clip_count": 3, "centroid": [1.0, 0.0], "updated_ts": 1.0},
                    "user_002": {"clip_count": 3, "centroid": [0.0, 1.0], "updated_ts": 2.0},
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    assert runtime_module._clear_speaker_profile_for_user(memory_path, "user_001") is True
    payload = json.loads(speaker_profiles_path.read_text(encoding="utf-8"))
    assert "user_001" not in payload["users"]
    assert "user_002" in payload["users"]
    assert runtime_module._clear_speaker_profile_for_user(memory_path, "missing") is False


def test_desktop_runtime_clears_unlinked_runtime_speaker_profiles(monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")

    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    runtime_module = _load_module(
        "desktop_runtime_unlinked_speaker_profiles_module",
        PYTHON_VOICE_DIR / "desktop_runtime.py",
    )
    cleared = []

    class FakeAudioAgent:
        async def speaker_profiles_status(self):
            return {
                "users": [
                    {"user_id": "user_001"},
                    {"user_id": "user_008"},
                    {"user_id": "__guest__"},
                ]
            }

        async def clear_speaker_profile(self, *, user_id):
            cleared.append(user_id)
            return {"user_id": user_id, "has_profile": False}

    monkeypatch.setattr(runtime_module, "audio_agent", FakeAudioAgent())

    result = asyncio.run(runtime_module._clear_unlinked_runtime_speaker_profiles({"user_001"}))

    assert result == ["user_008"]
    assert cleared == ["user_008"]


def test_desktop_runtime_migrates_guest_turns_into_confirmed_memory(tmp_path: Path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")

    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    runtime_module = _load_module("desktop_runtime_guest_migration_module", PYTHON_VOICE_DIR / "desktop_runtime.py")
    memory_path = tmp_path / "user_memory.json"
    memory_path.write_text(
        json.dumps(
            {
                "version": 1,
                "next_user_index": 2,
                "identity_map": {},
                "profiles": {
                    "user_001": runtime_module._normalize_memory_profile(
                        "user_001",
                        {"display_name": "Lio"},
                        None,
                    )
                },
            }
        ),
        encoding="utf-8",
    )

    migrated = runtime_module._migrate_guest_turns_to_memory(
        memory_path,
        "user_001",
        [{"role": "user", "text": "I really enjoy disc golf.", "ts": 123.0}],
    )

    payload = json.loads(memory_path.read_text(encoding="utf-8"))
    profile = payload["profiles"]["user_001"]
    assert migrated == 1
    assert profile["dialog_turns"][-1]["text"] == "I really enjoy disc golf."
    assert set(profile) == {"display_name", "dialog_turns"}


def test_desktop_runtime_memory_payload_lists_unlinked_speaker_profiles(tmp_path: Path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")

    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))

    runtime_module = _load_module("desktop_runtime_unlinked_speaker_module", PYTHON_VOICE_DIR / "desktop_runtime.py")
    memory_path = tmp_path / "user_memory.json"
    memory_path.write_text(
        json.dumps(
            {
                "version": 1,
                "next_user_index": 2,
                "identity_map": {},
                "profiles": {"user_001": {"display_name": "Known User"}},
            }
        ),
        encoding="utf-8",
    )
    memory_path.with_name("speaker_profiles.json").write_text(
        json.dumps(
            {
                "version": 1,
                "users": {
                    "user_001": {"clip_count": 3, "centroid": [1.0, 0.0]},
                    "user_002": {"clip_count": 4, "centroid": [0.0, 1.0], "updated_ts": 12.0},
                },
            }
        ),
        encoding="utf-8",
    )

    payload = runtime_module._memory_payload(memory_path, "", "loaded")

    assert [item["user_id"] for item in payload["users"]] == ["user_001"]
    assert payload["unlinked_speaker_profiles"] == [
        {"user_id": "user_002", "clip_count": 4, "updated_ts": 12.0}
    ]
