import importlib.util
import asyncio
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
                        "recommendation_weight": 0.85,
                    },
                    {
                        "id": "disc_golf",
                        "name": "Disc Golf",
                        "synonyms": ["disc golf", "disk golf"],
                        "description": "Disc Golf is a throwing game where players aim discs at basket targets.",
                        "how_to_play": "Players throw discs toward the target and try to finish in as few throws as possible.",
                        "recommendation_weight": 0.8,
                    },
                    {
                        "id": "balance_quest",
                        "name": "Balance Quest",
                        "synonyms": ["balance quest"],
                        "description": "Balance Quest is a balance-focused game with guided standing tasks.",
                        "how_to_play": "Players follow balance prompts and complete standing movement tasks.",
                        "recommendation_weight": 0.92,
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _build_runtime(tmp_path: Path):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    game_grounding = _load_module("game_qa_router_grounding_module", PYTHON_VOICE_DIR / "game_grounding.py")
    session_context = _load_module("game_qa_router_session_module", PYTHON_VOICE_DIR / "session_context.py")
    voice_main = _load_module("game_qa_router_main_module", PYTHON_VOICE_DIR / "main.py")

    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    catalog = game_grounding.GameCatalog(manifest_path)

    runtime = voice_main._UnifiedConversationRuntime.__new__(voice_main._UnifiedConversationRuntime)
    runtime.intent_cfg = None
    runtime.intent_resolver = None
    runtime.intent_router = object()
    runtime.game_catalog = catalog
    runtime.dialog_cfg = None
    runtime.dialog_helper = SimpleNamespace(
        user_memory=None,
        reply_compress=False,
        cfg=SimpleNamespace(enable_vision_query=False),
        _is_memory_query=lambda text: "what do you know about me" in text.lower(),
        _is_vision_query=lambda text: "what do you see" in text.lower(),
    )
    runtime.session_store = session_context.SessionContextStore(max_age_sec=600.0)
    runtime._capability_classifier_cache = {}
    runtime.local_docs_rag = None
    runtime.ready = True
    runtime.error = ""

    async def _render_structured_reply(*, user_text, payload):
        return str(payload.get("text") or "")

    runtime._render_structured_reply = _render_structured_reply
    runtime.finalize_assistant_turn = lambda **_kwargs: None
    return runtime, voice_main, session_context


def _attach_doc_rag(runtime, handler):
    def _probe(text, *, focus_state=None, session_state=None, user_profile=None):
        result = handler(
            text=text,
            focus_state=focus_state,
            session_state=session_state,
            user_profile=user_profile,
        )
        payload = dict(result.get("payload") or {})
        response_text = str(result.get("response_text") or payload.get("text") or "").strip()
        telemetry = {
            "query": text,
            "stage1_result": result.get("stage1_result", "doc_candidate"),
            "query_doc_affinity": result.get("query_doc_affinity", 0.9),
            "retrieval_support": result.get("retrieval_support", 0.8),
            "entity_binding_strength": result.get("entity_binding_strength", 0.8),
            "stage1_reason": result.get("stage1_reason", "test_probe"),
            "force_doc_reason": result.get("force_doc_reason", "test_force"),
            "stage2_result": result.get("stage2_result", "doc_answer"),
            "domain": result.get("domain", str(payload.get("domain") or "")),
            "answer_mode": result.get("answer_mode", str(payload.get("answer_mode") or "")),
            "routing_confidence": result.get("routing_confidence", 0.9),
            "answerability_confidence": result.get("answerability_confidence", 0.8),
            "doc_confidence": result.get("doc_confidence", 0.8),
            "top_hit_ids": list(result.get("top_hit_ids", ["hit-1", "hit-2"])),
            "selected_evidence_ids": list(result.get("selected_evidence_ids", payload.get("doc_source_ids", ["hit-1"]))),
            "fallback_reason": result.get("fallback_reason", ""),
            "clarify_kind": result.get("clarify_kind", str(payload.get("clarify_kind") or "")),
        }
        return SimpleNamespace(
            stage1_result=telemetry["stage1_result"],
            stage2_result=telemetry["stage2_result"],
            payload=payload,
            response_text=response_text,
            doc_confidence=telemetry["doc_confidence"],
            routing_confidence=telemetry["routing_confidence"],
            clarify_kind=telemetry["clarify_kind"],
            fallback_reason=telemetry["fallback_reason"],
            telemetry=lambda: dict(telemetry),
        )

    runtime.local_docs_rag = SimpleNamespace(ready=True, probe=_probe)


def test_game_catalog_extracts_manifest_mentions(tmp_path: Path):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    game_grounding = _load_module("game_qa_extract_mentions_module", PYTHON_VOICE_DIR / "game_grounding.py")

    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    catalog = game_grounding.GameCatalog(manifest_path)

    mentions = catalog.extract_game_mentions("Could you compare disc golf and cornhole for me?", limit=4)

    assert mentions == ["Disc Golf", "Bean Bag Toss"]


def test_game_catalog_generic_chat_is_not_treated_as_game_followup(tmp_path: Path):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    game_grounding = _load_module("game_qa_generic_chat_module", PYTHON_VOICE_DIR / "game_grounding.py")

    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    catalog = game_grounding.GameCatalog(manifest_path)
    session_state = {
        "focused_game": "Bean Bag Toss",
        "primary_recommendation": "Bean Bag Toss",
        "candidate_games": ["Bean Bag Toss", "Disc Golf"],
        "updated_at": 9999999999.0,
    }

    assert catalog.looks_like_game_followup("Can you hear me clearly?", session_state=session_state) is False
    assert catalog.looks_like_game_followup("What options do we have?", session_state=session_state) is False
    assert catalog.looks_like_game_followup("Tell me more about it.", session_state=session_state) is True


def test_session_context_store_enforces_balanced_context_budget():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    session_context = _load_module("game_qa_session_context_module", PYTHON_VOICE_DIR / "session_context.py")

    store = session_context.SessionContextStore(max_age_sec=600.0)
    store.remember_turn(user_id=None, role="user", text="I want to compare the local game options for something active today.")
    store.remember_turn(user_id=None, role="assistant", text="I can compare the current options if you want.")
    store.remember_turn(user_id=None, role="user", text="Please keep it short and focus on the game with more movement and balance work.")
    store.remember_turn(user_id=None, role="assistant", text="Would you like me to compare Bean Bag Toss and Disc Golf?")
    store.remember_turn(user_id=None, role="user", text="Yes, and remember I prefer short answers.")

    context = store.build_general_session_context(
        user_id=None,
        current_topic="game recommendation",
        open_question="Would you like me to compare Bean Bag Toss and Disc Golf?",
        exclude_user_text="Yes, and remember I prefer short answers.",
    )

    assert "Current topic:" not in context
    assert "Pending follow-up question:" not in context
    assert "Recent user messages:" in context
    assert "Assistant:" not in context
    assert "Coach:" not in context
    assert len(context) < 900


def test_session_context_cools_and_clears_game_focus_after_general_turns(tmp_path: Path):
    _, _, session_context = _build_runtime(tmp_path)

    store = session_context.SessionContextStore(max_age_sec=600.0)
    store.update_game_state(
        user_id=None,
        focused_game="Bean Bag Toss",
        candidate_games=["Bean Bag Toss", "Disc Golf"],
        primary_recommendation="Bean Bag Toss",
        last_router_intent="game_recommend",
    )

    assert store.context_game_name(None, "Open it.") == "Bean Bag Toss"

    store.record_general_turn(user_id=None, capability="general_chat")

    assert store.context_game_name(None, "What options do you have?") == ""
    assert store.context_game_name(None, "Open it.") == "Bean Bag Toss"

    store.record_general_turn(user_id=None, capability="general_chat")

    assert store.context_game_name(None, "Open it.") == ""
    focus = store.capability_state(None)
    assert focus.candidate_entities == []


def test_runtime_route_query_capability_clarifies_ambiguous_options_without_focus(tmp_path: Path):
    runtime, _, _ = _build_runtime(tmp_path)

    async def _semantic_label(**_kwargs):
        return "general_chat"

    runtime._semantic_capability_label = _semantic_label

    decision = asyncio.run(
        runtime.route_query_capability(
            user_id=None,
            text="What options do you have?",
        )
    )

    assert decision.label == "clarify"
    assert decision.clarification_text == "What kind of options do you mean?"


def test_runtime_route_query_capability_clarifies_ambiguous_help_without_game_bias(tmp_path: Path):
    runtime, _, _ = _build_runtime(tmp_path)

    decision = asyncio.run(
        runtime.route_query_capability(
            user_id=None,
            text="Help me with.",
        )
    )

    assert decision.label == "clarify"
    assert decision.clarification_text == "What kind of help do you want?"
    assert decision.clarification_kind == "help"


def test_runtime_route_query_capability_prefers_doc_probe_for_exercise_game(tmp_path: Path):
    runtime, _, _ = _build_runtime(tmp_path)
    _attach_doc_rag(
        runtime,
        lambda **_kwargs: {
            "payload": {
                "type": "doc_answer",
                "domain": "game",
                "answer_mode": "availability",
                "text": "Right now I have Bean Bag Toss and Disc Golf available.",
                "doc_source_ids": ["game_card:bean_bag_toss", "game_card:disc_golf"],
                "candidate_entities": ["Bean Bag Toss", "Disc Golf"],
                "allowed_entities": ["Bean Bag Toss", "Disc Golf"],
                "launchable_games": ["Bean Bag Toss", "Disc Golf"],
                "doc_confidence": 0.82,
            },
            "stage2_result": "doc_answer",
            "domain": "game",
            "answer_mode": "availability",
            "doc_confidence": 0.82,
        },
    )

    async def _semantic_label(**_kwargs):
        return "game_availability"

    runtime._semantic_capability_label = _semantic_label

    decision = asyncio.run(
        runtime.route_query_capability(
            user_id=None,
            text="Do you got any exercise game?",
        )
    )

    assert decision.label == "doc_query"
    assert decision.structured_payload["answer_mode"] == "availability"
    assert decision.structured_payload["domain"] == "game"


def test_runtime_route_query_capability_uses_confirmed_general_doc_followup_before_other_guards(tmp_path: Path):
    runtime, _, _ = _build_runtime(tmp_path)
    runtime.session_store.record_structured_capability(
        user_id=None,
        active_capability="doc_query",
        focused_entity="John E. Munoz",
        candidate_entities=["John E. Munoz"],
        last_structured_intent="introduce",
        focus_domain="general",
        focus_general_focus="people",
        related_entities={"lab": ["BioAdaptive Interface Lab"]},
        focus_source="answer",
    )

    _attach_doc_rag(
        runtime,
        lambda **kwargs: {
            "payload": {
                "type": "doc_answer",
                "domain": "general",
                "answer_mode": "introduce",
                "general_focus": "overview",
                "text": "BioAdaptive Interface Lab is a research lab at Wilfrid Laurier University.",
                "summary_text": "BioAdaptive Interface Lab is a research lab at Wilfrid Laurier University.",
                "doc_snippets": ["Lab name: BioAdaptive Interface Lab."],
                "doc_source_ids": ["general:lab_identity:0:0"],
                "candidate_entities": ["BioAdaptive Interface Lab"],
                "allowed_entities": ["BioAdaptive Interface Lab"],
                "doc_confidence": 0.88,
            },
            "stage2_result": "doc_answer",
            "domain": "general",
            "answer_mode": "introduce",
            "doc_confidence": 0.88,
            "selected_evidence_ids": ["general:lab_identity:0:0"],
            "top_hit_ids": ["general:lab_identity:0:0"],
        }
        if kwargs.get("text") == "What is BioAdaptive Interface Lab?"
        else {
            "stage1_result": "not_doc",
            "stage2_result": "",
            "fallback_reason": "not_doc",
        },
    )

    decision = asyncio.run(
        runtime.route_query_capability(
            user_id=None,
            text="Do you know his lab?",
        )
    )

    assert decision.label == "doc_query"
    assert decision.routed_text == "What is BioAdaptive Interface Lab?"
    assert decision.structured_payload["domain"] == "general"
    assert decision.structured_payload["general_focus"] == "overview"


def test_runtime_route_query_capability_resumes_typed_doc_clarification_on_yes(tmp_path: Path):
    runtime, _, _ = _build_runtime(tmp_path)
    runtime.session_store.save_clarification(
        user_id=None,
        kind="doc_clarify",
        source_user_text="What do you know about John?",
        assistant_clarify_text="Do you mean John E. Munoz?",
        target_domain="general",
        target_answer_mode="introduce",
        target_general_focus="people",
        target_entities=["John E. Munoz"],
        related_entities={"lab": ["BioAdaptive Interface Lab"]},
        resume_strategy="confirm_candidate",
    )
    _attach_doc_rag(
        runtime,
        lambda **kwargs: {
            "payload": {
                "type": "doc_answer",
                "domain": "general",
                "answer_mode": "introduce",
                "general_focus": "people",
                "text": "John E. Munoz is Director of BioAdaptive Interface Lab.",
                "summary_text": "John E. Munoz is Director of BioAdaptive Interface Lab.",
                "doc_snippets": ["Name: John E. Munoz. Role: Director of BioAdaptive Interface Lab."],
                "doc_source_ids": ["general:team:0:0"],
                "candidate_entities": ["John E. Munoz"],
                "allowed_entities": ["John E. Munoz", "BioAdaptive Interface Lab"],
                "primary_entity": "John E. Munoz",
                "doc_confidence": 0.84,
            },
            "stage2_result": "doc_answer",
            "domain": "general",
            "answer_mode": "introduce",
            "doc_confidence": 0.84,
            "selected_evidence_ids": ["general:team:0:0"],
            "top_hit_ids": ["general:team:0:0"],
        }
        if kwargs.get("text") == "Tell me about John E. Munoz."
        else {
            "stage1_result": "not_doc",
            "stage2_result": "",
            "fallback_reason": "not_doc",
        },
    )

    decision = asyncio.run(
        runtime.route_query_capability(
            user_id=None,
            text="Yes.",
        )
    )

    assert decision.label == "doc_query"
    assert decision.merged_from_clarification is True
    assert decision.routed_text == "Tell me about John E. Munoz."


def test_runtime_route_query_capability_keeps_introduce_yourself_as_general_chat(tmp_path: Path):
    runtime, _, _ = _build_runtime(tmp_path)
    runtime.session_store.update_game_state(
        user_id=None,
        focused_game="Bean Bag Toss",
        candidate_games=["Bean Bag Toss", "Disc Golf"],
        primary_recommendation="Bean Bag Toss",
        last_router_intent="game_recommend",
    )

    decision = asyncio.run(
        runtime.route_query_capability(
            user_id=None,
            text="Introduce yourself.",
        )
    )

    assert decision.label == "general_chat"


def test_runtime_route_query_capability_routes_exercise_plan_to_general_chat(tmp_path: Path):
    runtime, _, _ = _build_runtime(tmp_path)

    async def _semantic_label(**_kwargs):
        return "clarify"

    runtime._semantic_capability_label = _semantic_label

    decision = asyncio.run(
        runtime.route_query_capability(
            user_id=None,
            text="Help me with my exercise plan.",
        )
    )

    assert decision.label == "general_chat"


def test_runtime_route_query_capability_switches_from_game_to_memory_query(tmp_path: Path):
    runtime, _, _ = _build_runtime(tmp_path)
    runtime.session_store.update_game_state(
        user_id=None,
        focused_game="Bean Bag Toss",
        candidate_games=["Bean Bag Toss", "Disc Golf"],
        primary_recommendation="Bean Bag Toss",
        last_router_intent="game_recommend",
    )

    decision = asyncio.run(
        runtime.route_query_capability(
            user_id=None,
            text="What do you know about me?",
        )
    )

    assert decision.label == "memory_query"


def test_runtime_route_query_capability_topic_switch_clarifies_after_unrelated_chat(tmp_path: Path):
    runtime, _, _ = _build_runtime(tmp_path)
    runtime.session_store.update_game_state(
        user_id=None,
        focused_game="Bean Bag Toss",
        candidate_games=["Bean Bag Toss", "Disc Golf"],
        primary_recommendation="Bean Bag Toss",
        last_router_intent="game_recommend",
    )
    runtime.session_store.record_general_turn(user_id=None, capability="general_chat")
    runtime.session_store.record_general_turn(user_id=None, capability="general_chat")

    async def _semantic_label(**_kwargs):
        return "general_chat"

    runtime._semantic_capability_label = _semantic_label

    decision = asyncio.run(
        runtime.route_query_capability(
            user_id=None,
            text="What options do you have?",
        )
    )

    assert decision.label == "clarify"
    assert decision.clarification_text == "What kind of options do you mean?"


def test_session_context_hard_reset_suppresses_game_reuse_and_filters_old_context(tmp_path: Path):
    _, _, session_context = _build_runtime(tmp_path)

    store = session_context.SessionContextStore(max_age_sec=600.0)
    store.remember_turn(user_id=None, role="user", text="Recommend a game for me.")
    store.remember_turn(user_id=None, role="assistant", text="Bean Bag Toss is my pick right now.")
    store.update_game_state(
        user_id=None,
        focused_game="Bean Bag Toss",
        candidate_games=["Bean Bag Toss", "Disc Golf"],
        primary_recommendation="Bean Bag Toss",
        last_router_intent="game_recommend",
    )
    store.remember_turn(user_id=None, role="user", text="Let's go back to the exercise plan.")
    store.activate_game_suppression(user_id=None, reason="topic_switch")
    store.remember_turn(user_id=None, role="user", text="I would like to exercise on Friday.")

    assert store.is_game_suppressed(None) is True
    assert store.context_game_name(None, "Open it.") == ""

    context = store.build_general_session_context(
        user_id=None,
        exclude_user_text="I would like to exercise on Friday.",
    )

    assert "Bean Bag Toss" not in context
    assert "Recommend a game for me." not in context
    assert "Recent user messages:" in context


def test_runtime_route_query_capability_keeps_vague_recommendation_out_of_games_after_reset(tmp_path: Path):
    runtime, _, _ = _build_runtime(tmp_path)
    runtime.session_store.remember_turn(user_id=None, role="user", text="Recommend a game for me.")
    runtime.session_store.update_game_state(
        user_id=None,
        focused_game="Bean Bag Toss",
        candidate_games=["Bean Bag Toss", "Disc Golf"],
        primary_recommendation="Bean Bag Toss",
        last_router_intent="game_recommend",
    )
    runtime.session_store.remember_turn(user_id=None, role="user", text="Let's go back to the exercise plan.")
    runtime.session_store.activate_game_suppression(user_id=None, reason="topic_switch")
    runtime.session_store.record_general_turn(user_id=None, capability="general_chat")

    async def _semantic_label(**_kwargs):
        return "game_recommend"

    runtime._semantic_capability_label = _semantic_label

    decision = asyncio.run(
        runtime.route_query_capability(
            user_id=None,
            text="What would you recommend?",
        )
    )

    assert decision.label == "general_chat"


def test_runtime_route_query_capability_allows_explicit_game_reopen_after_reset(tmp_path: Path):
    runtime, _, _ = _build_runtime(tmp_path)
    runtime.session_store.remember_turn(user_id=None, role="user", text="Let's switch topic to normal talking.")
    runtime.session_store.activate_game_suppression(user_id=None, reason="normal_talk")
    _attach_doc_rag(
        runtime,
        lambda **_kwargs: {
            "payload": {
                "type": "doc_answer",
                "domain": "game",
                "answer_mode": "availability",
                "text": "Right now I have Bean Bag Toss and Disc Golf available.",
                "doc_source_ids": ["game_card:bean_bag_toss", "game_card:disc_golf"],
                "candidate_entities": ["Bean Bag Toss", "Disc Golf"],
                "allowed_entities": ["Bean Bag Toss", "Disc Golf"],
                "launchable_games": ["Bean Bag Toss", "Disc Golf"],
                "doc_confidence": 0.85,
            },
            "stage2_result": "doc_answer",
            "domain": "game",
            "answer_mode": "availability",
            "force_doc_reason": "availability_query",
            "doc_confidence": 0.85,
        },
    )

    async def _semantic_label(**_kwargs):
        return "game_availability"

    runtime._semantic_capability_label = _semantic_label

    decision = asyncio.run(
        runtime.route_query_capability(
            user_id=None,
            text="What games do you have?",
        )
    )

    assert decision.label == "doc_query"
    assert decision.structured_payload["answer_mode"] == "availability"


def test_coach_prompt_package_uses_neutral_assistant_label():
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    voice_main = _load_module("game_qa_prompt_label_module", PYTHON_VOICE_DIR / "api_routes.py")

    async def _run():
        return await voice_main._build_coach_prompt_package("Can you hear me?")

    system_prompt, prompt = asyncio.run(_run())

    assert "Coach:" not in prompt
    assert "Assistant:" in prompt
    assert "switches away from games" in system_prompt


def test_runtime_route_query_capability_routes_other_option_after_game_recommendation(tmp_path: Path):
    runtime, _, _ = _build_runtime(tmp_path)
    runtime.session_store.update_game_state(
        user_id=None,
        focused_game="Bean Bag Toss",
        candidate_games=["Bean Bag Toss", "Disc Golf", "Balance Quest"],
        primary_recommendation="Bean Bag Toss",
        last_router_intent="game_recommend",
    )
    _attach_doc_rag(
        runtime,
        lambda **_kwargs: {
            "payload": {
                "type": "doc_clarify",
                "domain": "game",
                "answer_mode": "",
                "clarify_kind": "clarify_ambiguous_intent",
                "text": "Do you want me to compare Bean Bag Toss and Disc Golf, or recommend one?",
                "doc_source_ids": ["game_card:bean_bag_toss", "game_card:disc_golf"],
                "candidate_entities": ["Bean Bag Toss", "Disc Golf", "Balance Quest"],
                "allowed_entities": ["Bean Bag Toss", "Disc Golf", "Balance Quest"],
                "launchable_games": ["Bean Bag Toss", "Disc Golf", "Balance Quest"],
                "doc_confidence": 0.44,
            },
            "stage2_result": "doc_clarify",
            "domain": "game",
            "clarify_kind": "clarify_ambiguous_intent",
            "doc_confidence": 0.44,
        },
    )

    decision = asyncio.run(
        runtime.route_query_capability(
            user_id=None,
            text="Any other option?",
        )
    )

    assert decision.label == "doc_query"
    assert decision.structured_payload["type"] == "doc_clarify"
    assert decision.clarification_kind == "clarify_ambiguous_intent"


def test_runtime_route_query_capability_routes_introduce_game_with_focus(tmp_path: Path):
    runtime, _, _ = _build_runtime(tmp_path)
    runtime.session_store.update_game_state(
        user_id=None,
        focused_game="Disc Golf",
        candidate_games=["Bean Bag Toss", "Disc Golf"],
        primary_recommendation="Bean Bag Toss",
        last_router_intent="game_availability",
    )
    _attach_doc_rag(
        runtime,
        lambda **_kwargs: {
            "payload": {
                "type": "doc_answer",
                "domain": "game",
                "answer_mode": "introduce",
                "text": "Disc Golf is a throwing game where players aim discs at basket targets.",
                "doc_source_ids": ["game_card:disc_golf"],
                "primary_entity": "Disc Golf",
                "candidate_entities": ["Disc Golf"],
                "allowed_entities": ["Disc Golf"],
                "launchable_games": ["Disc Golf"],
                "doc_confidence": 0.78,
            },
            "stage2_result": "doc_answer",
            "domain": "game",
            "answer_mode": "introduce",
            "doc_confidence": 0.78,
        },
    )

    decision = asyncio.run(
        runtime.route_query_capability(
            user_id=None,
            text="Introduce the game to me.",
        )
    )

    assert decision.label == "doc_query"
    assert decision.structured_payload["answer_mode"] == "introduce"
    assert decision.structured_payload["primary_entity"] == "Disc Golf"


def test_capability_router_replays_failing_sequence_without_game_latch(tmp_path: Path):
    runtime, _, _ = _build_runtime(tmp_path)
    _attach_doc_rag(
        runtime,
        lambda *, text, **_kwargs: {
            "payload": {
                "type": "doc_answer",
                "domain": "game",
                "answer_mode": "availability",
                "text": "Right now I have Bean Bag Toss and Disc Golf available.",
                "doc_source_ids": ["game_card:bean_bag_toss", "game_card:disc_golf"],
                "candidate_entities": ["Bean Bag Toss", "Disc Golf"],
                "allowed_entities": ["Bean Bag Toss", "Disc Golf"],
                "launchable_games": ["Bean Bag Toss", "Disc Golf"],
                "doc_confidence": 0.8,
            },
            "stage1_result": "doc_candidate" if "exercise game" in text.lower() else "not_doc",
            "stage2_result": "doc_answer",
            "domain": "game",
            "answer_mode": "availability",
            "doc_confidence": 0.8,
        },
    )

    async def _semantic_label(*, text, **_kwargs):
        lowered = text.lower()
        if "exercise game" in lowered:
            return "game_availability"
        return "general_chat"

    runtime._semantic_capability_label = _semantic_label

    first = asyncio.run(
        runtime.route_query_capability(
            user_id=None,
            text="Planning exercise for me.",
        )
    )
    assert first.label == "general_chat"

    runtime.session_store.record_general_turn(user_id=None, capability="general_chat")

    second = asyncio.run(
        runtime.route_query_capability(
            user_id=None,
            text="What options do you have?",
        )
    )
    assert second.label == "clarify"

    third = asyncio.run(
        runtime.route_query_capability(
            user_id=None,
            text="Do you got any exercise game?",
        )
    )
    assert third.label == "doc_query"
    assert third.structured_payload["answer_mode"] == "availability"


def test_runtime_clarification_handoff_merges_exercise_plan_followup(tmp_path: Path):
    runtime, _, _ = _build_runtime(tmp_path)

    runtime.session_store.save_clarification(
        user_id=None,
        kind="help",
        source_user_text="Help me with.",
        assistant_clarify_text="What kind of help do you want?",
    )

    async def _semantic_label(**_kwargs):
        return "clarify"

    runtime._semantic_capability_label = _semantic_label

    decision = asyncio.run(
        runtime.route_query_capability(
            user_id=None,
            text="Exercise plan.",
        )
    )

    assert decision.label == "general_chat"
    assert decision.merged_from_clarification is True
    assert "Help me with." in decision.routed_text
    assert "Exercise plan." in decision.routed_text


def test_runtime_clarification_handoff_can_route_games_from_short_followup(tmp_path: Path):
    runtime, _, _ = _build_runtime(tmp_path)

    runtime.session_store.save_clarification(
        user_id=None,
        kind="help",
        source_user_text="Help me with.",
        assistant_clarify_text="What kind of help do you want?",
    )
    _attach_doc_rag(
        runtime,
        lambda *, text, **_kwargs: {
            "payload": {
                "type": "doc_answer",
                "domain": "game",
                "answer_mode": "availability",
                "text": "Right now I have Bean Bag Toss and Disc Golf available.",
                "doc_source_ids": ["game_card:bean_bag_toss", "game_card:disc_golf"],
                "candidate_entities": ["Bean Bag Toss", "Disc Golf"],
                "allowed_entities": ["Bean Bag Toss", "Disc Golf"],
                "launchable_games": ["Bean Bag Toss", "Disc Golf"],
                "doc_confidence": 0.83,
            },
            "stage1_result": "doc_candidate" if "games" in text.lower() else "not_doc",
            "stage2_result": "doc_answer",
            "domain": "game",
            "answer_mode": "availability",
            "force_doc_reason": "availability_query",
            "doc_confidence": 0.83,
        },
    )

    async def _semantic_label(*, text, **_kwargs):
        if "games" in text.lower():
            return "game_availability"
        return "general_chat"

    runtime._semantic_capability_label = _semantic_label

    decision = asyncio.run(
        runtime.route_query_capability(
            user_id=None,
            text="Games.",
        )
    )

    assert decision.label == "doc_query"
    assert decision.merged_from_clarification is True


def test_runtime_route_query_capability_keeps_not_doc_probe_telemetry(tmp_path: Path):
    runtime, _, _ = _build_runtime(tmp_path)
    _attach_doc_rag(
        runtime,
        lambda **_kwargs: {
            "stage1_result": "not_doc",
            "stage1_reason": "weak_retrieval_support",
            "fallback_reason": "weak_retrieval_support",
            "query_doc_affinity": 0.12,
            "retrieval_support": 0.08,
            "entity_binding_strength": 0.05,
            "routing_confidence": 0.14,
            "doc_confidence": 0.14,
        },
    )

    async def _semantic_label(**_kwargs):
        return "general_chat"

    runtime._semantic_capability_label = _semantic_label

    decision = asyncio.run(
        runtime.route_query_capability(
            user_id=None,
            text="Tell me something fun.",
        )
    )

    assert decision.label == "general_chat"
    assert decision.probe_telemetry is not None
    assert decision.probe_telemetry["stage1_result"] == "not_doc"
    assert decision.fallback_reason == "weak_retrieval_support"


def test_runtime_clarification_handoff_expires_after_one_unrelated_turn(tmp_path: Path):
    runtime, _, _ = _build_runtime(tmp_path)

    runtime.session_store.save_clarification(
        user_id=None,
        kind="help",
        source_user_text="Help me with.",
        assistant_clarify_text="What kind of help do you want?",
    )

    decision = asyncio.run(
        runtime.route_query_capability(
            user_id=None,
            text="What do you know about me?",
        )
    )

    assert decision.label == "memory_query"

    async def _semantic_label(**_kwargs):
        return "general_chat"

    runtime._semantic_capability_label = _semantic_label

    followup = asyncio.run(
        runtime.route_query_capability(
            user_id=None,
            text="Exercise plan.",
        )
    )

    assert followup.merged_from_clarification is False
    assert followup.routed_text == "Exercise plan."


def test_session_context_clarification_state_expires_by_timeout(tmp_path: Path, monkeypatch):
    _, _, session_context = _build_runtime(tmp_path)

    now_box = {"value": 1000.0}
    monkeypatch.setattr(session_context.time, "time", lambda: now_box["value"])

    store = session_context.SessionContextStore(max_age_sec=600.0)
    store.save_clarification(
        user_id=None,
        kind="help",
        source_user_text="Help me with.",
        assistant_clarify_text="What kind of help do you want?",
    )

    assert store.clarification_state(None).kind == "help"

    now_box["value"] = 1035.0

    assert store.clarification_state(None).kind == ""
    assert store.take_clarification(None).kind == ""
