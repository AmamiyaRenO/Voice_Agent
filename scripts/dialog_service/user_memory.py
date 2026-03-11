#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .onnx_embedder import OnnxTextEmbedder, cosine_similarity, np as onnx_np
except Exception:
    from onnx_embedder import OnnxTextEmbedder, cosine_similarity, np as onnx_np


def parse_nonnegative_int(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = int(text)
    except Exception:
        return None
    if value < 0:
        return None
    return value


def speaker_identity_key(payload: Dict[str, Any]) -> str:
    profile_id = str(payload.get("speaker_profile_id") or "").strip()
    if profile_id:
        return f"profile:{profile_id}"

    speaker_index = parse_nonnegative_int(payload.get("speaker_index"))
    speaker_id = parse_nonnegative_int(payload.get("speaker_id"))
    if speaker_index is not None or speaker_id is not None:
        index_part = str(speaker_index if speaker_index is not None else -1)
        id_part = str(speaker_id if speaker_id is not None else -1)
        return f"moonshine:{index_part}:{id_part}"

    # Fallback for sources without speaker tags: a single shared profile.
    return "source:default"


def normalize_memory_value(text: str, *, max_len: int = 64) -> str:
    compact = " ".join((text or "").strip().split())
    compact = compact.strip(" \t\r\n.,!?;:()[]{}\"'")
    if not compact:
        return ""
    if len(compact) > max_len:
        compact = compact[:max_len].rstrip()
    return compact


def _normalize_name_value(text: str) -> str:
    value = normalize_memory_value(text, max_len=40)
    if not value:
        return ""
    lowered = value.casefold()
    for marker in (" and ", " but ", " because ", " who ", " that ", " i "):
        idx = lowered.find(marker)
        if idx > 0:
            value = value[:idx].strip()
            lowered = value.casefold()
    tokens = [token for token in value.split(" ") if token]
    if not tokens:
        return ""
    if len(tokens) > 3:
        tokens = tokens[:3]
    candidate = " ".join(tokens).strip()
    if not candidate:
        return ""
    if not _is_plausible_name_candidate(candidate):
        return ""
    return candidate.title()


def _append_unique_casefold(values: List[str], value: str, *, max_items: int) -> None:
    candidate = normalize_memory_value(value)
    if not candidate:
        return
    folded = {item.casefold() for item in values}
    if candidate.casefold() in folded:
        return
    values.append(candidate)
    if max_items > 0 and len(values) > max_items:
        del values[:-max_items]


_NAME_PATTERNS = [
    re.compile(r"\bmy name is\s+([a-z][a-z '\-]{1,31})\b", re.IGNORECASE),
    re.compile(r"\bi am\s+(?!from\b|working\b|trying\b|feeling\b|looking\b|doing\b)([a-z][a-z '\-]{1,31})\b", re.IGNORECASE),
    re.compile(r"\bi'm\s+(?!from\b|working\b|trying\b|feeling\b|looking\b|doing\b)([a-z][a-z '\-]{1,31})\b", re.IGNORECASE),
    re.compile(r"\bcall me\s+([a-z][a-z '\-]{1,31})\b", re.IGNORECASE),
]
_LIKE_PATTERNS = [
    re.compile(r"\bi like\s+([^.!?]{2,80})", re.IGNORECASE),
    re.compile(r"\bi love\s+([^.!?]{2,80})", re.IGNORECASE),
    re.compile(r"\bmy favorite game is\s+([^.!?]{2,80})", re.IGNORECASE),
]
_FAVORITE_GAME_PATTERNS = [
    re.compile(r"\bmy favorite game is\s+([^.!?]{2,80})", re.IGNORECASE),
    re.compile(r"\bmy favourite game is\s+([^.!?]{2,80})", re.IGNORECASE),
]
_DISLIKE_PATTERNS = [
    re.compile(r"\bi don't like\s+([^.!?]{2,80})", re.IGNORECASE),
    re.compile(r"\bi dislike\s+([^.!?]{2,80})", re.IGNORECASE),
    re.compile(r"\bi hate\s+([^.!?]{2,80})", re.IGNORECASE),
]
_NO_LONGER_LIKE_PATTERNS = [
    re.compile(r"\bi no longer like\s+([^.!?]{2,80})", re.IGNORECASE),
    re.compile(r"\bi don't like\s+([^.!?]{2,80})\s+anymore", re.IGNORECASE),
]
_GOAL_PATTERNS = [
    re.compile(r"\bmy goal is\s+([^.!?]{2,100})", re.IGNORECASE),
    re.compile(r"\bi am working on\s+([^.!?]{2,100})", re.IGNORECASE),
    re.compile(r"\bi'm working on\s+([^.!?]{2,100})", re.IGNORECASE),
    re.compile(r"\bi want to\s+([^.!?]{2,100})", re.IGNORECASE),
]
_ORIGIN_PATTERNS = [
    re.compile(
        r"\b(?:i am from|i'm from|i come from|i came from|i live in|i'm living in|i reside in|you are from|you come from)\s+([^.!?]{2,60})",
        re.IGNORECASE,
    ),
    re.compile(r"\bfrom\s+([^.!?]{2,40})", re.IGNORECASE),
]
_DIALOG_WHITESPACE = re.compile(r"\s+")
_WEEKDAY_PATTERN = re.compile(
    r"\b(mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:rs(?:day)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b",
    re.IGNORECASE,
)
_TIME_OF_DAY_PATTERN = re.compile(r"\b(morning|afternoon|evening|night)\b", re.IGNORECASE)
_WEEKDAY_MAP = {
    "mon": "Monday",
    "monday": "Monday",
    "tue": "Tuesday",
    "tues": "Tuesday",
    "tuesday": "Tuesday",
    "wed": "Wednesday",
    "wednesday": "Wednesday",
    "thu": "Thursday",
    "thur": "Thursday",
    "thurs": "Thursday",
    "thursday": "Thursday",
    "fri": "Friday",
    "friday": "Friday",
    "sat": "Saturday",
    "saturday": "Saturday",
    "sun": "Sunday",
    "sunday": "Sunday",
}
_TIME_OF_DAY_MAP = {
    "morning": "morning",
    "afternoon": "afternoon",
    "evening": "evening",
    "night": "night",
}
_GAME_CONTEXT_MAX_AGE_SEC = 600.0
_GAME_CONTEXT_MAX_CANDIDATES = 3
_EXPLICIT_MEMORY_PATTERNS = [
    re.compile(r"^\s*(?:please\s+)?remember(?:\s+that)?\s+", re.IGNORECASE),
    re.compile(r"^\s*(?:please\s+)?don't forget(?:\s+that)?\s+", re.IGNORECASE),
    re.compile(r"^\s*keep in mind(?:\s+that)?\s+", re.IGNORECASE),
]
_MEMORY_NAME_QUERY_PATTERNS = [
    re.compile(r"\bwhat(?:'s| is)\s+my name\b", re.IGNORECASE),
    re.compile(r"\bwho am i\b", re.IGNORECASE),
]
_MEMORY_LIKE_QUERY_PATTERNS = [
    re.compile(r"\bwhat do i like\b", re.IGNORECASE),
    re.compile(r"\bwhat are my likes\b", re.IGNORECASE),
    re.compile(r"\bwhat is my favorite game\b", re.IGNORECASE),
    re.compile(r"\bwhat's my favorite game\b", re.IGNORECASE),
]
_MEMORY_DISLIKE_QUERY_PATTERNS = [
    re.compile(r"\bwhat do i dislike\b", re.IGNORECASE),
    re.compile(r"\bwhat don't i like\b", re.IGNORECASE),
]
_MEMORY_GOAL_QUERY_PATTERNS = [
    re.compile(r"\bwhat are my goals\b", re.IGNORECASE),
    re.compile(r"\bwhat is my goal\b", re.IGNORECASE),
    re.compile(r"\bwhat am i working on\b", re.IGNORECASE),
]
_MEMORY_ORIGIN_QUERY_PATTERNS = [
    re.compile(r"\bwhere am i from\b", re.IGNORECASE),
    re.compile(r"\bwhere did i say i am from\b", re.IGNORECASE),
]
_MEMORY_SCHEDULE_QUERY_PATTERNS = [
    re.compile(r"\bwhen do i prefer to train\b", re.IGNORECASE),
    re.compile(r"\bwhat day do i prefer to train\b", re.IGNORECASE),
    re.compile(r"\bwhat time do i prefer to train\b", re.IGNORECASE),
]
_MEMORY_EPISODIC_QUERY_PATTERNS = [
    re.compile(r"\bwhat did i say about\b", re.IGNORECASE),
    re.compile(r"\bwhat did i mention about\b", re.IGNORECASE),
    re.compile(r"\bwhat was i talking about\b", re.IGNORECASE),
    re.compile(r"\bwhat did we talk about\b", re.IGNORECASE),
]
_MEMORY_SUMMARY_QUERY_PATTERNS = [
    re.compile(r"\bwhat do you know about me\b", re.IGNORECASE),
    re.compile(r"\bwhat do you remember about me\b", re.IGNORECASE),
    re.compile(r"\btell me about me\b", re.IGNORECASE),
    re.compile(r"你了解我什么"),
    re.compile(r"你知道我什么"),
    re.compile(r"你记得我什么"),
]
_MEMORY_QUERY_PATTERN_GROUPS = {
    "summary": _MEMORY_SUMMARY_QUERY_PATTERNS,
    "name": _MEMORY_NAME_QUERY_PATTERNS,
    "likes": _MEMORY_LIKE_QUERY_PATTERNS,
    "dislikes": _MEMORY_DISLIKE_QUERY_PATTERNS,
    "goals": _MEMORY_GOAL_QUERY_PATTERNS,
    "origin": _MEMORY_ORIGIN_QUERY_PATTERNS,
    "schedule": _MEMORY_SCHEDULE_QUERY_PATTERNS,
    "episodic": _MEMORY_EPISODIC_QUERY_PATTERNS,
}
_FACT_MULTI_FIELDS = {"like", "dislike", "goal"}
_FACT_FIELD_PHRASES = {
    "name": ("your name is {value}", "{value}"),
    "like": ("you like {value}", "{value}"),
    "dislike": ("you dislike {value}", "{value}"),
    "goal": ("your goal is {value}", "{value}"),
    "origin": ("you are from {value}", "{value}"),
    "preferred_training_day": ("you prefer training on {value}", "{value}"),
    "preferred_training_time": ("you prefer training in the {value}", "{value}"),
    "favorite_game": ("your favorite game is {value}", "{value}"),
}


def _memory_fact_clause(field: str, value: str) -> str:
    normalized_value = _normalize_fact_value(field, value)
    if not normalized_value:
        return ""
    templates = _FACT_FIELD_PHRASES.get(field)
    if not templates:
        return normalized_value
    return str(templates[0]).format(value=normalized_value)


def _normalize_game_context_kind(value: str) -> str:
    normalized = normalize_memory_value(value, max_len=24).casefold()
    if not normalized:
        return "mentioned"
    if "launch" in normalized or normalized in {"open", "start", "play"}:
        return "launch"
    if "recommend" in normalized or "alternative" in normalized:
        return "recommend"
    return "mentioned"


def _coerce_recent_game_candidates(value: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(value, list):
        return out
    for item in value:
        if isinstance(item, dict):
            game_name = normalize_memory_value(str(item.get("game_name") or item.get("name") or ""), max_len=64)
            if not game_name:
                continue
            out.append(
                {
                    "game_name": game_name,
                    "ts": float(item.get("ts") or 0.0),
                    "kind": _normalize_game_context_kind(str(item.get("kind") or "mentioned")),
                    "source": normalize_memory_value(str(item.get("source") or ""), max_len=32),
                }
            )
            continue
        game_name = normalize_memory_value(str(item or ""), max_len=64)
        if game_name:
            out.append({"game_name": game_name, "ts": 0.0, "kind": "mentioned", "source": ""})
    return out[:_GAME_CONTEXT_MAX_CANDIDATES]
_NAME_STOPWORDS = {
    "a",
    "an",
    "the",
    "to",
    "and",
    "or",
    "but",
    "for",
    "with",
    "ready",
    "help",
    "helping",
    "sorry",
    "hear",
    "going",
    "go",
    "here",
    "there",
    "able",
    "happy",
    "glad",
    "trying",
    "working",
    "looking",
    "feeling",
    "proceed",
    "explore",
    "assist",
    "session",
    "game",
}
_ORIGIN_PREFIX_BLACKLIST = (
    "all of",
    "some of",
    "part of",
    "one of",
    "ready to",
    "going to",
    "sorry to",
    "here to",
)
_IMPLICIT_ACK_FIELDS = {
    "name",
    "origin",
    "like",
    "dislike",
    "favorite_game",
    "preferred_training_day",
    "preferred_training_time",
}


def normalize_dialog_text(text: str, *, max_len: int = 220) -> str:
    compact = _DIALOG_WHITESPACE.sub(" ", (text or "").strip())
    if not compact:
        return ""
    if len(compact) > max_len:
        compact = compact[:max_len].rstrip()
    return compact


def _normalize_dialog_role(role: str) -> str:
    lowered = (role or "").strip().lower()
    if lowered in {"assistant", "coach", "system", "agent"}:
        return "assistant"
    return "user"


def _has_training_schedule_context(text: str) -> bool:
    lowered = " ".join((text or "").strip().lower().split())
    if not lowered:
        return False
    training_markers = (
        " train",
        "training",
        " workout",
        "exercise",
        "exercising",
        " rehab",
        "rehabilitation",
        " gym",
        " practice",
        "practicing",
        " session",
    )
    return any(marker in f" {lowered} " for marker in training_markers)


def _extract_preferred_training_day(text: str) -> str:
    lowered = " ".join((text or "").strip().lower().split())
    if not lowered:
        return ""
    if not _has_training_schedule_context(lowered):
        return ""
    match = _WEEKDAY_PATTERN.search(lowered)
    if match:
        key = (match.group(1) or "").strip().lower()
        return _WEEKDAY_MAP.get(key, "")
    if "weekend" in lowered:
        return "weekend"
    if "weekdays" in lowered or "weekday" in lowered:
        return "weekday"
    if "tomorrow" in lowered:
        return "tomorrow"
    if "today" in lowered:
        return "today"
    return ""


def _looks_like_explicit_memory_write(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    return any(pattern.search(raw) for pattern in _EXPLICIT_MEMORY_PATTERNS)


def _looks_like_question_text(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    normalized = " ".join(raw.lower().split())
    if "?" in raw:
        return True
    return bool(
        re.match(
            r"^(who|what|when|where|why|how|do|does|did|can|could|would|will|should|is|are|am|was|were|tell me)\b",
            normalized,
        )
    )


def _is_plausible_name_candidate(text: str) -> bool:
    candidate = normalize_memory_value(text, max_len=40)
    if not candidate:
        return False
    normalized = candidate.casefold()
    if normalized.startswith(("ready to ", "sorry to ", "going to ", "here to ", "happy to ", "glad to ")):
        return False
    tokens = [token for token in re.split(r"[\s\-']+", normalized) if token]
    if not tokens or len(tokens) > 3:
        return False
    if any(token in _NAME_STOPWORDS for token in tokens):
        return False
    return all(re.fullmatch(r"[a-z]+", token) for token in tokens)


def _normalize_origin_value(text: str) -> str:
    candidate = normalize_memory_value(text, max_len=48).strip(" .,!?:;'\"")
    if not candidate:
        return ""
    if len(candidate.split(" ")) > 6:
        candidate = " ".join(candidate.split(" ")[:6]).strip()
    lowered = candidate.casefold()
    if any(lowered.startswith(prefix) for prefix in _ORIGIN_PREFIX_BLACKLIST):
        return ""
    if lowered in {"here", "there", "this", "that", "it", "all of the"}:
        return ""
    return candidate


def _should_ack_implicit_fact_share(text: str, updates: List[Dict[str, Any]]) -> bool:
    if _looks_like_question_text(text):
        return False
    if not updates:
        return False
    fields = {str(item.get("field") or "").strip() for item in updates}
    if not fields:
        return False
    return fields.issubset(_IMPLICIT_ACK_FIELDS)


def _normalize_fact_value(field: str, value: str) -> str:
    max_len = 80
    if field == "name":
        return _normalize_name_value(value)
    if field == "origin":
        return _normalize_origin_value(value)
    if field in {"goal"}:
        max_len = 120
    elif field in {"favorite_game"}:
        max_len = 48
    elif field in {"preferred_training_day", "preferred_training_time"}:
        max_len = 32
    return normalize_memory_value(value, max_len=max_len)


def _fact_phrase(field: str, value: str, *, include_subject: bool) -> str:
    pair = _FACT_FIELD_PHRASES.get(field)
    if not pair:
        return value
    template = pair[0] if include_subject else pair[1]
    return template.format(value=value)


def _contains_only_ok_instruction(text: str) -> bool:
    lowered = " ".join((text or "").strip().lower().split())
    if not lowered:
        return False
    return "only ok" in lowered or "reply with ok" in lowered or "just say ok" in lowered


def _extract_preferred_training_time(text: str) -> str:
    lowered = " ".join((text or "").strip().lower().split())
    if not lowered:
        return ""
    if not _has_training_schedule_context(lowered):
        return ""
    match = _TIME_OF_DAY_PATTERN.search(lowered)
    if not match:
        return ""
    key = (match.group(1) or "").strip().lower()
    return _TIME_OF_DAY_MAP.get(key, "")


def _extract_origin_hint(texts: List[str]) -> str:
    if not texts:
        return ""
    for raw in reversed(texts):
        value = normalize_memory_value(str(raw), max_len=120)
        if not value:
            continue
        for pattern in _ORIGIN_PATTERNS:
            match = pattern.search(value)
            if not match:
                continue
            candidate = _normalize_origin_value(match.group(1))
            if candidate:
                return candidate
    return ""


class UserMemoryStore:
    def __init__(
        self,
        *,
        path: str,
        max_notes: int,
        prompt_max_chars: int,
        embedder: Optional[OnnxTextEmbedder] = None,
        retrieve_top_k: int = 3,
    ) -> None:
        self.path = Path(path).expanduser()
        self.max_notes = max(0, int(max_notes))
        self.prompt_max_chars = max(120, int(prompt_max_chars))
        self.embedder = embedder
        self.retrieve_top_k = max(1, int(retrieve_top_k))
        self._lock = threading.Lock()
        self._db = self._default_db()
        self._file_mtime_ns = 0
        self._file_ctime_ns = 0
        self._file_size = -1
        self._file_content_hash = ""
        self._load()

    @staticmethod
    def _default_db() -> Dict[str, Any]:
        return {
            "version": 1,
            "next_user_index": 1,
            "identity_map": {},
            "profiles": {},
        }

    def _load(self) -> None:
        with self._lock:
            if not self.path.exists():
                self._file_mtime_ns = 0
                self._file_ctime_ns = 0
                self._file_size = -1
                self._file_content_hash = ""
                return
            try:
                raw = self.path.read_text(encoding="utf-8-sig")
                node = json.loads(raw)
                if not isinstance(node, dict):
                    return
                self._db = self._default_db()
                self._db["next_user_index"] = max(1, int(node.get("next_user_index", 1) or 1))
                identity_map = node.get("identity_map")
                profiles = node.get("profiles")
                if isinstance(identity_map, dict):
                    self._db["identity_map"] = identity_map
                if isinstance(profiles, dict):
                    self._db["profiles"] = profiles
                self._file_content_hash = self._hash_text(raw)
                self._update_file_stamp_unlocked()
            except Exception as exc:
                print(f"[dialog] user memory load failed: {exc}")

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
            serialized = json.dumps(self._db, ensure_ascii=False, indent=2)
            temp_path.write_text(
                serialized,
                encoding="utf-8",
            )
            temp_path.replace(self.path)
            self._file_content_hash = self._hash_text(serialized)
            self._update_file_stamp_unlocked()
        except Exception as exc:
            print(f"[dialog] user memory save failed: {exc}")

    @staticmethod
    def _hash_text(text: str) -> str:
        raw = (text or "").encode("utf-8", errors="ignore")
        return hashlib.sha1(raw).hexdigest()

    def _update_file_stamp_unlocked(self) -> None:
        try:
            stat = self.path.stat()
            self._file_mtime_ns = int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)))
            self._file_ctime_ns = int(getattr(stat, "st_ctime_ns", int(stat.st_ctime * 1_000_000_000)))
            self._file_size = int(stat.st_size)
        except Exception:
            self._file_mtime_ns = 0
            self._file_ctime_ns = 0
            self._file_size = -1

    def _reload_if_external_change_unlocked(self) -> bool:
        try:
            if not self.path.exists():
                if self._file_mtime_ns != 0 or self._file_ctime_ns != 0 or self._file_size != -1:
                    self._file_mtime_ns = 0
                    self._file_ctime_ns = 0
                    self._file_size = -1
                    self._file_content_hash = ""
                return True

            raw = self.path.read_text(encoding="utf-8-sig")
            current_hash = self._hash_text(raw)
            stat = self.path.stat()
            mtime_ns = int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)))
            ctime_ns = int(getattr(stat, "st_ctime_ns", int(stat.st_ctime * 1_000_000_000)))
            size = int(stat.st_size)
            if (
                current_hash == self._file_content_hash
                and mtime_ns == self._file_mtime_ns
                and ctime_ns == self._file_ctime_ns
                and size == self._file_size
            ):
                return True

            node = json.loads(raw)
            if isinstance(node, dict):
                self._db = self._default_db()
                self._db["next_user_index"] = max(1, int(node.get("next_user_index", 1) or 1))
                identity_map = node.get("identity_map")
                profiles = node.get("profiles")
                if isinstance(identity_map, dict):
                    self._db["identity_map"] = identity_map
                if isinstance(profiles, dict):
                    self._db["profiles"] = profiles
            self._file_mtime_ns = mtime_ns
            self._file_ctime_ns = ctime_ns
            self._file_size = size
            self._file_content_hash = current_hash
            return True
        except Exception as exc:
            print(f"[dialog] user memory reload failed: {exc}")
            return False

    def _ensure_profile(self, user_id: str, now_ts: float) -> Dict[str, Any]:
        profiles = self._db.setdefault("profiles", {})
        profile = profiles.get(user_id)
        if not isinstance(profile, dict):
            profile = {
                "display_name": user_id,
                "name": "",
                "likes": [],
                "dislikes": [],
                "goals": [],
                "facts": [],
                "episodes": [],
                "game_history": [],
                "recent_notes": [],
                "memory_items": [],
                "preferred_training_day": "",
                "preferred_training_time": "",
                "origin": "",
                "favorite_game": "",
                "last_game_reference": "",
                "last_game_reference_kind": "",
                "last_game_reference_source": "",
                "last_game_reference_ts": 0.0,
                "last_game_mentioned": "",
                "last_game_mentioned_ts": 0.0,
                "last_game_recommended": "",
                "last_game_recommended_ts": 0.0,
                "last_game_launched": "",
                "last_game_launched_ts": 0.0,
                "recent_game_candidates": [],
                "current_topic": "",
                "open_question": "",
                "dialog_summary": "",
                "dialog_turns": [],
                "first_seen_ts": now_ts,
                "last_seen_ts": now_ts,
                "utterance_count": 0,
            }
            profiles[user_id] = profile
            return profile

        profile.setdefault("display_name", user_id)
        profile.setdefault("name", "")
        profile.setdefault("likes", [])
        profile.setdefault("dislikes", [])
        profile.setdefault("goals", [])
        profile.setdefault("facts", [])
        profile.setdefault("episodes", [])
        profile.setdefault("game_history", [])
        profile.setdefault("recent_notes", [])
        profile.setdefault("memory_items", [])
        profile.setdefault("preferred_training_day", "")
        profile.setdefault("preferred_training_time", "")
        profile.setdefault("origin", "")
        profile.setdefault("favorite_game", "")
        profile.setdefault("last_game_reference", "")
        profile.setdefault("last_game_reference_kind", "")
        profile.setdefault("last_game_reference_source", "")
        profile.setdefault("last_game_reference_ts", 0.0)
        profile.setdefault("last_game_mentioned", "")
        profile.setdefault("last_game_mentioned_ts", 0.0)
        profile.setdefault("last_game_recommended", "")
        profile.setdefault("last_game_recommended_ts", 0.0)
        profile.setdefault("last_game_launched", "")
        profile.setdefault("last_game_launched_ts", 0.0)
        profile.setdefault("recent_game_candidates", [])
        profile.setdefault("current_topic", "")
        profile.setdefault("open_question", "")
        profile.setdefault("dialog_summary", "")
        profile.setdefault("dialog_turns", [])
        profile.setdefault("first_seen_ts", now_ts)
        profile.setdefault("last_seen_ts", now_ts)
        profile.setdefault("utterance_count", 0)
        profile["recent_game_candidates"] = _coerce_recent_game_candidates(profile.get("recent_game_candidates"))
        self._migrate_legacy_profile_fields(profile, now_ts)
        return profile

    def _fact_items(self, profile: Dict[str, Any]) -> List[Dict[str, Any]]:
        facts = profile.get("facts")
        if not isinstance(facts, list):
            facts = []
            profile["facts"] = facts
        return facts

    def _episode_items(self, profile: Dict[str, Any]) -> List[Dict[str, Any]]:
        episodes = profile.get("episodes")
        if not isinstance(episodes, list):
            episodes = []
            profile["episodes"] = episodes
        return episodes

    def _game_history_items(self, profile: Dict[str, Any]) -> List[Dict[str, Any]]:
        history = profile.get("game_history")
        if not isinstance(history, list):
            history = []
            profile["game_history"] = history
        return history

    def _recent_game_candidates_items(self, profile: Dict[str, Any]) -> List[Dict[str, Any]]:
        items = _coerce_recent_game_candidates(profile.get("recent_game_candidates"))
        profile["recent_game_candidates"] = items
        return items

    def _push_recent_game_candidate(
        self,
        profile: Dict[str, Any],
        *,
        game_name: str,
        now_ts: float,
        reference_kind: str,
        source: str,
    ) -> None:
        normalized_name = normalize_memory_value(game_name, max_len=64)
        if not normalized_name:
            return
        items = self._recent_game_candidates_items(profile)
        folded = normalized_name.casefold()
        items = [item for item in items if str(item.get("game_name") or "").strip().casefold() != folded]
        items.insert(
            0,
            {
                "game_name": normalized_name,
                "ts": float(now_ts),
                "kind": _normalize_game_context_kind(reference_kind),
                "source": normalize_memory_value(source, max_len=32),
            },
        )
        profile["recent_game_candidates"] = items[:_GAME_CONTEXT_MAX_CANDIDATES]

    @staticmethod
    def _fresh_game_context_value(profile: Dict[str, Any], field: str, *, now_ts: float, max_age_sec: float) -> str:
        name = normalize_memory_value(str(profile.get(field) or ""), max_len=64)
        if not name:
            return ""
        if max_age_sec <= 0.0:
            return name
        ts = float(profile.get(f"{field}_ts") or 0.0)
        if ts > 0.0 and now_ts - ts > max_age_sec:
            return ""
        return name

    def _active_fact_records(self, profile: Dict[str, Any], field: Optional[str] = None) -> List[Dict[str, Any]]:
        items = self._fact_items(profile)
        out: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("status") or "active").strip().lower() != "active":
                continue
            item_field = str(item.get("field") or "").strip()
            if field and item_field != field:
                continue
            value = _normalize_fact_value(item_field, str(item.get("value") or ""))
            if not value:
                continue
            out.append(item)
        out.sort(key=lambda entry: float(entry.get("updated_ts") or entry.get("created_ts") or 0.0))
        return out

    def _active_fact_values(self, profile: Dict[str, Any], field: str, *, limit: int = 0) -> List[str]:
        values: List[str] = []
        seen = set()
        for item in reversed(self._active_fact_records(profile, field)):
            value = _normalize_fact_value(field, str(item.get("value") or ""))
            if not value:
                continue
            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            values.append(value)
            if limit > 0 and len(values) >= limit:
                break
        values.reverse()
        return values

    def _latest_fact_value(self, profile: Dict[str, Any], field: str) -> str:
        values = self._active_fact_values(profile, field, limit=1)
        return values[-1] if values else ""

    def _sync_legacy_profile_fields(self, profile: Dict[str, Any]) -> None:
        profile["name"] = self._latest_fact_value(profile, "name")
        profile["likes"] = self._active_fact_values(profile, "like", limit=8)
        profile["dislikes"] = self._active_fact_values(profile, "dislike", limit=8)
        profile["goals"] = self._active_fact_values(profile, "goal", limit=8)
        profile["preferred_training_day"] = self._latest_fact_value(profile, "preferred_training_day")
        profile["preferred_training_time"] = self._latest_fact_value(profile, "preferred_training_time")
        profile["origin"] = self._latest_fact_value(profile, "origin")
        profile["favorite_game"] = self._latest_fact_value(profile, "favorite_game")

    def _migrate_legacy_profile_fields(self, profile: Dict[str, Any], now_ts: float) -> None:
        facts = self._fact_items(profile)
        if facts:
            self._sync_legacy_profile_fields(profile)
            return

        def _seed(field: str, raw_value: str) -> None:
            value = _normalize_fact_value(field, raw_value)
            if not value:
                return
            record_id = self._hash_text(f"{field}|{value}|{now_ts:.6f}")
            facts.append(
                {
                    "id": record_id,
                    "field": field,
                    "value": value,
                    "normalized_value": value.casefold(),
                    "status": "active",
                    "confidence": 0.5,
                    "source_text": "",
                    "source_kind": "migrated",
                    "created_ts": now_ts,
                    "updated_ts": now_ts,
                    "last_confirmed_ts": now_ts,
                    "explicit": False,
                }
            )

        _seed("name", str(profile.get("name") or ""))
        _seed("preferred_training_day", str(profile.get("preferred_training_day") or ""))
        _seed("preferred_training_time", str(profile.get("preferred_training_time") or ""))
        _seed("origin", str(profile.get("origin") or ""))
        _seed("favorite_game", str(profile.get("favorite_game") or ""))
        for raw in profile.get("likes", []) or []:
            _seed("like", str(raw))
        for raw in profile.get("dislikes", []) or []:
            _seed("dislike", str(raw))
        for raw in profile.get("goals", []) or []:
            _seed("goal", str(raw))
        self._sync_legacy_profile_fields(profile)

    def _record_episode(
        self,
        profile: Dict[str, Any],
        text: str,
        *,
        role: str,
        now_ts: float,
        max_items: int = 48,
    ) -> None:
        utterance = normalize_dialog_text(text, max_len=220)
        if not utterance:
            return
        episodes = self._episode_items(profile)
        entry: Dict[str, Any] = {
            "text": utterance,
            "role": _normalize_dialog_role(role),
            "ts": now_ts,
        }
        if self.embedder is not None and self.embedder.ready:
            vector = self.embedder.doc_embedding(utterance)
            if vector is not None:
                entry["embedding"] = [round(float(x), 6) for x in vector.tolist()]
        episodes.append(entry)
        if len(episodes) > max_items:
            del episodes[:-max_items]

    def record_game_event(
        self,
        user_id: str,
        *,
        game_name: str,
        action: str = "launch",
        source: str = "conversation",
        now_ts: Optional[float] = None,
        max_items: int = 12,
    ) -> None:
        normalized_name = normalize_memory_value(game_name, max_len=64)
        normalized_action = normalize_memory_value(action, max_len=24) or "launch"
        if not user_id or not normalized_name:
            return
        event_ts = float(now_ts if now_ts is not None else time.time())
        with self._lock:
            reload_ok = self._reload_if_external_change_unlocked()
            if not reload_ok:
                return
            profile = self._ensure_profile(user_id, event_ts)
            profile["last_seen_ts"] = event_ts
            history = self._game_history_items(profile)
            history.append(
                {
                    "game_name": normalized_name,
                    "action": normalized_action,
                    "source": normalize_memory_value(source, max_len=32) or "conversation",
                    "ts": event_ts,
                }
            )
            if len(history) > max_items:
                del history[:-max_items]
            profile["last_game_reference"] = normalized_name
            profile["last_game_reference_kind"] = "launch"
            profile["last_game_reference_source"] = normalize_memory_value(source, max_len=32) or "conversation"
            profile["last_game_reference_ts"] = event_ts
            profile["last_game_launched"] = normalized_name
            profile["last_game_launched_ts"] = event_ts
            profile["last_game_mentioned"] = normalized_name
            profile["last_game_mentioned_ts"] = event_ts
            self._push_recent_game_candidate(
                profile,
                game_name=normalized_name,
                now_ts=event_ts,
                reference_kind="launch",
                source=source,
            )
            self._save()

    def remember_game_mentions(
        self,
        user_id: str,
        game_names: List[str],
        *,
        reference_kind: str = "mentioned",
        source: str = "assistant",
        now_ts: Optional[float] = None,
    ) -> None:
        if not user_id:
            return
        cleaned_names: List[str] = []
        seen = set()
        for raw in game_names:
            game_name = normalize_memory_value(str(raw or ""), max_len=64)
            if not game_name:
                continue
            folded = game_name.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            cleaned_names.append(game_name)
        if not cleaned_names:
            return

        reference_ts = float(now_ts if now_ts is not None else time.time())
        normalized_kind = _normalize_game_context_kind(reference_kind)
        normalized_source = normalize_memory_value(source, max_len=32) or "assistant"
        with self._lock:
            reload_ok = self._reload_if_external_change_unlocked()
            if not reload_ok:
                return
            profile = self._ensure_profile(user_id, reference_ts)
            profile["last_seen_ts"] = reference_ts
            for game_name in cleaned_names:
                profile["last_game_mentioned"] = game_name
                profile["last_game_mentioned_ts"] = reference_ts
                self._push_recent_game_candidate(
                    profile,
                    game_name=game_name,
                    now_ts=reference_ts,
                    reference_kind=normalized_kind,
                    source=normalized_source,
                )
            self._save()

    def set_game_reference(
        self,
        user_id: str,
        *,
        game_name: str,
        reference_kind: str = "mentioned",
        source: str = "assistant",
        now_ts: Optional[float] = None,
    ) -> None:
        normalized_name = normalize_memory_value(game_name, max_len=64)
        if not user_id or not normalized_name:
            return
        reference_ts = float(now_ts if now_ts is not None else time.time())
        normalized_kind = _normalize_game_context_kind(reference_kind)
        normalized_source = normalize_memory_value(source, max_len=32) or "assistant"
        with self._lock:
            reload_ok = self._reload_if_external_change_unlocked()
            if not reload_ok:
                return
            profile = self._ensure_profile(user_id, reference_ts)
            profile["last_seen_ts"] = reference_ts
            profile["last_game_reference"] = normalized_name
            profile["last_game_reference_kind"] = normalized_kind
            profile["last_game_reference_source"] = normalized_source
            profile["last_game_reference_ts"] = reference_ts
            profile["last_game_mentioned"] = normalized_name
            profile["last_game_mentioned_ts"] = reference_ts
            if normalized_kind == "recommend":
                profile["last_game_recommended"] = normalized_name
                profile["last_game_recommended_ts"] = reference_ts
            elif normalized_kind == "launch":
                profile["last_game_launched"] = normalized_name
                profile["last_game_launched_ts"] = reference_ts
            self._push_recent_game_candidate(
                profile,
                game_name=normalized_name,
                now_ts=reference_ts,
                reference_kind=normalized_kind,
                source=normalized_source,
            )
            self._save()

    def get_game_reference(self, user_id: str, *, max_age_sec: float = _GAME_CONTEXT_MAX_AGE_SEC) -> str:
        if not user_id:
            return ""
        now_ts = time.time()
        with self._lock:
            self._reload_if_external_change_unlocked()
            profiles = self._db.get("profiles")
            if not isinstance(profiles, dict):
                return ""
            profile = profiles.get(user_id)
            if not isinstance(profile, dict):
                return ""
            for field in ("last_game_mentioned", "last_game_recommended", "last_game_launched", "last_game_reference"):
                name = self._fresh_game_context_value(profile, field, now_ts=now_ts, max_age_sec=max_age_sec)
                if name:
                    return name
            return ""

    def _upsert_fact(
        self,
        profile: Dict[str, Any],
        *,
        field: str,
        value: str,
        now_ts: float,
        source_text: str,
        explicit: bool,
        confidence: float = 0.92,
        source_kind: str = "utterance",
    ) -> Optional[Dict[str, Any]]:
        normalized_value = _normalize_fact_value(field, value)
        if not normalized_value:
            return None

        facts = self._fact_items(profile)
        multi = field in _FACT_MULTI_FIELDS
        existing_same: Optional[Dict[str, Any]] = None
        active_conflicts: List[Dict[str, Any]] = []
        for item in facts:
            if not isinstance(item, dict):
                continue
            if str(item.get("field") or "") != field:
                continue
            status = str(item.get("status") or "active").strip().lower()
            item_value = _normalize_fact_value(field, str(item.get("value") or ""))
            if status == "active" and item_value == normalized_value:
                existing_same = item
                break
            if status == "active" and not multi and item_value and item_value != normalized_value:
                active_conflicts.append(item)

        if existing_same is not None:
            existing_same["value"] = normalized_value
            existing_same["normalized_value"] = normalized_value.casefold()
            existing_same["updated_ts"] = now_ts
            existing_same["last_confirmed_ts"] = now_ts
            existing_same["confidence"] = max(float(existing_same.get("confidence") or 0.0), float(confidence))
            existing_same["explicit"] = bool(existing_same.get("explicit")) or explicit
            if source_text:
                existing_same["source_text"] = source_text
            return existing_same

        record_id = self._hash_text(f"{field}|{normalized_value}|{now_ts:.6f}")
        record = {
            "id": record_id,
            "field": field,
            "value": normalized_value,
            "normalized_value": normalized_value.casefold(),
            "status": "active",
            "confidence": float(confidence),
            "source_text": source_text,
            "source_kind": source_kind,
            "created_ts": now_ts,
            "updated_ts": now_ts,
            "last_confirmed_ts": now_ts,
            "explicit": explicit,
        }
        if active_conflicts:
            record["supersedes"] = [str(item.get("id") or "") for item in active_conflicts if str(item.get("id") or "")]
            for item in active_conflicts:
                item["status"] = "superseded"
                item["updated_ts"] = now_ts
                item["superseded_by"] = record_id
        facts.append(record)
        return record

    def _deactivate_fact(
        self,
        profile: Dict[str, Any],
        *,
        field: str,
        value: str,
        now_ts: float,
        reason: str,
    ) -> bool:
        normalized_value = _normalize_fact_value(field, value)
        if not normalized_value:
            return False
        changed = False
        for item in self._fact_items(profile):
            if not isinstance(item, dict):
                continue
            if str(item.get("field") or "") != field:
                continue
            status = str(item.get("status") or "active").strip().lower()
            item_value = _normalize_fact_value(field, str(item.get("value") or ""))
            if status != "active" or item_value != normalized_value:
                continue
            item["status"] = "inactive"
            item["updated_ts"] = now_ts
            item["deactivation_reason"] = reason
            changed = True
        return changed

    def _extract_fact_candidates(self, utterance: str) -> tuple[List[Dict[str, str]], List[Dict[str, str]]]:
        lowered = utterance.casefold()
        add_ops: List[Dict[str, str]] = []
        remove_ops: List[Dict[str, str]] = []

        for pattern in _NAME_PATTERNS:
            match = pattern.search(lowered)
            if not match:
                continue
            candidate = _normalize_name_value(match.group(1))
            if candidate:
                add_ops.append({"field": "name", "value": candidate})
            break

        for pattern in _FAVORITE_GAME_PATTERNS:
            match = pattern.search(lowered)
            if match:
                candidate = _normalize_fact_value("favorite_game", match.group(1))
                if candidate:
                    add_ops.append({"field": "favorite_game", "value": candidate})
                    add_ops.append({"field": "like", "value": candidate})
                break

        for pattern in _LIKE_PATTERNS:
            match = pattern.search(lowered)
            if not match:
                continue
            candidate = _normalize_fact_value("like", match.group(1))
            if candidate:
                add_ops.append({"field": "like", "value": candidate})
            break

        for pattern in _NO_LONGER_LIKE_PATTERNS:
            match = pattern.search(lowered)
            if match:
                candidate = _normalize_fact_value("like", match.group(1))
                if candidate:
                    remove_ops.append({"field": "like", "value": candidate})
                break

        for pattern in _DISLIKE_PATTERNS:
            match = pattern.search(lowered)
            if not match:
                continue
            candidate = _normalize_fact_value("dislike", match.group(1))
            if candidate:
                add_ops.append({"field": "dislike", "value": candidate})
            break

        for pattern in _GOAL_PATTERNS:
            match = pattern.search(lowered)
            if not match:
                continue
            candidate = _normalize_fact_value("goal", match.group(1))
            if candidate:
                add_ops.append({"field": "goal", "value": candidate})
            break

        preferred_day = _extract_preferred_training_day(utterance)
        if preferred_day:
            add_ops.append({"field": "preferred_training_day", "value": preferred_day})

        preferred_time = _extract_preferred_training_time(utterance)
        if preferred_time:
            add_ops.append({"field": "preferred_training_time", "value": preferred_time})

        origin_value = _extract_origin_hint([utterance])
        if origin_value:
            add_ops.append({"field": "origin", "value": origin_value})

        deduped_adds: List[Dict[str, str]] = []
        seen_adds = set()
        for item in add_ops:
            key = (str(item.get("field") or ""), str(item.get("value") or "").casefold())
            if key in seen_adds:
                continue
            seen_adds.add(key)
            deduped_adds.append(item)

        deduped_removes: List[Dict[str, str]] = []
        seen_removes = set()
        for item in remove_ops:
            key = (str(item.get("field") or ""), str(item.get("value") or "").casefold())
            if key in seen_removes:
                continue
            seen_removes.add(key)
            deduped_removes.append(item)
        return deduped_adds, deduped_removes

    def _summarize_memory_write(self, source_text: str, updates: List[Dict[str, Any]]) -> str:
        if _contains_only_ok_instruction(source_text):
            return "Okay."
        if not updates:
            return "Okay, I'll remember that."
        rendered: List[str] = []
        for item in updates[:3]:
            field = str(item.get("field") or "")
            value = _normalize_fact_value(field, str(item.get("value") or ""))
            if not value:
                continue
            if field == "goal":
                rendered.append(f"your goal is {value}")
            elif field == "name":
                rendered.append(f"your name is {value}")
            elif field == "like":
                rendered.append(f"you like {value}")
            elif field == "dislike":
                rendered.append(f"you dislike {value}")
            elif field == "favorite_game":
                rendered.append(f"your favorite game is {value}")
            elif field == "origin":
                rendered.append(f"you are from {value}")
            elif field == "preferred_training_day":
                rendered.append(f"you prefer training on {value}")
            elif field == "preferred_training_time":
                rendered.append(f"you prefer training in the {value}")
        if not rendered:
            return "Okay, I'll remember that."
        if len(rendered) == 1:
            return f"Okay, I'll remember {rendered[0]}."
        return "Okay, I'll remember " + "; ".join(rendered) + "."

    @staticmethod
    def _summarize_implicit_fact_ack(source_text: str, updates: List[Dict[str, Any]]) -> str:
        if not _should_ack_implicit_fact_share(source_text, updates):
            return ""
        return "Got it."

    def resolve_user(self, identity_key: str) -> str:
        now_ts = time.time()
        with self._lock:
            reload_ok = self._reload_if_external_change_unlocked()
            identity_map = self._db.setdefault("identity_map", {})
            record = identity_map.get(identity_key)
            if isinstance(record, dict):
                user_id = str(record.get("user_id") or "").strip()
                if user_id:
                    record["last_seen_ts"] = now_ts
                    record["sample_count"] = int(record.get("sample_count", 0) or 0) + 1
                    profile = self._ensure_profile(user_id, now_ts)
                    profile["last_seen_ts"] = now_ts
                    if reload_ok:
                        self._save()
                    return user_id

            next_user_index = max(1, int(self._db.get("next_user_index", 1) or 1))
            user_id = f"user_{next_user_index:03d}"
            self._db["next_user_index"] = next_user_index + 1
            identity_map[identity_key] = {
                "user_id": user_id,
                "first_seen_ts": now_ts,
                "last_seen_ts": now_ts,
                "sample_count": 1,
            }
            profile = self._ensure_profile(user_id, now_ts)
            profile["display_name"] = profile.get("display_name") or f"User {next_user_index:03d}"
            if reload_ok:
                self._save()
            return user_id

    def remember_utterance(self, user_id: str, text: str) -> Dict[str, Any]:
        utterance = normalize_memory_value(text, max_len=180)
        if not utterance:
            return {
                "explicit_memory": False,
                "facts_written": [],
                "facts_removed": [],
                "ack_text": "",
                "utterance": "",
            }

        with self._lock:
            reload_ok = self._reload_if_external_change_unlocked()
            if not reload_ok:
                # Avoid writing stale in-memory data back to disk when external file is transiently unreadable.
                return {
                    "explicit_memory": False,
                    "facts_written": [],
                    "facts_removed": [],
                    "ack_text": "",
                    "utterance": utterance,
                }
            now_ts = time.time()
            profile = self._ensure_profile(user_id, now_ts)
            profile["last_seen_ts"] = now_ts
            profile["utterance_count"] = int(profile.get("utterance_count", 0) or 0) + 1
            explicit_memory = _looks_like_explicit_memory_write(text)
            added_facts, removed_facts = self._extract_fact_candidates(utterance)
            written_records: List[Dict[str, Any]] = []
            removed_records: List[Dict[str, Any]] = []
            for removal in removed_facts:
                field = str(removal.get("field") or "").strip()
                value = str(removal.get("value") or "").strip()
                if field and value and self._deactivate_fact(
                    profile,
                    field=field,
                    value=value,
                    now_ts=now_ts,
                    reason="user_update",
                ):
                    removed_records.append({"field": field, "value": value})
            for addition in added_facts:
                field = str(addition.get("field") or "").strip()
                value = str(addition.get("value") or "").strip()
                if not field or not value:
                    continue
                record = self._upsert_fact(
                    profile,
                    field=field,
                    value=value,
                    now_ts=now_ts,
                    source_text=utterance,
                    explicit=explicit_memory,
                )
                if record is not None:
                    written_records.append({"field": field, "value": str(record.get("value") or value)})

            notes = profile.get("recent_notes")
            if not isinstance(notes, list):
                notes = []
                profile["recent_notes"] = notes
            if self.max_notes > 0 and len(utterance) >= 8:
                _append_unique_casefold(notes, utterance, max_items=max(0, self.max_notes))

            memories = profile.get("memory_items")
            if not isinstance(memories, list):
                memories = []
                profile["memory_items"] = memories
            if self.max_notes > 0 and len(utterance) >= 8 and self.embedder is not None and self.embedder.ready:
                vector = self.embedder.doc_embedding(utterance)
                if vector is not None:
                    has_duplicate = False
                    for item in memories:
                        if not isinstance(item, dict):
                            continue
                        text_value = normalize_memory_value(str(item.get("text") or ""), max_len=180)
                        if text_value and text_value.casefold() == utterance.casefold():
                            has_duplicate = True
                            break
                    if not has_duplicate:
                        memories.append(
                            {
                                "text": utterance,
                                "embedding": [round(float(x), 6) for x in vector.tolist()],
                                "ts": time.time(),
                            }
                        )
                        if len(memories) > self.max_notes:
                            del memories[:-self.max_notes]

            self._record_episode(profile, utterance, role="user", now_ts=now_ts)
            self._sync_legacy_profile_fields(profile)
            self._save()
            ack_text = ""
            if explicit_memory:
                ack_text = self._summarize_memory_write(text, written_records)
            elif written_records:
                ack_text = self._summarize_implicit_fact_ack(text, written_records)
            return {
                "explicit_memory": explicit_memory,
                "facts_written": written_records,
                "facts_removed": removed_records,
                "ack_text": ack_text,
                "utterance": utterance,
            }

    @staticmethod
    def _summarize_dialog_chunk(turns: List[Dict[str, Any]], *, max_chars: int = 360) -> str:
        if not turns:
            return ""
        parts: List[str] = []
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            role = _normalize_dialog_role(str(turn.get("role") or "user"))
            text = normalize_dialog_text(str(turn.get("text") or ""), max_len=140)
            if not text:
                continue
            speaker = "User" if role == "user" else "Coach"
            parts.append(f"{speaker}: {text}")
        if not parts:
            return ""
        summary = " | ".join(parts).strip()
        if len(summary) > max_chars:
            summary = summary[-max_chars:].lstrip()
            if summary and not summary.startswith("..."):
                if len(summary) > max(12, max_chars - 4):
                    summary = summary[-(max_chars - 4) :].lstrip()
                summary = f"... {summary}"
        return summary

    def remember_dialog_turn(
        self,
        user_id: str,
        role: str,
        text: str,
        *,
        max_turns: int = 8,
        summary_max_chars: int = 420,
    ) -> None:
        utterance = normalize_dialog_text(text, max_len=240)
        if not utterance:
            return

        normalized_role = _normalize_dialog_role(role)
        max_turns = max(2, int(max_turns))
        summary_max_chars = max(120, int(summary_max_chars))

        with self._lock:
            reload_ok = self._reload_if_external_change_unlocked()
            if not reload_ok:
                return

            now_ts = time.time()
            profile = self._ensure_profile(user_id, now_ts)
            profile["last_seen_ts"] = now_ts

            turns = profile.get("dialog_turns")
            if not isinstance(turns, list):
                turns = []
                profile["dialog_turns"] = turns
            turns.append(
                {
                    "role": normalized_role,
                    "text": utterance,
                    "ts": time.time(),
                }
            )

            overflow = len(turns) - max_turns
            if overflow > 0:
                dropped = [item for item in turns[:overflow] if isinstance(item, dict)]
                del turns[:overflow]

                chunk_summary = self._summarize_dialog_chunk(dropped, max_chars=max(120, summary_max_chars // 2))
                if chunk_summary:
                    existing = normalize_dialog_text(
                        str(profile.get("dialog_summary") or ""),
                        max_len=max(summary_max_chars * 2, summary_max_chars),
                    )
                    merged = f"{existing} {chunk_summary}".strip() if existing else chunk_summary
                    if len(merged) > summary_max_chars:
                        merged = merged[-summary_max_chars:].lstrip()
                        if merged and not merged.startswith("..."):
                            if len(merged) > max(12, summary_max_chars - 4):
                                merged = merged[-(summary_max_chars - 4) :].lstrip()
                            merged = f"... {merged}"
                    profile["dialog_summary"] = merged

            self._record_episode(profile, utterance, role=normalized_role, now_ts=now_ts)
            self._save()

    def update_dialog_slots(
        self,
        user_id: str,
        *,
        current_topic: Optional[str] = None,
        open_question: Optional[str] = None,
    ) -> None:
        with self._lock:
            reload_ok = self._reload_if_external_change_unlocked()
            if not reload_ok:
                return
            profile = self._ensure_profile(user_id, time.time())
            changed = False

            if current_topic is not None:
                topic = normalize_memory_value(current_topic, max_len=96)
                if str(profile.get("current_topic") or "") != topic:
                    profile["current_topic"] = topic
                    changed = True

            if open_question is not None:
                question = normalize_dialog_text(open_question, max_len=160)
                if question and question[-1] not in "?!。！？":
                    question = f"{question}?"
                if str(profile.get("open_question") or "") != question:
                    profile["open_question"] = question
                    changed = True

            if changed:
                profile["last_seen_ts"] = time.time()
                self._save()

    def get_dialog_slots(self, user_id: str) -> Dict[str, str]:
        with self._lock:
            self._reload_if_external_change_unlocked()
            profiles = self._db.get("profiles")
            if not isinstance(profiles, dict):
                return {"current_topic": "", "open_question": "", "dialog_summary": ""}
            profile = profiles.get(user_id)
            if not isinstance(profile, dict):
                return {"current_topic": "", "open_question": "", "dialog_summary": ""}
            return {
                "current_topic": normalize_memory_value(str(profile.get("current_topic") or ""), max_len=96),
                "open_question": normalize_dialog_text(str(profile.get("open_question") or ""), max_len=160),
                "dialog_summary": normalize_dialog_text(str(profile.get("dialog_summary") or ""), max_len=480),
            }

    def build_dialog_context(
        self,
        user_id: str,
        *,
        max_turns: int = 8,
        max_chars: int = 900,
        include_summary: bool = True,
        include_slots: bool = True,
        include_recent_dialogue: bool = True,
    ) -> str:
        max_turns = max(2, int(max_turns))
        max_chars = max(180, int(max_chars))
        with self._lock:
            self._reload_if_external_change_unlocked()
            profiles = self._db.get("profiles")
            if not isinstance(profiles, dict):
                return ""
            profile = profiles.get(user_id)
            if not isinstance(profile, dict):
                return ""

            summary = (
                normalize_dialog_text(str(profile.get("dialog_summary") or ""), max_len=max_chars)
                if include_summary
                else ""
            )
            current_topic = (
                normalize_memory_value(str(profile.get("current_topic") or ""), max_len=96)
                if include_slots
                else ""
            )
            open_question = (
                normalize_dialog_text(str(profile.get("open_question") or ""), max_len=160)
                if include_slots
                else ""
            )
            turns = profile.get("dialog_turns")
            recent_lines: List[str] = []
            if include_recent_dialogue and isinstance(turns, list) and turns:
                for turn in turns[-max_turns:]:
                    if not isinstance(turn, dict):
                        continue
                    role = _normalize_dialog_role(str(turn.get("role") or "user"))
                    text = normalize_dialog_text(str(turn.get("text") or ""), max_len=180)
                    if not text:
                        continue
                    speaker = "User" if role == "user" else "Coach"
                    recent_lines.append(f"{speaker}: {text}")

            base_lines: List[str] = []
            if current_topic:
                base_lines.append(f"Current topic: {current_topic}.")
            if open_question:
                base_lines.append(f"Open question: {open_question}")

            summary_line = f"Conversation summary: {summary}" if summary else ""

            def _compose(lines: List[str], summary_text: str, recents: List[str]) -> str:
                parts: List[str] = []
                parts.extend(lines)
                if summary_text:
                    parts.append(summary_text)
                if recents:
                    parts.append("Recent dialogue:")
                    parts.extend(recents)
                return "\n".join(parts).strip()

            context = _compose(base_lines, summary_line, recent_lines)
            if len(context) <= max_chars:
                return context

            # Trim oldest recent turns first to preserve summary and slot state.
            while recent_lines and len(context) > max_chars:
                recent_lines.pop(0)
                context = _compose(base_lines, summary_line, recent_lines)
            if len(context) <= max_chars:
                return context

            if summary_line:
                prefix = "Conversation summary: "
                summary_text = summary_line[len(prefix) :]
                available = max(48, max_chars - len(_compose(base_lines, "", recent_lines)) - len(prefix) - 1)
                if len(summary_text) > available:
                    summary_text = summary_text[-available:].lstrip()
                    if summary_text and not summary_text.startswith("..."):
                        if len(summary_text) > available - 4:
                            summary_text = summary_text[-(available - 4) :].lstrip()
                        summary_text = f"... {summary_text}"
                summary_line = prefix + summary_text
                context = _compose(base_lines, summary_line, recent_lines)

            if len(context) <= max_chars:
                return context

            # Final conservative fallback.
            return context[-max_chars:].lstrip()

    def _retrieve_relevant_notes(self, profile: Dict[str, Any], query_text: str) -> List[str]:
        if self.embedder is None or not self.embedder.ready or onnx_np is None:
            return []
        query = normalize_memory_value(query_text, max_len=220)
        if not query:
            return []
        qvec = self.embedder.query_embedding(query)
        if qvec is None:
            return []

        scored: List[tuple[float, str]] = []
        candidate_groups = []
        episodes = profile.get("episodes")
        if isinstance(episodes, list) and episodes:
            candidate_groups.append(episodes)
        memories = profile.get("memory_items")
        if isinstance(memories, list) and memories:
            candidate_groups.append(memories)
        if not candidate_groups:
            return []

        for group in candidate_groups:
            for item in group:
                if not isinstance(item, dict):
                    continue
                text = normalize_memory_value(str(item.get("text") or ""), max_len=120)
                if not text:
                    continue
                embedding = item.get("embedding")
                if not isinstance(embedding, list) or not embedding:
                    continue
                try:
                    vec = onnx_np.asarray(embedding, dtype=onnx_np.float32).reshape(-1)
                except Exception:
                    continue
                score = cosine_similarity(qvec, vec)
                if score <= 0.05:
                    continue
                scored.append((score, text))

        if not scored:
            return []
        scored.sort(key=lambda item: item[0], reverse=True)
        out: List[str] = []
        seen = set()
        for _, text in scored:
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
            if len(out) >= self.retrieve_top_k:
                break
        return out

    def build_memory_context(self, user_id: str, query_text: str = "") -> str:
        with self._lock:
            self._reload_if_external_change_unlocked()
            profiles = self._db.get("profiles")
            if not isinstance(profiles, dict):
                return ""
            profile = profiles.get(user_id)
            if not isinstance(profile, dict):
                return ""

            lines: List[str] = [f"Active user id: {user_id}."]
            self._sync_legacy_profile_fields(profile)
            name = self._latest_fact_value(profile, "name")
            if name:
                lines.append(f"Preferred name: {name}.")

            current_topic = normalize_memory_value(str(profile.get("current_topic") or ""), max_len=96)
            if current_topic:
                lines.append(f"Current topic: {current_topic}.")

            last_game_mentioned = self._fresh_game_context_value(
                profile,
                "last_game_mentioned",
                now_ts=time.time(),
                max_age_sec=_GAME_CONTEXT_MAX_AGE_SEC,
            )
            if last_game_mentioned:
                lines.append(f"Recent mentioned game: {last_game_mentioned}.")

            last_game_recommended = self._fresh_game_context_value(
                profile,
                "last_game_recommended",
                now_ts=time.time(),
                max_age_sec=_GAME_CONTEXT_MAX_AGE_SEC,
            )
            if last_game_recommended:
                lines.append(f"Recent recommended game: {last_game_recommended}.")

            last_game_launched = self._fresh_game_context_value(
                profile,
                "last_game_launched",
                now_ts=time.time(),
                max_age_sec=_GAME_CONTEXT_MAX_AGE_SEC,
            )
            if last_game_launched:
                lines.append(f"Recent launched game: {last_game_launched}.")

            last_game_reference = normalize_memory_value(str(profile.get("last_game_reference") or ""), max_len=64)
            if last_game_reference:
                lines.append(f"Recent referenced game: {last_game_reference}.")

            recent_candidates = [
                normalize_memory_value(str(item.get("game_name") or ""), max_len=64)
                for item in self._recent_game_candidates_items(profile)
            ]
            recent_candidates = [item for item in recent_candidates if item]
            if recent_candidates:
                lines.append("Recent game candidates: " + ", ".join(recent_candidates) + ".")

            open_question = normalize_dialog_text(str(profile.get("open_question") or ""), max_len=160)
            if open_question:
                lines.append(f"Open question: {open_question}")

            preferred_day = self._latest_fact_value(profile, "preferred_training_day")
            if preferred_day:
                lines.append(f"Preferred training day: {preferred_day}.")

            preferred_time = self._latest_fact_value(profile, "preferred_training_time")
            if preferred_time:
                lines.append(f"Preferred training time: {preferred_time}.")

            origin = self._latest_fact_value(profile, "origin")
            if origin:
                lines.append(f"Origin: {origin}.")

            favorite_game = self._latest_fact_value(profile, "favorite_game")
            if favorite_game:
                lines.append(f"Favorite game: {favorite_game}.")

            likes_clean = self._active_fact_values(profile, "like", limit=5)
            if likes_clean:
                lines.append("Likes: " + ", ".join(likes_clean) + ".")

            dislikes_clean = self._active_fact_values(profile, "dislike", limit=5)
            if dislikes_clean:
                lines.append("Dislikes: " + ", ".join(dislikes_clean) + ".")

            goals_clean = self._active_fact_values(profile, "goal", limit=4)
            if goals_clean:
                lines.append("Goals: " + ", ".join(goals_clean) + ".")

            notes = profile.get("recent_notes")
            notes_clean: List[str] = []
            if isinstance(notes, list) and notes:
                tail = notes[-3:]
                notes_clean = [normalize_memory_value(str(item), max_len=80) for item in tail]
                notes_clean = [item for item in notes_clean if item]
                if notes_clean:
                    lines.append("Recent notes: " + " | ".join(notes_clean) + ".")

            history = profile.get("game_history")
            if isinstance(history, list) and history:
                recent_games: List[str] = []
                seen_games = set()
                for item in reversed(history[-4:]):
                    if not isinstance(item, dict):
                        continue
                    game_name = normalize_memory_value(str(item.get("game_name") or ""), max_len=48)
                    if not game_name:
                        continue
                    key = game_name.casefold()
                    if key in seen_games:
                        continue
                    seen_games.add(key)
                    recent_games.append(game_name)
                    if len(recent_games) >= 3:
                        break
                recent_games.reverse()
                if recent_games:
                    lines.append("Recent games: " + ", ".join(recent_games) + ".")

            relevant = self._retrieve_relevant_notes(profile, query_text)
            if relevant:
                lines.append("Relevant memory: " + " | ".join(relevant) + ".")

            context = " ".join(lines).strip()
            if len(context) > self.prompt_max_chars:
                context = context[: self.prompt_max_chars].rstrip()
            return context

    def build_facts_reply(self, user_id: str) -> str:
        with self._lock:
            self._reload_if_external_change_unlocked()
            profiles = self._db.get("profiles")
            if not isinstance(profiles, dict):
                return ""
            profile = profiles.get(user_id)
            if not isinstance(profile, dict):
                return ""
            self._sync_legacy_profile_fields(profile)
            facts: List[str] = []
            name = self._latest_fact_value(profile, "name")
            if name:
                facts.append(f"your name is {name}")
            favorite_game = self._latest_fact_value(profile, "favorite_game")
            if favorite_game:
                facts.append(f"your favorite game is {favorite_game}")
            likes_clean = self._active_fact_values(profile, "like", limit=3)
            if likes_clean:
                facts.append("you like " + ", ".join(likes_clean))
            dislikes_clean = self._active_fact_values(profile, "dislike", limit=3)
            if dislikes_clean:
                facts.append("you dislike " + ", ".join(dislikes_clean))
            goals_clean = self._active_fact_values(profile, "goal", limit=2)
            if goals_clean:
                facts.append("your goals include " + ", ".join(goals_clean))
            preferred_day = self._latest_fact_value(profile, "preferred_training_day")
            if preferred_day:
                facts.append(f"you prefer training on {preferred_day}")
            preferred_time = self._latest_fact_value(profile, "preferred_training_time")
            if preferred_time:
                facts.append(f"you prefer training in the {preferred_time}")
            origin = self._latest_fact_value(profile, "origin")
            if origin:
                facts.append(f"you are from {origin}")
            return "; ".join(facts)

    def profile_snapshot(self, user_id: str) -> Dict[str, Any]:
        with self._lock:
            self._reload_if_external_change_unlocked()
            profiles = self._db.get("profiles")
            if not isinstance(profiles, dict):
                return {}
            profile = profiles.get(user_id)
            if not isinstance(profile, dict):
                return {}

            self._sync_legacy_profile_fields(profile)
            facts_out: List[Dict[str, Any]] = []
            for item in self._active_fact_records(profile):
                field = str(item.get("field") or "").strip()
                value = _normalize_fact_value(field, str(item.get("value") or ""))
                if not field or not value:
                    continue
                facts_out.append(
                    {
                        "id": str(item.get("id") or ""),
                        "field": field,
                        "value": value,
                        "confidence": float(item.get("confidence") or 0.0),
                        "source_kind": str(item.get("source_kind") or ""),
                        "source_text": str(item.get("source_text") or ""),
                        "updated_ts": float(item.get("updated_ts") or item.get("created_ts") or 0.0),
                    }
                )
            episodes = profile.get("episodes")
            episode_out: List[Dict[str, Any]] = []
            if isinstance(episodes, list):
                for item in episodes[-12:]:
                    if not isinstance(item, dict):
                        continue
                    text = normalize_dialog_text(str(item.get("text") or ""), max_len=180)
                    if not text:
                        continue
                    episode_out.append(
                        {
                            "role": _normalize_dialog_role(str(item.get("role") or "user")),
                            "text": text,
                            "ts": float(item.get("ts") or 0.0),
                        }
                    )
            game_history = profile.get("game_history")
            recent_games: List[Dict[str, Any]] = []
            if isinstance(game_history, list):
                for item in game_history[-8:]:
                    if not isinstance(item, dict):
                        continue
                    game_name = normalize_memory_value(str(item.get("game_name") or ""), max_len=64)
                    if not game_name:
                        continue
                    recent_games.append(
                        {
                            "game_name": game_name,
                            "action": normalize_memory_value(str(item.get("action") or "launch"), max_len=24) or "launch",
                            "source": normalize_memory_value(str(item.get("source") or ""), max_len=32),
                            "ts": float(item.get("ts") or 0.0),
                        }
                    )
            recent_candidates = self._recent_game_candidates_items(profile)
            return {
                "display_name": str(profile.get("display_name") or user_id).strip() or user_id,
                "name": str(profile.get("name") or "").strip(),
                "origin": str(profile.get("origin") or "").strip(),
                "favorite_game": str(profile.get("favorite_game") or "").strip(),
                "likes": list(profile.get("likes") or []),
                "dislikes": list(profile.get("dislikes") or []),
                "goals": list(profile.get("goals") or []),
                "recent_notes": list(profile.get("recent_notes") or []),
                "preferred_training_day": str(profile.get("preferred_training_day") or "").strip(),
                "preferred_training_time": str(profile.get("preferred_training_time") or "").strip(),
                "last_game_reference": str(profile.get("last_game_reference") or "").strip(),
                "last_game_reference_kind": str(profile.get("last_game_reference_kind") or "").strip(),
                "last_game_reference_source": str(profile.get("last_game_reference_source") or "").strip(),
                "last_game_reference_ts": float(profile.get("last_game_reference_ts") or 0.0),
                "last_game_mentioned": str(profile.get("last_game_mentioned") or "").strip(),
                "last_game_mentioned_ts": float(profile.get("last_game_mentioned_ts") or 0.0),
                "last_game_recommended": str(profile.get("last_game_recommended") or "").strip(),
                "last_game_recommended_ts": float(profile.get("last_game_recommended_ts") or 0.0),
                "last_game_launched": str(profile.get("last_game_launched") or "").strip(),
                "last_game_launched_ts": float(profile.get("last_game_launched_ts") or 0.0),
                "recent_game_candidates": recent_candidates,
                "facts": facts_out,
                "episodes": episode_out,
                "recent_games": recent_games,
            }

    def _answer_values(
        self,
        *,
        label: str,
        values: List[str],
        empty_text: str,
        prefix: str = "From what I have saved: ",
    ) -> str:
        clean = [value for value in values if value]
        if not clean:
            return empty_text
        if len(clean) == 1:
            return prefix + f"{label} {clean[0]}."
        return prefix + f"{label} " + ", ".join(clean) + "."

    def _episodic_memory_matches(self, profile: Dict[str, Any], query_text: str) -> List[str]:
        normalized = " ".join((query_text or "").strip().lower().split())
        preferred_fields: List[str] = []
        if "name" in normalized:
            preferred_fields.append("name")
        if "goal" in normalized or "working on" in normalized:
            preferred_fields.append("goal")
        if "like" in normalized or "favorite game" in normalized:
            preferred_fields.extend(["favorite_game", "like"])
        if "dislike" in normalized:
            preferred_fields.append("dislike")
        if "from" in normalized:
            preferred_fields.append("origin")

        fact_source_hits: List[str] = []
        for field in preferred_fields:
            for item in reversed(self._active_fact_records(profile, field)):
                source_text = normalize_dialog_text(str(item.get("source_text") or ""), max_len=180)
                if source_text and source_text.casefold() != normalized.casefold():
                    fact_source_hits.append(source_text)
                    if len(fact_source_hits) >= 2:
                        break
            if fact_source_hits:
                break
        if fact_source_hits:
            return fact_source_hits[:2]

        relevant = self._retrieve_relevant_notes(profile, query_text)
        filtered_relevant = []
        for item in relevant:
            clean = normalize_dialog_text(item, max_len=120)
            if not clean:
                continue
            if clean.casefold() == normalized.casefold():
                continue
            if "?" in clean:
                continue
            filtered_relevant.append(clean)
        relevant = filtered_relevant
        if not relevant:
            episodes = profile.get("episodes")
            if isinstance(episodes, list):
                for item in reversed(episodes[-4:]):
                    if not isinstance(item, dict):
                        continue
                    text = normalize_dialog_text(str(item.get("text") or ""), max_len=120)
                    role = _normalize_dialog_role(str(item.get("role") or "user"))
                    if text and role == "user" and "?" not in text and text.casefold() != normalized.casefold():
                        relevant.append(text)
                        if len(relevant) >= 2:
                            break
        return [item for item in relevant if item][:2]

    def _episodic_memory_reply(self, profile: Dict[str, Any], query_text: str) -> str:
        relevant = self._episodic_memory_matches(profile, query_text)
        if not relevant:
            return "I do not have a matching memory for that yet."
        if len(relevant) == 1:
            return f"You said: {relevant[0]}."
        return "Here is what you said: " + " | ".join(relevant[:2]) + "."

    def _memory_query_payload_to_text(self, payload: Dict[str, Any]) -> str:
        result_kind = str(payload.get("result_kind") or "").strip().lower()
        facts = [item for item in payload.get("facts", []) or [] if isinstance(item, dict)]
        notes = [normalize_dialog_text(str(item), max_len=120) for item in payload.get("notes", []) or []]
        notes = [item for item in notes if item]
        missing_field = str(payload.get("missing_field") or "").strip()
        requested_label = str(payload.get("requested_label") or "").strip()

        if result_kind == "known_specific_fact":
            if facts:
                spoken = str(facts[0].get("spoken_text") or "").strip()
                if spoken:
                    return f"I remember that {spoken}."
            if notes:
                if len(notes) == 1:
                    return f"I remember you said {notes[0]}."
                return "I remember you mentioned " + " and ".join(notes[:2]) + "."

        if result_kind == "known_recent_notes":
            if notes:
                if len(notes) == 1:
                    return f"I remember you said {notes[0]}."
                return "I remember you mentioned " + " and ".join(notes[:2]) + "."

        if result_kind == "known_profile_summary":
            clauses = [str(item.get("spoken_text") or "").strip() for item in facts if str(item.get("spoken_text") or "").strip()]
            if clauses:
                if len(clauses) == 1:
                    return f"So far I remember that {clauses[0]}."
                if len(clauses) == 2:
                    return f"So far I remember that {clauses[0]}, and {clauses[1]}."
                return f"So far I remember that {clauses[0]}, {clauses[1]}, and {clauses[2]}."
            if notes:
                if len(notes) == 1:
                    return f"So far I remember you mentioned {notes[0]}."
                return "So far I remember you mentioned " + " and ".join(notes[:2]) + "."

        if result_kind == "no_saved_profile":
            if missing_field == "name":
                return "I do not have your name saved yet."
            if missing_field == "favorite_game":
                return "I do not have your favorite game saved yet."
            if missing_field == "origin":
                return "I do not have where you are from saved yet."
            if missing_field == "preferred_training_day":
                return "I do not have your training day preference saved yet."
            if missing_field == "preferred_training_time":
                return "I do not have your preferred training time saved yet."
            if requested_label:
                return f"I do not have any saved {requested_label} yet."
            return "I do not have much saved for you yet. Tell me a preference or goal and I will keep it in mind."

        return normalize_dialog_text(str(payload.get("text") or ""), max_len=240)

    def answer_memory_query_payload(self, user_id: str, query_text: str) -> Dict[str, Any]:
        with self._lock:
            self._reload_if_external_change_unlocked()
            profiles = self._db.get("profiles")
            if not isinstance(profiles, dict):
                return {
                    "type": "memory_query",
                    "query_kind": "summary",
                    "result_kind": "no_saved_profile",
                    "binding_state": "bound_profile",
                    "facts": [],
                    "notes": [],
                    "required_terms": [],
                    "text": "I do not have much saved for you yet. Tell me a preference or goal and I will keep it in mind.",
                }
            profile = profiles.get(user_id)
            if not isinstance(profile, dict):
                return {
                    "type": "memory_query",
                    "query_kind": "summary",
                    "result_kind": "no_saved_profile",
                    "binding_state": "bound_profile",
                    "facts": [],
                    "notes": [],
                    "required_terms": [],
                    "text": "I do not have much saved for you yet. Tell me a preference or goal and I will keep it in mind.",
                }

            self._sync_legacy_profile_fields(profile)
            normalized = " ".join((query_text or "").strip().lower().split())
            query_kind = ""
            for kind, patterns in _MEMORY_QUERY_PATTERN_GROUPS.items():
                if any(pattern.search(normalized) for pattern in patterns):
                    query_kind = kind
                    break

            def fact_entry(field: str, value: str) -> Dict[str, str]:
                clean = _normalize_fact_value(field, value)
                return {
                    "field": field,
                    "value": clean,
                    "spoken_text": _memory_fact_clause(field, clean),
                }

            payload: Dict[str, Any] = {
                "type": "memory_query",
                "query_kind": query_kind or "summary",
                "result_kind": "no_saved_profile",
                "binding_state": "bound_profile",
                "facts": [],
                "notes": [],
                "required_terms": [],
                "missing_field": "",
                "requested_label": "",
                "text": "",
            }

            if query_kind == "name":
                name = self._latest_fact_value(profile, "name")
                if name:
                    payload["result_kind"] = "known_specific_fact"
                    payload["facts"] = [fact_entry("name", name)]
                    payload["required_terms"] = [name]
                else:
                    payload["missing_field"] = "name"

            elif query_kind == "likes":
                favorite_game = self._latest_fact_value(profile, "favorite_game")
                if "favorite game" in normalized or "favourite game" in normalized:
                    if favorite_game:
                        payload["result_kind"] = "known_specific_fact"
                        payload["facts"] = [fact_entry("favorite_game", favorite_game)]
                        payload["required_terms"] = [favorite_game]
                    else:
                        payload["missing_field"] = "favorite_game"
                else:
                    likes = self._active_fact_values(profile, "like", limit=3)
                    if favorite_game and favorite_game.casefold() not in {value.casefold() for value in likes}:
                        likes.append(favorite_game)
                    if likes:
                        payload["result_kind"] = "known_profile_summary"
                        payload["facts"] = [fact_entry("like", value) for value in likes[:3]]
                        payload["required_terms"] = [str(item.get("value") or "") for item in payload["facts"][:2]]
                    else:
                        payload["missing_field"] = "like"
                        payload["requested_label"] = "likes"

            elif query_kind == "dislikes":
                dislikes = self._active_fact_values(profile, "dislike", limit=3)
                if dislikes:
                    payload["result_kind"] = "known_profile_summary"
                    payload["facts"] = [fact_entry("dislike", value) for value in dislikes[:3]]
                    payload["required_terms"] = [str(item.get("value") or "") for item in payload["facts"][:2]]
                else:
                    payload["missing_field"] = "dislike"
                    payload["requested_label"] = "dislikes"

            elif query_kind == "goals":
                values = self._active_fact_values(profile, "goal", limit=3)
                if values:
                    payload["result_kind"] = "known_profile_summary"
                    payload["facts"] = [fact_entry("goal", value) for value in values[:3]]
                    payload["required_terms"] = [str(item.get("value") or "") for item in payload["facts"][:2]]
                else:
                    payload["missing_field"] = "goal"
                    payload["requested_label"] = "goals"

            elif query_kind == "origin":
                origin = self._latest_fact_value(profile, "origin")
                if origin:
                    payload["result_kind"] = "known_specific_fact"
                    payload["facts"] = [fact_entry("origin", origin)]
                    payload["required_terms"] = [origin]
                else:
                    payload["missing_field"] = "origin"

            elif query_kind == "schedule":
                preferred_day = self._latest_fact_value(profile, "preferred_training_day")
                preferred_time = self._latest_fact_value(profile, "preferred_training_time")
                facts: List[Dict[str, str]] = []
                if preferred_day:
                    facts.append(fact_entry("preferred_training_day", preferred_day))
                if preferred_time:
                    facts.append(fact_entry("preferred_training_time", preferred_time))
                if facts:
                    payload["result_kind"] = "known_profile_summary"
                    payload["facts"] = facts
                    payload["required_terms"] = [str(item.get("value") or "") for item in facts]
                else:
                    payload["missing_field"] = "preferred_training_day"
                    payload["requested_label"] = "training schedule preferences"

            elif query_kind == "episodic":
                notes = self._episodic_memory_matches(profile, query_text)
                if notes:
                    payload["result_kind"] = "known_recent_notes"
                    payload["notes"] = notes[:2]
                else:
                    payload["requested_label"] = "matching memories"

            else:
                facts: List[Dict[str, str]] = []
                name = self._latest_fact_value(profile, "name")
                if name:
                    facts.append(fact_entry("name", name))
                favorite_game = self._latest_fact_value(profile, "favorite_game")
                if favorite_game:
                    facts.append(fact_entry("favorite_game", favorite_game))
                likes_clean = self._active_fact_values(profile, "like", limit=2)
                for value in likes_clean[:1]:
                    facts.append(fact_entry("like", value))
                goals_clean = self._active_fact_values(profile, "goal", limit=2)
                for value in goals_clean[:1]:
                    facts.append(fact_entry("goal", value))
                preferred_day = self._latest_fact_value(profile, "preferred_training_day")
                if preferred_day and len(facts) < 3:
                    facts.append(fact_entry("preferred_training_day", preferred_day))
                notes = profile.get("recent_notes")
                notes_clean: List[str] = []
                if isinstance(notes, list):
                    notes_clean = [normalize_memory_value(str(item), max_len=90) for item in notes[-3:]]
                    notes_clean = [item for item in notes_clean if item]
                if facts:
                    payload["result_kind"] = "known_profile_summary"
                    payload["facts"] = facts[:3]
                    payload["required_terms"] = [str(item.get("value") or "") for item in facts[:2]]
                elif notes_clean:
                    payload["result_kind"] = "known_recent_notes"
                    payload["notes"] = notes_clean[:2]

            payload["text"] = self._memory_query_payload_to_text(payload)
            return payload

    def answer_memory_query(self, user_id: str, query_text: str) -> str:
        payload = self.answer_memory_query_payload(user_id, query_text)
        return normalize_dialog_text(str(payload.get("text") or ""), max_len=240)
