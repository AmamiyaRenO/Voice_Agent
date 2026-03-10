import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
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


def test_smoke_dialog_user_memory_persists_identity(tmp_path: Path):
    if str(DIALOG_DIR) not in sys.path:
        sys.path.insert(0, str(DIALOG_DIR))

    user_memory = _load_module("smoke_dialog_user_memory", DIALOG_DIR / "user_memory.py")
    memory_path = tmp_path / "user_memory.json"

    store_a = user_memory.UserMemoryStore(
        path=str(memory_path),
        max_notes=8,
        prompt_max_chars=360,
        embedder=None,
        retrieve_top_k=2,
    )
    user_id_a = store_a.resolve_user("moonshine:0:11")
    store_a.remember_utterance(user_id_a, "my name is alex")

    store_b = user_memory.UserMemoryStore(
        path=str(memory_path),
        max_notes=8,
        prompt_max_chars=360,
        embedder=None,
        retrieve_top_k=2,
    )
    user_id_b = store_b.resolve_user("moonshine:0:11")
    context = store_b.build_memory_context(user_id_b, query_text="hello")

    assert user_id_a == user_id_b
    assert "Preferred name: Alex." in context


def test_smoke_dialog_relevant_memory_retrieval(tmp_path: Path):
    if str(DIALOG_DIR) not in sys.path:
        sys.path.insert(0, str(DIALOG_DIR))

    user_memory = _load_module("smoke_dialog_retrieval", DIALOG_DIR / "user_memory.py")
    np = pytest.importorskip("numpy")

    class DummyEmbedder:
        ready = True

        def doc_embedding(self, text):
            lower = text.lower()
            if "cornhole" in lower:
                return np.asarray([1.0, 0.0], dtype=np.float32)
            if "disc golf" in lower:
                return np.asarray([0.0, 1.0], dtype=np.float32)
            return np.asarray([0.6, 0.6], dtype=np.float32)

        def query_embedding(self, text):
            return self.doc_embedding(text)

    store = user_memory.UserMemoryStore(
        path=str(tmp_path / "user_memory.json"),
        max_notes=8,
        prompt_max_chars=360,
        embedder=DummyEmbedder(),
        retrieve_top_k=2,
    )
    user_id = store.resolve_user("moonshine:0:21")
    store.remember_utterance(user_id, "I like cornhole")
    store.remember_utterance(user_id, "disc golf is easy")

    context = store.build_memory_context(user_id, query_text="let us play cornhole")
    assert "Relevant memory:" in context
    assert "cornhole" in context.lower()


def test_smoke_intent_router_core_paths(tmp_path: Path):
    pytest.importorskip("paho.mqtt.client")
    pytest.importorskip("yaml")

    if str(INTENT_DIR) not in sys.path:
        sys.path.insert(0, str(INTENT_DIR))
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))

    intent_main = _load_module("smoke_intent_main_router", INTENT_DIR / "main.py")

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "games": [
                    {"name": "Disc Golf", "synonyms": ["disc golf", "frisbee golf"]},
                    {"name": "Cornhole", "synonyms": ["corn hole", "bean bag toss"]},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    cfg = intent_main.Config(
        require_wake_word=False,
        manifest_path=str(manifest_path),
        use_llm_classifier=False,
        use_moonshine_intent_recognizer=False,
    )
    resolver = intent_main.ManifestAliasResolver(cfg.manifest_path)
    router = intent_main.IntentRouterEngine(cfg, resolver)
    try:
        launch = router.route("disc golf", "c1")
        back = router.route("back home", "c2")
        query = router.route("what is the score", "c3")
    finally:
        router.close()

    assert launch.payload["type"] == "LAUNCH_GAME"
    assert launch.payload["game_name"] == "Disc Golf"
    assert back.payload["type"] == "BACK_HOME"
    assert query.payload["type"] == "QUERY"


def test_smoke_intent_identity_passthrough():
    pytest.importorskip("paho.mqtt.client")
    pytest.importorskip("yaml")

    if str(INTENT_DIR) not in sys.path:
        sys.path.insert(0, str(INTENT_DIR))
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))

    intent_main = _load_module("smoke_intent_main_identity", INTENT_DIR / "main.py")

    cfg = intent_main.Config(
        require_wake_word=False,
        use_llm_classifier=False,
        use_moonshine_intent_recognizer=False,
    )
    service = intent_main.IntentService(cfg)

    published = []

    class DummyClient:
        def publish(self, topic, payload):
            published.append((topic, payload))

    class DummyMsg:
        topic = cfg.topics.voice_text
        payload = json.dumps(
            {
                "text": "back home",
                "corr_id": "corr-smoke-1",
                "speaker_index": 1,
                "speaker_id": 42,
            }
        ).encode("utf-8")

    service.client = DummyClient()
    service._on_message(None, None, DummyMsg())
    service._router.close()

    assert len(published) == 1
    topic, payload = published[0]
    node = json.loads(payload)
    assert topic == cfg.topics.intent
    assert node.get("type") == "BACK_HOME"
    assert node.get("speaker_index") == 1
    assert node.get("speaker_id") == 42


