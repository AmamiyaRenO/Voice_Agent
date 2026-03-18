from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
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
_EXPLAIN_HINTS_RAW: tuple[str, ...] = ()
_RECOMMEND_HINTS = ("recommend", "suggest", "what game should", "which game should", "best game")
_RECOMMEND_HINTS_RAW: tuple[str, ...] = ()
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
_LIST_HINTS_RAW: tuple[str, ...] = ()
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
_ALTERNATIVE_HINTS_RAW: tuple[str, ...] = ()
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
_GAME_FOLLOWUP_REFERENCE_PHRASES = (
    "current choice",
    "current option",
    "current recommendation",
    "current selection",
    "that one",
    "this one",
    "the other one",
    "another one",
    "other option",
    "other options",
    "that game",
    "this game",
    "the game",
)
_GAME_FOLLOWUP_ACTION_HINTS = (
    "what about",
    "how about",
    "tell me more",
    "more about",
    "introduce",
    "tell me about",
    "describe",
    "explain",
    "compare",
    "difference",
    "recommend",
    "suggest",
    "option",
    "options",
    "choice",
    "choices",
    "available",
    "open",
    "launch",
    "start",
    "play",
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


class GameCatalog:
    def __init__(self, manifest_path: Path) -> None:
        self.manifest_path = Path(manifest_path)
        self.cards: List[GameCard] = []
        self.alias_map: Dict[str, GameCard] = {}
        self.card_map: Dict[str, GameCard] = {}
        self.resolver = ManifestAliasResolver(str(self.manifest_path)) if ManifestAliasResolver is not None else None
        self._manifest_mtime_ns = 0
        self._manifest_size = 0
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
        if force or manifest_mtime_ns != self._manifest_mtime_ns or manifest_size != self._manifest_size:
            self._load()

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

    @staticmethod
    def _session_state_value(session_state: Any, field: str) -> Any:
        if isinstance(session_state, dict):
            return session_state.get(field)
        return getattr(session_state, field, None)

    def _has_recent_game_context(self, session_state: Any = None) -> bool:
        if session_state is None:
            return False
        now_ts = time.time()
        updated_at = float(self._session_state_value(session_state, "updated_at") or 0.0)
        if updated_at > 0.0 and now_ts - updated_at > _GAME_CONTEXT_MAX_AGE_SEC:
            return False
        if str(self._session_state_value(session_state, "focused_game") or "").strip():
            return True
        if str(self._session_state_value(session_state, "primary_recommendation") or "").strip():
            return True
        for field in ("candidate_games", "last_introduced_games"):
            values = self._session_state_value(session_state, field) or []
            if any(str(item).strip() for item in values):
                return True
        return False

    def looks_like_game_followup(self, text: str, session_state: Any = None) -> bool:
        normalized = _normalize_text(text)
        if not normalized or not self._has_recent_game_context(session_state):
            return False
        padded = f" {normalized} "
        if any(phrase in normalized for phrase in _GAME_FOLLOWUP_REFERENCE_PHRASES):
            return True
        has_pronoun_reference = any(token in padded for token in (" it ", " them ", " those "))
        if not has_pronoun_reference:
            return False
        return any(hint in normalized for hint in _GAME_FOLLOWUP_ACTION_HINTS)
