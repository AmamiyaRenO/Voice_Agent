#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import httpx
import paho.mqtt.client as mqtt

try:
    from .dialog_config import Config, _default_embedder_repo, _env_bool, _env_int
    from .onnx_embedder import OnnxTextEmbedder, cosine_similarity
    from .text_utils import (
        compress_reply_by_words,
        compress_reply_for_latency,
        sanitize_tts_text,
        trim_trailing_connectors,
    )
    from .user_memory import UserMemoryStore, speaker_identity_key
except Exception:
    from dialog_config import Config, _default_embedder_repo, _env_bool, _env_int
    from onnx_embedder import OnnxTextEmbedder, cosine_similarity
    from text_utils import (
        compress_reply_by_words,
        compress_reply_for_latency,
        sanitize_tts_text,
        trim_trailing_connectors,
    )
    from user_memory import UserMemoryStore, speaker_identity_key

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON_VOICE_DIR = REPO_ROOT / "python_voice_service"
_python_voice_dir_str = str(PYTHON_VOICE_DIR)
if _python_voice_dir_str not in sys.path:
    sys.path.insert(0, _python_voice_dir_str)
try:
    from game_grounding import GameCatalog
except Exception:
    GameCatalog = None

DEFAULT_GAME_MANIFEST = REPO_ROOT / "scripts" / "intent_service" / "manifest.json"


_POLICY_WHITESPACE = re.compile(r"\s+")
_POLICY_TOKEN_RE = re.compile(r"[a-z0-9]+")
_OPEN_QUESTION_RE = re.compile(r"([^?？.!。]{3,200}[?？])")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

_SWITCH_TOPIC_CUES = (
    "new topic",
    "switch topic",
    "change topic",
    "another question",
    "something else",
    "different question",
    "by the way",
    "anyway",
    "off topic",
    "go back to",
    "back to",
)

_CONTINUE_TOPIC_CUES = (
    "and",
    "also",
    "then",
    "what about",
    "how about",
    "that",
    "this",
    "it",
)

_AMBIGUOUS_SHORT_QUERIES = {
    "why",
    "how",
    "what",
    "when",
    "where",
    "which",
    "why?",
    "how?",
    "what?",
    "then?",
}

_TOPIC_STOPWORDS: Set[str] = {
    "the",
    "a",
    "an",
    "to",
    "of",
    "in",
    "on",
    "for",
    "and",
    "or",
    "is",
    "are",
    "was",
    "were",
    "be",
    "am",
    "i",
    "you",
    "he",
    "she",
    "it",
    "we",
    "they",
    "me",
    "my",
    "your",
    "our",
    "their",
    "do",
    "does",
    "did",
    "can",
    "could",
    "would",
    "should",
    "please",
    "about",
    "with",
    "at",
    "by",
    "from",
    "as",
    "if",
    "so",
    "just",
    "there",
    "here",
    "this",
    "that",
    "these",
    "those",
}

_MEMORY_QUERY_PATTERNS: tuple[str, ...] = (
    "what do you know about me",
    "what do you remember about me",
    "remind me what you remember about me",
    "can you remind me what you remember about me",
    "remind me what you know about me",
    "which day do i prefer",
    "what day do i prefer",
    "which day do i like",
    "what time do i prefer",
    "which time do i prefer",
    "do i prefer morning or evening",
    "do you remember me",
    "tell me about me",
    "who am i",
    "what do you know of me",
    "where am i from",
    "where do i come from",
    "what is my name",
    "what's my name",
    "what do i like",
    "what don't i like",
    "what do i dislike",
    "what is my favorite game",
    "what's my favorite game",
    "what is my goal",
    "what are my goals",
    "what am i working on",
    "when do i prefer to train",
    "what did i say about",
    "what did i mention about",
    "what did we talk about",
)

_MEMORY_QUERY_SEMANTIC_PROTOTYPES: tuple[str, ...] = (
    "What do you remember about me?",
    "What do you know about my preferences?",
    "Can you remind me of my goals?",
    "Which day do I prefer for training?",
    "Do I prefer morning or evening sessions?",
    "What did I tell you earlier?",
    "Summarize my saved profile.",
)

