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
    assert below_threshold.reason == "below_threshold"

    service._profiles = {
        "alice": {"centroid": speaker_id._normalize_embedding(_unit_vector(0.95, math.sqrt(1.0 - 0.95**2)))},
        "bob": {"centroid": speaker_id._normalize_embedding(_unit_vector(0.92, math.sqrt(1.0 - 0.92**2)))},
    }
    below_margin = service.match_audio(dummy_audio, 16000)
    assert below_margin.matched is False
    assert below_margin.top1_score >= config.match_threshold
    assert below_margin.margin < config.match_margin

    service._profiles = {
        "alice": {"centroid": speaker_id._normalize_embedding(_unit_vector(0.96, math.sqrt(1.0 - 0.96**2)))},
        "bob": {"centroid": speaker_id._normalize_embedding(_unit_vector(0.20, math.sqrt(1.0 - 0.20**2)))},
    }
    matched = service.match_audio(dummy_audio, 16000)
    assert matched.matched is True
    assert matched.user_id == "alice"
    assert matched.reason == "matched"


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
        canonicalize=lambda text: SimpleNamespace(
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


def test_desktop_audio_agent_live_captions_can_use_fallback_segment_window():
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

    assert user_id == "user_003"
    assert identity_resolution == "auto"
    assert agent._active_user_id == "user_003"
    assert agent._last_speaker_match["matched"] is True
    assert agent._last_speaker_match["segment_used_fallback_window"] is True
    assert agent._last_speaker_match["candidate_count"] >= 2
    assert older_voice.caption_uses == 1
