from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9']+")
_LEADING_FILLERS = {
    "a",
    "an",
    "hey",
    "hi",
    "please",
    "rachel",
    "agent",
    "the",
    "uh",
    "um",
}
_POST_TRIGGER_FILLERS = {
    "a",
    "an",
    "my",
    "the",
    "this",
    "that",
}
_DEFAULT_LAUNCH_TRIGGERS = ["open", "start", "launch", "play", "begin", "load"]
_DEFAULT_EXIT_KEYWORDS = ["back home", "go home", "return home", "go back", "quit", "exit", "close game"]


def _collapse_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _tokenize(value: str) -> List[str]:
    return [match.group(0) for match in _TOKEN_PATTERN.finditer(str(value or "").lower())]


def _compact(value: str) -> str:
    return "".join(ch for ch in str(value or "").casefold() if ch.isalnum())


def _sequence_ratio(left: str, right: str) -> float:
    lhs = _collapse_spaces(left).casefold()
    rhs = _collapse_spaces(right).casefold()
    if not lhs or not rhs:
        return 0.0
    if lhs == rhs:
        return 1.0
    lhs_compact = _compact(lhs)
    rhs_compact = _compact(rhs)
    if lhs_compact and lhs_compact == rhs_compact:
        return 1.0
    shorter = min(len(lhs), len(rhs))
    longer = max(len(lhs), len(rhs))
    if shorter >= 3 and (lhs.startswith(rhs) or rhs.startswith(lhs)):
        return 0.84 + (0.16 * shorter / max(1, longer))
    return difflib.SequenceMatcher(None, lhs, rhs).ratio()


def _coerce_string_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item or "").strip() for item in value if str(item or "").strip()]
    if isinstance(value, str):
        merged = value.replace("\r\n", "\n").replace(";", ",").replace("\n", ",")
        return [part.strip() for part in merged.split(",") if part.strip()]
    return []


@dataclass
class CommandGame:
    name: str
    aliases: List[str] = field(default_factory=list)

    def phrases(self) -> List[str]:
        phrases: List[str] = []
        seen = set()
        for phrase in [self.name, *self.aliases]:
            text = _collapse_spaces(phrase)
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            phrases.append(text)
        return phrases


@dataclass
class CommandGrammarMatch:
    original_text: str
    canonical_text: str
    route_type: str = "QUERY"
    game_name: str = ""
    confidence: float = 0.0

    @property
    def applied(self) -> bool:
        return self.route_type != "QUERY" and self.canonical_text.strip() != self.original_text.strip()


