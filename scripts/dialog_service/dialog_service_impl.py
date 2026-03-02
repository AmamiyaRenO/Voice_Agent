#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Optional

import httpx
import paho.mqtt.client as mqtt

try:
    from .dialog_config import Config, _default_embedder_repo, _env_bool, _env_int
    from .onnx_embedder import OnnxTextEmbedder
    from .text_utils import (
        compress_reply_by_words,
        compress_reply_for_latency,
        sanitize_tts_text,
        trim_trailing_connectors,
    )
    from .user_memory import UserMemoryStore, speaker_identity_key
except Exception:
    from dialog_config import Config, _default_embedder_repo, _env_bool, _env_int
    from onnx_embedder import OnnxTextEmbedder
    from text_utils import (
        compress_reply_by_words,
        compress_reply_for_latency,
        sanitize_tts_text,
        trim_trailing_connectors,
    )
    from user_memory import UserMemoryStore, speaker_identity_key


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
        self.reply_max_chars = _env_int("DIALOG_MAX_REPLY_CHARS", 0, floor=0)
        self.reply_max_words = _env_int("DIALOG_MAX_REPLY_WORDS", 0, floor=0)
        self.user_memory_embedder: Optional[OnnxTextEmbedder] = None
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
        if os.environ.get("DIALOG_SPEAK_AUDIO"):
            print("[dialog] NOTE: DIALOG_SPEAK_AUDIO is set but will be ignored (forced off for AEC).")

    def start(self) -> None:
        print(f"[dialog] connecting to mqtt {self.cfg.host}:{self.cfg.port}")
        if self.user_memory is not None:
            print(f"[dialog] user memory enabled path={self.cfg.user_memory_path}")
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
        else:
            print("[dialog] user memory disabled")
        self.client.connect(self.cfg.host, self.cfg.port, keepalive=20)
        self.client.loop_start()

    def stop(self) -> None:
        try:
            self.client.loop_stop()
        finally:
            self.client.disconnect()
            self.http.close()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        print(f"[dialog] connected rc={reason_code}")
        client.subscribe(self.cfg.topics.dialog_query)
        print(f"[dialog] subscribed {self.cfg.topics.dialog_query}")
        client.subscribe(self.cfg.topics.tts_options)
        print(f"[dialog] subscribed {self.cfg.topics.tts_options}")

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
        memory_context = ""
        if self.user_memory is not None:
            try:
                identity_key = speaker_identity_key(payload)
                user_id = self.user_memory.resolve_user(identity_key)
                self.user_memory.remember_utterance(user_id, text)
                memory_context = self.user_memory.build_memory_context(user_id, query_text=text)
            except Exception as exc:
                print(f"[dialog] user memory resolve failed: {exc}")
                memory_context = ""

        if user_id and self.user_memory is not None and self._is_memory_query(text):
            try:
                memory_reply = (self.user_memory.build_facts_reply(user_id) or "").strip()
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
                    if self.cfg.speak_audio:
                        self._tts_and_play(memory_reply, corr_id)
                    return

        try:
            url = f"{self.cfg.respond_api_url}{self.cfg.respond_endpoint}"
            body = {"text": text}
            if memory_context:
                body["memory_context"] = memory_context
            if user_id:
                body["user_id"] = user_id
            if user_id:
                print(f"[dialog] respond uses backend runtime/default system prompt user_id={user_id}")
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

    @staticmethod
    def _is_memory_query(text: str) -> bool:
        lowered = " ".join((text or "").strip().lower().split())
        if not lowered:
            return False
        patterns = (
            "what do you know about me",
            "what do you remember about me",
            "do you remember me",
            "tell me about me",
            "who am i",
            "what do you know of me",
            "where am i from",
            "where do i come from",
            "what is my name",
            "what's my name",
            "what do i like",
            "what are my goals",
        )
        return any(p in lowered for p in patterns)
