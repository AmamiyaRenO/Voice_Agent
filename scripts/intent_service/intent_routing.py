#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

try:
    from .intent_config import Config
    from .llm_classifier import LlmIntentClassifier, LlmIntentDecision
    from .manifest_resolver import ManifestAliasResolver
    from .match_utils import (
        best_exit_similarity,
        has_wake_word,
        new_corr_id,
        normalize,
        normalize_match_text,
    )
except Exception:
    from intent_config import Config
    from llm_classifier import LlmIntentClassifier, LlmIntentDecision
    from manifest_resolver import ManifestAliasResolver
    from match_utils import (
        best_exit_similarity,
        has_wake_word,
        new_corr_id,
        normalize,
        normalize_match_text,
    )


@dataclass
class RouteDecision:
    topic: Optional[str]
    payload: Optional[Dict[str, Any]]
    log_line: Optional[str]


class MoonshineIntentMatcher:
    def __init__(self, cfg: Config, resolver: ManifestAliasResolver) -> None:
        self.cfg = cfg
        self.resolver = resolver
        self.enabled = bool(cfg.use_moonshine_intent_recognizer)
        self.ready = False
        self.error: str = ""
        self._recognizer = None
        self._last_hit: Optional[Tuple[str, float]] = None
        if not self.enabled:
            return

        try:
            from moonshine_voice import IntentRecognizer, get_embedding_model
        except Exception as exc:
            self.error = str(exc)
            print(f"[intent] moonshine matcher unavailable: {exc}")
            return

        aliases = sorted(self.resolver.normalized_alias_to_name.keys())
        if not aliases:
            self.error = "no aliases available from manifest"
            print("[intent] moonshine matcher disabled: no aliases available from manifest")
            return

        try:
            threshold = max(0.0, min(1.0, float(self.cfg.moonshine_intent_threshold)))
            model_path, model_arch = get_embedding_model(
                self.cfg.moonshine_embedding_model,
                self.cfg.moonshine_embedding_variant,
            )
            recognizer = IntentRecognizer(
                model_path=model_path,
                model_arch=model_arch,
                model_variant=self.cfg.moonshine_embedding_variant,
                threshold=threshold,
            )
            for alias in aliases:
                recognizer.register_intent(alias, self._on_hit)
            self._recognizer = recognizer
            self.ready = True
            print(
                "[intent] moonshine matcher enabled: "
                f"aliases={len(aliases)} threshold={threshold:.2f} "
                f"model={self.cfg.moonshine_embedding_model} variant={self.cfg.moonshine_embedding_variant}"
            )
        except Exception as exc:
            self.error = str(exc)
            print(f"[intent] moonshine matcher failed to initialize: {exc}")
            self.close()

    def _on_hit(self, trigger_phrase: str, utterance: str, similarity: float) -> None:
        normalized_trigger = normalize_match_text(trigger_phrase)
        self._last_hit = (normalized_trigger, float(similarity))

    def resolve_best_name(self, text: str) -> Tuple[Optional[str], float]:
        if not self.ready or self._recognizer is None:
            return None, 0.0

        utterance = (text or "").strip()
        if not utterance:
            return None, 0.0

        self._last_hit = None
        try:
            matched = self._recognizer.process_utterance(utterance)
        except Exception as exc:
            self.error = str(exc)
            return None, 0.0

        if not matched or self._last_hit is None:
            return None, 0.0

        trigger, similarity = self._last_hit
        game_name = self.resolver.normalized_alias_to_name.get(trigger)
        if not game_name:
            game_name = self.resolver.resolve_best_name(trigger, self.cfg.fuzzy_threshold)
        if not game_name:
            return None, 0.0
        return game_name, max(0.0, min(1.0, similarity))

    def close(self) -> None:
        recognizer = self._recognizer
        self._recognizer = None
        self.ready = False
        if recognizer is None:
            return
        try:
            recognizer.close()
        except Exception:
            pass


