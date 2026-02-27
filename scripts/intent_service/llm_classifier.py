#!/usr/bin/env python3
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

try:
    from .intent_config import Config
    from .manifest_resolver import ManifestAliasResolver
    from .match_utils import extract_first_json_object, normalize_intent_label
except Exception:
    from intent_config import Config
    from manifest_resolver import ManifestAliasResolver
    from match_utils import extract_first_json_object, normalize_intent_label


@dataclass
class LlmIntentDecision:
    intent: str
    game_name: str
    confidence: float


class LlmIntentClassifier:
    def __init__(self, cfg: Config, resolver: ManifestAliasResolver) -> None:
        self.cfg = cfg
        self.resolver = resolver
        self._cache: Dict[str, Tuple[float, LlmIntentDecision]] = {}

    def classify(self, text: str) -> Optional[LlmIntentDecision]:
        if not self.cfg.use_llm_classifier:
            return None
        if not self._should_use_llm(text):
            return None

        normalized = " ".join((text or "").strip().lower().split())
        if not normalized:
            return None

        cached = self._cache_get(normalized)
        if cached is not None:
            return cached

        decision = self._request_llm(text)
        if decision is None:
            return None

        self._cache_put(normalized, decision)
        return decision

    def _should_use_llm(self, text: str) -> bool:
        candidate = (text or "").strip()
        if not candidate:
            return False
        if len(candidate) < 3:
            return False
        return True

    def _cache_get(self, key: str) -> Optional[LlmIntentDecision]:
        item = self._cache.get(key)
        if item is None:
            return None
        ts, decision = item
        if (time.time() - ts) > max(0.5, self.cfg.llm_cache_ttl_sec):
            self._cache.pop(key, None)
            return None
        return decision

    def _cache_put(self, key: str, decision: LlmIntentDecision) -> None:
        self._cache[key] = (time.time(), decision)
        # Avoid unbounded growth under noisy ASR.
        if len(self._cache) > 256:
            oldest_key = min(self._cache.items(), key=lambda kv: kv[1][0])[0]
            self._cache.pop(oldest_key, None)

    def _request_llm(self, text: str) -> Optional[LlmIntentDecision]:
        url = (self.cfg.llm_classifier_url or "").strip()
        if not url:
            return None

        system_prompt = (
            "You are an intent classifier for a voice-controlled game launcher. "
            "Use semantic relevance and pronunciation similarity, and tolerate ASR errors. "
            "Classify the utterance into exactly one intent: LAUNCH_GAME, BACK_HOME, or QUERY. "
            "If launching, choose the closest game from the provided catalog even when user wording is fuzzy. "
            "Return strict JSON only with schema: "
            "{\"intent\":\"LAUNCH_GAME|BACK_HOME|QUERY\",\"game_name\":\"\",\"confidence\":0.0}. "
            "Set game_name only when intent is LAUNCH_GAME."
        )
        text_for_llm = (text or "").strip()
        max_chars = max(24, int(self.cfg.llm_max_chars))
        if len(text_for_llm) > max_chars:
            text_for_llm = text_for_llm[:max_chars]
        body = {
            "text": text_for_llm,
            "system": system_prompt,
            "games": self.resolver.prompt_catalog(),
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )

        timeout = max(0.2, float(self.cfg.llm_timeout_sec))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
        except urllib.error.URLError:
            return None
        except Exception:
            return None

        try:
            payload = json.loads(raw)
            reply_text = str(payload.get("text") or "").strip() if isinstance(payload, dict) else ""
        except Exception:
            reply_text = ""

        if not reply_text:
            return None

        return self._parse_llm_reply(reply_text, text)

    def _parse_llm_reply(self, reply_text: str, source_text: str) -> Optional[LlmIntentDecision]:
        obj = extract_first_json_object(reply_text)
        if obj is None:
            return None

        intent = normalize_intent_label(str(obj.get("intent") or obj.get("type") or obj.get("label") or ""))

        confidence_raw = obj.get("confidence")
        if confidence_raw is None:
            confidence_raw = obj.get("score")
        try:
            confidence = float(confidence_raw) if confidence_raw is not None else 0.0
        except Exception:
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        game_name = str(obj.get("game_name") or obj.get("game") or obj.get("target") or "").strip()
        if not game_name:
            slots = obj.get("slots")
            if isinstance(slots, dict):
                game_name = str(slots.get("game_name") or slots.get("game") or "").strip()

        if intent == "LAUNCH_GAME":
            # Never pass raw unknown names through to launcher. Only accept manifest-mapped targets.
            if game_name:
                canonical = self.resolver.resolve_best_name(game_name, self.cfg.fuzzy_threshold)
                if canonical:
                    game_name = canonical
                else:
                    game_name = ""
            else:
                canonical = self.resolver.resolve_best_name(source_text, self.cfg.fuzzy_threshold)
                if canonical:
                    game_name = canonical

        return LlmIntentDecision(intent=intent, game_name=game_name, confidence=confidence)
