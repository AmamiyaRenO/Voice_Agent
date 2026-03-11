#!/usr/bin/env python3
from __future__ import annotations

import json
import uuid
from typing import Any, Dict

import paho.mqtt.client as mqtt

try:
    from .intent_config import Config
    from .intent_routing import IntentRouterEngine, ManifestAliasResolver, new_corr_id, normalize
except Exception:
    from intent_config import Config
    from intent_routing import IntentRouterEngine, ManifestAliasResolver, new_corr_id, normalize

try:
    from dialog_service.dialog_config import load_config as load_dialog_config
    from dialog_service.user_memory import UserMemoryStore, speaker_identity_key
except Exception:
    try:
        from scripts.dialog_service.dialog_config import load_config as load_dialog_config
        from scripts.dialog_service.user_memory import UserMemoryStore, speaker_identity_key
    except Exception:
        load_dialog_config = None
        UserMemoryStore = None
        speaker_identity_key = None


def _normalize_identity_resolution(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "none":
        return "none"
    return "auto"


def extract_identity_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    if not isinstance(payload, dict):
        return result

    for int_key in ("speaker_index", "speaker_id"):
        if int_key not in payload:
            continue
        raw = payload.get(int_key)
        if raw is None or raw == "":
            continue
        try:
            value = int(raw)
        except Exception:
            continue
        if value < 0:
            continue
        result[int_key] = value

    for str_key in ("speaker_profile_id", "speaker_label", "user_id"):
        if str_key not in payload:
            continue
        value = str(payload.get(str_key) or "").strip()
        if not value:
            continue
        result[str_key] = value

    identity_resolution = _normalize_identity_resolution(payload.get("identity_resolution"))
    if identity_resolution:
        result["identity_resolution"] = identity_resolution

    return result


class IntentService:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"intent-svc-{uuid.uuid4().hex[:8]}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self._stopping = False
        self._resolver = ManifestAliasResolver(cfg.manifest_path)
        self._router = IntentRouterEngine(cfg, self._resolver)
        self._user_memory = self._init_user_memory()

    def _init_user_memory(self):
        if load_dialog_config is None or UserMemoryStore is None:
            return None
        try:
            dialog_cfg = load_dialog_config()
            if not getattr(dialog_cfg, "enable_user_memory", True):
                return None
            return UserMemoryStore(
                path=str(dialog_cfg.user_memory_path),
                max_notes=int(dialog_cfg.user_memory_max_notes),
                prompt_max_chars=int(dialog_cfg.user_memory_prompt_max_chars),
                embedder=None,
                retrieve_top_k=max(1, int(getattr(dialog_cfg, "user_memory_retrieve_top_k", 3))),
            )
        except Exception as exc:
            print(f"[intent] user memory unavailable for contextual routing: {exc}")
            return None

    def start(self) -> None:
        print(f"[intent] connecting to mqtt {self.cfg.host}:{self.cfg.port}")
        if self._router.moonshine_matcher.enabled:
            if self._router.moonshine_matcher.ready:
                print("[intent] moonshine intent matcher enabled")
            else:
                detail = self._router.moonshine_matcher.error or "initialization failed"
                print(f"[intent] moonshine intent matcher requested but unavailable: {detail}")
        else:
            print("[intent] moonshine intent matcher disabled")
        if self.cfg.use_llm_classifier:
            print(
                "[intent] llm classifier enabled (semantic+phonetic route): "
                f"url={self.cfg.llm_classifier_url} "
                f"timeout={self.cfg.llm_timeout_sec:.2f}s "
                f"min_conf={self.cfg.llm_min_confidence:.2f}"
            )
        else:
            print("[intent] llm classifier disabled")
        self.client.connect(self.cfg.host, self.cfg.port, keepalive=20)
        self.client.loop_start()

    def stop(self) -> None:
        self._stopping = True
        self._router.close()
        try:
            self.client.loop_stop()
        finally:
            self.client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        print(f"[intent] connected rc={reason_code}")
        client.subscribe(self.cfg.topics.voice_text)
        print(f"[intent] subscribed {self.cfg.topics.voice_text}")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except json.JSONDecodeError:
            print("[intent] invalid json, ignored")
            return

        topic = getattr(msg, "topic", "") or ""
        if topic != self.cfg.topics.voice_text:
            return

        text = normalize(str(payload.get("text") or ""))
        if not text:
            return

        corr_id = str(payload.get("corr_id") or new_corr_id())
        identity_fields = extract_identity_fields(payload)
        context_game_name = ""
        if self._user_memory is not None:
            try:
                resolved_user_id = str(payload.get("user_id") or "").strip()
                if (
                    not resolved_user_id
                    and _normalize_identity_resolution(payload.get("identity_resolution")) != "none"
                    and speaker_identity_key is not None
                ):
                    resolved_user_id = self._user_memory.resolve_user(speaker_identity_key(payload))
                if resolved_user_id:
                    context_game_name = self._user_memory.get_game_reference(resolved_user_id)
            except Exception as exc:
                print(f"[intent] contextual game resolve failed: {exc}")
        decision = self._router.route(text, corr_id, context_game_name=context_game_name)
        if decision.log_line:
            print(decision.log_line)
        if decision.topic and decision.payload is not None:
            if identity_fields:
                for key, value in identity_fields.items():
                    decision.payload.setdefault(key, value)
            self.client.publish(decision.topic, json.dumps(decision.payload))