class IntentRouterEngine:
    def __init__(self, cfg: Config, resolver: ManifestAliasResolver) -> None:
        self.cfg = cfg
        self.resolver = resolver
        self.llm_classifier = LlmIntentClassifier(cfg, resolver)
        self.moonshine_matcher = MoonshineIntentMatcher(cfg, resolver)
        self._query_hint_tokens = {
            "what",
            "why",
            "how",
            "who",
            "where",
            "when",
            "tell",
            "explain",
            "describe",
            "remember",
            "favorite",
            "sentence",
            "say",
        }
        self._launcher_filler_tokens = {
            "please",
            "now",
            "right",
            "away",
            "up",
            "the",
            "a",
            "an",
            "game",
        }
        self._context_game_tokens = {
            "it",
            "that",
            "this",
            "one",
            "recommended",
            "recommend",
            "suggested",
            "mentioned",
            "last",
        }
        self._exit_command_tokens = {
            "back",
            "home",
            "go",
            "return",
            "quit",
            "exit",
            "stop",
            "cancel",
            "close",
            "game",
            "the",
            "please",
            "now",
        }

    def close(self) -> None:
        self.moonshine_matcher.close()

    def _has_launch_signal(self, text: str) -> bool:
        value = normalize_match_text(text)
        if not value:
            return False
        haystack = f" {value} "
        for raw_trigger in self.cfg.launch_triggers or []:
            trigger = normalize_match_text(str(raw_trigger))
            if not trigger:
                continue
            if haystack.find(f" {trigger} ") >= 0:
                return True
        return False

    def _tokens(self, text: str) -> list[str]:
        return [token for token in normalize_match_text(text).split(" ") if token]

    def _looks_like_query_or_statement(self, text: str) -> bool:
        raw = (text or "").strip()
        if not raw:
            return False
        if "?" in raw:
            return True
        tokens = set(self._tokens(raw))
        return bool(tokens & self._query_hint_tokens)

    def _is_direct_game_request(self, text: str) -> bool:
        tokens = self._tokens(text)
        if not tokens:
            return False
        if self._has_launch_signal(text):
            return True
        if self._looks_like_query_or_statement(text):
            return False
        meaningful = [token for token in tokens if token not in self._launcher_filler_tokens]
        return 0 < len(meaningful) <= 3

    def _is_direct_exit_command(self, text: str) -> bool:
        tokens = self._tokens(text)
        if not tokens:
            return False
        if self._looks_like_query_or_statement(text):
            return False
        unexpected = [token for token in tokens if token not in self._exit_command_tokens]
        return len(unexpected) == 0

    def _is_contextual_game_request(self, text: str) -> bool:
        tokens = self._tokens(text)
        if not tokens or not self._has_launch_signal(text):
            return False
        if self._looks_like_query_or_statement(text):
            return False
        launch_tokens = set()
        for raw_trigger in self.cfg.launch_triggers or []:
            launch_tokens.update(token for token in normalize_match_text(str(raw_trigger)).split(" ") if token)
        meaningful = [
            token
            for token in tokens
            if token not in self._launcher_filler_tokens and token not in launch_tokens
        ]
        if not meaningful:
            return False
        return all(token in self._context_game_tokens for token in meaningful)

    def route(self, text: str, corr_id: str, *, context_game_name: str = "") -> RouteDecision:
        context_game_name = (context_game_name or "").strip()
        if self.cfg.require_wake_word and not has_wake_word(text, self.cfg.wake_words):
            return RouteDecision(topic=None, payload=None, log_line=f"[intent] no wake word: {text}")

        llm_decision: Optional[LlmIntentDecision] = self.llm_classifier.classify(text)
        min_conf = max(0.0, min(1.0, float(self.cfg.llm_min_confidence)))
        if llm_decision is not None and llm_decision.confidence >= min_conf:
            if llm_decision.intent == "BACK_HOME":
                payload = {
                    "type": "BACK_HOME",
                    "source": self.cfg.source_label,
                    "raw": {"text": text},
                    "corr_id": corr_id,
                }
                return RouteDecision(
                    topic=self.cfg.topics.intent,
                    payload=payload,
                    log_line=(
                        f"[intent] -> BACK_HOME {self.cfg.topics.intent} "
                        f"(llm conf={llm_decision.confidence:.2f})"
                    ),
                )

            if llm_decision.intent == "LAUNCH_GAME":
                game_name = llm_decision.game_name
                if not game_name and context_game_name and self._is_contextual_game_request(text):
                    game_name = context_game_name
                if not game_name:
                    # LLM says "launch", but ASR/LLM may still output noisy target text
                    # (e.g. "core hog"). Under this branch only, allow a softer threshold
                    # to recover pronunciation-near game names.
                    relaxed_threshold = max(50, min(100, int(self.cfg.fuzzy_threshold) - 15))
                    game_name = self.resolver.resolve_best_name(text, relaxed_threshold)
                if not game_name:
                    llm_note = f"(llm launch unresolved conf={llm_decision.confidence:.2f})"
                    return RouteDecision(
                        topic=self.cfg.topics.dialog_query,
                        payload={
                            "type": "QUERY",
                            "text": text,
                            "source": self.cfg.source_label,
                            "corr_id": corr_id,
                        },
                        log_line=f"[intent] -> QUERY {self.cfg.topics.dialog_query} {llm_note}",
                    )
                payload = {
                    "type": "LAUNCH_GAME",
                    "game_name": game_name,
                    "source": self.cfg.source_label,
                    "raw": {"text": text},
                    "corr_id": corr_id,
                }
                return RouteDecision(
                    topic=self.cfg.topics.intent,
                    payload=payload,
                    log_line=(
                        f"[intent] -> LAUNCH_GAME '{game_name}' {self.cfg.topics.intent} "
                        f"(llm conf={llm_decision.confidence:.2f})"
                    ),
                )

        # Curated manifest aliases should win over generic BACK_HOME matching.
        exact_game = self.resolver.canonical_name(text)
        if exact_game and self._is_direct_game_request(text):
            payload = {
                "type": "LAUNCH_GAME",
                "game_name": exact_game,
                "source": self.cfg.source_label,
                "raw": {"text": text},
                "corr_id": corr_id,
            }
            return RouteDecision(
                topic=self.cfg.topics.intent,
                payload=payload,
                log_line=f"[intent] -> LAUNCH_GAME '{exact_game}' {self.cfg.topics.intent} (exact alias)",
            )

        if context_game_name and self._is_contextual_game_request(text):
            payload = {
                "type": "LAUNCH_GAME",
                "game_name": context_game_name,
                "source": self.cfg.source_label,
                "raw": {"text": text},
                "corr_id": corr_id,
            }
            return RouteDecision(
                topic=self.cfg.topics.intent,
                payload=payload,
                log_line=(
                    f"[intent] -> LAUNCH_GAME '{context_game_name}' {self.cfg.topics.intent} "
                    "(context reference)"
                ),
            )

        # Semantic/phonetic fallback for BACK_HOME (no exact keyword hard-match).
        # This keeps "back", "go back", "return home" stable even when LLM is off
        # or confidence is below threshold.
        exit_score = best_exit_similarity(text, self.cfg.exit_keywords)
        exit_threshold = max(50.0, min(95.0, float(self.cfg.back_home_similarity_threshold)))
        if exit_score >= exit_threshold and self._is_direct_exit_command(text):
            payload = {
                "type": "BACK_HOME",
                "source": self.cfg.source_label,
                "raw": {"text": text},
                "corr_id": corr_id,
            }
            return RouteDecision(
                topic=self.cfg.topics.intent,
                payload=payload,
                log_line=(
                    f"[intent] -> BACK_HOME {self.cfg.topics.intent} "
                    f"(sim={exit_score:.1f})"
                ),
            )

        # Optional semantic intent matching via Moonshine embedding model.
        moonshine_game, moonshine_similarity = self.moonshine_matcher.resolve_best_name(text)
        if moonshine_game and self._has_launch_signal(text):
            payload = {
                "type": "LAUNCH_GAME",
                "game_name": moonshine_game,
                "source": self.cfg.source_label,
                "raw": {"text": text},
                "corr_id": corr_id,
            }
            return RouteDecision(
                topic=self.cfg.topics.intent,
                payload=payload,
                log_line=(
                    f"[intent] -> LAUNCH_GAME '{moonshine_game}' {self.cfg.topics.intent} "
                    f"(moonshine sim={moonshine_similarity:.2f})"
                ),
            )

        # Soft fallback without hard keyword rules: relevance + phonetic similarity only.
        game = self.resolver.resolve_best_name(text, self.cfg.fuzzy_threshold)
        if game and self._is_direct_game_request(text):
            payload = {
                "type": "LAUNCH_GAME",
                "game_name": game,
                "source": self.cfg.source_label,
                "raw": {"text": text},
                "corr_id": corr_id,
            }
            return RouteDecision(
                topic=self.cfg.topics.intent,
                payload=payload,
                log_line=f"[intent] -> LAUNCH_GAME '{game}' {self.cfg.topics.intent}",
            )

        payload = {
            "type": "QUERY",
            "text": text,
            "source": self.cfg.source_label,
            "corr_id": corr_id,
        }
        llm_note = ""
        if llm_decision is not None:
            llm_note = f" (llm={llm_decision.intent} conf={llm_decision.confidence:.2f})"
        return RouteDecision(
            topic=self.cfg.topics.dialog_query,
            payload=payload,
            log_line=f"[intent] -> QUERY {self.cfg.topics.dialog_query}{llm_note}",
        )