class CommandGrammarMatcher:
    def __init__(
        self,
        *,
        launch_triggers: Iterable[str],
        exit_keywords: Iterable[str],
        games: Iterable[CommandGame],
    ) -> None:
        self.launch_triggers = [item for item in (_collapse_spaces(value) for value in launch_triggers) if item]
        self.exit_keywords = [item for item in (_collapse_spaces(value) for value in exit_keywords) if item]
        self.games = [item for item in games if _collapse_spaces(item.name)]
        self._game_phrases: List[tuple[str, str]] = []
        for game in self.games:
            for phrase in game.phrases():
                self._game_phrases.append((game.name, phrase))

    @classmethod
    def from_sources(
        cls,
        *,
        launch_triggers: Any,
        exit_keywords: Any,
        manifest_path: str,
    ) -> "CommandGrammarMatcher":
        games: List[CommandGame] = []
        manifest_file = Path(str(manifest_path or "").strip())
        if manifest_file.exists():
            try:
                payload = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
            except Exception:
                payload = {}
            for item in payload.get("games", []) if isinstance(payload, dict) else []:
                if not isinstance(item, dict):
                    continue
                name = _collapse_spaces(str(item.get("name") or item.get("id") or "").strip())
                if not name:
                    continue
                aliases = _coerce_string_list(item.get("synonyms") or item.get("keywords") or [])
                if " " in name:
                    aliases.append(name.replace(" ", ""))
                games.append(CommandGame(name=name, aliases=aliases))
        return cls(
            launch_triggers=_coerce_string_list(launch_triggers) or list(_DEFAULT_LAUNCH_TRIGGERS),
            exit_keywords=_coerce_string_list(exit_keywords) or list(_DEFAULT_EXIT_KEYWORDS),
            games=games,
        )

    def canonicalize(self, text: str, *, allow_bare_game: bool = False) -> CommandGrammarMatch:
        original = _collapse_spaces(text)
        if not original:
            return CommandGrammarMatch(original_text="", canonical_text="")

        stripped = self._strip_leading_fillers(original)
        exit_match = self._match_exit(stripped)
        if exit_match is not None:
            return exit_match

        launch_match = self._match_launch(stripped)
        if launch_match is not None:
            return launch_match
        if allow_bare_game:
            bare_game_match = self._match_bare_game(stripped)
            if bare_game_match is not None:
                return bare_game_match

        return CommandGrammarMatch(original_text=original, canonical_text=original)

    def _strip_leading_fillers(self, text: str) -> str:
        tokens = _tokenize(text)
        raw_tokens = re.findall(r"[A-Za-z0-9']+|[^A-Za-z0-9'\s]+", text)
        del raw_tokens
        if not tokens:
            return _collapse_spaces(text)
        kept = list(tokens)
        while kept and kept[0] in _LEADING_FILLERS:
            kept.pop(0)
        if not kept:
            return _collapse_spaces(text)
        return " ".join(kept)

    def _match_exit(self, text: str) -> Optional[CommandGrammarMatch]:
        candidate = _collapse_spaces(text)
        if not candidate:
            return None
        candidate_tokens = _tokenize(candidate)
        if not candidate_tokens or len(candidate_tokens) > 3:
            return None
        best_phrase = ""
        best_score = 0.0
        for phrase in self.exit_keywords:
            score = _sequence_ratio(candidate, phrase)
            if len(candidate_tokens) == 1 and phrase.casefold().startswith(candidate.casefold()) and len(candidate_tokens[0]) >= 4:
                score = max(score, 0.9)
            if score > best_score:
                best_phrase = phrase
                best_score = score
        if best_score < 0.84:
            return None
        canonical = "back home" if "home" in best_phrase.casefold() or "back" in best_phrase.casefold() else best_phrase
        return CommandGrammarMatch(
            original_text=_collapse_spaces(text),
            canonical_text=canonical,
            route_type="BACK_HOME",
            confidence=best_score,
        )

    def _match_launch(self, text: str) -> Optional[CommandGrammarMatch]:
        candidate = _collapse_spaces(text)
        if not candidate:
            return None
        tokens = _tokenize(candidate)
        if len(tokens) < 2 or len(tokens) > 7:
            return None
        trigger_token = tokens[0]
        trigger_score = max((_sequence_ratio(trigger_token, phrase) for phrase in self.launch_triggers), default=0.0)
        if trigger_score < 0.8:
            return None
        remainder = tokens[1:]
        while remainder and remainder[0] in _POST_TRIGGER_FILLERS:
            remainder.pop(0)
        if not remainder:
            return None
        best_game = ""
        best_score = 0.0
        remainder_text = " ".join(remainder)
        for game_name, phrase in self._game_phrases:
            score = _sequence_ratio(remainder_text, phrase)
            compact_remainder = _compact(remainder_text)
            compact_phrase = _compact(phrase)
            if compact_remainder and compact_phrase.startswith(compact_remainder) and len(compact_remainder) >= 3:
                score = max(score, 0.9)
            if score > best_score:
                best_game = game_name
                best_score = score
        if best_score < 0.8:
            return None
        return CommandGrammarMatch(
            original_text=_collapse_spaces(text),
            canonical_text=f"open {best_game}",
            route_type="LAUNCH_GAME",
            game_name=best_game,
            confidence=min(trigger_score, best_score),
        )

    def _match_bare_game(self, text: str) -> Optional[CommandGrammarMatch]:
        candidate = _collapse_spaces(text)
        if not candidate:
            return None
        tokens = _tokenize(candidate)
        if not tokens or len(tokens) > 4:
            return None
        best_game = ""
        best_score = 0.0
        for game_name, phrase in self._game_phrases:
            score = _sequence_ratio(candidate, phrase)
            if _compact(candidate) and _compact(candidate) == _compact(phrase):
                score = 1.0
            if score > best_score:
                best_game = game_name
                best_score = score
        if best_score < 0.92:
            return None
        return CommandGrammarMatch(
            original_text=candidate,
            canonical_text=f"open {best_game}",
            route_type="LAUNCH_GAME",
            game_name=best_game,
            confidence=best_score,
        )
