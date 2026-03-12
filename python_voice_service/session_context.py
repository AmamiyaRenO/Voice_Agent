from __future__ import annotations

import re
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
FOCUS_CLEAR_AFTER_GENERAL_TURNS = 2
CLARIFICATION_MAX_AGE_SEC = 30.0
_EXPLICIT_REFERENCE_MARKERS = (
    " it ",
    " them ",
    " that one ",
    " this one ",
    " the other one ",
    " other one ",
    " another one ",
    " that game ",
    " this game ",
    " the game ",
    " that option ",
    " this option ",
    " current choice ",
    " current option ",
    " current recommendation ",
)
_REFERENCE_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


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


def _looks_like_explicit_reference(text: str) -> bool:
    normalized = _clean_text(text, max_len=160).casefold()
    normalized = _REFERENCE_NON_ALNUM_RE.sub(" ", normalized)
    normalized = f" {' '.join(normalized.split())} "
    if not normalized.strip():
        return False
    return any(marker in normalized for marker in _EXPLICIT_REFERENCE_MARKERS)


@dataclass
class GameDialogState:
    focused_game: str = ""
    candidate_games: List[str] = field(default_factory=list)
    primary_recommendation: str = ""
    last_introduced_games: List[str] = field(default_factory=list)
    last_router_intent: str = ""
    updated_at: float = 0.0


@dataclass
class CapabilityFocusState:
    active_capability: str = ""
    focused_entity: str = ""
    candidate_entities: List[str] = field(default_factory=list)
    last_structured_intent: str = ""
    consecutive_general_turns: int = 0
    updated_at: float = 0.0


@dataclass
class ClarificationState:
    kind: str = ""
    source_user_text: str = ""
    assistant_clarify_text: str = ""
    created_at: float = 0.0


