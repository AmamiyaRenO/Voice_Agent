from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


_SPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
INTENT_SERVICE_DIR = SCRIPTS_DIR / "intent_service"
for _module_dir in (SCRIPTS_DIR, INTENT_SERVICE_DIR):
    _module_dir_str = str(_module_dir)
    if _module_dir_str not in sys.path:
        sys.path.insert(0, _module_dir_str)

try:
    from manifest_resolver import ManifestAliasResolver
except Exception:
    ManifestAliasResolver = None

_EXPLAIN_HINTS = ("what is", "what's", "explain", "describe", "tell me about", "say what")
_EXPLAIN_HINTS_RAW = ("是什么", "介绍", "说说", "讲讲")
_RECOMMEND_HINTS = ("recommend", "suggest", "what game should", "which game should", "best game")
_RECOMMEND_HINTS_RAW = ("推荐", "建议", "玩什么", "什么游戏")
_LIST_HINTS = (
    "what games",
    "which games",
    "available games",
    "games do you have",
    "do you have any games",
    "any games",
    "what game do you have",
    "what else do you have",
)
_LIST_HINTS_RAW = ("有什么游戏", "有哪些游戏", "有什么可玩", "还有什么游戏")
_ALTERNATIVE_HINTS = (
    "other game",
    "another game",
    "other recommendation",
    "another recommendation",
    "different game",
    "other option",
    "another option",
    "something else",
    "what else",
    "besides",
    "except for",
    "except",
    "other one",
    "another one",
    "not ",
)
_ALTERNATIVE_HINTS_RAW = ("还有别的推荐", "换一个", "别的游戏", "还有别的吗", "再推荐一个", "别推荐这个")
_INTRODUCE_HINTS = (
    "introduce",
    "tell me about",
    "what is",
    "what's",
    "describe",
    "explain",
    "overview",
    "current choice",
    "current option",
    "current recommendation",
)
_COMPARE_HINTS = (
    "compare",
    "difference",
    "different",
    "better than",
    "versus",
    "vs",
    "compare them",
)
_GAME_DOMAIN_HINTS = (
    "game",
    "games",
    "option",
    "options",
    "recommend",
    "play",
    "introduce",
    "choice",
    "available",
)
_SINGLE_SENTENCE_HINTS = ("one short sentence", "one sentence", "single sentence")
_GAME_ALIAS_FUZZY_THRESHOLD = 72
_GAME_CONTEXT_MAX_AGE_SEC = 600.0
_PLAYER_COUNT_WORDS = {
    "solo": 1,
    "single": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
}
_DEFAULT_CARD_OVERRIDES: Dict[str, Dict[str, Any]] = {
    "cornhole": {
        "description": "Bean Bag Toss is a tossing game where players throw bean bags at a raised board with a hole.",
        "how_to_play": "Players take turns throwing bean bags and score by landing on the board or through the hole.",
        "players_min": 1,
        "players_max": 1,
        "tags": ["casual", "throwing", "accuracy", "precision", "coordination", "low-impact", "stationary", "upper-body"],
        "activity_level": "low",
        "recommendation_weight": 0.85,
    },
    "disc_golf": {
        "description": "Disc Golf is a throwing game where players aim flying discs at basket-style targets.",
        "how_to_play": "Players throw discs toward the target and try to finish each hole in as few throws as possible.",
        "players_min": 1,
        "players_max": 1,
        "tags": ["throwing", "walking", "accuracy", "precision", "balance", "mobility", "endurance", "cardio", "dynamic", "upper-body"],
        "activity_level": "medium",
        "recommendation_weight": 0.8,
    },
}
_ACTIVITY_LEVEL_SCORE = {"low": 0.0, "medium": 1.0, "high": 2.0}
_GOAL_RULES: List[Dict[str, Any]] = [
    {
        "keywords": {"walk", "walking", "cardio", "endurance", "stamina", "steps", "aerobic"},
        "boost_tags": {"walking", "endurance", "cardio", "dynamic"},
        "boost_levels": {"medium", "high"},
        "penalty_tags": {"stationary"},
        "weight": 2.8,
        "reason": "it better matches your walking or endurance goal",
    },
    {
        "keywords": {"balance", "mobility", "move", "movement", "range", "range of motion", "agility"},
        "boost_tags": {"balance", "mobility", "dynamic", "walking"},
        "weight": 2.2,
        "reason": "it better matches your mobility or balance goal",
    },
    {
        "keywords": {"accuracy", "aim", "coordination", "precision", "hand eye", "focus"},
        "boost_tags": {"accuracy", "precision", "coordination", "throwing"},
        "weight": 2.0,
        "reason": "it better matches your accuracy or coordination goal",
    },
    {
        "keywords": {"gentle", "easy", "light", "warm up", "warmup", "low impact"},
        "boost_tags": {"low-impact", "stationary", "casual"},
        "boost_levels": {"low"},
        "penalty_levels": {"medium", "high"},
        "weight": 2.0,
        "reason": "it is the gentler option right now",
    },
]
_LIMITATION_RULES: List[Dict[str, Any]] = [
    {
        "keywords": {"tired", "fatigue", "fatigued", "sore", "pain", "hurting", "low energy", "exhausted"},
        "boost_tags": {"low-impact", "stationary", "casual"},
        "penalty_tags": {"walking", "cardio", "dynamic", "endurance"},
        "penalty_levels": {"medium", "high"},
        "weight": 3.0,
        "reason": "it is the lower-effort option for how you feel today",
    },
    {
        "keywords": {"knee", "knees", "ankle", "ankles", "hip", "hips", "back", "lower back"},
        "boost_tags": {"low-impact", "stationary"},
        "penalty_tags": {"walking", "dynamic", "balance", "mobility"},
        "penalty_levels": {"medium", "high"},
        "weight": 3.2,
        "reason": "it avoids extra walking and lower-body load",
    },
    {
        "keywords": {"shoulder", "arm", "elbow", "wrist", "hand"},
        "boost_tags": {"low-impact", "stationary"},
        "penalty_tags": {"upper-body", "throwing", "dynamic"},
        "penalty_levels": {"medium", "high"},
        "weight": 2.4,
        "reason": "it is the safer throwing option for an upper-body limitation",
    },
]
_VARIETY_HINTS = ("different", "another", "something else", "not the same", "change it up", "variety")


def _normalize_text(text: str) -> str:
    value = _SPACE_RE.sub(" ", (text or "").strip().lower())
    if not value:
        return ""
    value = _NON_ALNUM_RE.sub(" ", value)
    return _SPACE_RE.sub(" ", value).strip()


def _clean_text(text: str, *, max_len: int = 240) -> str:
    clean = " ".join((text or "").strip().split())
    if not clean:
        return ""
    if len(clean) <= max_len:
        return clean
    return clean[: max(1, int(max_len))].rstrip()


def _coerce_string_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        merged = value.replace("\r\n", "\n").replace(";", ",").replace("\n", ",")
        return [part.strip() for part in merged.split(",") if part.strip()]
    return []


