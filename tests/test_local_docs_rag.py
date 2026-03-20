import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
PYTHON_VOICE_DIR = ROOT / "python_voice_service"
SCRIPTS_DIR = ROOT / "scripts" / "dialog_service"


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
                        "players_min": 1,
                        "players_max": 2,
                        "tags": ["coordination", "throwing"],
                        "activity_level": "low",
                        "recommendation_weight": 0.91,
                        "exec": "games/bean_bag_toss.exe",
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
                        "exec": "games/disc_golf.exe",
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
                        "exec": "games/balance_quest.exe",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class DummyEmbedder:
    ready = True

    def _vector(self, text: str):
        lower = str(text or "").lower()
        if "disc golf" in lower or "basket" in lower or "walking" in lower:
            return np.asarray([1.0, 0.0, 0.0], dtype=np.float32)
        if "bean bag toss" in lower or "cornhole" in lower or "board" in lower:
            return np.asarray([0.0, 1.0, 0.0], dtype=np.float32)
        if "balance quest" in lower or "balance" in lower:
            return np.asarray([0.3, 0.3, 0.9], dtype=np.float32)
        if "hydration" in lower or "water" in lower or "manual" in lower or "safety" in lower:
            return np.asarray([0.0, 0.0, 1.0], dtype=np.float32)
        return np.asarray([0.1, 0.1, 0.1], dtype=np.float32)

    def doc_embedding(self, text: str):
        return self._vector(text)

    def query_embedding(self, text: str):
        return self._vector(text)


