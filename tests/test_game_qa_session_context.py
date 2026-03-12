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
    runtime.ready = True
    runtime.error = ""

    async def _render_structured_reply(*, user_text, payload):
        return str(payload.get("text") or "")

    runtime._render_structured_reply = _render_structured_reply
    runtime.finalize_assistant_turn = lambda **_kwargs: None
    return runtime, voice_main, session_context


def test_game_catalog_introduce_current_choice_uses_session_state(tmp_path: Path):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    game_grounding = _load_module("game_qa_current_choice_module", PYTHON_VOICE_DIR / "game_grounding.py")

    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    catalog = game_grounding.GameCatalog(manifest_path)

    result = catalog.grounded_reply(
        "Introduce your current choice to me.",
        session_state={
            "focused_game": "Bean Bag Toss",
            "primary_recommendation": "Bean Bag Toss",
            "candidate_games": ["Bean Bag Toss", "Disc Golf"],
            "updated_at": 9999999999.0,
        },
    )

    assert result["type"] == "game_explain"
    assert result["primary_game_name"] == "Bean Bag Toss"
    assert "Bean Bag Toss" in result["required_terms"]
    assert "Bean Bag Toss" in result["text"]


def test_game_catalog_introduce_them_uses_candidate_games(tmp_path: Path):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    game_grounding = _load_module("game_qa_intro_them_module", PYTHON_VOICE_DIR / "game_grounding.py")

    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    catalog = game_grounding.GameCatalog(manifest_path)

    result = catalog.grounded_reply(
        "Introduce them to me.",
        session_state={
            "focused_game": "Bean Bag Toss",
            "primary_recommendation": "Bean Bag Toss",
            "candidate_games": ["Bean Bag Toss", "Disc Golf"],
            "updated_at": 9999999999.0,
        },
    )

    assert result["type"] == "game_explain"
    assert set(result["candidate_games"]) == {"Bean Bag Toss", "Disc Golf"}
    assert "Bean Bag Toss" in result["text"]
    assert "Disc Golf" in result["text"]


def test_game_catalog_alternative_pivots_to_compare_after_intro(tmp_path: Path):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    game_grounding = _load_module("game_qa_compare_pivot_module", PYTHON_VOICE_DIR / "game_grounding.py")

    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    catalog = game_grounding.GameCatalog(manifest_path)

    result = catalog.grounded_reply(
        "Any other option?",
        session_state={
            "focused_game": "Bean Bag Toss",
            "primary_recommendation": "Bean Bag Toss",
            "candidate_games": ["Bean Bag Toss", "Disc Golf"],
            "last_introduced_games": ["Disc Golf"],
            "updated_at": 9999999999.0,
        },
    )

    assert result["type"] == "game_explain"
    assert set(result["candidate_games"]) == {"Bean Bag Toss", "Disc Golf"}
    assert "Bean Bag Toss" in result["text"]
    assert "Disc Golf" in result["text"]


def test_game_catalog_qmd_overview_is_used_for_introduction(tmp_path: Path):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    game_grounding = _load_module("game_qa_qmd_module", PYTHON_VOICE_DIR / "game_grounding.py")

    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    catalog = game_grounding.GameCatalog(manifest_path)
    qmd_root = tmp_path / "qmd" / "games"
    qmd_root.mkdir(parents=True, exist_ok=True)
    (qmd_root / "disc_golf.qmd").write_text(
        "\n".join(
            [
                "---",
                "type: game_card",
                "name: Disc Golf",
                "---",
                "",
                "# Game Card: Disc Golf",
                "",
                "## Description",
                "Disc Golf is the walking-focused throwing option with basket targets.",
                "",
                "## How To Play",
                "Throw discs toward the basket and finish each hole in as few throws as possible.",
            ]
        ),
        encoding="utf-8",
    )
    catalog.qmd_games_dir = qmd_root
    catalog._load()

    result = catalog.grounded_reply(
        "Introduce Disc Golf to me.",
        session_state={"updated_at": 9999999999.0},
    )

    assert result["type"] == "game_explain"
    assert result["primary_game_name"] == "Disc Golf"
    assert "walking-focused throwing option" in result["text"]


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

    assert catalog.looks_like_game_domain("Can you hear me clearly?", session_state=session_state) is False
    assert catalog.looks_like_game_followup("Can you hear me clearly?", session_state=session_state) is False
    assert catalog.route_game_query("Can you hear me clearly?", session_state=session_state).intent == "none"
    assert catalog.looks_like_game_domain("What options do we have?", session_state=session_state) is False
    assert catalog.route_game_query("What options do we have?", session_state=session_state).intent == "none"