_MEMORY_QUERY_ANCHOR_TOKENS: Set[str] = {
    "remind",
    "remember",
    "memory",
    "know",
    "saved",
    "profile",
    "goal",
    "goals",
    "preference",
    "preferences",
    "prefer",
    "name",
    "from",
    "told",
    "recap",
    "summarize",
    "summary",
    "again",
    "before",
    "earlier",
}

_MEMORY_QUERY_INTENT_PHRASES: tuple[str, ...] = (
    "remember about me",
    "know about me",
    "remind me",
    "saved profile",
    "my profile",
    "my name",
    "where am i from",
    "where do i come from",
    "what did i tell",
    "what did i say about",
    "what did i mention about",
    "what did we talk about",
    "told you earlier",
    "my goals",
    "my goal",
    "what don't i like",
    "what do i dislike",
    "what is my favorite game",
    "when do i prefer to train",
    "do i prefer",
    "which day do i prefer",
    "what day do i prefer",
    "which time do i prefer",
    "what time do i prefer",
    "goals",
)

_MEMORY_QUERY_REQUEST_PREFIXES: tuple[str, ...] = (
    "what ",
    "who ",
    "where ",
    "which ",
    "can you ",
    "do you ",
    "tell me ",
    "remind me ",
    "summarize ",
)

_VISION_QUERY_PATTERNS: tuple[str, ...] = (
    "what can you see",
    "what do you see",
    "what are you seeing",
    "can you see me",
    "can you see anything",
    "tell me what you see",
    "describe what you see",
    "describe what you can see",
    "what does the camera see",
    "what can the camera see",
    "what can you see right now",
    "what do you see right now",
    "describe the camera view",
    "describe this camera frame",
    "describe the scene in front of you",
    "look at this and describe it",
    "你能看到什么",
    "你看到了什么",
    "你现在看到什么",
    "描述一下你看到的",
)

_VISION_QUERY_REQUEST_PREFIXES: tuple[str, ...] = (
    "what ",
    "can you ",
    "could you ",
    "do you ",
    "are you ",
    "tell me ",
    "describe ",
    "show me ",
)

_VISION_SEE_REGEX = re.compile(r"\bsee(?:ing)?\b")
_VISION_TARGET_REGEX = re.compile(r"\b(?:you|camera|frame|image|scene|view)\b")
_VISION_DESCRIBE_REGEX = re.compile(r"\b(?:describe|show)\b")


def _normalize_policy_text(text: str) -> str:
    return _POLICY_WHITESPACE.sub(" ", (text or "").strip().lower())


def _normalize_identity_resolution(value: Optional[str]) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "none":
        return "none"
    return "auto"


def _tokenize_policy_text(text: str) -> Set[str]:
    normalized = _normalize_policy_text(text)
    if not normalized:
        return set()
    return {token for token in _POLICY_TOKEN_RE.findall(normalized) if token and token not in _TOPIC_STOPWORDS}


def _extract_topic_hint(text: str) -> str:
    normalized = _normalize_policy_text(text)
    if not normalized:
        return ""

    if _CJK_RE.search(normalized):
        compact = normalized.strip("，。！？!?;:,. ")
        if len(compact) <= 48:
            return compact
        return compact[:48].rstrip()

    tokens = [token for token in _POLICY_TOKEN_RE.findall(normalized) if token not in _TOPIC_STOPWORDS]
    if not tokens:
        return ""
    return " ".join(tokens[:6]).strip()


def _extract_open_question(text: str) -> str:
    raw = " ".join((text or "").strip().split())
    if not raw:
        return ""
    match = _OPEN_QUESTION_RE.search(raw)
    if not match:
        return ""
    question = match.group(1).strip()
    if len(question) > 160:
        question = question[:160].rstrip()
    return question


