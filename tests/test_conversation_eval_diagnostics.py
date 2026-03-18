import asyncio
import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"


def _load_module(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load module spec: {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeStreamResponse:
    def __init__(self, events):
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for event in self._events:
            yield json.dumps(event, ensure_ascii=False)


class _FakeClient:
    def __init__(self, events):
        self._events = events

    def stream(self, *_args, **_kwargs):
        return _FakeStreamResponse(self._events)


def test_conversation_eval_stream_parser_captures_doc_probe():
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    conversation_eval = _load_module("conversation_eval_diag_module", SCRIPTS_DIR / "conversation_eval.py")
    client = _FakeClient(
        [
            {"type": "route", "route": "QUERY", "provider": "doc_rag"},
            {"type": "chunk", "route": "QUERY", "provider": "doc_rag", "text": "Right now I have Bean Bag Toss and Disc Golf available."},
            {
                "type": "final",
                "route": "QUERY",
                "provider": "doc_rag",
                "text": "Right now I have Bean Bag Toss and Disc Golf available.",
                "structured_type": "doc_answer",
                "domain": "general",
                "answer_mode": "introduce",
                "general_focus": "overview",
                "summary_used": True,
                "summary_model": "deterministic",
                "summary_fallback_reason": "",
                "fallback_reason": "",
                "doc_probe": {
                    "stage1_result": "doc_candidate",
                    "stage2_result": "doc_answer",
                    "answer_mode": "introduce",
                    "general_focus": "overview",
                },
            },
        ]
    )

    result = asyncio.run(
        conversation_eval._run_turn_stream(
            client,
            "http://127.0.0.1:8000",
            {"text": "What is the BioAdaptive Interface Lab?"},
        )
    )

    assert result["provider"] == "doc_rag"
    assert result["structured_type"] == "doc_answer"
    assert result["domain"] == "general"
    assert result["answer_mode"] == "introduce"
    assert result["general_focus"] == "overview"
    assert result["doc_probe"]["stage2_result"] == "doc_answer"
    assert result["doc_probe"]["general_focus"] == "overview"
    assert result["summary_used"] is True
    assert result["summary_model"] == "deterministic"