def test_runtime_try_game_reply_skips_generic_chat_even_with_game_context(tmp_path: Path):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    game_grounding = _load_module("game_qa_runtime_grounding_module", PYTHON_VOICE_DIR / "game_grounding.py")
    session_context = _load_module("game_qa_runtime_session_module", PYTHON_VOICE_DIR / "session_context.py")
    voice_main = _load_module("game_qa_runtime_main_module", PYTHON_VOICE_DIR / "main.py")

    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    catalog = game_grounding.GameCatalog(manifest_path)

    runtime = voice_main._UnifiedConversationRuntime.__new__(voice_main._UnifiedConversationRuntime)
    runtime.intent_router = object()
    runtime.dialog_helper = SimpleNamespace(user_memory=None, reply_compress=False)
    runtime.game_catalog = catalog
    runtime.session_store = session_context.SessionContextStore(max_age_sec=600.0)
    runtime.ready = True
    runtime.error = ""

    async def _fail_render(**_kwargs):
        raise AssertionError("structured game renderer should not be called for generic chat")

    runtime._render_structured_reply = _fail_render
    runtime.finalize_assistant_turn = lambda **_kwargs: None
    runtime.session_store.update_game_state(
        user_id=None,
        focused_game="Bean Bag Toss",
        candidate_games=["Bean Bag Toss", "Disc Golf"],
        primary_recommendation="Bean Bag Toss",
    )

    reply = asyncio.run(
        runtime.try_game_reply(
            user_id=None,
            text="Can you hear me clearly?",
            dialog_request_ctx={},
        )
    )

    assert reply == ""


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


def test_runtime_route_query_capability_uses_semantic_game_availability_for_exercise_game(tmp_path: Path):
    runtime, _, _ = _build_runtime(tmp_path)

    async def _semantic_label(**_kwargs):
        return "game_availability"

    runtime._semantic_capability_label = _semantic_label

    decision = asyncio.run(
        runtime.route_query_capability(
            user_id=None,
            text="Do you got any exercise game?",
        )
    )

    assert decision.label == "game_availability"


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


def test_runtime_try_game_reply_forced_availability_handles_fuzzy_game_query(tmp_path: Path):
    runtime, _, _ = _build_runtime(tmp_path)

    reply = asyncio.run(
        runtime.try_game_reply(
            user_id=None,
            text="Do you got any exercise game?",
            dialog_request_ctx={},
            forced_intent="game_availability",
        )
    )

    assert "Bean Bag Toss" in reply
    assert "Disc Golf" in reply


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

    async def _semantic_label(**_kwargs):
        return "game_availability"

    runtime._semantic_capability_label = _semantic_label

    decision = asyncio.run(
        runtime.route_query_capability(
            user_id=None,
            text="What games do you have?",
        )
    )

    assert decision.label == "game_availability"


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

    decision = asyncio.run(
        runtime.route_query_capability(
            user_id=None,
            text="Any other option?",
        )
    )

    assert decision.label == "game_alternative"


def test_runtime_route_query_capability_routes_introduce_game_with_focus(tmp_path: Path):
    runtime, _, _ = _build_runtime(tmp_path)
    runtime.session_store.update_game_state(
        user_id=None,
        focused_game="Disc Golf",
        candidate_games=["Bean Bag Toss", "Disc Golf"],
        primary_recommendation="Bean Bag Toss",
        last_router_intent="game_availability",
    )

    decision = asyncio.run(
        runtime.route_query_capability(
            user_id=None,
            text="Introduce the game to me.",
        )
    )

    assert decision.label == "game_introduce"


def test_capability_router_replays_failing_sequence_without_game_latch(tmp_path: Path):
    runtime, _, _ = _build_runtime(tmp_path)

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
    assert third.label == "game_availability"


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

    assert decision.label == "game_availability"
    assert decision.merged_from_clarification is True


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
