from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


_SPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_EXPLAIN_HINTS = ("what is", "what's", "explain", "describe", "tell me about", "say what")
_RECOMMEND_HINTS = ("recommend", "suggest", "what game should", "which game should", "best game")
_LIST_HINTS = ("what games", "which games", "available games", "games do you have")
_SINGLE_SENTENCE_HINTS = ("one short sentence", "one sentence", "single sentence")
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
        self._load()

    def _load(self) -> None:
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
            for alias in [card.name, card.game_id, *card.aliases]:
                normalized_alias = _normalize_text(alias)
                if normalized_alias:
                    self.alias_map[normalized_alias] = card

    def resolve_card(self, text: str) -> Optional[GameCard]:
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
        return best

    def profile_matches(self, card: GameCard, values: List[str]) -> bool:
        return _matches_any(card, values)

    def explain_reply(self, text: str) -> str:
        normalized = _normalize_text(text)
        card = self.resolve_card(text)
        if not normalized or card is None:
            return ""
        if not any(hint in normalized for hint in _EXPLAIN_HINTS):
            if not (" what " in f" {normalized} " and " is " in f" {normalized} "):
                return ""
        sentence = card.description or card.how_to_play
        if not sentence:
            return ""
        normalized_name = _normalize_text(card.name)
        if " cornhole " in f" {normalized} " and normalized_name != "cornhole":
            core = sentence
            if sentence.lower().startswith(card.name.lower() + " is "):
                core = sentence[len(card.name) + 1 :].lstrip()
            elif sentence.lower().startswith(card.name.lower()):
                core = sentence[len(card.name) :].lstrip(" ,")
            sentence = "Cornhole, also called Bean Bag Toss, " + core
        if _wants_single_sentence(text):
            return sentence
        follow_up = card.how_to_play or ""
        if follow_up and follow_up != sentence:
            return f"{sentence} {follow_up}"
        return sentence

    def list_reply(self, text: str) -> str:
        normalized = _normalize_text(text)
        if not normalized or not any(hint in normalized for hint in _LIST_HINTS):
            return ""
        names = [card.name for card in self.cards if card.name]
        if not names:
            return "I do not have any local games available right now."
        if len(names) == 1:
            return f"Right now I have {names[0]} available."
        return "Right now I have " + ", ".join(names[:-1]) + f", and {names[-1]} available."

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

    def recommend_result(self, text: str, user_profile: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        normalized = _normalize_text(text)
        if not normalized or not any(hint in normalized for hint in _RECOMMEND_HINTS):
            return {}
        if not self.cards:
            return {"text": "I do not have any local games to recommend right now.", "game_name": ""}

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
            return {"text": "I could not find a local game to recommend right now.", "game_name": ""}
        player_constraint = ""
        if desired_players is not None and not any_player_fit:
            player_constraint = "I only have single-player games locally right now, but "
        reply_text = ""
        if _wants_single_sentence(text):
            reply_text = f"{player_constraint}I recommend {best_card.name} because {best_reason}."
        else:
            reply_text = f"{player_constraint}I recommend {best_card.name} because {best_reason}. {best_card.description}"
        return {"text": reply_text, "game_name": best_card.name}

    def recommend_reply(self, text: str, user_profile: Optional[Dict[str, Any]] = None) -> str:
        return str(self.recommend_result(text, user_profile=user_profile).get("text") or "")

    def grounded_reply(self, text: str, user_profile: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        reply = self.explain_reply(text)
        if reply:
            card = self.resolve_card(text)
            return {"type": "game_explain", "text": reply, "game_name": card.name if card else ""}
        recommend = self.recommend_result(text, user_profile=user_profile)
        reply = str(recommend.get("text") or "").strip()
        if reply:
            return {"type": "game_recommend", "text": reply, "game_name": str(recommend.get("game_name") or "").strip()}
        reply = self.list_reply(text)
        if reply:
            return {"type": "game_list", "text": reply, "game_name": ""}
        return {}
