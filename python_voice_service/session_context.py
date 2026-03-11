from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional


DEFAULT_SESSION_MAX_AGE_SEC = 600.0
GENERAL_SUMMARY_MAX_CHARS = 220
GENERAL_RECENT_DIALOGUE_MAX_CHARS = 320
GENERAL_MAX_TURNS = 4
GAME_MAX_CANDIDATES = 4
MEMORY_NOTES_MAX_ITEMS = 2


def _clean_text(text: str, *, max_len: int = 240) -> str:
    clean = " ".join((text or "").strip().split())
    if not clean:
        return ""
    return clean[:max_len].rstrip()


def _trim_chars(text: str, limit: int) -> str:
    clean = _clean_text(text, max_len=max(1, limit * 2))
    if len(clean) <= limit:
        return clean
    return clean[: max(1, limit)].rstrip()


def _dedupe_names(values: List[str], *, limit: int) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in values:
        name = _clean_text(str(raw or ""), max_len=80)
        if not name:
            continue
        folded = name.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        out.append(name)
        if len(out) >= limit:
            break
    return out


@dataclass
class GameDialogState:
    focused_game: str = ""
    candidate_games: List[str] = field(default_factory=list)
    primary_recommendation: str = ""
    last_introduced_games: List[str] = field(default_factory=list)
    last_router_intent: str = ""
    updated_at: float = 0.0


@dataclass
class GeneralSessionState:
    rolling_summary: str = ""
    recent_turns: Deque[Dict[str, Any]] = field(default_factory=lambda: deque(maxlen=GENERAL_MAX_TURNS))
    last_assistant_prompt: str = ""
    updated_at: float = 0.0