class DialogService:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"dialog-svc-{uuid.uuid4().hex[:8]}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.http = httpx.Client(timeout=30.0)
        self.tts_voice: Optional[str] = None
        self.tts_model: Optional[str] = None
        # Keep compression for latency, but defaults must not truncate sentence semantics.
        self.reply_compress = _env_bool("DIALOG_REPLY_COMPRESS", True)
        self.reply_max_sentences = _env_int("DIALOG_MAX_REPLY_SENTENCES", 2, floor=0)
        self.reply_max_chars = _env_int("DIALOG_MAX_REPLY_CHARS", 220, floor=0)
        self.reply_max_words = _env_int("DIALOG_MAX_REPLY_WORDS", 0, floor=0)
        self.user_memory_embedder: Optional[OnnxTextEmbedder] = None
        self._memory_query_semantic_vectors: List[Tuple[str, object]] = []
        self.game_catalog = GameCatalog(DEFAULT_GAME_MANIFEST) if GameCatalog is not None else None
        self.user_memory: Optional[UserMemoryStore] = None
        if self.cfg.enable_user_memory:
            if self.cfg.enable_user_memory_embeddings:
                repo_id = (
                    self.cfg.user_memory_embedding_repo_id
                    or _default_embedder_repo(self.cfg.user_memory_embedder)
                )
                self.user_memory_embedder = OnnxTextEmbedder(
                    embedder=self.cfg.user_memory_embedder,
                    repo_id=repo_id,
                    model_dir=self.cfg.user_memory_embedding_model_dir,
                    model_file=self.cfg.user_memory_embedding_model_file,
                    tokenizer_file=self.cfg.user_memory_embedding_tokenizer_file,
                    max_length=self.cfg.user_memory_embedding_max_length,
                    auto_download=self.cfg.user_memory_embedding_auto_download,
                    cache_dir=self.cfg.user_memory_embedding_cache_dir,
                    query_prefix=self.cfg.user_memory_query_prefix,
                    doc_prefix=self.cfg.user_memory_doc_prefix,
                )
            self.user_memory = UserMemoryStore(
                path=self.cfg.user_memory_path,
                max_notes=self.cfg.user_memory_max_notes,
                prompt_max_chars=self.cfg.user_memory_prompt_max_chars,
                embedder=self.user_memory_embedder,
                retrieve_top_k=self.cfg.user_memory_retrieve_top_k,
            )
        self._prepare_memory_query_semantic_vectors()
        if os.environ.get("DIALOG_SPEAK_AUDIO"):
            print("[dialog] NOTE: DIALOG_SPEAK_AUDIO is set but will be ignored (forced off for AEC).")

    def _remember_game_context(
        self,
        *,
        user_id: Optional[str],
        text: str,
        primary_game_name: str = "",
        reference_kind: str = "mentioned",
        source: str = "assistant",
    ) -> None:
        if not user_id or self.user_memory is None or self.game_catalog is None:
            return
        mention_names = self.game_catalog.extract_game_mentions(text, limit=3)
        primary_name = str(primary_game_name or "").strip()
        mention_names = [
            name
            for name in mention_names
            if name and name.casefold() != primary_name.casefold()
        ]
        if mention_names:
            self.user_memory.remember_game_mentions(
                user_id,
                mention_names,
                reference_kind="mentioned",
                source=source,
            )
        target_name = primary_name or (mention_names[-1] if mention_names else "")
        if target_name:
            self.user_memory.set_game_reference(
                user_id,
                game_name=target_name,
                reference_kind=reference_kind if primary_name else "mentioned",
                source=source,
            )

    def start(self) -> None:
        print(f"[dialog] connecting to mqtt {self.cfg.host}:{self.cfg.port}")
        if self.user_memory is not None:
            print(f"[dialog] user memory enabled path={self.cfg.user_memory_path}")
            if self.cfg.enable_dialog_context:
                print(
                    "[dialog] dialog context enabled "
                    f"turns={self.cfg.dialog_history_turns} "
                    f"summary_chars={self.cfg.dialog_summary_max_chars} "
                    f"context_chars={self.cfg.dialog_context_max_chars}"
                )
            if self.user_memory_embedder is not None:
                if self.user_memory_embedder.ready:
                    print(
                        "[dialog] user memory embedder ready "
                        f"embedder={self.user_memory_embedder.embedder} "
                        f"repo={self.user_memory_embedder.repo_id} "
                        f"dim={self.user_memory_embedder.dimension}"
                    )
                else:
                    print(
                        "[dialog] user memory embedder unavailable: "
                        f"{self.user_memory_embedder.error or 'init failed'}"
                    )
            else:
                print("[dialog] user memory embedder disabled")
            print(
                "[dialog] memory query routing "
                f"rule={'on' if self.cfg.memory_query_rule else 'off'} "
                f"semantic={'on' if self.cfg.memory_query_semantic else 'off'} "
                f"threshold={self.cfg.memory_query_threshold:.2f} "
                f"proto_vectors={len(self._memory_query_semantic_vectors)}"
            )
        else:
            print("[dialog] user memory disabled")
        if self.cfg.enable_vision_query:
            print(
                "[dialog] vision query routing on "
                f"url={self.cfg.vision_describe_url} "
                f"timeout={self.cfg.vision_timeout_seconds:.1f}s"
            )
        else:
            print("[dialog] vision query routing off")
        self.client.connect(self.cfg.host, self.cfg.port, keepalive=20)
        self.client.loop_start()

    def stop(self) -> None:
        try:
            self.client.loop_stop()
        finally:
            self.client.disconnect()
            self.http.close()

    def _prepare_memory_query_semantic_vectors(self) -> None:
        self._memory_query_semantic_vectors = []
        if not self.cfg.memory_query_semantic:
            return
        if self.user_memory_embedder is None or not self.user_memory_embedder.ready:
            return

        for prototype in _MEMORY_QUERY_SEMANTIC_PROTOTYPES:
            try:
                vec = self.user_memory_embedder.doc_embedding(prototype)
            except Exception:
                vec = None
            if vec is not None:
                self._memory_query_semantic_vectors.append((prototype, vec))

    @staticmethod
    def _has_memory_query_anchor(text: str) -> bool:
        tokens = _tokenize_policy_text(text)
        if not tokens:
            return False
        return bool(tokens.intersection(_MEMORY_QUERY_ANCHOR_TOKENS))

    @staticmethod
    def _has_explicit_memory_query_intent(normalized_text: str) -> bool:
        if not normalized_text:
            return False
        if not any(normalized_text.startswith(prefix) for prefix in _MEMORY_QUERY_REQUEST_PREFIXES):
            return False

        has_self_ref = re.search(r"\b(i|me|my|mine)\b", normalized_text) is not None
        if not has_self_ref:
            return False

        return any(phrase in normalized_text for phrase in _MEMORY_QUERY_INTENT_PHRASES)

    def _memory_query_rule_match(self, normalized_text: str) -> bool:
        if not self.cfg.memory_query_rule:
            return False
        return any(pattern in normalized_text for pattern in _MEMORY_QUERY_PATTERNS)

    def _memory_query_semantic_score(self, normalized_text: str) -> float:
        if not self.cfg.memory_query_semantic:
            return 0.0
        if not normalized_text:
            return 0.0
        if self.user_memory_embedder is None or not self.user_memory_embedder.ready:
            return 0.0
        if not self._memory_query_semantic_vectors:
            return 0.0
        if not self._has_memory_query_anchor(normalized_text):
            return 0.0
        if not self._has_explicit_memory_query_intent(normalized_text):
            return 0.0

        try:
            query_vec = self.user_memory_embedder.query_embedding(normalized_text)
        except Exception:
            query_vec = None
        if query_vec is None:
            return 0.0

        best = 0.0
        for _, proto_vec in self._memory_query_semantic_vectors:
            try:
                score = float(cosine_similarity(query_vec, proto_vec))
            except Exception:
                continue
            if score > best:
                best = score
        return best

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        print(f"[dialog] connected rc={reason_code}")
        client.subscribe(self.cfg.topics.dialog_query)
        print(f"[dialog] subscribed {self.cfg.topics.dialog_query}")
        client.subscribe(self.cfg.topics.tts_options)
        print(f"[dialog] subscribed {self.cfg.topics.tts_options}")

    def _classify_dialog_policy(
        self,
        *,
        user_text: str,
        current_topic: str,
        open_question: str,
    ) -> str:
        if not self.cfg.enable_dialog_policy:
            return "switch_topic"

        normalized = _normalize_policy_text(user_text)
        if not normalized:
            return "ask_clarify"

        if any(cue in normalized for cue in _SWITCH_TOPIC_CUES):
            return "switch_topic"

        if open_question and not normalized.endswith("?") and not normalized.endswith("？"):
            # If coach asked a question in the previous turn and user responds declaratively,
            # treat this as continuing the same thread.
            return "continue_topic"

        if any(cue in normalized for cue in _CONTINUE_TOPIC_CUES):
            return "continue_topic"

        if normalized in _AMBIGUOUS_SHORT_QUERIES:
            return "ask_clarify"

        tokens = _tokenize_policy_text(normalized)
        if current_topic:
            topic_tokens = _tokenize_policy_text(current_topic)
            if tokens and topic_tokens and tokens.intersection(topic_tokens):
                return "continue_topic"

        if len(tokens) <= 1 and (normalized.endswith("?") or normalized.endswith("？")):
            return "ask_clarify"

        if len(normalized) <= 3:
            return "ask_clarify"

        return "switch_topic"

    def _update_dialog_slots_after_reply(
        self,
        *,
        user_id: str,
        user_text: str,
        answer_text: str,
        previous_topic: str,
    ) -> None:
        if self.user_memory is None or not self.cfg.enable_dialog_context:
            return

        topic_hint = _extract_topic_hint(user_text)
        next_topic = topic_hint or previous_topic
        next_open_question = _extract_open_question(answer_text)
        self.user_memory.update_dialog_slots(
            user_id,
            current_topic=next_topic,
            open_question=next_open_question,
        )

    def _build_dialog_request_context(
        self,
        *,
        user_id: Optional[str],
        user_text: str,
    ) -> Dict[str, str]:
        result: Dict[str, str] = {
            "dialog_context": "",
            "current_topic": "",
            "open_question": "",
        }
        if not user_id or self.user_memory is None:
            return result
        try:
            self._remember_game_context(
                user_id=user_id,
                text=user_text,
                reference_kind="mentioned",
                source="user",
            )
        except Exception as exc:
            print(f"[dialog] game context update failed: {exc}")
        if not self.cfg.enable_dialog_context:
            return result

        slots = self.user_memory.get_dialog_slots(user_id)
        current_topic = str(slots.get("current_topic") or "").strip()
        open_question = str(slots.get("open_question") or "").strip()

        self.user_memory.remember_dialog_turn(
            user_id,
            "user",
            user_text,
            max_turns=self.cfg.dialog_history_turns,
            summary_max_chars=self.cfg.dialog_summary_max_chars,
        )
        dialog_context = self.user_memory.build_dialog_context(
            user_id,
            max_turns=self.cfg.dialog_history_turns,
            max_chars=self.cfg.dialog_context_max_chars,
            include_summary=True,
            include_slots=False,
            include_recent_dialogue=True,
        )

        result["dialog_context"] = dialog_context
        result["current_topic"] = current_topic
        result["open_question"] = open_question
        return result

    def _publish_answer_ex(
        self,
        *,
        text: str,
        corr_id: Optional[str],
        tts_speaker: Optional[str],
        user_id: Optional[str],
    ) -> None:
        payload = {
            "type": "ANSWER",
            "text": text,
            "source": self.cfg.source_label,
            "corr_id": corr_id or uuid.uuid4().hex,
        }
        if tts_speaker:
            payload["tts_speaker"] = tts_speaker
        if user_id:
            payload["user_id"] = user_id
        self.client.publish(self.cfg.topics.dialog_answer, json.dumps(payload))

    def _publish_tts_state(self, speaking: bool, corr_id: Optional[str], text: Optional[str] = None) -> None:
        try:
            payload = {
                "speaking": speaking,
                "source": self.cfg.source_label,
                "corr_id": corr_id or uuid.uuid4().hex,
                "ts": time.time(),
            }
            if speaking and text:
                payload["text"] = text
            self.client.publish(self.cfg.topics.tts_state, json.dumps(payload))
        except Exception:
            pass

    def _tts_and_play(self, text: str, corr_id: Optional[str]) -> None:
        # Local playback disabled (Unity will play audio using robot/dialog/answer).
        return

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except json.JSONDecodeError:
            print("[dialog] invalid json, ignored")
            return

        topic = msg.topic or ""

        if topic == self.cfg.topics.tts_options:
            voice = str(payload.get("voice") or "").strip()
            model = str(payload.get("model") or "").strip()
            self.tts_voice = voice or None
            self.tts_model = model or None
            print(f"[dialog] tts options updated voice='{self.tts_voice}' model='{self.tts_model}'")
            return

        text = str(payload.get("text") or "").strip()
        if not text:
            return
        corr_id = payload.get("corr_id")
        user_id = str(payload.get("user_id") or "").strip() or None
        identity_resolution = _normalize_identity_resolution(payload.get("identity_resolution"))
        barge_in = bool(payload.get("barge_in"))
        interrupted_tts_text = str(payload.get("interrupted_tts_text") or "").strip()
        memory_context = ""
        dialog_request_ctx: Dict[str, str] = {
            "dialog_context": "",
            "current_topic": "",
            "open_question": "",
        }
        memory_update: Dict[str, object] = {
            "explicit_memory": False,
            "facts_written": [],
            "facts_removed": [],
            "ack_text": "",
            "utterance": "",
        }
        if self.user_memory is not None:
            try:
                resolved_user_id = user_id
                if (
                    not resolved_user_id
                    and identity_resolution != "none"
                    and speaker_identity_key is not None
                ):
                    identity_key = speaker_identity_key(payload)
                    resolved_user_id = self.user_memory.resolve_user(identity_key)
                user_id = resolved_user_id
                if user_id:
                    memory_update = self.user_memory.remember_utterance(user_id, text)
                    memory_context = self.user_memory.build_memory_context(user_id, query_text=text)
                    dialog_request_ctx = self._build_dialog_request_context(user_id=user_id, user_text=text)
            except Exception as exc:
                print(f"[dialog] user memory resolve failed: {exc}")
                memory_context = ""
                dialog_request_ctx = {
                    "dialog_context": "",
                    "current_topic": "",
                    "open_question": "",
                }

        if user_id and self.user_memory is not None:
            memory_write_reply = sanitize_tts_text(str(memory_update.get("ack_text") or "").strip())
            if memory_write_reply:
                self._publish_answer_ex(
                    text=memory_write_reply,
                    corr_id=corr_id,
                    tts_speaker=self.tts_voice or None,
                    user_id=user_id,
                )
                if self.cfg.enable_dialog_context:
                    try:
                        self.user_memory.remember_dialog_turn(
                            user_id,
                            "assistant",
                            memory_write_reply,
                            max_turns=self.cfg.dialog_history_turns,
                            summary_max_chars=self.cfg.dialog_summary_max_chars,
                        )
                        self._update_dialog_slots_after_reply(
                            user_id=user_id,
                            user_text=text,
                            answer_text=memory_write_reply,
                            previous_topic=dialog_request_ctx.get("current_topic", ""),
                        )
                    except Exception as exc:
                        print(f"[dialog] dialog slot update failed: {exc}")
                if self.cfg.speak_audio:
                    self._tts_and_play(memory_write_reply, corr_id)
                return

        if user_id and self.user_memory is not None and self._is_memory_query(text):
            try:
                memory_reply = (self.user_memory.answer_memory_query(user_id, text) or "").strip()
            except Exception as exc:
                print(f"[dialog] user memory reply failed: {exc}")
                memory_reply = ""
            if memory_reply:
                memory_reply = sanitize_tts_text(memory_reply)
                if memory_reply:
                    if self.reply_compress:
                        memory_reply = compress_reply_for_latency(
                            memory_reply,
                            max_sentences=self.reply_max_sentences,
                            max_chars=self.reply_max_chars,
                        )
                        memory_reply = compress_reply_by_words(memory_reply, self.reply_max_words)
                        memory_reply = trim_trailing_connectors(memory_reply)
                        if memory_reply and memory_reply[-1] not in ".!?":
                            memory_reply = f"{memory_reply}."
                    self._publish_answer_ex(
                        text=memory_reply,
                        corr_id=corr_id,
                        tts_speaker=self.tts_voice or None,
                        user_id=user_id,
                    )
                    if self.cfg.enable_dialog_context:
                        try:
                            self.user_memory.remember_dialog_turn(
                                user_id,
                                "assistant",
                                memory_reply,
                                max_turns=self.cfg.dialog_history_turns,
                                summary_max_chars=self.cfg.dialog_summary_max_chars,
                            )
                            self._update_dialog_slots_after_reply(
                                user_id=user_id,
                                user_text=text,
                                answer_text=memory_reply,
                                previous_topic=dialog_request_ctx.get("current_topic", ""),
                            )
                        except Exception as exc:
                            print(f"[dialog] dialog slot update failed: {exc}")
                    if self.cfg.speak_audio:
                        self._tts_and_play(memory_reply, corr_id)
                    return

        if self.cfg.enable_vision_query and self._is_vision_query(text):
            print("[dialog] vision query routed to camera describe endpoint")
            vision_reply = self._request_vision_description()
            vision_reply = sanitize_tts_text(vision_reply)
            if vision_reply:
                if self.reply_compress:
                    vision_reply = compress_reply_for_latency(
                        vision_reply,
                        max_sentences=self.reply_max_sentences,
                        max_chars=self.reply_max_chars,
                    )
                    vision_reply = compress_reply_by_words(vision_reply, self.reply_max_words)
                    vision_reply = trim_trailing_connectors(vision_reply)
                    if vision_reply and vision_reply[-1] not in ".!?":
                        vision_reply = f"{vision_reply}."
                self._publish_answer_ex(
                    text=vision_reply,
                    corr_id=corr_id,
                    tts_speaker=self.tts_voice or None,
                    user_id=user_id,
                )
                if user_id and self.user_memory is not None and self.cfg.enable_dialog_context:
                    try:
                        self.user_memory.remember_dialog_turn(
                            user_id,
                            "assistant",
                            vision_reply,
                            max_turns=self.cfg.dialog_history_turns,
                            summary_max_chars=self.cfg.dialog_summary_max_chars,
                        )
                        self._update_dialog_slots_after_reply(
                            user_id=user_id,
                            user_text=text,
                            answer_text=vision_reply,
                            previous_topic=dialog_request_ctx.get("current_topic", ""),
                        )
                    except Exception as exc:
                        print(f"[dialog] dialog slot update failed: {exc}")
                if self.cfg.speak_audio:
                    self._tts_and_play(vision_reply, corr_id)
                return

        try:
            url = f"{self.cfg.respond_api_url}{self.cfg.respond_endpoint}"
            body = {"text": text}
            if memory_context:
                body["memory_context"] = memory_context
            if user_id:
                body["user_id"] = user_id
            if dialog_request_ctx.get("dialog_context"):
                body["dialog_context"] = dialog_request_ctx["dialog_context"]
            if barge_in:
                body["barge_in"] = True
            if interrupted_tts_text:
                body["interrupted_tts_text"] = interrupted_tts_text
            if user_id:
                print(
                    "[dialog] respond uses backend runtime/default system prompt "
                    f"user_id={user_id}"
                )
            else:
                print("[dialog] respond uses backend runtime/default system prompt")
            resp = self.http.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
            reply_text = (data.get("text") or "").strip()
        except Exception as exc:
            print(f"[dialog] LLM request failed: {exc}")
            return

        if not reply_text:
            return

        answer_text = self._extract_answer_text(reply_text)
        answer_text = sanitize_tts_text(answer_text)
        if not answer_text:
            print("[dialog] dropped stage-direction-like reply text")
            return
        if self.reply_compress:
            answer_text = compress_reply_for_latency(
                answer_text,
                max_sentences=self.reply_max_sentences,
                max_chars=self.reply_max_chars,
            )
            answer_text = compress_reply_by_words(answer_text, self.reply_max_words)
            answer_text = trim_trailing_connectors(answer_text)
            if answer_text and answer_text[-1] not in ".!?":
                answer_text = f"{answer_text}."
        if not answer_text:
            return

        tts_speaker = self.tts_voice or None
        self._publish_answer_ex(
            text=answer_text,
            corr_id=corr_id,
            tts_speaker=tts_speaker,
            user_id=user_id,
        )
        if user_id and self.user_memory is not None:
            try:
                self._remember_game_context(
                    user_id=user_id,
                    text=answer_text,
                    reference_kind="mentioned",
                    source="assistant",
                )
            except Exception as exc:
                print(f"[dialog] game context update failed: {exc}")
        if user_id and self.user_memory is not None and self.cfg.enable_dialog_context:
            try:
                self.user_memory.remember_dialog_turn(
                    user_id,
                    "assistant",
                    answer_text,
                    max_turns=self.cfg.dialog_history_turns,
                    summary_max_chars=self.cfg.dialog_summary_max_chars,
                )
                self._update_dialog_slots_after_reply(
                    user_id=user_id,
                    user_text=text,
                    answer_text=answer_text,
                    previous_topic=dialog_request_ctx.get("current_topic", ""),
                )
            except Exception as exc:
                print(f"[dialog] dialog slot update failed: {exc}")
        if self.cfg.speak_audio:
            self._tts_and_play(reply_text, corr_id)

    @staticmethod
    def _extract_answer_text(reply_text: str) -> str:
        raw = (reply_text or "").strip()
        if not raw:
            return ""
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                text = str(obj.get("text") or "").strip()
                if text:
                    return text
        except Exception:
            pass
        # Common fallback when models output a quoted answer as first line.
        try:
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            if len(lines) >= 1:
                t0 = json.loads(lines[0]) if lines[0].startswith('"') else lines[0]
                if isinstance(t0, str) and t0.strip():
                    return t0.strip()
        except Exception:
            pass
        return raw

    def _is_vision_query(self, text: str) -> bool:
        normalized = " ".join((text or "").strip().lower().split())
        if not normalized:
            return False

        if any(pattern in normalized for pattern in _VISION_QUERY_PATTERNS):
            return True

        # Regex fallback is for English paraphrases; explicit patterns cover CJK.
        if _CJK_RE.search(normalized):
            return False

        is_request_like = normalized.endswith("?") or any(
            normalized.startswith(prefix) for prefix in _VISION_QUERY_REQUEST_PREFIXES
        )
        if not is_request_like:
            return False

        has_see = _VISION_SEE_REGEX.search(normalized) is not None
        has_target = _VISION_TARGET_REGEX.search(normalized) is not None
        if has_see and has_target:
            return True

        if _VISION_DESCRIBE_REGEX.search(normalized) and has_target:
            return True

        return False

    @staticmethod
    def _format_vision_failure_reply(detail: str) -> str:
        clean = " ".join((detail or "").strip().split())
        if not clean:
            return "I can't access the camera right now."

        lowered = clean.lower()
        if "start preview" in lowered or "not active" in lowered:
            return "I can't see yet because camera preview is not active. Click Start Preview, wait 1-2 seconds, then ask again."
        if "preview disabled" in lowered:
            return "I can't see because camera preview is disabled in the current setup."
        if "no camera frame" in lowered:
            return "I can't see a valid camera frame yet. Keep preview running for 1-2 seconds, then ask again."
        if "backend unavailable" in lowered:
            return "I can't reach the vision backend right now. Please check Ollama and try again."

        return f"I can't access the camera right now: {clean}"

    def _request_vision_description(self) -> str:
        endpoint = (self.cfg.vision_describe_url or "").strip()
        if not endpoint:
            return "I can't access the camera because vision endpoint is not configured."

        payload: Dict[str, str] = {}
        prompt = (self.cfg.vision_query_prompt or "").strip()
        if prompt:
            payload["prompt"] = prompt
        model = (self.cfg.vision_query_model or "").strip()
        if model:
            payload["model"] = model

        try:
            response = self.http.post(
                endpoint,
                json=payload,
                timeout=max(1.0, float(self.cfg.vision_timeout_seconds)),
            )
        except Exception as exc:
            return self._format_vision_failure_reply(str(exc))

        response_obj: Optional[Dict[str, object]] = None
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                response_obj = parsed
        except Exception:
            response_obj = None

        if response.status_code >= 400:
            detail = ""
            if response_obj is not None:
                detail = str(response_obj.get("message") or response_obj.get("error") or "").strip()
            if not detail:
                detail = (response.text or "").strip()
            return self._format_vision_failure_reply(detail)

        description = ""
        if response_obj is not None:
            description = str(
                response_obj.get("description")
                or response_obj.get("text")
                or response_obj.get("message")
                or ""
            ).strip()
        if not description:
            description = (response.text or "").strip()

        if not description:
            return "I checked the camera but couldn't get a usable description."
        return description

    def _is_memory_query(self, text: str) -> bool:
        normalized = " ".join((text or "").strip().lower().split())
        if not normalized:
            return False

        if self._memory_query_rule_match(normalized):
            return True

        semantic_score = self._memory_query_semantic_score(normalized)
        if semantic_score >= self.cfg.memory_query_threshold:
            print(
                "[dialog] memory query semantic hit "
                f"score={semantic_score:.3f} threshold={self.cfg.memory_query_threshold:.3f}"
            )
        return semantic_score >= self.cfg.memory_query_threshold
