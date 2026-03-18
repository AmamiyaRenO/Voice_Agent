import asyncio
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
PYTHON_VOICE_DIR = ROOT / "python_voice_service"
SCRIPTS_DIR = ROOT / "scripts" / "dialog_service"
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "dialogue_regression_corpus.json"
REPORT_JSON = ROOT / "runtime" / "evals" / "latest_dialogue_regression_report.json"
REPORT_MD = ROOT / "runtime" / "evals" / "latest_dialogue_regression_report.md"


def _load_module(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load module spec: {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class DummyEmbedder:
    ready = True

    def _vector(self, text: str):
        lower = str(text or "").lower()
        if any(term in lower for term in ("john", "munoz", "director", "team", "researcher", "collaborator")):
            return np.asarray([1.0, 0.2, 0.1, 0.0], dtype=np.float32)
        if any(term in lower for term in ("bioadaptive", "interface lab", "wilfrid", "brantford")):
            return np.asarray([0.8, 1.0, 0.2, 0.0], dtype=np.float32)
        if any(term in lower for term in ("social robotics", "research", "serious games", "vr", "virtual reality")):
            return np.asarray([0.2, 0.9, 1.0, 0.0], dtype=np.float32)
        if any(term in lower for term in ("eeg", "emg", "headset", "kinect", "equipment", "tools", "device")):
            return np.asarray([0.1, 0.3, 1.0, 0.5], dtype=np.float32)
        if "disc golf" in lower or "basket" in lower or "walking" in lower:
            return np.asarray([0.0, 0.0, 0.2, 1.0], dtype=np.float32)
        if "bean bag toss" in lower or "cornhole" in lower or "board" in lower:
            return np.asarray([0.0, 0.0, 0.1, 0.95], dtype=np.float32)
        if "balance quest" in lower or "balance" in lower:
            return np.asarray([0.0, 0.0, 0.5, 0.8], dtype=np.float32)
        return np.asarray([0.1, 0.1, 0.1, 0.1], dtype=np.float32)

    def doc_embedding(self, text: str):
        return self._vector(text)

    def query_embedding(self, text: str):
        return self._vector(text)


def _write_manifest(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "games": [
                    {
                        "id": "cornhole",
                        "name": "Bean Bag Toss",
                        "synonyms": ["bean bag toss", "cornhole"],
                        "description": "Bean Bag Toss is a tossing game where players aim bean bags at a raised board.",
                        "how_to_play": "Players take turns throwing bean bags and score by landing on the board or through the hole.",
                        "players_min": 1,
                        "players_max": 2,
                        "tags": ["coordination", "throwing"],
                        "activity_level": "low",
                        "recommendation_weight": 0.91,
                        "exec": "games/bean_bag_toss.exe"
                    },
                    {
                        "id": "disc_golf",
                        "name": "Disc Golf",
                        "synonyms": ["disc golf", "disk golf"],
                        "description": "Disc Golf is a throwing game where players aim discs at basket targets.",
                        "how_to_play": "Players throw discs toward the target and try to finish in as few throws as possible.",
                        "players_min": 1,
                        "players_max": 4,
                        "tags": ["walking", "throwing"],
                        "activity_level": "medium",
                        "recommendation_weight": 0.82,
                        "exec": "games/disc_golf.exe"
                    },
                    {
                        "id": "balance_quest",
                        "name": "Balance Quest",
                        "synonyms": ["balance quest"],
                        "description": "Balance Quest is a guided balance game with standing tasks.",
                        "how_to_play": "Players follow standing balance prompts and hold each pose.",
                        "players_min": 1,
                        "players_max": 1,
                        "tags": ["balance", "stability"],
                        "activity_level": "low",
                        "recommendation_weight": 0.77,
                        "exec": "games/balance_quest.exe"
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_lab_docs(qmd_root: Path) -> None:
    docs_dir = qmd_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "01_lab_identity.md").write_text(
        "\n".join(
            [
                "# BioAdaptive Interface Lab",
                "",
                "Lab name: BioAdaptive Interface Lab.",
                "Institution: Wilfrid Laurier University.",
                "Location: Brantford, Ontario, Canada.",
                "The BioAdaptive Interface Lab studies the intersection of human physiology and digital interaction.",
                "Research vision: serious games, virtual reality simulations, social robotics, and physiologically adaptive interfaces.",
                "Director: John E. Munoz.",
            ]
        ),
        encoding="utf-8",
    )
    (docs_dir / "02_research.md").write_text(
        "\n".join(
            [
                "---",
                "section: research",
                "title: BioAdaptive Interface Lab Research",
                "---",
                "",
                "# Research",
                "",
                "Research vision: The lab studies serious games, virtual reality, social robotics, and physiologically adaptive interfaces.",
                "Project: Robo Ludens explores game design in social robotics.",
            ]
        ),
        encoding="utf-8",
    )
    (docs_dir / "03_team.md").write_text(
        "\n".join(
            [
                "---",
                "section: team",
                "title: Team Active Members And Collaborators",
                "---",
                "",
                "# Team",
                "",
                "Name: John E. Munoz.",
                "Role: Director of BioAdaptive Interface Lab.",
                "Academic position: Assistant Professor of User Experience Design, Liberal Arts, Wilfrid Laurier University.",
                "- Nahomi Ramirez - Master student working on Active Aging Through Play.",
                "- Natalia Luciani - Undergraduate student working on VR for wellbeing.",
                "- Aaron Jumarang - Undergraduate student working on Biofeedback Gaming.",
            ]
        ),
        encoding="utf-8",
    )
    (docs_dir / "04_equipment.md").write_text(
        "\n".join(
            [
                "---",
                "section: equipment",
                "title: Equipment and Tools",
                "---",
                "",
                "# Equipment and Tools",
                "",
                "The lab has EEG systems, EMG armbands, VR headsets, Kinect devices, and exergaming kits.",
            ]
        ),
        encoding="utf-8",
    )
    (docs_dir / "05_news.md").write_text(
        "\n".join(
            [
                "---",
                "section: news_updates",
                "title: BioAdaptive Interface Lab - News and Updates",
                "---",
                "",
                "# News and Updates",
                "",
                "- March 2026: Promotional video of the BioAdaptive Interface Lab released.",
                "- January 2026: Article on designing a tabletop social companion called RACHEL.",
            ]
        ),
        encoding="utf-8",
    )
    (docs_dir / "README.md").write_text(
        "# Query Examples\n\n- What is the BioAdaptive Interface Lab?\n- What equipment does the lab have?\n",
        encoding="utf-8",
    )
    (docs_dir / "entity_aliases.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "canonical": "BioAdaptive Interface Lab",
                        "domain": "general",
                        "entity_type": "lab",
                        "alias_strength": "strong",
                        "aliases": [
                            "bio adaptive interface lab",
                            "bio adaptive interface",
                            "bioadactive interface lab",
                            "bio ductive interface lab"
                        ]
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    games_dir = qmd_root / "games"
    games_dir.mkdir(parents=True, exist_ok=True)
    (games_dir / "disc_golf.qmd").write_text(
        "\n".join(
            [
                "---",
                "type: game_card",
                "name: Disc Golf",
                "---",
                "",
                "# Disc Golf",
                "",
                "Disc Golf is the walking-focused throwing option with basket targets.",
                "",
                "## How To Play",
                "Throw discs toward the basket and finish each hole in as few throws as possible.",
            ]
        ),
        encoding="utf-8",
    )


def _iter_events(generator):
    async def _collect():
        items = []
        async for raw in generator:
            items.append(json.loads(raw.decode("utf-8")))
        return items

    return asyncio.run(_collect())


def _build_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))

    api_routes = _load_module(f"dialogue_regression_api_{tmp_path.name}", PYTHON_VOICE_DIR / "api_routes.py")
    game_grounding = _load_module(f"dialogue_regression_grounding_{tmp_path.name}", PYTHON_VOICE_DIR / "game_grounding.py")
    local_docs_rag = _load_module(f"dialogue_regression_rag_{tmp_path.name}", PYTHON_VOICE_DIR / "local_docs_rag.py")
    session_context = _load_module(f"dialogue_regression_session_{tmp_path.name}", PYTHON_VOICE_DIR / "session_context.py")

    async def _noop_dispatch(*_args, **_kwargs):
        return None

    async def _fake_summary(_user_text, payload):
        return str(payload.get("summary_text") or payload.get("text") or "").strip()

    async def _fake_render(_user_text, payload):
        return str(payload.get("summary_text") or payload.get("text") or "").strip()

    def _fake_general_guard(text: str) -> str:
        normalized = " ".join(re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split())
        if normalized == "can you hear me":
            return "Yes, I can hear you."
        return ""

    monkeypatch.setattr(api_routes, "_dispatch_command_intent", _noop_dispatch)
    monkeypatch.setattr(api_routes, "_generate_local_doc_summary", _fake_summary)
    monkeypatch.setattr(api_routes, "_generate_structured_spoken_reply", _fake_render)
    monkeypatch.setattr(api_routes, "_try_general_query_reply_text", _fake_general_guard)

    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    qmd_root = tmp_path / "qmd"
    _write_lab_docs(qmd_root)

    catalog = game_grounding.GameCatalog(manifest_path)
    catalog.qmd_games_dir = qmd_root / "games"
    catalog._load()

    rag = local_docs_rag.LocalDocsRAG(
        manifest_path=manifest_path,
        game_catalog=catalog,
        doc_root=qmd_root,
        embedder=DummyEmbedder(),
    )
    assert rag.ready, rag.error

    runtime = api_routes._UnifiedConversationRuntime.__new__(api_routes._UnifiedConversationRuntime)
    runtime.intent_cfg = None
    runtime.intent_resolver = None
    runtime.intent_router = object()
    runtime.game_catalog = catalog
    runtime.local_docs_rag = rag
    runtime.dialog_cfg = None
    runtime.dialog_helper = SimpleNamespace(
        user_memory=None,
        reply_compress=False,
        cfg=SimpleNamespace(enable_vision_query=True, enable_dialog_context=False),
        _is_memory_query=lambda text: "what do you know about me" in text.lower() or "remember me" in text.lower(),
        _is_vision_query=lambda text: any(
            marker in " ".join(str(text or "").strip().lower().split())
            for marker in ("camera", "what do you see", "can you see", "image", "scene", "frame", "preview")
        ),
    )
    runtime.session_store = session_context.SessionContextStore(max_age_sec=600.0)
    runtime._capability_classifier_cache = {}
    runtime.ready = True
    runtime.error = ""

    runtime.resolve_user_id = lambda **_kwargs: None

    def _route_text(text, corr_id="", *, payload=None, user_id=None, identity_resolution=None):
        normalized = " ".join(re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split())
        if normalized in {"the cornhole game", "cornhole", "cornhole game", "open cornhole", "open the cornhole game"}:
            return SimpleNamespace(payload={"type": "LAUNCH_GAME", "game_name": "Bean Bag Toss"})
        return SimpleNamespace(payload={"type": "QUERY"})

    runtime.route_text = _route_text
    runtime.build_turn_context = lambda **_kwargs: (None, "", {}, {})
    runtime.remember_user_turn = lambda **kwargs: runtime.session_store.remember_turn(
        user_id=kwargs.get("user_id"),
        role="user",
        text=kwargs.get("text", ""),
    )

    async def _empty_reply(**_kwargs):
        return ""

    runtime.try_memory_write_reply = _empty_reply
    runtime.try_memory_reply = _empty_reply
    runtime.try_vision_reply = lambda **_kwargs: "I can't see yet because camera preview is not active."
    runtime.build_general_session_context = lambda **_kwargs: ""

    def _finalize_assistant_turn(*, user_id=None, answer_text="", **_kwargs):
        runtime.session_store.remember_turn(user_id=user_id, role="assistant", text=answer_text)

    runtime.finalize_assistant_turn = _finalize_assistant_turn

    async def _semantic_label(**_kwargs):
        return "general_chat"

    runtime._semantic_capability_label = _semantic_label
    return runtime, api_routes


def _evaluate_expected(final_event: dict, expected: dict) -> list[str]:
    failures: list[str] = []
    actual_provider = str(final_event.get("provider") or "").strip()
    actual_route = str(final_event.get("route") or "").strip().upper()
    actual_text = str(final_event.get("text") or "").strip()
    haystack = actual_text.lower()
    if expected.get("provider") and actual_provider != str(expected["provider"]).strip():
        failures.append(f"provider expected {expected['provider']} got {actual_provider}")
    if expected.get("route") and actual_route != str(expected["route"]).strip().upper():
        failures.append(f"route expected {expected['route']} got {actual_route}")
    if expected.get("structured_type") and str(final_event.get("structured_type") or "").strip() != str(expected["structured_type"]).strip():
        failures.append(
            f"structured_type expected {expected['structured_type']} got {str(final_event.get('structured_type') or '').strip()}"
        )
    if expected.get("structured_type_any"):
        allowed = {str(item).strip() for item in expected.get("structured_type_any", [])}
        actual = str(final_event.get("structured_type") or "").strip()
        if actual not in allowed:
            failures.append(f"structured_type expected one of {sorted(allowed)} got {actual}")
    for key in ("domain", "answer_mode", "general_focus"):
        if expected.get(key) and str(final_event.get(key) or "").strip() != str(expected[key]).strip():
            failures.append(f"{key} expected {expected[key]} got {str(final_event.get(key) or '').strip()}")
    for phrase in expected.get("contains", []) or []:
        if str(phrase).lower() not in haystack:
            failures.append(f"missing phrase: {phrase}")
    for phrase in expected.get("reject_contains", []) or []:
        if str(phrase).lower() in haystack:
            failures.append(f"unexpected phrase: {phrase}")
    return failures


def _write_report(entries: list[dict]) -> None:
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps({"entries": entries}, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Dialogue Regression Report", ""]
    for entry in entries:
        lines.append(f"## {entry['case_id']}")
        lines.append(f"- Notes: {entry['notes']}")
        lines.append(f"- User: {entry['user_turn']}")
        lines.append(f"- Expected: {entry['expected_behavior']}")
        lines.append(f"- Actual route/provider: {entry['actual_route']} / {entry['actual_provider']}")
        lines.append(f"- Actual answer: {entry['actual_answer']}")
        lines.append(f"- Result: {'PASS' if entry['passed'] else 'FAIL'}")
        if entry["fail_reasons"]:
            lines.append(f"- Why: {'; '.join(entry['fail_reasons'])}")
        lines.append("")
    REPORT_MD.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def test_dialogue_regression_corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runtime, api_routes = _build_runtime(tmp_path, monkeypatch)
    corpus = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    report_entries: list[dict] = []
    failures: list[str] = []

    try:
        for transcript in corpus.get("transcripts", []):
            transcript_id = str(transcript.get("id") or "").strip() or "transcript"
            notes = str(transcript.get("notes") or "").strip()
            for turn_index, turn in enumerate(transcript.get("turns", []), start=1):
                user_text = str(turn.get("user") or "").strip()
                payload = api_routes.ConversationTurnRequest(text=user_text)
                events = _iter_events(api_routes._stream_unified_conversation_events(runtime, payload))
                final_event = next(event for event in events if event.get("type") == "final")
                expected = dict(turn.get("expected") or {})
                turn_failures = _evaluate_expected(final_event, expected)
                case_id = f"{transcript_id}:{turn_index}"
                report_entries.append(
                    {
                        "case_id": case_id,
                        "notes": notes,
                        "user_turn": user_text,
                        "expected_behavior": expected,
                        "actual_route": str(final_event.get("route") or "").strip(),
                        "actual_provider": str(final_event.get("provider") or "").strip(),
                        "actual_answer": str(final_event.get("text") or "").strip(),
                        "actual_structured_type": str(final_event.get("structured_type") or "").strip(),
                        "actual_domain": str(final_event.get("domain") or "").strip(),
                        "actual_answer_mode": str(final_event.get("answer_mode") or "").strip(),
                        "actual_general_focus": str(final_event.get("general_focus") or "").strip(),
                        "passed": not turn_failures,
                        "fail_reasons": turn_failures,
                    }
                )
                if turn_failures:
                    failures.append(f"{case_id}: {'; '.join(turn_failures)}")
    finally:
        _write_report(report_entries)
        runtime.local_docs_rag.close()

    assert not failures, "\n".join(failures)
