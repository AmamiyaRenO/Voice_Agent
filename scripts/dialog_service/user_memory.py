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
    re.compile(r"\bi am\s+([a-z][a-z '\-]{1,31})\b", re.IGNORECASE),
    re.compile(r"\bi'm\s+([a-z][a-z '\-]{1,31})\b", re.IGNORECASE),
    re.compile(r"\bcall me\s+([a-z][a-z '\-]{1,31})\b", re.IGNORECASE),
]
_LIKE_PATTERNS = [
    re.compile(r"\bi like\s+([^.!?]{2,80})", re.IGNORECASE),
    re.compile(r"\bi love\s+([^.!?]{2,80})", re.IGNORECASE),
    re.compile(r"\bmy favorite game is\s+([^.!?]{2,80})", re.IGNORECASE),
]
_DISLIKE_PATTERNS = [
    re.compile(r"\bi don't like\s+([^.!?]{2,80})", re.IGNORECASE),
    re.compile(r"\bi dislike\s+([^.!?]{2,80})", re.IGNORECASE),
    re.compile(r"\bi hate\s+([^.!?]{2,80})", re.IGNORECASE),
]
_GOAL_PATTERNS = [
    re.compile(r"\bmy goal is\s+([^.!?]{2,100})", re.IGNORECASE),
    re.compile(r"\bi am working on\s+([^.!?]{2,100})", re.IGNORECASE),
    re.compile(r"\bi'm working on\s+([^.!?]{2,100})", re.IGNORECASE),
    re.compile(r"\bi want to\s+([^.!?]{2,100})", re.IGNORECASE),
]
_ORIGIN_PATTERNS = [
    re.compile(
        r"\b(?:i am from|i'm from|i come from|i came from|you are from|you come from)\s+([^.!?]{2,60})",
        re.IGNORECASE,
    ),
    re.compile(r"\bfrom\s+([^.!?]{2,40})", re.IGNORECASE),
]