class SessionContextStore:
    def __init__(self, *, max_age_sec: float = DEFAULT_SESSION_MAX_AGE_SEC) -> None:
        self.max_age_sec = max(60.0, float(max_age_sec))
        self._lock = threading.Lock()
        self._states: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _session_key(user_id: Optional[str]) -> str:
        return _clean_text(str(user_id or ""), max_len=80) or "__anonymous__"

    def _prune_unlocked(self, *, now_ts: Optional[float] = None) -> None:
        cutoff = float(now_ts if now_ts is not None else time.time()) - self.max_age_sec
        stale_keys = [
            key
            for key, state in self._states.items()
            if float(state.get("updated_at") or 0.0) < cutoff
        ]
        for key in stale_keys:
            self._states.pop(key, None)

    def _ensure_state_unlocked(self, user_id: Optional[str], *, now_ts: float) -> Dict[str, Any]:
        key = self._session_key(user_id)
        state = self._states.get(key)
        if not isinstance(state, dict):
            state = {
                "updated_at": now_ts,
                "game": GameDialogState(updated_at=now_ts),
                "general": GeneralSessionState(updated_at=now_ts),
            }
            self._states[key] = state
            return state
        if not isinstance(state.get("game"), GameDialogState):
            state["game"] = GameDialogState(updated_at=now_ts)
        if not isinstance(state.get("general"), GeneralSessionState):
            state["general"] = GeneralSessionState(updated_at=now_ts)
        state["updated_at"] = now_ts
        return state

    def _user_notes_from_turns(self, turns: List[Dict[str, Any]], *, query_text: str = "") -> List[str]:
        normalized_query = _clean_text(query_text, max_len=240).casefold()
        notes: List[str] = []
        seen = set()
        for item in reversed(turns):
            if str(item.get("role") or "").strip().lower() != "user":
                continue
            text = _clean_text(str(item.get("text") or ""), max_len=120)
            if not text:
                continue
            if normalized_query and text.casefold() == normalized_query:
                continue
            folded = text.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            notes.append(text)
            if len(notes) >= MEMORY_NOTES_MAX_ITEMS:
                break
        notes.reverse()
        return notes

    @staticmethod
    def _render_summary(turns: List[Dict[str, Any]]) -> str:
        user_points: List[str] = []
        seen = set()
        for item in reversed(turns):
            if str(item.get("role") or "").strip().lower() != "user":
                continue
            text = _clean_text(str(item.get("text") or ""), max_len=96)
            if not text:
                continue
            folded = text.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            user_points.append(text)
            if len(user_points) >= 2:
                break
        user_points.reverse()
        if not user_points:
            return ""
        if len(user_points) == 1:
            return _trim_chars(user_points[0], GENERAL_SUMMARY_MAX_CHARS)
        return _trim_chars(f"{user_points[0]}; {user_points[1]}", GENERAL_SUMMARY_MAX_CHARS)

    def remember_turn(self, *, user_id: Optional[str], role: str, text: str) -> None:
        clean = _clean_text(text)
        normalized_role = str(role or "user").strip().lower() or "user"
        if not clean:
            return
        now_ts = time.time()
        with self._lock:
            self._prune_unlocked(now_ts=now_ts)
            state = self._ensure_state_unlocked(user_id, now_ts=now_ts)
            general = state["general"]
            assert isinstance(general, GeneralSessionState)
            general.recent_turns.append({"role": normalized_role, "text": clean, "ts": now_ts})
            general.updated_at = now_ts
            general.rolling_summary = self._render_summary(list(general.recent_turns))
            if normalized_role == "assistant":
                lowered = clean.casefold()
                if clean.endswith("?") or any(
                    cue in lowered
                    for cue in (
                        "would you like",
                        "let me know",
                        "tell me",
                        "what would you like",
                        "which one",
                    )
                ):
                    general.last_assistant_prompt = _trim_chars(clean, 140)
            state["updated_at"] = now_ts

    def update_game_state(
        self,
        *,
        user_id: Optional[str],
        focused_game: Optional[str] = None,
        candidate_games: Optional[List[str]] = None,
        primary_recommendation: Optional[str] = None,
        last_introduced_games: Optional[List[str]] = None,
        last_router_intent: Optional[str] = None,
    ) -> None:
        now_ts = time.time()
        with self._lock:
            self._prune_unlocked(now_ts=now_ts)
            state = self._ensure_state_unlocked(user_id, now_ts=now_ts)
            game = state["game"]
            assert isinstance(game, GameDialogState)
            if focused_game is not None:
                game.focused_game = _clean_text(focused_game, max_len=80)
            if candidate_games is not None:
                game.candidate_games = _dedupe_names(candidate_games, limit=GAME_MAX_CANDIDATES)
            if primary_recommendation is not None:
                game.primary_recommendation = _clean_text(primary_recommendation, max_len=80)
            if last_introduced_games is not None:
                game.last_introduced_games = _dedupe_names(last_introduced_games, limit=GAME_MAX_CANDIDATES)
            if last_router_intent is not None:
                game.last_router_intent = _clean_text(last_router_intent, max_len=48)
            game.updated_at = now_ts
            state["updated_at"] = now_ts

    def game_state(self, user_id: Optional[str]) -> GameDialogState:
        now_ts = time.time()
        with self._lock:
            self._prune_unlocked(now_ts=now_ts)
            state = self._states.get(self._session_key(user_id))
            if not isinstance(state, dict) or not isinstance(state.get("game"), GameDialogState):
                return GameDialogState()
            game = state["game"]
            assert isinstance(game, GameDialogState)
            return GameDialogState(
                focused_game=game.focused_game,
                candidate_games=list(game.candidate_games),
                primary_recommendation=game.primary_recommendation,
                last_introduced_games=list(game.last_introduced_games),
                last_router_intent=game.last_router_intent,
                updated_at=game.updated_at,
            )

    def context_game_name(self, user_id: Optional[str]) -> str:
        state = self.game_state(user_id)
        return _clean_text(state.focused_game, max_len=80)

    def build_general_session_context(
        self,
        *,
        user_id: Optional[str],
        current_topic: str = "",
        open_question: str = "",
        exclude_user_text: str = "",
    ) -> str:
        now_ts = time.time()
        with self._lock:
            self._prune_unlocked(now_ts=now_ts)
            state = self._states.get(self._session_key(user_id))
            if not isinstance(state, dict) or not isinstance(state.get("general"), GeneralSessionState):
                return ""
            general = state["general"]
            assert isinstance(general, GeneralSessionState)
            turns = list(general.recent_turns)
            summary = _trim_chars(general.rolling_summary, GENERAL_SUMMARY_MAX_CHARS)
            assistant_prompt = _trim_chars(general.last_assistant_prompt, 140)

        excluded = _clean_text(exclude_user_text, max_len=240).casefold()
        rendered_turns: List[str] = []
        recent_chars = 0
        for item in turns:
            role = str(item.get("role") or "user").strip().lower()
            text = _clean_text(str(item.get("text") or ""), max_len=120)
            if not text:
                continue
            if role == "user" and excluded and text.casefold() == excluded:
                continue
            prefix = "User" if role == "user" else "Coach"
            line = f"{prefix}: {text}"
            if recent_chars + len(line) > GENERAL_RECENT_DIALOGUE_MAX_CHARS:
                break
            rendered_turns.append(line)
            recent_chars += len(line) + 1

        lines: List[str] = []
        if summary:
            lines.append(f"Conversation summary: {summary}")
        topic_text = _clean_text(current_topic, max_len=120)
        if topic_text:
            lines.append(f"Current topic: {topic_text}.")
        follow_up = _clean_text(open_question, max_len=140) or assistant_prompt
        if follow_up:
            lines.append(f"Pending follow-up question: {follow_up}")
        if rendered_turns:
            lines.append("Recent dialogue:")
            lines.extend(rendered_turns)
        return "\n".join(lines).strip()

    def build_memory_payload(self, *, user_id: Optional[str], query_text: str) -> Dict[str, Any]:
        now_ts = time.time()
        with self._lock:
            self._prune_unlocked(now_ts=now_ts)
            state = self._states.get(self._session_key(user_id))
            if not isinstance(state, dict) or not isinstance(state.get("general"), GeneralSessionState):
                turns: List[Dict[str, Any]] = []
                summary = ""
            else:
                general = state["general"]
                assert isinstance(general, GeneralSessionState)
                turns = list(general.recent_turns)
                summary = _trim_chars(general.rolling_summary, GENERAL_SUMMARY_MAX_CHARS)

        notes = self._user_notes_from_turns(turns, query_text=query_text)
        if notes:
            if len(notes) == 1:
                fallback = (
                    f"So far in this conversation, I know you mentioned {notes[0]}. "
                    "I just do not have a linked long-term profile yet."
                )
            else:
                fallback = (
                    f"So far in this conversation, I know you mentioned {notes[0]} and {notes[1]}. "
                    "I just do not have a linked long-term profile yet."
                )
        elif summary:
            fallback = (
                f"So far in this conversation, I know {summary}. "
                "I just do not have a linked long-term profile yet."
            )
        else:
            fallback = "So far I only know what we have talked about in this conversation, and I do not have a linked long-term profile yet."
        return {
            "type": "memory_query",
            "query_kind": "summary",
            "result_kind": "session_only_summary",
            "binding_state": "session_only",
            "facts": [],
            "notes": notes,
            "required_terms": [],
            "text": fallback,
            "max_sentences": 2,
        }
