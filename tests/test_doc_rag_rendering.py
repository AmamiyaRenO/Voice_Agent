import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PYTHON_VOICE_DIR = ROOT / "python_voice_service"
SCRIPTS_DIR = ROOT / "scripts"


def _load_module(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load module spec: {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_voice_main():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    return _load_module("voice_service_doc_rag_render_module", PYTHON_VOICE_DIR / "api_routes.py")


def test_doc_rag_renderer_downgrades_invalid_compare_to_clarify(monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("faster_whisper")

    voice_main = _load_voice_main()

    async def fake_render(_user_text, _payload):
        return "Disc Golf is the more active option."

    monkeypatch.setattr(voice_main, "_generate_structured_spoken_reply", fake_render)
    reply = asyncio.run(
        voice_main._spoken_reply_from_payload(
            "Compare Disc Golf and Bean Bag Toss.",
            {
                "type": "doc_answer",
                "domain": "game",
                "answer_mode": "compare",
                "candidate_entities": ["Disc Golf", "Bean Bag Toss"],
                "allowed_entities": ["Disc Golf", "Bean Bag Toss"],
                "required_terms": ["Disc Golf", "Bean Bag Toss"],
                "doc_snippets": ["Disc Golf is active.", "Bean Bag Toss is lower impact."],
                "text": "",
                "max_sentences": 2,
            },
            all_game_names=["Disc Golf", "Bean Bag Toss", "Balance Quest"],
        )
    )

    assert reply == "Which two games do you want me to compare?"


def test_doc_rag_renderer_repairs_invalid_recommend_entity(monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("faster_whisper")

    voice_main = _load_voice_main()

    async def fake_render(_user_text, _payload):
        return "I recommend Balance Quest."

    monkeypatch.setattr(voice_main, "_generate_structured_spoken_reply", fake_render)
    reply = asyncio.run(
        voice_main._spoken_reply_from_payload(
            "Recommend Disc Golf or Bean Bag Toss.",
            {
                "type": "doc_answer",
                "domain": "game",
                "answer_mode": "recommend",
                "primary_entity": "Disc Golf",
                "candidate_entities": ["Disc Golf", "Bean Bag Toss"],
                "allowed_entities": ["Disc Golf", "Bean Bag Toss"],
                "launchable_games": ["Disc Golf", "Bean Bag Toss"],
                "required_terms": ["Disc Golf"],
                "recommendation_reason": "it fits what you like",
                "doc_snippets": ["Disc Golf fits walking goals."],
                "text": "",
                "max_sentences": 2,
            },
            all_game_names=["Disc Golf", "Bean Bag Toss", "Balance Quest"],
        )
    )

    assert "Disc Golf" in reply
    assert "Balance Quest" not in reply


def test_doc_rag_renderer_returns_no_evidence_when_snippets_are_missing():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("faster_whisper")

    voice_main = _load_voice_main()

    reply = asyncio.run(
        voice_main._spoken_reply_from_payload(
            "According to the docs, what is the warranty period?",
            {
                "type": "doc_answer",
                "domain": "general",
                "answer_mode": "factual",
                "doc_snippets": [],
                "text": "",
            },
        )
    )

    assert reply == "I could not find that in the local documents."


def test_doc_rag_renderer_prefers_summary_text_for_general_doc_answers(monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("faster_whisper")

    voice_main = _load_voice_main()

    async def fake_render(_user_text, _payload):
        return str(_payload.get("summary_text") or _payload.get("text") or "")

    monkeypatch.setattr(voice_main, "_generate_structured_spoken_reply", fake_render)
    reply = asyncio.run(
        voice_main._spoken_reply_from_payload(
            "What is the BioAdaptive Interface Lab?",
            {
                "type": "doc_answer",
                "domain": "general",
                "answer_mode": "introduce",
                "general_focus": "overview",
                "summary_text": "BioAdaptive Interface Lab is a research lab at Wilfrid Laurier University.",
                "text": "Description: raw snippet text that should not win.",
                "doc_snippets": [
                    "Lab name: BioAdaptive Interface Lab.",
                    "Institution: Wilfrid Laurier University.",
                ],
                "allowed_entities": ["BioAdaptive Interface Lab", "Wilfrid Laurier University"],
                "required_terms": ["BioAdaptive Interface Lab"],
                "candidate_entities": ["BioAdaptive Interface Lab"],
                "primary_entity": "BioAdaptive Interface Lab",
                "max_sentences": 2,
            },
        )
    )

    assert reply == "BioAdaptive Interface Lab is a research lab at Wilfrid Laurier University."
