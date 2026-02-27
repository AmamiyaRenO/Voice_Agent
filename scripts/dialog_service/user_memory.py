#!/usr/bin/env python3
from __future__ import annotations

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
                return
            try:
                raw = self.path.read_text(encoding="utf-8")
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
            except Exception as exc:
                print(f"[dialog] user memory load failed: {exc}")

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
            temp_path.write_text(
                json.dumps(self._db, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp_path.replace(self.path)
        except Exception as exc:
            print(f"[dialog] user memory save failed: {exc}")

    def _ensure_profile(self, user_id: str, now_ts: float) -> Dict[str, Any]:
        profiles = self._db.setdefault("profiles", {})
        profile = profiles.get(user_id)
        if not isinstance(profile, dict):
            profile = {
                "display_name": user_id,
                "name": "",
                "likes": [],
                "dislikes": [],
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
        profile.setdefault("recent_notes", [])
        profile.setdefault("memory_items", [])
        profile.setdefault("first_seen_ts", now_ts)
        profile.setdefault("last_seen_ts", now_ts)
        profile.setdefault("utterance_count", 0)
        return profile

    def resolve_user(self, identity_key: str) -> str:
        now_ts = time.time()
        with self._lock:
            identity_map = self._db.setdefault("identity_map", {})
            record = identity_map.get(identity_key)
            if isinstance(record, dict):
                user_id = str(record.get("user_id") or "").strip()
                if user_id:
                    record["last_seen_ts"] = now_ts
                    record["sample_count"] = int(record.get("sample_count", 0) or 0) + 1
                    profile = self._ensure_profile(user_id, now_ts)
                    profile["last_seen_ts"] = now_ts
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
            self._save()
            return user_id

    def remember_utterance(self, user_id: str, text: str) -> None:
        utterance = normalize_memory_value(text, max_len=180)
        if not utterance:
            return

        with self._lock:
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

            relevant = self._retrieve_relevant_notes(profile, query_text)
            if relevant:
                lines.append("Relevant memory: " + " | ".join(relevant) + ".")
            else:
                notes = profile.get("recent_notes")
                if isinstance(notes, list) and notes:
                    tail = notes[-3:]
                    notes_clean = [normalize_memory_value(str(item), max_len=80) for item in tail]
                    notes_clean = [item for item in notes_clean if item]
                    if notes_clean:
                        lines.append("Recent notes: " + " | ".join(notes_clean) + ".")

            context = " ".join(lines).strip()
            if len(context) > self.prompt_max_chars:
                context = context[: self.prompt_max_chars].rstrip()
            return context