def test_smoke_dialog_context_summary_and_slots(tmp_path: Path):
    if str(DIALOG_DIR) not in sys.path:
        sys.path.insert(0, str(DIALOG_DIR))

    user_memory = _load_module("smoke_dialog_context_memory", DIALOG_DIR / "user_memory.py")
    store = user_memory.UserMemoryStore(
        path=str(tmp_path / "user_memory.json"),
        max_notes=8,
        prompt_max_chars=420,
        embedder=None,
        retrieve_top_k=2,
    )
    user_id = store.resolve_user("moonshine:2:9")

    for i in range(12):
        role = "user" if i % 2 == 0 else "assistant"
        store.remember_dialog_turn(
            user_id,
            role,
            f"turn-{i} this is a test message for dialogue continuity",
            max_turns=6,
            summary_max_chars=240,
        )

    store.update_dialog_slots(
        user_id,
        current_topic="balance training schedule",
        open_question="Do you prefer morning or evening practice",
    )

    slots = store.get_dialog_slots(user_id)
    dialog_context = store.build_dialog_context(user_id, max_turns=6, max_chars=600)

    assert slots["current_topic"] == "balance training schedule"
    assert slots["open_question"].startswith("Do you prefer morning or evening practice")
    assert "Conversation summary:" in dialog_context
    assert "Current topic: balance training schedule." in dialog_context
    assert "Recent dialogue:" in dialog_context
    assert len(dialog_context) <= 600


def test_smoke_dialog_schedule_preference_memory(tmp_path: Path):
    if str(DIALOG_DIR) not in sys.path:
        sys.path.insert(0, str(DIALOG_DIR))

    user_memory = _load_module("smoke_dialog_schedule_memory", DIALOG_DIR / "user_memory.py")
    store = user_memory.UserMemoryStore(
        path=str(tmp_path / "user_memory.json"),
        max_notes=8,
        prompt_max_chars=420,
        embedder=None,
        retrieve_top_k=2,
    )
    user_id = store.resolve_user("moonshine:7:33")
    store.remember_utterance(user_id, "I want to plan rehab for tomorrow.")
    store.remember_utterance(user_id, "Morning is better.")
    store.remember_utterance(user_id, "What about Friday then?")

    context = store.build_memory_context(user_id, query_text="which day do I prefer")
    facts = store.build_facts_reply(user_id)
    assert "Preferred training day: Friday." in context
    assert "Preferred training time: morning." in context
    assert "you prefer training on Friday" in facts
    assert "you prefer training in the morning" in facts


def test_smoke_dialog_policy_classifier():
    pytest.importorskip("paho.mqtt.client")

    if str(DIALOG_DIR) not in sys.path:
        sys.path.insert(0, str(DIALOG_DIR))

    dialog_cfg = _load_module("smoke_dialog_policy_cfg", DIALOG_DIR / "dialog_config.py")
    dialog_impl = _load_module("smoke_dialog_policy_impl", DIALOG_DIR / "dialog_service_impl.py")

    cfg = dialog_cfg.Config(
        enable_user_memory=False,
        enable_dialog_context=True,
        enable_dialog_policy=True,
    )
    service = dialog_impl.DialogService(cfg)
    try:
        assert (
            service._classify_dialog_policy(
                user_text="what about tomorrow morning then?",
                current_topic="rehab schedule",
                open_question="",
            )
            == "continue_topic"
        )
        assert (
            service._classify_dialog_policy(
                user_text="by the way, switch topic to music",
                current_topic="rehab schedule",
                open_question="",
            )
            == "switch_topic"
        )
        assert (
            service._classify_dialog_policy(
                user_text="why?",
                current_topic="",
                open_question="",
            )
            == "ask_clarify"
        )
        assert (
            service._classify_dialog_policy(
                user_text="morning is better",
                current_topic="rehab schedule",
                open_question="Do you prefer morning or evening practice?",
            )
            == "continue_topic"
        )
    finally:
        service.http.close()


