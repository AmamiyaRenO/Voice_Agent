import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
PYTHON_VOICE_DIR = ROOT / "python_voice_service"


def _load_module(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load module spec: {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _iter_events(generator):
    async def _collect():
        items = []
        async for raw in generator:
            items.append(json.loads(raw.decode("utf-8")))
        return items

    return asyncio.run(_collect())


def _base_runtime(api_routes):
    return SimpleNamespace(
        resolve_user_id=lambda **_kwargs: None,
        route_text=lambda *_args, **_kwargs: SimpleNamespace(payload={"type": "QUERY"}),
        build_turn_context=lambda **_kwargs: (None, "", {}, {}),
        remember_user_turn=lambda **_kwargs: None,
        try_memory_write_reply=lambda **_kwargs: _async_value(""),
        session_store=SimpleNamespace(
            save_clarification=lambda **_kwargs: None,
            record_general_turn=lambda **_kwargs: None,
            record_structured_capability=lambda **_kwargs: None,
            remember_turn=lambda **_kwargs: None,
            update_game_state=lambda **_kwargs: None,
        ),
        finalize_assistant_turn=lambda **_kwargs: None,
        try_memory_reply=lambda **_kwargs: _async_value(""),
        try_vision_reply=lambda **_kwargs: "",
        try_general_query_reply=lambda **_kwargs: "General reply.",
        build_general_session_context=lambda **_kwargs: "",
        dialog_helper=SimpleNamespace(user_memory=None),
        record_game_event=lambda **_kwargs: None,
    )


async def _async_value(value):
    return value


def test_stream_final_event_includes_not_doc_probe_diagnostics():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    api_routes = _load_module("doc_rag_stream_diag_api", PYTHON_VOICE_DIR / "api_routes.py")
    runtime = _base_runtime(api_routes)

    async def route_query_capability(**_kwargs):
        return api_routes.CapabilityRouteDecision(
            label="general_chat",
            confidence=0.5,
            routed_text="Tell me something fun.",
            probe_telemetry={
                "query": "Tell me something fun.",
                "stage1_result": "not_doc",
                "stage1_reason": "weak_retrieval_support",
            },
            fallback_reason="weak_retrieval_support",
        )

    runtime.route_query_capability = route_query_capability
    payload = api_routes.ConversationTurnRequest(text="Tell me something fun.")

    events = _iter_events(api_routes._stream_unified_conversation_events(runtime, payload))
    final_event = next(event for event in events if event.get("type") == "final")

    assert final_event["provider"] == "general_guard"
    assert final_event["doc_probe"]["stage1_result"] == "not_doc"
    assert final_event["fallback_reason"] == "weak_retrieval_support"


def test_stream_final_event_includes_doc_query_diagnostics():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    api_routes = _load_module("doc_rag_stream_diag_api_doc", PYTHON_VOICE_DIR / "api_routes.py")
    runtime = _base_runtime(api_routes)

    async def _render_structured_reply(*, user_text, payload):
        return str(payload.get("text") or "")

    runtime._render_structured_reply = _render_structured_reply

    async def route_query_capability(**_kwargs):
        return api_routes.CapabilityRouteDecision(
            label="doc_query",
            confidence=0.82,
            routed_text="What games do you have?",
            structured_payload={
                "type": "doc_answer",
                "domain": "general",
                "answer_mode": "introduce",
                "general_focus": "overview",
                "text": "The BioAdaptive Interface Lab is a research lab at Wilfrid Laurier University.",
                "summary_text": "The BioAdaptive Interface Lab is a research lab at Wilfrid Laurier University.",
                "summary_used": True,
                "summary_model": "deterministic",
                "doc_snippets": ["Lab name: BioAdaptive Interface Lab.", "Institution: Wilfrid Laurier University."],
                "doc_source_ids": ["general:lab_identity:0:0", "general:lab_identity:0:1"],
                "candidate_entities": ["BioAdaptive Interface Lab"],
                "allowed_entities": ["BioAdaptive Interface Lab"],
                "doc_confidence": 0.82,
                "binding_state": "doc_grounded",
            },
            probe_telemetry={
                "query": "What is the BioAdaptive Interface Lab?",
                "stage1_result": "doc_candidate",
                "stage2_result": "doc_answer",
                "answer_mode": "introduce",
                "general_focus": "overview",
            },
        )

    runtime.route_query_capability = route_query_capability
    payload = api_routes.ConversationTurnRequest(text="What is the BioAdaptive Interface Lab?")

    events = _iter_events(api_routes._stream_unified_conversation_events(runtime, payload))
    final_event = next(event for event in events if event.get("type") == "final")

    assert final_event["provider"] == "doc_rag"
    assert final_event["structured_type"] == "doc_answer"
    assert final_event["domain"] == "general"
    assert final_event["answer_mode"] == "introduce"
    assert final_event["general_focus"] == "overview"
    assert final_event["doc_probe"]["stage2_result"] == "doc_answer"
    assert final_event["doc_probe"]["general_focus"] == "overview"
    assert final_event["summary_used"] is True
    assert final_event["summary_model"] == "deterministic"