def _build_rag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch | None = None,
    *,
    answer_threshold: float | None = None,
    embedder=None,
    doc_root: Path | None = None,
):
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    if monkeypatch is not None:
        monkeypatch.setenv("DOC_RAG_ENABLE", "1")
        monkeypatch.setenv("DOC_RAG_INCLUDE_GAMES", "1")
        if answer_threshold is not None:
            monkeypatch.setenv("DOC_RAG_ANSWER_THRESHOLD", str(answer_threshold))

    game_grounding = _load_module(f"local_docs_game_grounding_{tmp_path.name}", PYTHON_VOICE_DIR / "game_grounding.py")
    local_docs_rag = _load_module(f"local_docs_rag_{tmp_path.name}", PYTHON_VOICE_DIR / "local_docs_rag.py")

    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)

    qmd_root = tmp_path / "qmd"
    docs_dir = qmd_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "manual.md").write_text(
        "# Safety Manual\n\nHydration matters during sessions. Offer water breaks when needed.\n",
        encoding="utf-8",
    )
    (docs_dir / "lab_identity.md").write_text(
        "\n".join(
            [
                "# BioAdaptive Interface Lab",
                "",
                "Lab name: BioAdaptive Interface Lab.",
                "Institution: Wilfrid Laurier University.",
                "Location: Brantford, Ontario, Canada.",
                "The BioAdaptive Interface Lab studies the intersection of human physiology and digital interaction.",
                "The lab works on serious games, virtual reality simulations, social robotics, and brain-computer interfaces.",
                "Director: John E. Munoz, PhD.",
            ]
        ),
        encoding="utf-8",
    )
    (docs_dir / "research.md").write_text(
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
    (docs_dir / "team.md").write_text(
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
                "- Nahomi Ramirez - Master student working on Active Aging Through Play.",
                "- Natalia Luciani - Undergraduate student working on VR for wellbeing.",
                "- Aaron Jumarang - Undergraduate student working on Biofeedback Gaming.",
            ]
        ),
        encoding="utf-8",
    )
    (docs_dir / "news.md").write_text(
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
    (docs_dir / "equipment.md").write_text(
        "\n".join(
            [
                "# Equipment and Tools",
                "",
                "The lab has EEG systems, EMG armbands, VR headsets, Kinect devices, and exergaming kits.",
            ]
        ),
        encoding="utf-8",
    )
    (docs_dir / "README.md").write_text(
        "\n".join(
            [
                "# Query Examples",
                "",
                "- What is the BioAdaptive Interface Lab?",
                "- What equipment does the lab have?",
            ]
        ),
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
                            "bio ductive interface lab",
                        ],
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

    catalog = game_grounding.GameCatalog(manifest_path)
    catalog.qmd_games_dir = games_dir
    catalog._load()

    rag = local_docs_rag.LocalDocsRAG(
        manifest_path=manifest_path,
        game_catalog=catalog,
        doc_root=doc_root or qmd_root,
        embedder=embedder or DummyEmbedder(),
    )
    assert rag.ready, rag.error
    return rag


def test_local_docs_rag_game_card_snippet_omits_recommendation_weight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rag = _build_rag(tmp_path, monkeypatch)

    game_chunk = next(chunk for chunk in rag._chunks_by_id.values() if chunk.doc_type == "game_card" and chunk.title == "Bean Bag Toss")

    assert "recommendation_weight" not in game_chunk.snippet_text
    assert "0.91" not in game_chunk.snippet_text
    assert float(game_chunk.metadata["recommendation_weight"]) == pytest.approx(0.91)
    rag.close()


def test_local_docs_rag_distinguishes_not_doc_from_no_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rag = _build_rag(tmp_path, monkeypatch)

    social_probe = rag.probe("Tell me something fun")
    assert social_probe.stage1_result == "not_doc"
    assert social_probe.stage2_result == ""

    missing_probe = rag.probe("According to the docs, what is the warranty period?")
    assert missing_probe.stage1_result == "doc_candidate"
    assert missing_probe.stage2_result == "no_evidence"
    assert missing_probe.force_doc_reason == "docs_reference"
    rag.close()


def test_local_docs_rag_detects_availability_and_force_doc_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rag = _build_rag(tmp_path, monkeypatch)

    probe = rag.probe("What games do you have?")

    assert probe.stage1_result == "doc_candidate"
    assert probe.stage2_result == "doc_answer"
    assert probe.force_doc_reason == "availability_query"
    assert probe.answer_mode == "availability"
    assert probe.payload["domain"] == "game"
    assert probe.payload["type"] == "doc_answer"
    rag.close()


def test_local_docs_rag_distinguishes_introduce_from_factual(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rag = _build_rag(tmp_path, monkeypatch)

    introduce_probe = rag.probe("Tell me about Disc Golf")
    factual_probe = rag.probe("How many players does Disc Golf support?")

    assert introduce_probe.answer_mode == "introduce"
    assert introduce_probe.payload["primary_entity"] == "Disc Golf"
    assert factual_probe.answer_mode == "factual"
    assert "players" in factual_probe.payload["text"].lower()
    rag.close()


def test_local_docs_rag_general_factual_queries_get_doc_prior_and_rescue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rag = _build_rag(tmp_path, monkeypatch)

    equipment_probe = rag.probe("What equipment does the lab have?")
    person_probe = rag.probe("Who is John E. Munoz?")
    robotics_probe = rag.probe("Does the lab work on social robotics?")

    assert equipment_probe.stage1_result == "doc_candidate"
    assert equipment_probe.query_doc_affinity >= 0.5
    assert equipment_probe.stage1_reason in {"retrieval_rescue_doc_candidate", "stable_doc_candidate", "borderline_doc_candidate"}
    assert equipment_probe.stage2_result == "doc_answer"
    assert "eeg" in equipment_probe.payload["text"].lower()

    assert person_probe.stage1_result == "doc_candidate"
    assert person_probe.query_doc_affinity >= 0.5
    assert person_probe.stage2_result == "doc_answer"
    assert "john e. munoz" in person_probe.payload["text"].lower()

    assert robotics_probe.stage1_result == "doc_candidate"
    assert robotics_probe.query_doc_affinity >= 0.5
    assert robotics_probe.stage2_result == "doc_answer"
    assert "social robotics" in robotics_probe.payload["text"].lower()
    rag.close()


def test_local_docs_rag_supports_repo_tracked_direct_doc_root_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    original_root = tmp_path / "qmd"
    rag = _build_rag(tmp_path, monkeypatch)
    direct_root = tmp_path / "repo_docs"
    direct_root.mkdir(parents=True, exist_ok=True)
    for path in (original_root / "docs").iterdir():
        target = direct_root / path.name
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    rag.close()

    direct_rag = _build_rag(tmp_path, monkeypatch, doc_root=direct_root)
    probe = direct_rag.probe("What is the BioAdaptive Interface Lab?")

    assert probe.stage1_result == "doc_candidate"
    assert probe.stage2_result == "doc_answer"
    assert probe.domain == "general"
    assert "bioadaptive interface lab" in probe.payload["text"].lower()
    direct_rag.close()


def test_local_docs_rag_clarifies_missing_entity_and_ambiguous_intent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rag = _build_rag(tmp_path, monkeypatch)

    missing_entity = rag.probe("Tell me about the game")
    ambiguous_intent = rag.probe("Disc Golf or Bean Bag Toss?")

    assert missing_entity.stage2_result == "doc_clarify"
    assert missing_entity.clarify_kind == "clarify_missing_entity"
    assert ambiguous_intent.stage2_result == "doc_clarify"
    assert ambiguous_intent.clarify_kind == "clarify_ambiguous_intent"
    rag.close()


def test_local_docs_rag_stage1_reason_distinguishes_low_affinity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rag = _build_rag(tmp_path, monkeypatch)

    probe = rag.probe("Tell me something fun")

    assert probe.stage1_result == "not_doc"
    assert probe.stage1_reason == "low_query_doc_affinity"
    rag.close()


def test_local_docs_rag_skips_readme_and_query_example_docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rag = _build_rag(tmp_path, monkeypatch)

    chunk_paths = {Path(chunk.source_path).name for chunk in rag._chunks_by_id.values()}

    assert "README.md" not in chunk_paths
    rag.close()


def test_local_docs_rag_persists_entity_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rag = _build_rag(tmp_path, monkeypatch)

    assert rag.entity_registry_path.exists()
    registry_payload = json.loads(rag.entity_registry_path.read_text(encoding="utf-8"))
    canonicals = {str(item.get("canonical") or "") for item in registry_payload.get("entries", [])}

    assert "BioAdaptive Interface Lab" in canonicals
    assert "John E. Munoz" in canonicals
    rag.close()


def test_local_docs_rag_diagnostics_report_general_docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rag = _build_rag(tmp_path, monkeypatch)

    diagnostics = rag.diagnostics()

    assert diagnostics["ready"] is True
    assert diagnostics["docs_dir_exists"] is True
    assert diagnostics["general_source_files"] >= 2
    assert diagnostics["general_chunk_count"] >= 2
    assert diagnostics["game_chunk_count"] >= 1
    assert diagnostics["entity_registry_count"] >= 2
    rag.close()


def test_local_docs_rag_requires_ready_embedder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sparse_only_embedder = SimpleNamespace(ready=False, error="doc_rag embedder unavailable")
    if str(PYTHON_VOICE_DIR) not in sys.path:
        sys.path.insert(0, str(PYTHON_VOICE_DIR))
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    monkeypatch.setenv("DOC_RAG_ENABLE", "1")
    monkeypatch.setenv("DOC_RAG_INCLUDE_GAMES", "1")

    game_grounding = _load_module(f"local_docs_game_grounding_fail_{tmp_path.name}", PYTHON_VOICE_DIR / "game_grounding.py")
    local_docs_rag = _load_module(f"local_docs_rag_fail_{tmp_path.name}", PYTHON_VOICE_DIR / "local_docs_rag.py")

    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    qmd_root = tmp_path / "qmd"
    (qmd_root / "docs").mkdir(parents=True, exist_ok=True)
    ((qmd_root / "docs") / "lab_identity.md").write_text("# BioAdaptive Interface Lab\n", encoding="utf-8")
    catalog = game_grounding.GameCatalog(manifest_path)
    catalog._load()

    rag = local_docs_rag.LocalDocsRAG(
        manifest_path=manifest_path,
        game_catalog=catalog,
        doc_root=qmd_root,
        embedder=sparse_only_embedder,
    )

    diagnostics = rag.diagnostics()
    assert rag.ready is False
    assert diagnostics["ready"] is False
    assert diagnostics["dense_ready"] is False
    assert rag.error == "doc_rag embedder unavailable"
    assert rag.probe("What equipment does the lab have?").stage1_reason == "doc_rag embedder unavailable"


def test_local_docs_rag_resolver_handles_typo_variants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rag = _build_rag(tmp_path, monkeypatch)

    for query in (
        "What is Bioadactive Interface Lab?",
        "What is Bio Ductive Interface Lab?",
        "What is Bio Adaptive Interface Lab?",
    ):
        probe = rag.probe(query)
        assert probe.stage1_result == "doc_candidate"
        assert probe.stage2_result in {"doc_answer", "doc_clarify"}
        assert probe.resolver_attempted is True
        assert "BioAdaptive Interface Lab" in probe.candidate_entity_rewrites
        assert probe.open_world_fallback_blocked is (probe.stage2_result == "doc_clarify")
        if probe.stage2_result == "doc_clarify":
            assert probe.clarify_kind == "clarify_typo_correction"
        else:
            assert "bioadaptive interface lab" in probe.response_text.lower()
    rag.close()


def test_local_docs_rag_alias_override_keeps_bio_query_doc_grounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rag = _build_rag(tmp_path, monkeypatch)

    probe = rag.probe("Do you know BIO adaptive interface?")

    assert probe.stage1_result == "doc_candidate"
    assert probe.stage2_result == "doc_answer"
    assert probe.answer_mode == "introduce"
    assert probe.general_focus == "overview"
    assert "bioadaptive interface lab" in probe.response_text.lower()
    assert "social robotics" in probe.response_text.lower() or "wilfrid laurier university" in probe.response_text.lower()
    rag.close()


def test_local_docs_rag_person_lookup_prefers_doc_clarify_for_partial_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rag = _build_rag(tmp_path, monkeypatch)

    probe = rag.probe("Who is John?")

    assert probe.stage1_result == "doc_candidate"
    assert probe.stage2_result == "doc_clarify"
    assert probe.clarify_kind == "clarify_missing_entity"
    assert "john e. munoz" in probe.response_text.lower()
    assert probe.open_world_fallback_blocked is True
    rag.close()


def test_local_docs_rag_people_intro_prefers_team_docs_over_news(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rag = _build_rag(tmp_path, monkeypatch)

    probe = rag.probe("What do you know about John?")

    assert probe.stage2_result in {"doc_answer", "doc_clarify"}
    assert probe.general_focus == "people"
    assert "march 2026" not in probe.response_text.lower()
    assert "promotional video" not in probe.response_text.lower()
    rag.close()


def test_local_docs_rag_overview_prefers_identity_over_news(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rag = _build_rag(tmp_path, monkeypatch)

    probe = rag.probe("What do you know about BIO Adaptive Interface Lab?")

    assert probe.stage2_result == "doc_answer"
    assert probe.general_focus == "overview"
    assert probe.response_text.endswith(".")
    assert "promotional video" not in probe.response_text.lower()
    assert "tabletop social companion" not in probe.response_text.lower()
    assert "wilfrid laurier university" in probe.response_text.lower() or "social robotics" in probe.response_text.lower()
    assert "news" not in probe.general_doc_kinds[:1]
    rag.close()


def test_local_docs_rag_followup_researchers_uses_confirmed_focus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rag = _build_rag(tmp_path, monkeypatch)

    focus_state = SimpleNamespace(
        focused_entity="BioAdaptive Interface Lab",
        candidate_entities=["BioAdaptive Interface Lab"],
        focus_source="answer",
    )
    probe = rag.probe("What researchers do they have?", focus_state=focus_state)

    assert probe.stage2_result == "doc_answer"
    assert probe.general_focus == "people"
    assert "john e. munoz" in probe.response_text.lower()
    assert "nahomi" in probe.response_text.lower() or "natalia" in probe.response_text.lower()
    assert "promotional video" not in probe.response_text.lower()
    rag.close()


def test_local_docs_rag_followup_pronoun_without_confirmed_focus_does_not_answer_with_news(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rag = _build_rag(tmp_path, monkeypatch)

    probe = rag.probe("What researchers do they have?")

    assert probe.stage2_result in {"doc_clarify", "no_evidence"}
    assert "promotional video" not in probe.response_text.lower()
    rag.close()


def test_local_docs_rag_person_lookup_does_not_fabricate_generic_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rag = _build_rag(tmp_path, monkeypatch)

    probe = rag.probe("Who is Rachel?")

    assert probe.stage2_result != "doc_answer"
    assert probe.response_text
    assert probe.open_world_fallback_blocked is True or probe.stage1_result == "not_doc"
    rag.close()


def test_local_docs_rag_memory_only_reranks_without_expanding_candidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rag = _build_rag(tmp_path, monkeypatch)

    probe = rag.probe(
        "Recommend Disc Golf or Bean Bag Toss for two players.",
        focus_state=SimpleNamespace(focused_entity="", candidate_entities=[]),
        user_profile={"favorite_game": "Balance Quest", "likes": ["balance"]},
    )

    assert probe.stage2_result == "doc_answer"
    assert probe.answer_mode == "recommend"
    assert set(probe.payload["candidate_entities"]) <= {"Disc Golf", "Bean Bag Toss"}
    assert probe.payload["primary_entity"] in {"Disc Golf", "Bean Bag Toss"}
    assert probe.payload["primary_entity"] != "Balance Quest"
    rag.close()


def test_local_docs_rag_answer_threshold_can_force_no_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rag = _build_rag(tmp_path, monkeypatch, answer_threshold=0.9)

    probe = rag.probe("Tell me about Disc Golf")

    assert probe.stage1_result == "doc_candidate"
    assert probe.stage2_result == "no_evidence"
    assert probe.fallback_reason == "answerability_below_threshold"
    rag.close()