def test_smoke_dialog_request_includes_context_and_policy(tmp_path: Path):
    pytest.importorskip("paho.mqtt.client")

    if str(DIALOG_DIR) not in sys.path:
        sys.path.insert(0, str(DIALOG_DIR))

    dialog_cfg = _load_module("smoke_dialog_ctx_cfg", DIALOG_DIR / "dialog_config.py")
    dialog_impl = _load_module("smoke_dialog_ctx_impl", DIALOG_DIR / "dialog_service_impl.py")

    cfg = dialog_cfg.Config(
        enable_user_memory=True,
        enable_user_memory_embeddings=False,
        user_memory_path=str(tmp_path / "user_memory.json"),
        enable_dialog_context=True,
        enable_dialog_policy=True,
        dialog_history_turns=6,
        dialog_summary_max_chars=300,
        dialog_context_max_chars=700,
    )
    service = dialog_impl.DialogService(cfg)

    posted = []
    published = []

    class DummyResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class DummyHttp:
        def __init__(self):
            self.calls = 0

        def post(self, url, json):
            posted.append((url, json))
            self.calls += 1
            if self.calls == 1:
                return DummyResponse({"text": "Great. Do you prefer morning or evening practice?"})
            return DummyResponse({"text": "Morning works well. We will keep this schedule."})

        def close(self):
            return None

    class DummyClient:
        def publish(self, topic, payload):
            published.append((topic, payload))

    class DummyMsg:
        def __init__(self, payload_json: str):
            self.topic = cfg.topics.dialog_query
            self.payload = payload_json.encode("utf-8")

    service.client = DummyClient()
    service.http = DummyHttp()

    service._on_message(
        None,
        None,
        DummyMsg(
            json.dumps(
                {
                    "text": "I want to plan rehab for tomorrow",
                    "corr_id": "c1",
                    "speaker_index": 1,
                    "speaker_id": 7,
                }
            )
        ),
    )
    service._on_message(
        None,
        None,
        DummyMsg(
            json.dumps(
                {
                    "text": "morning works",
                    "corr_id": "c2",
                    "speaker_index": 1,
                    "speaker_id": 7,
                }
            )
        ),
    )

    assert len(posted) == 2
    first_body = posted[0][1]
    second_body = posted[1][1]
    assert first_body.get("dialog_policy") in {"switch_topic", "continue_topic", "ask_clarify"}
    assert "dialog_context" in second_body
    assert second_body.get("dialog_policy") == "continue_topic"
    assert second_body.get("open_question", "").startswith("Do you prefer")
    assert len(published) == 2


def test_smoke_dialog_memory_query_variants():
    pytest.importorskip("paho.mqtt.client")

    if str(DIALOG_DIR) not in sys.path:
        sys.path.insert(0, str(DIALOG_DIR))

    dialog_cfg = _load_module("smoke_dialog_memq_cfg", DIALOG_DIR / "dialog_config.py")
    dialog_impl = _load_module("smoke_dialog_memq_impl", DIALOG_DIR / "dialog_service_impl.py")

    cfg = dialog_cfg.Config(enable_user_memory=False)
    service = dialog_impl.DialogService(cfg)
    try:
        assert service._is_memory_query("what do you remember about me")
        assert service._is_memory_query("Can you remind me what you remember about me?")
        assert service._is_memory_query("remind me what you know about me")
        assert service._is_memory_query("which day do I prefer")
        assert service._is_memory_query("do I prefer morning or evening")
        assert not service._is_memory_query("can we play disc golf now")
    finally:
        service.http.close()


def test_smoke_dialog_memory_query_semantic_fallback():
    pytest.importorskip("paho.mqtt.client")

    if str(DIALOG_DIR) not in sys.path:
        sys.path.insert(0, str(DIALOG_DIR))

    dialog_cfg = _load_module("smoke_dialog_memq_sem_cfg", DIALOG_DIR / "dialog_config.py")
    dialog_impl = _load_module("smoke_dialog_memq_sem_impl", DIALOG_DIR / "dialog_service_impl.py")

    class DummyEmbedder:
        ready = True

        def query_embedding(self, text):
            return str(text)

    original_cosine = dialog_impl.cosine_similarity
    dialog_impl.cosine_similarity = lambda q, p: 0.78 if "goal" in str(q).lower() else 0.08
    cfg = dialog_cfg.Config(
        enable_user_memory=False,
        memory_query_rule=False,
        memory_query_semantic=True,
        memory_query_threshold=0.42,
    )
    service = dialog_impl.DialogService(cfg)
    try:
        service.user_memory_embedder = DummyEmbedder()
        service._memory_query_semantic_vectors = [
            ("Can you remind me of my goals?", "prototype-goals"),
            ("What do you remember about me?", "prototype-profile"),
        ]
        assert service._is_memory_query("can you summarize my rehab goals")
        assert not service._is_memory_query("can we play disc golf now")
    finally:
        dialog_impl.cosine_similarity = original_cosine
        service.http.close()