def _extract_origin_hint(texts: List[str]) -> str:
    if not texts:
        return ""
    for raw in reversed(texts):
        value = normalize_memory_value(str(raw), max_len=120)
        if not value:
            continue
        lowered = value.casefold()
        for pattern in _ORIGIN_PATTERNS:
            match = pattern.search(lowered)
            if not match:
                continue
            candidate = normalize_memory_value(match.group(1), max_len=48)
            candidate = candidate.strip(" .,!?:;'\"")
            if not candidate:
                continue
            if len(candidate.split(" ")) > 6:
                candidate = " ".join(candidate.split(" ")[:6]).strip()
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
                "recent_notes": [],
                "memory_items": [],
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
        profile.setdefault("recent_notes", [])
        profile.setdefault("memory_items", [])
        profile.setdefault("first_seen_ts", now_ts)
        profile.setdefault("last_seen_ts", now_ts)
        profile.setdefault("utterance_count", 0)
        return profile

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

    def remember_utterance(self, user_id: str, text: str) -> None:
        utterance = normalize_memory_value(text, max_len=180)
        if not utterance:
            return

        with self._lock:
            reload_ok = self._reload_if_external_change_unlocked()
            if not reload_ok:
                # Avoid writing stale in-memory data back to disk when external file is transiently unreadable.
                return
            profile = self._ensure_profile(user_id, time.time())
            profile["last_seen_ts"] = time.time()
            profile["utterance_count"] = int(profile.get("utterance_count", 0) or 0) + 1

            name = str(profile.get("name") or "").strip()
            lowered = utterance.casefold()

            for pattern in _NAME_PATTERNS:
                match = pattern.search(lowered)
                if not match:
                    continue
                candidate = _normalize_name_value(match.group(1))
                if candidate:
                    profile["name"] = candidate
                break

            likes = profile.get("likes")
            if not isinstance(likes, list):
                likes = []
                profile["likes"] = likes
            for pattern in _LIKE_PATTERNS:
                match = pattern.search(lowered)
                if match:
                    _append_unique_casefold(likes, match.group(1), max_items=8)
                    break

            dislikes = profile.get("dislikes")
            if not isinstance(dislikes, list):
                dislikes = []
                profile["dislikes"] = dislikes
            for pattern in _DISLIKE_PATTERNS:
                match = pattern.search(lowered)
                if match:
                    _append_unique_casefold(dislikes, match.group(1), max_items=8)
                    break

            goals = profile.get("goals")
            if not isinstance(goals, list):
                goals = []
                profile["goals"] = goals
            for pattern in _GOAL_PATTERNS:
                match = pattern.search(lowered)
                if match:
                    _append_unique_casefold(goals, match.group(1), max_items=8)
                    break

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

            # Keep shape clean and predictable.
            profile["name"] = str(profile.get("name") or name).strip()
            self._save()

    def _retrieve_relevant_notes(self, profile: Dict[str, Any], query_text: str) -> List[str]:
        if self.embedder is None or not self.embedder.ready or onnx_np is None:
            return []
        query = normalize_memory_value(query_text, max_len=220)
        if not query:
            return []
        qvec = self.embedder.query_embedding(query)
        if qvec is None:
            return []

        items = profile.get("memory_items")
        if not isinstance(items, list) or not items:
            return []

        scored: List[tuple[float, str]] = []
        for item in items:
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
            name = normalize_memory_value(str(profile.get("name") or ""), max_len=32)
            if name:
                lines.append(f"Preferred name: {name}.")

            likes = profile.get("likes")
            if isinstance(likes, list):
                likes_clean = [normalize_memory_value(str(item), max_len=40) for item in likes]
                likes_clean = [item for item in likes_clean if item]
                if likes_clean:
                    lines.append("Likes: " + ", ".join(likes_clean[:5]) + ".")

            dislikes = profile.get("dislikes")
            if isinstance(dislikes, list):
                dislikes_clean = [normalize_memory_value(str(item), max_len=40) for item in dislikes]
                dislikes_clean = [item for item in dislikes_clean if item]
                if dislikes_clean:
                    lines.append("Dislikes: " + ", ".join(dislikes_clean[:5]) + ".")

            goals = profile.get("goals")
            if isinstance(goals, list):
                goals_clean = [normalize_memory_value(str(item), max_len=60) for item in goals]
                goals_clean = [item for item in goals_clean if item]
                if goals_clean:
                    lines.append("Goals: " + ", ".join(goals_clean[:4]) + ".")

            notes = profile.get("recent_notes")
            notes_clean: List[str] = []
            if isinstance(notes, list) and notes:
                tail = notes[-3:]
                notes_clean = [normalize_memory_value(str(item), max_len=80) for item in tail]
                notes_clean = [item for item in notes_clean if item]
                if notes_clean:
                    lines.append("Recent notes: " + " | ".join(notes_clean) + ".")

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
                return "I do not have any saved details yet."
            profile = profiles.get(user_id)
            if not isinstance(profile, dict):
                return "I do not have any saved details yet."

            facts: List[str] = []
            name = normalize_memory_value(str(profile.get("name") or ""), max_len=32)
            if name:
                facts.append(f"your name is {name}")

            likes = profile.get("likes")
            if isinstance(likes, list):
                likes_clean = [normalize_memory_value(str(item), max_len=40) for item in likes]
                likes_clean = [item for item in likes_clean if item]
                if likes_clean:
                    facts.append("you like " + ", ".join(likes_clean[:3]))

            dislikes = profile.get("dislikes")
            if isinstance(dislikes, list):
                dislikes_clean = [normalize_memory_value(str(item), max_len=40) for item in dislikes]
                dislikes_clean = [item for item in dislikes_clean if item]
                if dislikes_clean:
                    facts.append("you dislike " + ", ".join(dislikes_clean[:3]))

            goals = profile.get("goals")
            if isinstance(goals, list):
                goals_clean = [normalize_memory_value(str(item), max_len=60) for item in goals]
                goals_clean = [item for item in goals_clean if item]
                if goals_clean:
                    facts.append("your goals include " + ", ".join(goals_clean[:2]))

            notes = profile.get("recent_notes")
            notes_clean: List[str] = []
            if isinstance(notes, list):
                notes_clean = [normalize_memory_value(str(item), max_len=90) for item in notes[-3:]]
                notes_clean = [item for item in notes_clean if item]

            origin_hint = _extract_origin_hint(notes_clean)
            if origin_hint:
                facts.append(f"you said you are from {origin_hint}")
            elif notes_clean:
                facts.append("recently you said " + " | ".join(notes_clean[:2]))

            if not facts:
                return "I only have a small amount of saved info so far. You can tell me your preferences and goals and I will remember them."

            return "From what I have saved: " + "; ".join(facts) + "."