@dataclass
class GameSuppressionState:
    active: bool = False
    reason: str = ""
    created_at: float = 0.0


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
                "focus": CapabilityFocusState(updated_at=now_ts),
                "clarification": ClarificationState(),
                "suppression": GameSuppressionState(),
                "general": GeneralSessionState(updated_at=now_ts),
            }
            self._states[key] = state
            return state
        if not isinstance(state.get("game"), GameDialogState):
            state["game"] = GameDialogState(updated_at=now_ts)
        if not isinstance(state.get("focus"), CapabilityFocusState):
            state["focus"] = CapabilityFocusState(updated_at=now_ts)
        if not isinstance(state.get("clarification"), ClarificationState):
            state["clarification"] = ClarificationState()
        if not isinstance(state.get("suppression"), GameSuppressionState):
            state["suppression"] = GameSuppressionState()
        if not isinstance(state.get("general"), GeneralSessionState):
            state["general"] = GeneralSessionState(updated_at=now_ts)
        state["updated_at"] = now_ts
        return state

    @staticmethod
    def _clarification_is_fresh(clarification: ClarificationState, *, now_ts: float) -> bool:
        created_at = float(clarification.created_at or 0.0)
        if not clarification.kind or not clarification.source_user_text or created_at <= 0.0:
            return False
        return (now_ts - created_at) <= CLARIFICATION_MAX_AGE_SEC

    @staticmethod
    def _reset_game_focus(game: GameDialogState, *, preserve_candidates: bool = False) -> None:
        game.focused_game = ""
        if not preserve_candidates:
            game.candidate_games = []
        game.primary_recommendation = ""
        game.last_introduced_games = []
        game.last_router_intent = ""

    @staticmethod
    def _clear_game_suppression_unlocked(state: Dict[str, Any]) -> None:
        state["suppression"] = GameSuppressionState()

    @staticmethod
    def _sync_focus_from_game(game: GameDialogState, focus: CapabilityFocusState, *, now_ts: float) -> None:
        focused_entity = _clean_text(game.focused_game or game.primary_recommendation, max_len=80)
        candidate_entities = _dedupe_names(
            list(game.candidate_games) or ([focused_entity] if focused_entity else []),
            limit=GAME_MAX_CANDIDATES,
        )
        if not focused_entity and candidate_entities:
            focused_entity = candidate_entities[0]
        if not focused_entity and not candidate_entities and not str(game.last_router_intent or "").strip():
            return
        focus.active_capability = "game"
        focus.focused_entity = focused_entity
        focus.candidate_entities = candidate_entities
        focus.last_structured_intent = _clean_text(game.last_router_intent, max_len=48)
        focus.consecutive_general_turns = 0
        focus.updated_at = now_ts

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
            focus = state["focus"]
            assert isinstance(game, GameDialogState)
            assert isinstance(focus, CapabilityFocusState)
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
            self._clear_game_suppression_unlocked(state)
            self._sync_focus_from_game(game, focus, now_ts=now_ts)
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

    def capability_state(self, user_id: Optional[str]) -> CapabilityFocusState:
        now_ts = time.time()
        with self._lock:
            self._prune_unlocked(now_ts=now_ts)
            state = self._states.get(self._session_key(user_id))
            if not isinstance(state, dict) or not isinstance(state.get("focus"), CapabilityFocusState):
                return CapabilityFocusState()
            focus = state["focus"]
            assert isinstance(focus, CapabilityFocusState)
            return CapabilityFocusState(
                active_capability=focus.active_capability,
                focused_entity=focus.focused_entity,
                candidate_entities=list(focus.candidate_entities),
                last_structured_intent=focus.last_structured_intent,
                consecutive_general_turns=int(focus.consecutive_general_turns or 0),
                updated_at=focus.updated_at,
            )

    def record_structured_capability(
        self,
        *,
        user_id: Optional[str],
        active_capability: str,
        focused_entity: str = "",
        candidate_entities: Optional[List[str]] = None,
        last_structured_intent: str = "",
    ) -> None:
        now_ts = time.time()
        normalized_capability = _clean_text(active_capability, max_len=48)
        entities = _dedupe_names(list(candidate_entities or []), limit=GAME_MAX_CANDIDATES)
        normalized_focus = _clean_text(focused_entity, max_len=80)
        if not normalized_focus and entities:
            normalized_focus = entities[0]
        with self._lock:
            self._prune_unlocked(now_ts=now_ts)
            state = self._ensure_state_unlocked(user_id, now_ts=now_ts)
            focus = state["focus"]
            game = state["game"]
            assert isinstance(focus, CapabilityFocusState)
            assert isinstance(game, GameDialogState)
            focus.active_capability = normalized_capability
            focus.focused_entity = normalized_focus
            focus.candidate_entities = entities
            focus.last_structured_intent = _clean_text(last_structured_intent, max_len=48)
            focus.consecutive_general_turns = 0
            focus.updated_at = now_ts
            if normalized_capability.startswith("game"):
                self._clear_game_suppression_unlocked(state)
                if normalized_focus:
                    game.focused_game = normalized_focus
                if entities:
                    game.candidate_games = entities[:]
                if last_structured_intent:
                    game.last_router_intent = _clean_text(last_structured_intent, max_len=48)
                game.updated_at = now_ts
            state["updated_at"] = now_ts

    def record_general_turn(self, *, user_id: Optional[str], capability: str = "general_chat") -> None:
        now_ts = time.time()
        normalized_capability = _clean_text(capability, max_len=48) or "general_chat"
        with self._lock:
            self._prune_unlocked(now_ts=now_ts)
            state = self._ensure_state_unlocked(user_id, now_ts=now_ts)
            focus = state["focus"]
            game = state["game"]
            assert isinstance(focus, CapabilityFocusState)
            assert isinstance(game, GameDialogState)
            if focus.focused_entity or focus.candidate_entities or game.focused_game or game.candidate_games:
                focus.consecutive_general_turns = max(0, int(focus.consecutive_general_turns or 0)) + 1
            else:
                focus.consecutive_general_turns = 0
            focus.active_capability = normalized_capability
            focus.updated_at = now_ts
            if focus.consecutive_general_turns >= FOCUS_CLEAR_AFTER_GENERAL_TURNS:
                focus.focused_entity = ""
                focus.candidate_entities = []
                focus.last_structured_intent = ""
                self._reset_game_focus(game)
                game.updated_at = now_ts
            state["updated_at"] = now_ts

    def activate_game_suppression(self, *, user_id: Optional[str], reason: str = "") -> None:
        now_ts = time.time()
        with self._lock:
            self._prune_unlocked(now_ts=now_ts)
            state = self._ensure_state_unlocked(user_id, now_ts=now_ts)
            general = state["general"]
            focus = state["focus"]
            game = state["game"]
            assert isinstance(general, GeneralSessionState)
            assert isinstance(focus, CapabilityFocusState)
            assert isinstance(game, GameDialogState)

            created_at = now_ts
            if general.recent_turns:
                latest = general.recent_turns[-1]
                if str(latest.get("role") or "").strip().lower() == "user":
                    try:
                        created_at = float(latest.get("ts") or now_ts)
                    except Exception:
                        created_at = now_ts

            state["suppression"] = GameSuppressionState(
                active=True,
                reason=_clean_text(reason, max_len=80),
                created_at=created_at,
            )
            state["clarification"] = ClarificationState()
            focus.active_capability = "general_chat"
            focus.focused_entity = ""
            focus.candidate_entities = []
            focus.last_structured_intent = ""
            focus.consecutive_general_turns = 0
            focus.updated_at = now_ts
            self._reset_game_focus(game)
            game.updated_at = now_ts
            state["updated_at"] = now_ts

    def game_suppression_state(self, user_id: Optional[str]) -> GameSuppressionState:
        now_ts = time.time()
        with self._lock:
            self._prune_unlocked(now_ts=now_ts)
            state = self._states.get(self._session_key(user_id))
            if not isinstance(state, dict) or not isinstance(state.get("suppression"), GameSuppressionState):
                return GameSuppressionState()
            suppression = state["suppression"]
            assert isinstance(suppression, GameSuppressionState)
            if not suppression.active:
                return GameSuppressionState()
            return GameSuppressionState(
                active=bool(suppression.active),
                reason=suppression.reason,
                created_at=float(suppression.created_at or 0.0),
            )

    def is_game_suppressed(self, user_id: Optional[str]) -> bool:
        return bool(self.game_suppression_state(user_id).active)

    def clear_game_suppression(self, user_id: Optional[str]) -> None:
        now_ts = time.time()
        with self._lock:
            self._prune_unlocked(now_ts=now_ts)
            state = self._states.get(self._session_key(user_id))
            if not isinstance(state, dict):
                return
            self._clear_game_suppression_unlocked(state)
            state["updated_at"] = now_ts

    def save_clarification(
        self,
        *,
        user_id: Optional[str],
        kind: str,
        source_user_text: str,
        assistant_clarify_text: str,
    ) -> None:
        now_ts = time.time()
        with self._lock:
            self._prune_unlocked(now_ts=now_ts)
            state = self._ensure_state_unlocked(user_id, now_ts=now_ts)
            clarification = state["clarification"]
            assert isinstance(clarification, ClarificationState)
            clarification.kind = _clean_text(kind, max_len=48)
            clarification.source_user_text = _clean_text(source_user_text, max_len=240)
            clarification.assistant_clarify_text = _clean_text(assistant_clarify_text, max_len=180)
            clarification.created_at = now_ts
            state["updated_at"] = now_ts

    def clarification_state(self, user_id: Optional[str]) -> ClarificationState:
        now_ts = time.time()
        with self._lock:
            self._prune_unlocked(now_ts=now_ts)
            state = self._states.get(self._session_key(user_id))
            if not isinstance(state, dict) or not isinstance(state.get("clarification"), ClarificationState):
                return ClarificationState()
            clarification = state["clarification"]
            assert isinstance(clarification, ClarificationState)
            if not self._clarification_is_fresh(clarification, now_ts=now_ts):
                return ClarificationState()
            return ClarificationState(
                kind=clarification.kind,
                source_user_text=clarification.source_user_text,
                assistant_clarify_text=clarification.assistant_clarify_text,
                created_at=clarification.created_at,
            )

    def take_clarification(self, user_id: Optional[str]) -> ClarificationState:
        now_ts = time.time()
        with self._lock:
            self._prune_unlocked(now_ts=now_ts)
            state = self._states.get(self._session_key(user_id))
            if not isinstance(state, dict) or not isinstance(state.get("clarification"), ClarificationState):
                return ClarificationState()
            clarification = state["clarification"]
            assert isinstance(clarification, ClarificationState)
            fresh = self._clarification_is_fresh(clarification, now_ts=now_ts)
            current = ClarificationState(
                kind=clarification.kind,
                source_user_text=clarification.source_user_text,
                assistant_clarify_text=clarification.assistant_clarify_text,
                created_at=clarification.created_at,
            ) if fresh else ClarificationState()
            state["clarification"] = ClarificationState()
            state["updated_at"] = now_ts
            return current

    def clear_clarification(self, user_id: Optional[str]) -> None:
        now_ts = time.time()
        with self._lock:
            self._prune_unlocked(now_ts=now_ts)
            state = self._states.get(self._session_key(user_id))
            if not isinstance(state, dict):
                return
            state["clarification"] = ClarificationState()
            state["updated_at"] = now_ts

    def context_game_name(self, user_id: Optional[str], text: str = "") -> str:
        if self.is_game_suppressed(user_id):
            return ""
        game = self.game_state(user_id)
        focused = _clean_text(game.focused_game or game.primary_recommendation, max_len=80)
        if not focused:
            return ""
        focus = self.capability_state(user_id)
        if int(focus.consecutive_general_turns or 0) >= FOCUS_CLEAR_AFTER_GENERAL_TURNS:
            return ""
        if int(focus.consecutive_general_turns or 0) >= 1 and not _looks_like_explicit_reference(text):
            return ""
        return focused

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
            clarification = state.get("clarification")
            suppression = state.get("suppression")
            assert isinstance(general, GeneralSessionState)
            turns = list(general.recent_turns)
            active_clarification = clarification if isinstance(clarification, ClarificationState) else ClarificationState()
            active_suppression = suppression if isinstance(suppression, GameSuppressionState) else GameSuppressionState()

        cutoff = float(active_suppression.created_at or 0.0) if active_suppression.active else 0.0
        if cutoff > 0.0:
            turns = [item for item in turns if float(item.get("ts") or 0.0) >= cutoff]
        summary = _trim_chars(self._render_summary(turns), GENERAL_SUMMARY_MAX_CHARS)

        excluded = _clean_text(exclude_user_text, max_len=240).casefold()
        rendered_turns: List[str] = []
        recent_chars = 0
        user_turns = [item for item in turns if str(item.get("role") or "").strip().lower() == "user"]
        for item in user_turns[-2:]:
            text = _clean_text(str(item.get("text") or ""), max_len=120)
            if not text:
                continue
            if excluded and text.casefold() == excluded:
                continue
            line = f"User: {text}"
            if recent_chars + len(line) > GENERAL_RECENT_DIALOGUE_MAX_CHARS:
                break
            rendered_turns.append(line)
            recent_chars += len(line) + 1

        lines: List[str] = []
        if summary:
            lines.append(f"Conversation summary: {summary}")
        if (
            active_clarification.assistant_clarify_text
            and self._clarification_is_fresh(active_clarification, now_ts=now_ts)
            and float(active_clarification.created_at or 0.0) >= cutoff
        ):
            lines.append(f"Active clarification: {active_clarification.assistant_clarify_text}")
        if rendered_turns:
            lines.append("Recent user messages:")
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
                suppression = state.get("suppression")
                assert isinstance(general, GeneralSessionState)
                turns = list(general.recent_turns)
                active_suppression = suppression if isinstance(suppression, GameSuppressionState) else GameSuppressionState()
                cutoff = float(active_suppression.created_at or 0.0) if active_suppression.active else 0.0
                if cutoff > 0.0:
                    turns = [item for item in turns if float(item.get("ts") or 0.0) >= cutoff]
                summary = _trim_chars(self._render_summary(turns), GENERAL_SUMMARY_MAX_CHARS)

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
