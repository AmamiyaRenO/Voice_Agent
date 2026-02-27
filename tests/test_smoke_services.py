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
