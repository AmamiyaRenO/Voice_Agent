import importlib.util
import json
import sys
from pathlib import Path


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

    assert "Current topic: game recommendation." in context
    assert "Pending follow-up question:" in context
    assert "Recent dialogue:" in context
    assert len(context) < 900