def _wants_single_sentence(text: str) -> bool:
    normalized = _normalize_text(text)
    return any(hint in normalized for hint in _SINGLE_SENTENCE_HINTS)


def _contains_any_hint(text: str, normalized_text: str, ascii_hints: tuple[str, ...], raw_hints: tuple[str, ...] = ()) -> bool:
    if any(hint in normalized_text for hint in ascii_hints):
        return True
    raw = str(text or "")
    if not raw:
        return False
    return any(hint in raw for hint in raw_hints)


def _text_contains_keyword(text: str, keyword: str) -> bool:
    normalized_text = _normalize_text(text)
    normalized_keyword = _normalize_text(keyword)
    if not normalized_text or not normalized_keyword:
        return False
    return f" {normalized_keyword} " in f" {normalized_text} "


def _profile_texts(user_profile: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for key in ("goals", "likes", "dislikes", "recent_notes"):
        for value in user_profile.get(key, []) or []:
            text = str(value).strip()
            if text:
                out.append(text)
    for entry in user_profile.get("episodes", []) or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("role") or "user").strip().lower() != "user":
            continue
        text = str(entry.get("text") or "").strip()
        if text:
            out.append(text)
    return out


def _normalized_tags(card: "GameCard") -> set[str]:
    return {_normalize_text(tag) for tag in card.tags if _normalize_text(tag)}


def _activity_level(card: "GameCard") -> str:
    value = _normalize_text(card.activity_level)
    return value if value in _ACTIVITY_LEVEL_SCORE else ""


def _rule_hits(profile_texts: List[str], rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    hits: List[Dict[str, Any]] = []
    for rule in rules:
        keywords = {str(keyword).strip().lower() for keyword in rule.get("keywords", set()) if str(keyword).strip()}
        if not keywords:
            continue
        if any(_text_contains_keyword(text, keyword) for text in profile_texts for keyword in keywords):
            hits.append(rule)
    return hits


def _extract_player_count(text: str) -> Optional[int]:
    normalized = _normalize_text(text)
    if not normalized:
        return None
    explicit_match = re.search(r"\b(?:for|with)\s+([a-z0-9]+)\s*(?:player|players|people|person)\b", normalized)
    if explicit_match:
        raw = explicit_match.group(1)
        if raw.isdigit():
            return max(1, int(raw))
        if raw in _PLAYER_COUNT_WORDS:
            return _PLAYER_COUNT_WORDS[raw]
    hyphen_match = re.search(r"\b([1-4])\s*player\b", normalized)
    if hyphen_match:
        try:
            return max(1, int(hyphen_match.group(1)))
        except Exception:
            return None
    if " single player " in f" {normalized} " or " single-player " in f" {normalized} ":
        return 1
    if " solo " in f" {normalized} " or " by myself " in f" {normalized} " or " for me " in f" {normalized} ":
        return 1
    if "for me" in normalized or "by myself" in normalized:
        return 1
    return None


def _matches_any(card: "GameCard", values: List[str]) -> bool:
    normalized_pool = {
        _normalize_text(card.name),
        _normalize_text(card.game_id),
        *[_normalize_text(alias) for alias in card.aliases],
        *[_normalize_text(tag) for tag in card.tags],
    }
    for value in values:
        normalized_value = _normalize_text(value)
        if normalized_value and normalized_value in normalized_pool:
            return True
    return False


def normalize_manifest_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    games_in = payload.get("games")
    if not isinstance(games_in, list):
        games_in = []
    games_out: List[Dict[str, Any]] = []
    for item in games_in:
        if not isinstance(item, dict):
            continue
        game_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or game_id).strip() or game_id
        overrides = _DEFAULT_CARD_OVERRIDES.get(game_id, {})
        synonyms = sorted({name, game_id, *[str(value).strip() for value in item.get("synonyms", []) or [] if str(value).strip()]})
        normalized = {
            "id": game_id,
            "name": name,
            "synonyms": synonyms,
            "exec": str(item.get("exec") or "").strip(),
            "workdir": str(item.get("workdir") or "").strip(),
            "args": list(item.get("args") or []),
            "env": dict(item.get("env") or {}),
            "description": str(item.get("description") or overrides.get("description") or "").strip(),
            "how_to_play": str(item.get("how_to_play") or overrides.get("how_to_play") or "").strip(),
            "players_min": int(item.get("players_min") or overrides.get("players_min") or 1),
            "players_max": int(item.get("players_max") or overrides.get("players_max") or 4),
            "tags": _coerce_string_list(item.get("tags") or overrides.get("tags") or []),
            "activity_level": str(item.get("activity_level") or overrides.get("activity_level") or "").strip(),
            "recommendation_weight": float(item.get("recommendation_weight") or overrides.get("recommendation_weight") or 0.5),
        }
        games_out.append(normalized)
    return {"games": games_out}


@dataclass
class GameCard:
    game_id: str
    name: str
    aliases: List[str]
    description: str
    how_to_play: str
    players_min: int
    players_max: int
    tags: List[str]
    activity_level: str
    recommendation_weight: float
    exec_path: str


@dataclass
class GameKnowledgeDoc:
    game_id: str
    game_name: str
    overview: str
    how_to_play: str
    tags: List[str]
    activity_level: str
    players_text: str
    comparison_cues: str
    source_path: str = ""


@dataclass
class GameQaRouteDecision:
    intent: str = "none"
    game_names: List[str] = field(default_factory=list)
    reference_game_name: str = ""
    use_current_choice: bool = False
    use_candidate_set: bool = False
    compare_requested: bool = False


@dataclass
class GameAnswerPlan:
    intent: str
    payload_type: str
    primary_game_name: str = ""
    reference_game_name: str = ""
    candidate_games: List[str] = field(default_factory=list)
    required_terms: List[str] = field(default_factory=list)
    reason_text: str = ""
    fallback_text: str = ""
    doc_snippets: List[str] = field(default_factory=list)
    max_sentences: int = 2