def test_smoke_dialog_vision_query_variants():
    pytest.importorskip("paho.mqtt.client")

    if str(DIALOG_DIR) not in sys.path:
        sys.path.insert(0, str(DIALOG_DIR))

    dialog_cfg = _load_module("smoke_dialog_visq_cfg", DIALOG_DIR / "dialog_config.py")
    dialog_impl = _load_module("smoke_dialog_visq_impl", DIALOG_DIR / "dialog_service_impl.py")

    cfg = dialog_cfg.Config(enable_user_memory=False)
    service = dialog_impl.DialogService(cfg)
    try:
        assert service._is_vision_query("what can you see")
        assert service._is_vision_query("Can you see me right now?")
        assert service._is_vision_query("describe what you see")
        assert service._is_vision_query("你能看到什么")
        assert not service._is_vision_query("I can see why this matters")
        assert not service._is_vision_query("can we play disc golf now")
    finally:
        service.http.close()


def test_smoke_dialog_vision_query_short_circuit_publish():
    pytest.importorskip("paho.mqtt.client")

    if str(DIALOG_DIR) not in sys.path:
        sys.path.insert(0, str(DIALOG_DIR))

    dialog_cfg = _load_module("smoke_dialog_visroute_cfg", DIALOG_DIR / "dialog_config.py")
    dialog_impl = _load_module("smoke_dialog_visroute_impl", DIALOG_DIR / "dialog_service_impl.py")

    cfg = dialog_cfg.Config(enable_user_memory=False, enable_vision_query=True)
    service = dialog_impl.DialogService(cfg)
    published = []

    class DummyClient:
        def publish(self, topic, payload):
            published.append((topic, payload))

    class DummyMsg:
        topic = cfg.topics.dialog_query
        payload = json.dumps({"text": "what can you see", "corr_id": "c-vis-1"}).encode("utf-8")

    service.client = DummyClient()
    service._request_vision_description = lambda: "I can see a monitor and a keyboard."
    try:
        service._on_message(None, None, DummyMsg())
        assert len(published) == 1
        topic, payload = published[0]
        node = json.loads(payload)
        assert topic == cfg.topics.dialog_answer
        assert "monitor" in node.get("text", "").lower()
        assert node.get("corr_id") == "c-vis-1"
    finally:
        service.http.close()


def test_smoke_dialog_switch_topic_reduces_context(tmp_path: Path):
    pytest.importorskip("paho.mqtt.client")

    if str(DIALOG_DIR) not in sys.path:
        sys.path.insert(0, str(DIALOG_DIR))

    dialog_cfg = _load_module("smoke_dialog_switch_cfg", DIALOG_DIR / "dialog_config.py")
    dialog_impl = _load_module("smoke_dialog_switch_impl", DIALOG_DIR / "dialog_service_impl.py")

    cfg = dialog_cfg.Config(
        enable_user_memory=True,
        enable_user_memory_embeddings=False,
        user_memory_path=str(tmp_path / "user_memory.json"),
        enable_dialog_context=True,
        enable_dialog_policy=True,
        dialog_history_turns=8,
        dialog_summary_max_chars=420,
        dialog_context_max_chars=900,
    )
    service = dialog_impl.DialogService(cfg)
    try:
        user_id = service.user_memory.resolve_user("moonshine:1:55")
        service.user_memory.update_dialog_slots(
            user_id,
            current_topic="rehab schedule",
            open_question="Do you prefer morning or evening practice?",
        )
        for i in range(10):
            role = "user" if i % 2 == 0 else "assistant"
            service.user_memory.remember_dialog_turn(
                user_id,
                role,
                f"rehab context turn {i}",
                max_turns=8,
                summary_max_chars=420,
            )

        result = service._build_dialog_request_context(
            user_id=user_id,
            user_text="Actually, switch topic to sleep quality.",
        )
        assert result["dialog_policy"] == "switch_topic"
        assert "sleep quality" in result["current_topic"]
        assert result["open_question"] == ""
        assert len(result["dialog_context"]) <= 420
    finally:
        service.http.close()