class GameCatalog:
    def __init__(self, manifest_path: Path) -> None:
        self.manifest_path = Path(manifest_path)
        self.qmd_games_dir = REPO_ROOT / "runtime" / "qmd" / "games"
        self.cards: List[GameCard] = []
        self.alias_map: Dict[str, GameCard] = {}
        self.card_map: Dict[str, GameCard] = {}
        self.knowledge_docs: Dict[str, GameKnowledgeDoc] = {}
        self.resolver = ManifestAliasResolver(str(self.manifest_path)) if ManifestAliasResolver is not None else None
        self._manifest_mtime_ns = 0
        self._manifest_size = 0
        self._qmd_signature = ""
        self._last_reload_check_ts = 0.0
        self._load()

    def _load(self) -> None:
        try:
            stat = self.manifest_path.stat()
            self._manifest_mtime_ns = int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9)))
            self._manifest_size = int(stat.st_size)
        except Exception:
            self._manifest_mtime_ns = 0
            self._manifest_size = 0
        try:
            if self.manifest_path.exists():
                with self.manifest_path.open("r", encoding="utf-8-sig") as handle:
                    payload = json.load(handle)
            else:
                payload = {"games": []}
        except Exception:
            payload = {"games": []}
        normalized = normalize_manifest_payload(payload)
        self.cards = []
        self.alias_map = {}
        self.card_map = {}
        self.resolver = ManifestAliasResolver(str(self.manifest_path)) if ManifestAliasResolver is not None else None
        for item in normalized.get("games", []):
            card = GameCard(
                game_id=str(item.get("id") or "").strip(),
                name=str(item.get("name") or "").strip(),
                aliases=[str(alias).strip() for alias in item.get("synonyms", []) or [] if str(alias).strip()],
                description=str(item.get("description") or "").strip(),
                how_to_play=str(item.get("how_to_play") or "").strip(),
                players_min=max(1, int(item.get("players_min") or 1)),
                players_max=max(1, int(item.get("players_max") or 4)),
                tags=[str(tag).strip() for tag in item.get("tags", []) or [] if str(tag).strip()],
                activity_level=str(item.get("activity_level") or "").strip(),
                recommendation_weight=float(item.get("recommendation_weight") or 0.5),
                exec_path=str(item.get("exec") or "").strip(),
            )
            self.cards.append(card)
            for canonical in (card.name, card.game_id):
                normalized_name = _normalize_text(canonical)
                if normalized_name:
                    self.card_map[normalized_name] = card
            for alias in [card.name, card.game_id, *card.aliases]:
                normalized_alias = _normalize_text(alias)
                if normalized_alias:
                    self.alias_map[normalized_alias] = card
        self.knowledge_docs = self._load_knowledge_docs()
        self._qmd_signature = self._compute_qmd_signature()

    def _compute_qmd_signature(self) -> str:
        if not self.qmd_games_dir.exists():
            return ""
        parts: List[str] = []
        for path in sorted(self.qmd_games_dir.glob("*.qmd")):
            try:
                stat = path.stat()
                parts.append(
                    f"{path.name}:{int(getattr(stat, 'st_mtime_ns', int(stat.st_mtime * 1e9)))}:{int(stat.st_size)}"
                )
            except Exception:
                parts.append(path.name)
        return "|".join(parts)

    def _maybe_reload(self, *, force: bool = False) -> None:
        now_ts = time.time()
        if not force and now_ts - self._last_reload_check_ts < 0.5:
            return
        self._last_reload_check_ts = now_ts
        manifest_mtime_ns = 0
        manifest_size = 0
        try:
            stat = self.manifest_path.stat()
            manifest_mtime_ns = int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9)))
            manifest_size = int(stat.st_size)
        except Exception:
            pass
        qmd_signature = self._compute_qmd_signature()
        if force or manifest_mtime_ns != self._manifest_mtime_ns or manifest_size != self._manifest_size or qmd_signature != self._qmd_signature:
            self._load()

    @staticmethod
    def _players_text(card: GameCard) -> str:
        if card.players_min == card.players_max:
            return f"{card.players_min} player" + ("" if card.players_min == 1 else "s")
        return f"{card.players_min} to {card.players_max} players"

    @staticmethod
    def _comparison_cues_for_card(card: GameCard) -> str:
        tags = _normalized_tags(card)
        cues: List[str] = []
        if "walking" in tags:
            cues.append("adds more walking")
        if "balance" in tags:
            cues.append("leans more on balance")
        if "low-impact" in tags or "stationary" in tags:
            cues.append("is gentler and more stationary")
        if "accuracy" in tags or "precision" in tags:
            cues.append("focuses on accuracy")
        return "; ".join(cues[:3])

    @staticmethod
    def _extract_qmd_sections(text: str) -> Dict[str, str]:
        sections: Dict[str, str] = {}
        current_key = ""
        lines: List[str] = []
        for raw_line in str(text or "").splitlines():
            line = raw_line.rstrip()
            heading_match = re.match(r"^##\s+(.+?)\s*$", line)
            if heading_match:
                if current_key:
                    sections[current_key] = "\n".join(lines).strip()
                current_key = _normalize_text(heading_match.group(1))
                lines = []
                continue
            if current_key:
                lines.append(line)
        if current_key:
            sections[current_key] = "\n".join(lines).strip()
        return sections

    def _load_knowledge_docs(self) -> Dict[str, GameKnowledgeDoc]:
        docs: Dict[str, GameKnowledgeDoc] = {}
        for card in self.cards:
            overview = card.description
            how_to_play = card.how_to_play
            source_path = ""
            qmd_path = self.qmd_games_dir / f"{card.game_id}.qmd"
            if qmd_path.exists():
                try:
                    raw_text = qmd_path.read_text(encoding="utf-8")
                    sections = self._extract_qmd_sections(raw_text)
                    overview = str(sections.get("description") or overview).strip()
                    how_to_play = str(sections.get("how to play") or how_to_play).strip()
                    tags_section = str(sections.get("tags") or "").strip()
                    qmd_tags = [
                        line.lstrip("- ").strip()
                        for line in tags_section.splitlines()
                        if line.strip().startswith("-")
                    ]
                    tags = qmd_tags or list(card.tags)
                    source_path = str(qmd_path)
                except Exception:
                    tags = list(card.tags)
            else:
                tags = list(card.tags)
            docs[_normalize_text(card.name)] = GameKnowledgeDoc(
                game_id=card.game_id,
                game_name=card.name,
                overview=overview,
                how_to_play=how_to_play,
                tags=tags,
                activity_level=card.activity_level,
                players_text=self._players_text(card),
                comparison_cues=self._comparison_cues_for_card(card),
                source_path=source_path,
            )
        return docs

    def _resolve_card_from_name(self, value: str) -> Optional[GameCard]:
        normalized = _normalize_text(value)
        if not normalized:
            return None
        return self.card_map.get(normalized) or self.alias_map.get(normalized)

    def resolve_card(self, text: str) -> Optional[GameCard]:
        self._maybe_reload()
        normalized = _normalize_text(text)
        if not normalized:
            return None
        if normalized in self.alias_map:
            return self.alias_map[normalized]
        haystack = f" {normalized} "
        best: Optional[GameCard] = None
        best_len = 0
        for alias, card in self.alias_map.items():
            if f" {alias} " not in haystack:
                continue
            if len(alias) > best_len:
                best = card
                best_len = len(alias)
        if best is not None:
            return best
        if self.resolver is not None:
            candidate_name = self.resolver.canonical_name(text) or self.resolver.resolve_best_name(
                text,
                _GAME_ALIAS_FUZZY_THRESHOLD,
            )
            if candidate_name:
                resolved = self._resolve_card_from_name(candidate_name)
                if resolved is not None:
                    return resolved
        return best

    def extract_game_mentions(self, text: str, *, limit: int = 3) -> List[str]:
        self._maybe_reload()
        normalized = _normalize_text(text)
        if not normalized:
            return []
        hits: List[tuple[int, int, str]] = []
        for alias, card in self.alias_map.items():
            pattern = re.compile(rf"(?<!\S){re.escape(alias)}(?!\S)")
            for match in pattern.finditer(normalized):
                hits.append((match.start(), -len(alias), card.name))
        hits.sort()
        out: List[str] = []
        seen = set()
        for _, _, name in hits:
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(name)
            if len(out) >= limit:
                return out
        if self.resolver is not None:
            fallback = self.resolver.resolve_best_name(text, _GAME_ALIAS_FUZZY_THRESHOLD)
            if fallback:
                key = fallback.casefold()
                if key not in seen:
                    out.append(fallback)
        return out[:limit]

    def profile_matches(self, card: GameCard, values: List[str]) -> bool:
        return _matches_any(card, values)

    def _context_card(self, user_profile: Optional[Dict[str, Any]]) -> Optional[GameCard]:
        profile = user_profile or {}
        now_ts = time.time()
        for field in (
            ("last_game_mentioned", "last_game_mentioned_ts"),
            ("last_game_recommended", "last_game_recommended_ts"),
            ("last_game_launched", "last_game_launched_ts"),
            ("last_game_reference", "last_game_reference_ts"),
        ):
            value = str(profile.get(field[0]) or "").strip()
            if not value:
                continue
            ts = float(profile.get(field[1]) or 0.0)
            if ts > 0.0 and now_ts - ts > 600.0:
                continue
            card = self.resolve_card(value)
            if card is not None:
                return card
        for item in profile.get("recent_game_candidates", []) or []:
            if not isinstance(item, dict):
                continue
            value = str(item.get("game_name") or "").strip()
            if not value:
                continue
            ts = float(item.get("ts") or 0.0)
            if ts > 0.0 and now_ts - ts > 600.0:
                continue
            card = self.resolve_card(value)
            if card is not None:
                return card
        return None

    @staticmethod
    def _session_state_value(session_state: Any, field: str) -> Any:
        if isinstance(session_state, dict):
            return session_state.get(field)
        return getattr(session_state, field, None)

    def _session_context_card(self, session_state: Any, user_profile: Optional[Dict[str, Any]]) -> Optional[GameCard]:
        now_ts = time.time()
        focused_game = str(self._session_state_value(session_state, "focused_game") or "").strip()
        updated_at = float(self._session_state_value(session_state, "updated_at") or 0.0)
        if focused_game and (updated_at <= 0.0 or now_ts - updated_at <= _GAME_CONTEXT_MAX_AGE_SEC):
            card = self.resolve_card(focused_game)
            if card is not None:
                return card
        primary = str(self._session_state_value(session_state, "primary_recommendation") or "").strip()
        if primary and (updated_at <= 0.0 or now_ts - updated_at <= _GAME_CONTEXT_MAX_AGE_SEC):
            card = self.resolve_card(primary)
            if card is not None:
                return card
        return self._context_card(user_profile)

    def _session_candidate_cards(self, session_state: Any, user_profile: Optional[Dict[str, Any]]) -> List[GameCard]:
        now_ts = time.time()
        updated_at = float(self._session_state_value(session_state, "updated_at") or 0.0)
        raw_candidates = self._session_state_value(session_state, "candidate_games") or []
        cards: List[GameCard] = []
        seen = set()
        if updated_at <= 0.0 or now_ts - updated_at <= _GAME_CONTEXT_MAX_AGE_SEC:
            for raw in raw_candidates:
                card = self.resolve_card(str(raw))
                if card is None:
                    continue
                folded = card.name.casefold()
                if folded in seen:
                    continue
                seen.add(folded)
                cards.append(card)
        if cards:
            return cards
        return list(self.cards)

    def knowledge_doc(self, game_name: str) -> Optional[GameKnowledgeDoc]:
        self._maybe_reload()
        normalized = _normalize_text(game_name)
        if not normalized:
            return None
        card = self.resolve_card(game_name)
        if card is None:
            return None
        return self.knowledge_docs.get(_normalize_text(card.name))

    def looks_like_game_domain(self, text: str, session_state: Any = None) -> bool:
        normalized = _normalize_text(text)
        if not normalized:
            return False
        if self.extract_game_mentions(text, limit=1):
            return True
        padded = f" {normalized} "
        if any(f" {hint} " in padded for hint in _GAME_DOMAIN_HINTS):
            return True
        if session_state is not None:
            if str(self._session_state_value(session_state, "focused_game") or "").strip():
                return True
            if self._session_state_value(session_state, "candidate_games"):
                return True
        return False

    def route_game_query(
        self,
        text: str,
        *,
        session_state: Any = None,
        user_profile: Optional[Dict[str, Any]] = None,
        forced_intent: str = "",
    ) -> GameQaRouteDecision:
        self._maybe_reload()
        normalized = _normalize_text(text)
        if not normalized:
            return GameQaRouteDecision()
        mentioned_names = self.extract_game_mentions(text, limit=4)
        forced = _normalize_text(forced_intent)
        if forced in {
            "game recommend",
            "game alternative",
            "game availability",
            "game introduce",
            "game compare",
        }:
            intent = forced.replace(" ", "_")
            return GameQaRouteDecision(
                intent=intent,
                game_names=mentioned_names,
                reference_game_name=mentioned_names[-1] if mentioned_names else "",
                use_current_choice="current choice" in normalized or "current option" in normalized,
                use_candidate_set=" them " in f" {normalized} " or " all " in f" {normalized} ",
                compare_requested=intent == "game_compare",
            )

        padded = f" {normalized} "
        if _contains_any_hint(text, normalized, _LIST_HINTS, _LIST_HINTS_RAW):
            return GameQaRouteDecision(intent="game_availability", game_names=mentioned_names, use_candidate_set=True)
        if any(hint in normalized for hint in _COMPARE_HINTS):
            return GameQaRouteDecision(intent="game_compare", game_names=mentioned_names, compare_requested=True)
        if _contains_any_hint(text, normalized, _ALTERNATIVE_HINTS, _ALTERNATIVE_HINTS_RAW):
            reference = mentioned_names[-1] if mentioned_names else ""
            return GameQaRouteDecision(intent="game_alternative", game_names=mentioned_names, reference_game_name=reference)
        if _contains_any_hint(text, normalized, _RECOMMEND_HINTS, _RECOMMEND_HINTS_RAW):
            return GameQaRouteDecision(intent="game_recommend", game_names=mentioned_names)
        if any(hint in normalized for hint in _INTRODUCE_HINTS):
            use_candidate_set = " them " in padded or " all " in padded or "games" in padded
            use_current_choice = "current choice" in normalized or "current option" in normalized or "current recommendation" in normalized
            return GameQaRouteDecision(
                intent="game_introduce",
                game_names=mentioned_names,
                reference_game_name=mentioned_names[-1] if mentioned_names else "",
                use_current_choice=use_current_choice,
                use_candidate_set=use_candidate_set,
            )
        if mentioned_names and self.looks_like_game_domain(text, session_state=session_state):
            return GameQaRouteDecision(intent="game_introduce", game_names=mentioned_names, reference_game_name=mentioned_names[-1])
        if session_state is not None and self.looks_like_game_domain(text, session_state=session_state):
            if " it " in padded or " the game " in padded or " that one " in padded:
                return GameQaRouteDecision(intent="game_introduce")
        return GameQaRouteDecision()

    def retrieve_game_sections(self, game_name: str, *, sections: List[str]) -> List[str]:
        doc = self.knowledge_doc(game_name)
        if doc is None:
            return []
        snippets: List[str] = []
        for section in sections:
            key = _normalize_text(section)
            text = ""
            if key == "overview":
                text = doc.overview
            elif key == "how to play":
                text = doc.how_to_play
            elif key in {"tags", "activity level", "players"}:
                if key == "players":
                    text = f"It is set up for {doc.players_text}."
                elif key == "activity level":
                    level = _normalize_text(doc.activity_level)
                    if level:
                        text = f"It is a {level}-activity option."
                else:
                    if doc.tags:
                        text = "It is tagged for " + ", ".join(doc.tags[:4]) + "."
            elif key == "comparison cues":
                text = doc.comparison_cues
            text = _clean_text(text, max_len=220)
            if text:
                snippets.append(text)
        return snippets

    def _games_text(self, cards: List[GameCard]) -> str:
        return self._format_card_names(cards)

    def _build_intro_plan(
        self,
        text: str,
        *,
        decision: GameQaRouteDecision,
        session_state: Any,
        user_profile: Optional[Dict[str, Any]],
    ) -> Optional[GameAnswerPlan]:
        selected_cards: List[GameCard] = []
        if decision.use_candidate_set:
            selected_cards = self._session_candidate_cards(session_state, user_profile)[:3]
        elif decision.use_current_choice:
            primary_name = str(self._session_state_value(session_state, "primary_recommendation") or "").strip()
            if primary_name:
                card = self.resolve_card(primary_name)
                if card is not None:
                    selected_cards = [card]
        if not selected_cards and decision.reference_game_name:
            card = self.resolve_card(decision.reference_game_name)
            if card is not None:
                selected_cards = [card]
        if not selected_cards and decision.game_names:
            for raw in decision.game_names:
                card = self.resolve_card(raw)
                if card is not None:
                    selected_cards = [card]
                    break
        if not selected_cards:
            context_card = self._session_context_card(session_state, user_profile)
            if context_card is not None:
                selected_cards = [context_card]
        if not selected_cards:
            return GameAnswerPlan(
                intent="game_introduce",
                payload_type="game_explain",
                fallback_text="Tell me which game you want me to introduce, and I can explain it in one or two sentences.",
                max_sentences=2,
            )

        snippets: List[str] = []
        candidate_names = [card.name for card in selected_cards]
        for card in selected_cards:
            card_snippets = self.retrieve_game_sections(card.name, sections=["overview", "how to play"])
            if card_snippets:
                if len(selected_cards) == 1:
                    snippets.extend(card_snippets[:2])
                else:
                    snippets.append(f"{card.name}: {card_snippets[0]}")
        primary = selected_cards[0].name
        if len(selected_cards) == 1:
            if snippets:
                intro = snippets[0].strip()
                if intro.casefold().startswith(primary.casefold()):
                    fallback = intro
                else:
                    fallback = f"{primary} is {intro[0].lower() + intro[1:]}"
            else:
                fallback = f"{primary} is a local game I can explain."
        else:
            fallback = " ".join(snippets[: min(3, len(snippets))]) or ("Current options are " + self._games_text(selected_cards) + ".")
        required_terms = [card.name for card in selected_cards]
        return GameAnswerPlan(
            intent="game_introduce",
            payload_type="game_explain",
            primary_game_name=primary,
            candidate_games=candidate_names,
            required_terms=required_terms,
            fallback_text=fallback,
            doc_snippets=snippets,
            max_sentences=3 if len(selected_cards) > 1 or not _wants_single_sentence(text) else 1,
        )

    def _build_compare_plan(
        self,
        *,
        decision: GameQaRouteDecision,
        session_state: Any,
        user_profile: Optional[Dict[str, Any]],
    ) -> Optional[GameAnswerPlan]:
        cards: List[GameCard] = []
        for raw in decision.game_names:
            card = self.resolve_card(raw)
            if card is None:
                continue
            if all(existing.name.casefold() != card.name.casefold() for existing in cards):
                cards.append(card)
        if len(cards) < 2:
            cards = self._session_candidate_cards(session_state, user_profile)[:2]
        if len(cards) < 2:
            return None
        first, second = cards[0], cards[1]
        first_doc = self.knowledge_doc(first.name)
        second_doc = self.knowledge_doc(second.name)
        first_cue = self._contrast_reason(first, second)
        second_cue = self._contrast_reason(second, first)
        snippets = [
            f"{first.name}: {(first_doc.overview if first_doc else first.description).strip()}",
            f"{second.name}: {(second_doc.overview if second_doc else second.description).strip()}",
        ]
        fallback = f"{first.name} and {second.name} are the current options. {first_cue} {second_cue}"
        return GameAnswerPlan(
            intent="game_compare",
            payload_type="game_explain",
            primary_game_name=first.name,
            candidate_games=[first.name, second.name],
            reference_game_name=second.name,
            required_terms=[first.name, second.name],
            fallback_text=fallback,
            reason_text=f"{first_cue} {second_cue}",
            doc_snippets=snippets,
            max_sentences=3,
        )

    def build_answer_plan(
        self,
        text: str,
        *,
        decision: GameQaRouteDecision,
        session_state: Any = None,
        user_profile: Optional[Dict[str, Any]] = None,
    ) -> Optional[GameAnswerPlan]:
        self._maybe_reload()
        if decision.intent == "game_recommend":
            result = self.recommend_result(text, user_profile=user_profile)
            if not result:
                return None
            primary = str(result.get("primary_game_name") or result.get("game_name") or "").strip()
            snippets = self.retrieve_game_sections(primary, sections=["overview", "how to play"])
            return GameAnswerPlan(
                intent=decision.intent,
                payload_type="game_recommend",
                primary_game_name=primary,
                candidate_games=[str(item).strip() for item in result.get("candidate_games", []) or [] if str(item).strip()],
                required_terms=[primary] if primary else [],
                reason_text=str(result.get("reason_text") or "").strip(),
                fallback_text=str(result.get("text") or "").strip(),
                doc_snippets=snippets,
                max_sentences=2 if not _wants_single_sentence(text) else 1,
            )
        if decision.intent == "game_alternative":
            reference_card = None
            if decision.reference_game_name:
                reference_card = self.resolve_card(decision.reference_game_name)
            if reference_card is None:
                reference_card = self._session_context_card(session_state, user_profile)
            candidate_cards = self._session_candidate_cards(session_state, user_profile)
            if reference_card is not None:
                candidate_cards = [
                    card
                    for card in candidate_cards
                    if _normalize_text(card.name) != _normalize_text(reference_card.name)
                ]
            if not candidate_cards and reference_card is not None:
                candidate_cards = [
                    card
                    for card in self.cards
                    if _normalize_text(card.name) != _normalize_text(reference_card.name)
                ]
            introduced_names = {
                str(name).strip().casefold()
                for name in (self._session_state_value(session_state, "last_introduced_games") or [])
                if str(name).strip()
            }
            if reference_card is not None and candidate_cards and all(card.name.casefold() in introduced_names for card in candidate_cards):
                return self._build_compare_plan(
                    decision=GameQaRouteDecision(intent="game_compare", game_names=[reference_card.name] + [card.name for card in candidate_cards[:1]]),
                    session_state=session_state,
                    user_profile=user_profile,
                )
            if reference_card is None and not candidate_cards:
                result = self.alternative_result(text, user_profile=user_profile)
                if not result:
                    return None
                primary = str(result.get("primary_game_name") or result.get("game_name") or "").strip()
                reference = str(result.get("reference_game_name") or "").strip()
                snippets = self.retrieve_game_sections(primary, sections=["overview", "how to play", "comparison cues"])
                return GameAnswerPlan(
                    intent=decision.intent,
                    payload_type="game_alternative",
                    primary_game_name=primary,
                    reference_game_name=reference,
                    candidate_games=[str(item).strip() for item in result.get("candidate_games", []) or [] if str(item).strip()],
                    required_terms=[term for term in (primary, reference) if term],
                    reason_text=str(result.get("reason_text") or "").strip(),
                    fallback_text=str(result.get("text") or "").strip(),
                    doc_snippets=snippets,
                    max_sentences=2 if not _wants_single_sentence(text) else 1,
                )
            if not candidate_cards:
                reference_name = reference_card.name if reference_card is not None else ""
                return GameAnswerPlan(
                    intent=decision.intent,
                    payload_type="game_alternative",
                    primary_game_name=reference_name,
                    reference_game_name=reference_name,
                    candidate_games=[],
                    required_terms=[reference_name] if reference_name else [],
                    fallback_text=(f"Right now {reference_name} is the only local option I have." if reference_name else "I do not have another local option right now."),
                    max_sentences=2,
                )
            candidate_cards = sorted(candidate_cards, key=lambda card: (card.recommendation_weight, card.name.casefold()), reverse=True)
            lead = candidate_cards[0]
            reference = reference_card.name if reference_card is not None else ""
            snippets = self.retrieve_game_sections(lead.name, sections=["overview", "how to play", "comparison cues"])
            if len(candidate_cards) == 1:
                difference = self._contrast_reason(lead, reference_card) if reference_card is not None else "It is another local option."
                fallback = f"The other option is {lead.name}. {difference}"
                required_terms = [lead.name]
                if reference:
                    required_terms.append(reference)
                return GameAnswerPlan(
                    intent=decision.intent,
                    payload_type="game_alternative",
                    primary_game_name=lead.name,
                    reference_game_name=reference,
                    candidate_games=[lead.name],
                    required_terms=required_terms,
                    reason_text=difference,
                    fallback_text=fallback.strip(),
                    doc_snippets=snippets,
                    max_sentences=2 if not _wants_single_sentence(text) else 1,
                )
            listed = self._games_text(candidate_cards)
            fallback = (
                f"Other options besides {reference} are {listed}. "
                f"I would start with {lead.name}."
                if reference
                else f"Other options are {listed}. I would start with {lead.name}."
            )
            return GameAnswerPlan(
                intent=decision.intent,
                payload_type="game_alternative",
                primary_game_name=lead.name,
                reference_game_name=reference,
                candidate_games=[card.name for card in candidate_cards],
                required_terms=[lead.name] + ([reference] if reference else []),
                reason_text=self._contrast_reason(lead, reference_card) if reference_card is not None else "",
                fallback_text=fallback.strip(),
                doc_snippets=snippets,
                max_sentences=2 if not _wants_single_sentence(text) else 1,
            )
        if decision.intent == "game_availability":
            result = self.list_result(text)
            if not result:
                return None
            primary = str(result.get("primary_game_name") or result.get("game_name") or "").strip()
            candidate_games = [str(item).strip() for item in result.get("candidate_games", []) or [] if str(item).strip()]
            return GameAnswerPlan(
                intent=decision.intent,
                payload_type="game_list",
                primary_game_name=primary,
                candidate_games=candidate_games,
                required_terms=candidate_games[:],
                fallback_text=str(result.get("text") or "").strip(),
                max_sentences=2,
            )
        if decision.intent == "game_introduce":
            return self._build_intro_plan(text, decision=decision, session_state=session_state, user_profile=user_profile)
        if decision.intent == "game_compare":
            return self._build_compare_plan(decision=decision, session_state=session_state, user_profile=user_profile)
        return None

    def _plan_to_payload(self, plan: GameAnswerPlan) -> Dict[str, Any]:
        payload = {
            "type": plan.payload_type,
            "intent": plan.intent,
            "text": plan.fallback_text,
            "game_name": plan.primary_game_name,
            "primary_game_name": plan.primary_game_name,
            "reference_game_name": plan.reference_game_name,
            "candidate_games": list(plan.candidate_games),
            "candidates": list(plan.candidate_games),
            "required_terms": list(plan.required_terms),
            "allowed_game_names": list(plan.candidate_games),
            "reason_text": plan.reason_text,
            "style_goal": "warm_natural",
            "doc_snippets": list(plan.doc_snippets),
            "max_sentences": int(plan.max_sentences or 2),
        }
        if plan.primary_game_name and plan.primary_game_name not in payload["allowed_game_names"]:
            payload["allowed_game_names"].append(plan.primary_game_name)
        if plan.reference_game_name and plan.reference_game_name not in payload["allowed_game_names"]:
            payload["allowed_game_names"].append(plan.reference_game_name)
        return payload

    def explain_result(self, text: str) -> Dict[str, Any]:
        self._maybe_reload()
        normalized = _normalize_text(text)
        card = self.resolve_card(text)
        if not normalized or card is None:
            return {}
        if not _contains_any_hint(text, normalized, _EXPLAIN_HINTS, _EXPLAIN_HINTS_RAW):
            if not (" what " in f" {normalized} " and " is " in f" {normalized} "):
                return {}
        sentence = card.description or card.how_to_play
        if not sentence:
            return {}
        normalized_name = _normalize_text(card.name)
        if " cornhole " in f" {normalized} " and normalized_name != "cornhole":
            core = sentence
            if sentence.lower().startswith(card.name.lower() + " is "):
                core = sentence[len(card.name) + 1 :].lstrip()
            elif sentence.lower().startswith(card.name.lower()):
                core = sentence[len(card.name) :].lstrip(" ,")
            sentence = "Cornhole, also called Bean Bag Toss, " + core
        reply_text = sentence
        follow_up = card.how_to_play or ""
        if follow_up and follow_up != sentence and not _wants_single_sentence(text):
            reply_text = f"{sentence} {follow_up}"
        return {
            "type": "game_explain",
            "text": reply_text.strip(),
            "game_name": card.name,
            "primary_game_name": card.name,
            "candidate_games": [card.name],
            "reason_text": sentence.strip(),
            "style_goal": "warm_natural",
            "description": card.description,
            "how_to_play": card.how_to_play,
            "max_sentences": 3 if not _wants_single_sentence(text) else 1,
        }

    def explain_reply(self, text: str) -> str:
        return str(self.explain_result(text).get("text") or "")

    def _format_card_names(self, cards: List[GameCard]) -> str:
        names = [card.name for card in cards if card.name]
        if not names:
            return ""
        if len(names) == 1:
            return names[0]
        if len(names) == 2:
            return f"{names[0]} and {names[1]}"
        return ", ".join(names[:-1]) + f", and {names[-1]}"

    def list_result(self, text: str) -> Dict[str, Any]:
        self._maybe_reload()
        normalized = _normalize_text(text)
        if not normalized or not _contains_any_hint(text, normalized, _LIST_HINTS, _LIST_HINTS_RAW):
            return {}
        if not self.cards:
            return {
                "type": "game_list",
                "text": "I do not have any local games available right now.",
                "game_name": "",
                "primary_game_name": "",
                "candidate_games": [],
                "reason_text": "",
                "style_goal": "warm_natural",
            }
        if len(self.cards) == 1:
            primary = self.cards[0].name
            reply_text = f"Right now I have {primary} available."
            candidates = [primary]
        else:
            candidates = [card.name for card in self.cards]
            primary = candidates[0]
            reply_text = "Right now I have " + self._format_card_names(self.cards) + " available."
        return {
            "type": "game_list",
            "text": reply_text,
            "game_name": primary,
            "primary_game_name": primary,
            "candidate_games": candidates,
            "reason_text": "",
            "style_goal": "warm_natural",
        }

    def list_reply(self, text: str) -> str:
        return str(self.list_result(text).get("text") or "")

    def _contrast_reason(self, candidate: GameCard, reference: GameCard) -> str:
        candidate_tags = _normalized_tags(candidate)
        reference_tags = _normalized_tags(reference)
        candidate_level = _ACTIVITY_LEVEL_SCORE.get(_activity_level(candidate), 0.0)
        reference_level = _ACTIVITY_LEVEL_SCORE.get(_activity_level(reference), 0.0)
        if "walking" in candidate_tags and "stationary" in reference_tags:
            return f"It adds more walking and movement than {reference.name}."
        if candidate_level > reference_level:
            return f"It is the more active option compared with {reference.name}."
        if candidate_level < reference_level:
            return f"It is the gentler option compared with {reference.name}."
        if "balance" in candidate_tags and "balance" not in reference_tags:
            return f"It adds more balance and movement than {reference.name}."
        return f"It is the other local option besides {reference.name}."

    def _score_goal_or_limitation_rules(
        self,
        *,
        card: GameCard,
        rules: List[Dict[str, Any]],
        profile_texts: List[str],
    ) -> tuple[float, List[tuple[float, str]]]:
        tags = _normalized_tags(card)
        level = _activity_level(card)
        score = 0.0
        reason_candidates: List[tuple[float, str]] = []
        for rule in _rule_hits(profile_texts, rules):
            boost_tags = {_normalize_text(tag) for tag in rule.get("boost_tags", set()) if _normalize_text(tag)}
            penalty_tags = {_normalize_text(tag) for tag in rule.get("penalty_tags", set()) if _normalize_text(tag)}
            boost_levels = {_normalize_text(item) for item in rule.get("boost_levels", set()) if _normalize_text(item)}
            penalty_levels = {_normalize_text(item) for item in rule.get("penalty_levels", set()) if _normalize_text(item)}
            weight = float(rule.get("weight") or 0.0)
            hit = False
            if boost_tags and tags.intersection(boost_tags):
                score += weight
                hit = True
            if boost_levels and level in boost_levels:
                score += max(0.8, weight * 0.7)
                hit = True
            if penalty_tags and tags.intersection(penalty_tags):
                score -= max(0.8, weight)
            if penalty_levels and level in penalty_levels:
                score -= max(0.8, weight * 0.8)
            if hit:
                reason = str(rule.get("reason") or "").strip()
                if reason:
                    reason_candidates.append((weight, reason))
        return score, reason_candidates

    def _history_penalty(
        self,
        *,
        card: GameCard,
        recent_games: List[Dict[str, Any]],
        wants_variety: bool,
    ) -> tuple[float, List[tuple[float, str]]]:
        if not recent_games:
            return 0.0, []
        score = 0.0
        reasons: List[tuple[float, str]] = []
        recent_names = [_normalize_text(str(item.get("game_name") or "")) for item in recent_games if isinstance(item, dict)]
        recent_names = [item for item in recent_names if item]
        if not recent_names:
            return 0.0, []
        card_names = {_normalize_text(card.name), _normalize_text(card.game_id), *[_normalize_text(alias) for alias in card.aliases]}
        last_game = recent_names[-1]
        if last_game in card_names:
            penalty = 2.8 if wants_variety else 1.9
            score -= penalty
        else:
            bonus = 1.1 if wants_variety else 0.8
            score += bonus
            reason = "it gives you a change from the last game" if wants_variety else "it gives you a change from your most recent game"
            reasons.append((bonus, reason))
        if sum(1 for item in recent_names[-3:] if item in card_names) >= 2:
            score -= 1.2
        return score, reasons

    def alternative_result(self, text: str, user_profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._maybe_reload()
        normalized = _normalize_text(text)
        if not normalized:
            return {}
        if not _contains_any_hint(text, normalized, _ALTERNATIVE_HINTS, _ALTERNATIVE_HINTS_RAW):
            return {}
        normalized_padded = f" {normalized} "
        mentioned_names = self.extract_game_mentions(text, limit=3)
        has_game_signal = any(
            token in normalized_padded
            for token in (" game ", " games ", " play ", " open ", " launch ", " recommend ", " recommendation ", " option ")
        ) or any(hint in str(text or "") for hint in _ALTERNATIVE_HINTS_RAW)
        if not mentioned_names and not has_game_signal:
            return {}
        if not self.cards:
            return {
                "type": "game_alternative",
                "text": "I do not have any local games available right now.",
                "game_name": "",
                "primary_game_name": "",
                "reference_game_name": "",
                "candidate_games": [],
                "candidates": [],
                "reason_text": "",
                "style_goal": "warm_natural",
            }

        reference_card: Optional[GameCard] = None
        if mentioned_names:
            reference_card = self.resolve_card(mentioned_names[-1])
        if reference_card is None:
            reference_card = self._context_card(user_profile)
        if reference_card is None:
            alternatives = sorted(self.cards, key=lambda card: (card.recommendation_weight, card.name.casefold()), reverse=True)
            lead = alternatives[0]
            listed = self._format_card_names(alternatives[: min(3, len(alternatives))])
            reply = f"Other good local options are {listed}. I would start with {lead.name}."
            return {
                "type": "game_alternative",
                "text": reply.strip(),
                "game_name": lead.name,
                "primary_game_name": lead.name,
                "reference_game_name": "",
                "candidate_games": [card.name for card in alternatives[: min(3, len(alternatives))]],
                "candidates": [card.name for card in alternatives[: min(3, len(alternatives))]],
                "reason_text": "",
                "style_goal": "warm_natural",
            }

        alternatives = [card for card in self.cards if _normalize_text(card.name) != _normalize_text(reference_card.name)]
        if not alternatives:
            return {
                "type": "game_alternative",
                "text": f"I do not have another local game besides {reference_card.name} right now.",
                "game_name": reference_card.name,
                "primary_game_name": reference_card.name,
                "reference_game_name": reference_card.name,
                "candidate_games": [],
                "candidates": [],
                "reason_text": "",
                "style_goal": "warm_natural",
            }

        if len(alternatives) == 1:
            candidate = alternatives[0]
            difference = self._contrast_reason(candidate, reference_card)
            if _wants_single_sentence(text):
                reply = f"The other local game is {candidate.name} because {difference[:-1].lower()}."
            else:
                reply = f"The other local game is {candidate.name}. {difference} {candidate.description}"
            return {
                "type": "game_alternative",
                "text": reply.strip(),
                "game_name": candidate.name,
                "primary_game_name": candidate.name,
                "reference_game_name": reference_card.name,
                "candidate_games": [candidate.name],
                "candidates": [candidate.name],
                "reason_text": difference.strip(),
                "style_goal": "warm_natural",
            }

        lead = max(alternatives, key=lambda card: (card.recommendation_weight, card.name.casefold()))
        listed = self._format_card_names(alternatives)
        difference = self._contrast_reason(lead, reference_card)
        reply = f"Other local games besides {reference_card.name} are {listed}. {lead.name} is the main alternative because {difference[:-1].lower()}."
        return {
            "type": "game_alternative",
            "text": reply.strip(),
            "game_name": lead.name,
            "primary_game_name": lead.name,
            "reference_game_name": reference_card.name,
            "candidate_games": [card.name for card in alternatives],
            "candidates": [card.name for card in alternatives],
            "reason_text": difference.strip(),
            "style_goal": "warm_natural",
        }

    def recommend_result(self, text: str, user_profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._maybe_reload()
        normalized = _normalize_text(text)
        if not normalized or not _contains_any_hint(text, normalized, _RECOMMEND_HINTS, _RECOMMEND_HINTS_RAW):
            return {}
        if not self.cards:
            return {
                "type": "game_recommend",
                "text": "I do not have any local games to recommend right now.",
                "game_name": "",
                "primary_game_name": "",
                "candidate_games": [],
                "reason_text": "",
                "style_goal": "warm_natural",
            }

        desired_players = _extract_player_count(text)
        user_profile = user_profile or {}
        likes = [str(value).strip() for value in user_profile.get("likes", []) or [] if str(value).strip()]
        dislikes = [str(value).strip() for value in user_profile.get("dislikes", []) or [] if str(value).strip()]
        favorite_game = str(user_profile.get("favorite_game") or "").strip()
        profile_texts = _profile_texts(user_profile)
        recent_games = [item for item in user_profile.get("recent_games", []) or [] if isinstance(item, dict)]
        wants_variety = any(hint in normalized for hint in _VARIETY_HINTS)
        any_player_fit = False
        if desired_players is not None:
            any_player_fit = any(card.players_min <= desired_players <= card.players_max for card in self.cards)

        best_card: Optional[GameCard] = None
        best_score = float("-inf")
        best_reason = ""
        for card in self.cards:
            score = card.recommendation_weight
            reason_candidates: List[tuple[float, str]] = []
            if desired_players is not None:
                if card.players_min <= desired_players <= card.players_max:
                    score += 3.0
                    reason_candidates.append((3.0, f"it fits {desired_players} player" + ("" if desired_players == 1 else "s")))
                else:
                    score -= 2.0 if any_player_fit else 0.8
            if favorite_game and self.profile_matches(card, [favorite_game]):
                score += 6.0
                reason_candidates.append((6.0, "it matches your favorite game"))
            if likes and self.profile_matches(card, likes):
                score += 3.0
                reason_candidates.append((3.0, "it matches what you like"))
            if dislikes and self.profile_matches(card, dislikes):
                score -= 5.0
            goal_score, goal_reasons = self._score_goal_or_limitation_rules(
                card=card,
                rules=_GOAL_RULES,
                profile_texts=profile_texts,
            )
            score += goal_score
            reason_candidates.extend(goal_reasons)
            limit_score, limit_reasons = self._score_goal_or_limitation_rules(
                card=card,
                rules=_LIMITATION_RULES,
                profile_texts=profile_texts,
            )
            score += limit_score
            reason_candidates.extend(limit_reasons)
            history_score, history_reasons = self._history_penalty(
                card=card,
                recent_games=recent_games,
                wants_variety=wants_variety,
            )
            score += history_score
            reason_candidates.extend(history_reasons)
            if card.exec_path:
                score += 0.5
            if score > best_score:
                best_score = score
                best_card = card
                if reason_candidates:
                    best_reason = max(reason_candidates, key=lambda item: item[0])[1]
                else:
                    best_reason = "it is available locally"

        if best_card is None:
            return {
                "type": "game_recommend",
                "text": "I could not find a local game to recommend right now.",
                "game_name": "",
                "primary_game_name": "",
                "candidate_games": [],
                "reason_text": "",
                "style_goal": "warm_natural",
            }
        player_constraint = ""
        if desired_players is not None and not any_player_fit:
            player_constraint = "I only have single-player games locally right now, but "
        reply_text = ""
        if _wants_single_sentence(text):
            reply_text = f"{player_constraint}I recommend {best_card.name} because {best_reason}."
        else:
            reply_text = f"{player_constraint}I recommend {best_card.name} because {best_reason}. {best_card.description}"
        sorted_candidates = sorted(self.cards, key=lambda card: (card.recommendation_weight, card.name.casefold()), reverse=True)
        return {
            "type": "game_recommend",
            "text": reply_text,
            "game_name": best_card.name,
            "primary_game_name": best_card.name,
            "candidate_games": [card.name for card in sorted_candidates[: min(3, len(sorted_candidates))]],
            "reason_text": best_reason,
            "style_goal": "warm_natural",
        }

    def recommend_reply(self, text: str, user_profile: Optional[Dict[str, Any]] = None) -> str:
        return str(self.recommend_result(text, user_profile=user_profile).get("text") or "")

    def grounded_reply(
        self,
        text: str,
        user_profile: Optional[Dict[str, Any]] = None,
        *,
        session_state: Any = None,
        forced_intent: str = "",
    ) -> Dict[str, Any]:
        decision = self.route_game_query(
            text,
            session_state=session_state,
            user_profile=user_profile,
            forced_intent=forced_intent,
        )
        plan = self.build_answer_plan(
            text,
            decision=decision,
            session_state=session_state,
            user_profile=user_profile,
        )
        if plan is not None:
            return self._plan_to_payload(plan)
        explain = self.explain_result(text)
        reply = str(explain.get("text") or "").strip()
        if reply:
            return explain
        alternative = self.alternative_result(text, user_profile=user_profile)
        reply = str(alternative.get("text") or "").strip()
        if reply:
            return alternative
        recommend = self.recommend_result(text, user_profile=user_profile)
        reply = str(recommend.get("text") or "").strip()
        if reply:
            return recommend
        listed = self.list_result(text)
        reply = str(listed.get("text") or "").strip()
        if reply:
            return listed
        return {}
