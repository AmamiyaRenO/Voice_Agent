#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from difflib import SequenceMatcher
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import paho.mqtt.client as mqtt

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from common.config_utils import load_yaml_file, resolve_optional_path
from common.service_runtime import run_service_loop


@dataclass
class Topics:
    voice_text: str = "robot/voice/text"
    intent: str = "robot/intent"
    dialog_query: str = "robot/dialog/query"


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 1883
    topics: Topics = field(default_factory=Topics)
    require_wake_word: bool = False
    wake_words: List[str] = None  # type: ignore[assignment]
    exit_keywords: List[str] = None  # type: ignore[assignment]
    launch_triggers: List[str] = None  # type: ignore[assignment]
    source_label: str = "intent_service"
    manifest_path: Optional[str] = None
    fuzzy_threshold: int = 80
    dedupe_window_sec: float = 1.2
    use_llm_classifier: bool = False
    llm_classifier_url: str = "http://127.0.0.1:8000/respond"
    llm_timeout_sec: float = 0.9
    llm_min_confidence: float = 0.72
    llm_max_chars: int = 120
    llm_cache_ttl_sec: float = 12.0
    back_home_similarity_threshold: float = 70.0

    def __post_init__(self) -> None:
        if self.wake_words is None:
            self.wake_words = [
                "hi rachel",
                "hey rachel",
                "hello rachel",
            ]
        if self.exit_keywords is None:
            self.exit_keywords = [
                "back home",
                "go home",
                "return home",
                "go back",
                "quit",
                "exit",
                "stop",
                "cancel",
                "close",
                "close game",
            ]
        if self.launch_triggers is None:
            self.launch_triggers = [
                "open ",
                "start ",
                "launch ",
                "play ",
                "begin ",
                "load ",
            ]


@dataclass
class RouteDecision:
    topic: Optional[str]
    payload: Optional[Dict[str, Any]]
    log_line: Optional[str]


def _parse_bool(raw: Optional[str], default: bool) -> bool:
    text = (raw or "").strip().lower()
    if not text:
        return default
    if text in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "off"}:
        return False
    return default


def _parse_float(raw: Optional[str], default: float) -> float:
    text = (raw or "").strip()
    if not text:
        return default
    try:
        return float(text)
    except Exception:
        return default


def _parse_int(raw: Optional[str], default: int) -> int:
    text = (raw or "").strip()
    if not text:
        return default
    try:
        return int(text)
    except Exception:
        return default


def _parse_bool_like(raw: object, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    return _parse_bool(str(raw), default)


def _apply_rule_overrides(cfg: Config, rules: Dict[str, Any]) -> None:
    rules_threshold = rules.get("fuzzy_threshold")
    if rules_threshold is not None:
        cfg.fuzzy_threshold = _parse_int(str(rules_threshold), cfg.fuzzy_threshold)

    rules_dedupe = rules.get("dedupe_window_sec")
    if rules_dedupe is not None:
        cfg.dedupe_window_sec = _parse_float(str(rules_dedupe), cfg.dedupe_window_sec)

    rules_use_llm = rules.get("use_llm_classifier")
    if rules_use_llm is not None:
        cfg.use_llm_classifier = _parse_bool_like(rules_use_llm, cfg.use_llm_classifier)

    rules_llm_url = rules.get("llm_classifier_url")
    if rules_llm_url is not None:
        cfg.llm_classifier_url = str(rules_llm_url).strip() or cfg.llm_classifier_url

    rules_llm_timeout = rules.get("llm_timeout_sec")
    if rules_llm_timeout is not None:
        cfg.llm_timeout_sec = _parse_float(str(rules_llm_timeout), cfg.llm_timeout_sec)

    rules_llm_conf = rules.get("llm_min_confidence")
    if rules_llm_conf is not None:
        cfg.llm_min_confidence = _parse_float(str(rules_llm_conf), cfg.llm_min_confidence)

    rules_llm_max_chars = rules.get("llm_max_chars")
    if rules_llm_max_chars is not None:
        cfg.llm_max_chars = _parse_int(str(rules_llm_max_chars), cfg.llm_max_chars)

    rules_llm_cache_ttl = rules.get("llm_cache_ttl_sec")
    if rules_llm_cache_ttl is not None:
        cfg.llm_cache_ttl_sec = _parse_float(str(rules_llm_cache_ttl), cfg.llm_cache_ttl_sec)

    rules_back_sim = rules.get("back_home_similarity_threshold")
    if rules_back_sim is not None:
        cfg.back_home_similarity_threshold = _parse_float(
            str(rules_back_sim),
            cfg.back_home_similarity_threshold,
        )


def _apply_env_intent_overrides(cfg: Config) -> None:
    cfg.use_llm_classifier = _parse_bool(
        os.environ.get("INTENT_USE_LLM_CLASSIFIER"),
        cfg.use_llm_classifier,
    )
    cfg.llm_classifier_url = (
        os.environ.get("INTENT_LLM_CLASSIFIER_URL", cfg.llm_classifier_url).strip()
        or cfg.llm_classifier_url
    )
    cfg.llm_timeout_sec = _parse_float(
        os.environ.get("INTENT_LLM_TIMEOUT_SEC"),
        cfg.llm_timeout_sec,
    )
    cfg.llm_min_confidence = _parse_float(
        os.environ.get("INTENT_LLM_CONFIDENCE_THRESHOLD"),
        cfg.llm_min_confidence,
    )
    cfg.llm_max_chars = _parse_int(
        os.environ.get("INTENT_LLM_MAX_CHARS"),
        cfg.llm_max_chars,
    )
    cfg.llm_cache_ttl_sec = _parse_float(
        os.environ.get("INTENT_LLM_CACHE_TTL_SEC"),
        cfg.llm_cache_ttl_sec,
    )
    cfg.back_home_similarity_threshold = _parse_float(
        os.environ.get("INTENT_BACK_HOME_SIMILARITY_THRESHOLD"),
        cfg.back_home_similarity_threshold,
    )
    cfg.launch_triggers = _apply_list_env_override(
        cfg.launch_triggers,
        os.environ.get("INTENT_LAUNCH_TRIGGERS"),
    )
    cfg.exit_keywords = _apply_list_env_override(
        cfg.exit_keywords,
        os.environ.get("INTENT_EXIT_KEYWORDS"),
    )


def _apply_file_config(cfg: Config, cfg_path: Path) -> Config:
    data = load_yaml_file(cfg_path)
    mqtt_cfg = data.get("mqtt", {})
    topics = data.get("topics", {})
    rules = data.get("rules", {})

    cfg.host = os.environ.get("MQTT_HOST", mqtt_cfg.get("host", cfg.host))
    cfg.port = int(os.environ.get("MQTT_PORT", mqtt_cfg.get("port", cfg.port)))
    cfg.topics = Topics(
        voice_text=os.environ.get("VOICE_TEXT_TOPIC", topics.get("voice_text", cfg.topics.voice_text)),
        intent=os.environ.get("INTENT_TOPIC", topics.get("intent", cfg.topics.intent)),
        dialog_query=os.environ.get("DIALOG_QUERY_TOPIC", topics.get("dialog_query", cfg.topics.dialog_query)),
    )
    cfg.require_wake_word = _parse_bool_like(rules.get("require_wake_word"), cfg.require_wake_word)
    cfg.wake_words = rules.get("wake_words", cfg.wake_words)
    cfg.exit_keywords = rules.get("exit_keywords", cfg.exit_keywords)
    cfg.launch_triggers = rules.get("launch_triggers", cfg.launch_triggers)
    cfg.source_label = data.get("source_label", cfg.source_label)

    env_manifest = os.environ.get("INTENT_MANIFEST_PATH")
    raw_manifest = env_manifest if env_manifest is not None else data.get("manifest_path")
    cfg.manifest_path = resolve_optional_path(raw_manifest, base_dir=cfg_path.parent)
    _apply_rule_overrides(cfg, rules)
    _apply_env_intent_overrides(cfg)
    return cfg


def _apply_env_only_config(cfg: Config) -> Config:
    cfg.host = os.environ.get("MQTT_HOST", cfg.host)
    cfg.port = int(os.environ.get("MQTT_PORT", cfg.port))
    cfg.topics = Topics(
        voice_text=os.environ.get("VOICE_TEXT_TOPIC", cfg.topics.voice_text),
        intent=os.environ.get("INTENT_TOPIC", cfg.topics.intent),
        dialog_query=os.environ.get("DIALOG_QUERY_TOPIC", cfg.topics.dialog_query),
    )
    env_manifest = os.environ.get("INTENT_MANIFEST_PATH")
    if env_manifest:
        cfg.manifest_path = resolve_optional_path(env_manifest)
    cfg.fuzzy_threshold = int(os.environ.get("FUZZY_THRESHOLD", cfg.fuzzy_threshold))
    cfg.dedupe_window_sec = float(os.environ.get("DEDUPE_WINDOW_SEC", cfg.dedupe_window_sec))
    _apply_env_intent_overrides(cfg)
    return cfg


def load_config() -> Config:
    default_cfg = Path(__file__).resolve().with_name("config.yaml")
    cfg_path = Path(os.environ.get("INTENT_CONFIG", str(default_cfg))).expanduser()
    cfg = Config()
    if cfg_path.exists():
        return _apply_file_config(cfg, cfg_path)
    return _apply_env_only_config(cfg)


def normalize(text: str) -> str:
    return text.strip()


_PUNCT_TRIM_CHARS = " \t\r\n.,!?;:'\"`~()[]{}<>"
_CANDIDATE_LEADING_NOISE_RE = re.compile(
    r"^(?:please\s+|uh\s+|um\s+|hey\s+|ok\s+|okay\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+|lets?\s+|let\s+us\s+)+",
    re.IGNORECASE,
)
_CANDIDATE_TRAILING_NOISE_RE = re.compile(
    r"(?:\s+(?:please|thanks|thank\s+you|right\s+now|now))+$",
    re.IGNORECASE,
)
_STOPWORDS = {
    "the",
    "a",
    "an",
    "to",
    "for",
    "of",
    "and",
    "or",
    "please",
    "now",
    "right",
    "open",
    "start",
    "launch",
    "play",
    "begin",
    "load",
    "game",
}


def _normalize_match_text(text: str) -> str:
    value = (text or "").strip().lower()
    if not value:
        return ""
    value = value.strip(_PUNCT_TRIM_CHARS)
    value = re.sub(r"[\/_|]+", " ", value)
    value = re.sub(r"[\s]+", " ", value)
    return value.strip()


def _clean_game_candidate(text: str) -> str:
    value = _normalize_match_text(text)
    if not value:
        return ""
    value = _CANDIDATE_LEADING_NOISE_RE.sub("", value)
    value = _CANDIDATE_TRAILING_NOISE_RE.sub("", value)
    value = value.strip(_PUNCT_TRIM_CHARS)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _phonetic_code(token: str) -> str:
    text = re.sub(r"[^a-z]", "", (token or "").lower())
    if not text:
        return ""
    groups = {
        "b": "1", "f": "1", "p": "1", "v": "1",
        "c": "2", "g": "2", "j": "2", "k": "2", "q": "2", "s": "2", "x": "2", "z": "2",
        "d": "3", "t": "3",
        "l": "4",
        "m": "5", "n": "5",
        "r": "6",
    }
    first = text[0]
    digits: List[str] = []
    prev = groups.get(first, "")
    for ch in text[1:]:
        code = groups.get(ch, "0")
        if code != "0" and code != prev:
            digits.append(code)
        prev = code
    return (first.upper() + "".join(digits) + "000")[:4]


def _phrase_phonetic(text: str) -> str:
    tokens = [t for t in _normalize_match_text(text).split(" ") if t]
    codes = [_phonetic_code(t) for t in tokens]
    return " ".join([c for c in codes if c])


def _consonant_skeleton(text: str) -> str:
    value = re.sub(r"[^a-z]", "", (text or "").lower())
    if not value:
        return ""
    return re.sub(r"[aeiou]", "", value)


def _token_jaccard(a: str, b: str) -> float:
    sa = {t for t in _normalize_match_text(a).split(" ") if t}
    sb = {t for t in _normalize_match_text(b).split(" ") if t}
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    if union == 0:
        return 0.0
    return float(inter) / float(union)


def _similarity_score(candidate: str, alias: str) -> float:
    cand = _normalize_match_text(candidate)
    ali = _normalize_match_text(alias)
    if not cand or not ali:
        return 0.0

    char_score = max(
        SequenceMatcher(None, cand, ali).ratio(),
        SequenceMatcher(None, cand.replace(" ", ""), ali.replace(" ", "")).ratio(),
    )
    phon_score = 0.0
    cand_ph = _phrase_phonetic(cand)
    ali_ph = _phrase_phonetic(ali)
    if cand_ph and ali_ph:
        phon_score = SequenceMatcher(None, cand_ph, ali_ph).ratio()
    skeleton_score = 0.0
    cand_sk = _consonant_skeleton(cand)
    ali_sk = _consonant_skeleton(ali)
    if cand_sk and ali_sk:
        skeleton_score = SequenceMatcher(None, cand_sk, ali_sk).ratio()
    token_score = _token_jaccard(cand, ali)
    score = (0.4 * char_score + 0.3 * phon_score + 0.2 * skeleton_score + 0.1 * token_score) * 100.0

    # Generic short-prefix bonus for partial spoken names (e.g., "corn" vs "cornhole").
    if ali.startswith(cand) or cand.startswith(ali):
        overlap = min(len(cand), len(ali)) / max(1, max(len(cand), len(ali)))
        score = max(score, (0.65 + 0.35 * overlap) * 100.0)

    return score


def _candidate_variants(text: str) -> List[str]:
    cleaned = _clean_game_candidate(text)
    if not cleaned:
        return []

    variants: List[str] = [cleaned]
    base_tokens = [tok for tok in cleaned.split(" ") if tok]
    if not base_tokens:
        return variants

    reduced_tokens = [tok for tok in base_tokens if tok not in _STOPWORDS]
    if reduced_tokens and reduced_tokens != base_tokens:
        variants.append(" ".join(reduced_tokens))

    source_tokens = reduced_tokens if reduced_tokens else base_tokens
    max_n = min(4, len(source_tokens))
    for n in range(1, max_n + 1):
        for i in range(0, len(source_tokens) - n + 1):
            gram = " ".join(source_tokens[i : i + n]).strip()
            if gram:
                variants.append(gram)

    deduped: List[str] = []
    seen = set()
    for value in variants:
        normalized = _normalize_match_text(value)
        if not normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)

    return deduped


def has_wake_word(text: str, wake_words: List[str]) -> bool:
    lower = text.lower()
    return any(w.lower() in lower for w in wake_words)


def _best_exit_similarity(text: str, exit_keywords: List[str]) -> float:
    variants = _candidate_variants(text)
    if not variants:
        normalized = _normalize_match_text(text)
        if normalized:
            variants = [normalized]
    if not variants:
        return 0.0

    best = 0.0
    for variant in variants:
        for keyword in exit_keywords or []:
            target = _normalize_match_text(keyword)
            if not target:
                continue
            score = _similarity_score(variant, target)
            if score > best:
                best = score
    return best


def _apply_list_env_override(current: Optional[List[str]], raw: Optional[str]) -> List[str]:
    base = [str(item).strip() for item in (current or []) if str(item).strip()]
    text = (raw or "").strip()
    if not text:
        return base

    parsed: List[str] = []
    try:
        node = json.loads(text)
        if isinstance(node, list):
            parsed = [str(item).strip() for item in node if str(item).strip()]
    except Exception:
        merged = text.replace("\r\n", "\n").replace(";", ",").replace("\n", ",")
        parsed = [part.strip() for part in merged.split(",") if part.strip()]

    return parsed if parsed else base


def new_corr_id() -> str:
    return uuid.uuid4().hex


@dataclass
class LlmIntentDecision:
    intent: str
    game_name: str
    confidence: float


def _extract_first_json_object(text: str) -> Optional[Dict[str, Any]]:
    raw = (text or "").strip()
    if not raw:
        return None

    try:
        node = json.loads(raw)
        if isinstance(node, dict):
            return node
    except Exception:
        pass

    for start in range(len(raw)):
        if raw[start] != "{":
            continue
        depth = 0
        for end in range(start, len(raw)):
            ch = raw[end]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    snippet = raw[start : end + 1].strip()
                    if not snippet:
                        break
                    try:
                        node = json.loads(snippet)
                        if isinstance(node, dict):
                            return node
                    except Exception:
                        pass
                    break
    return None


def _normalize_intent_label(value: str) -> str:
    key = (value or "").strip().upper()
    if key in {"BACK_HOME", "EXIT", "EXIT_GAME", "QUIT", "STOP", "GO_HOME"}:
        return "BACK_HOME"
    if key in {"LAUNCH", "LAUNCH_GAME", "OPEN_GAME", "START_GAME", "PLAY_GAME"}:
        return "LAUNCH_GAME"
    return "QUERY"


class ManifestAliasResolver:
    def __init__(self, manifest_path: Optional[str]) -> None:
        self.manifest_path = manifest_path
        self.alias_to_name: Dict[str, str] = {}
        self.normalized_alias_to_name: Dict[str, str] = {}
        self.game_to_aliases: Dict[str, List[str]] = {}
        self._load()

    def _load(self) -> None:
        path = self.manifest_path
        if not path:
            return
        try:
            if not os.path.exists(path):
                print(f"[intent] manifest not found: {path}")
                return
            with open(path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
            for game in data.get("games", []) or []:
                name = str(game.get("name") or game.get("id") or "").strip()
                if not name:
                    continue
                raw_aliases = [name]
                raw_id = str(game.get("id") or "").strip()
                if raw_id:
                    raw_aliases.append(raw_id)
                for alias in game.get("synonyms", []) or []:
                    text = str(alias).strip()
                    if text:
                        raw_aliases.append(text)

                for alias in raw_aliases:
                    key = alias.lower().strip()
                    if key:
                        self.alias_to_name[key] = name
                    normalized_key = _normalize_match_text(alias)
                    if normalized_key:
                        self.normalized_alias_to_name[normalized_key] = name
                self.game_to_aliases[name] = sorted({a.strip() for a in raw_aliases if str(a).strip()})
            print(f"[intent] loaded {len(self.alias_to_name)} aliases from manifest")
        except Exception as exc:
            print(f"[intent] failed to load manifest aliases: {exc}")

    def prompt_catalog(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for game, aliases in self.game_to_aliases.items():
            rows.append({"name": game, "aliases": aliases})
        return rows

    def canonical_name(self, candidate: str) -> Optional[str]:
        cleaned = _clean_game_candidate(candidate)
        if not cleaned:
            return None
        return self.alias_to_name.get(cleaned) or self.normalized_alias_to_name.get(cleaned)

    def resolve_best_name(self, candidate: str, fuzzy_threshold: int) -> Optional[str]:
        candidates = _candidate_variants(candidate)
        if not candidates or not self.normalized_alias_to_name:
            return None

        # 1) Exact normalized match
        for cleaned in candidates:
            exact = self.normalized_alias_to_name.get(cleaned)
            if exact:
                return exact

        # 2) Similarity scoring (lexical + phonetic + token overlap)
        best_name = None
        best_score = 0.0
        for cleaned in candidates:
            for alias, game_name in self.normalized_alias_to_name.items():
                score = _similarity_score(cleaned, alias)
                if score > best_score:
                    best_score = score
                    best_name = game_name

        threshold = max(50, min(100, int(fuzzy_threshold)))
        if best_name is not None and best_score >= float(threshold):
            return best_name

        return None

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
        obj = _extract_first_json_object(reply_text)
        if obj is None:
            return None

        intent = _normalize_intent_label(str(obj.get("intent") or obj.get("type") or obj.get("label") or ""))

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


class IntentRouterEngine:
    def __init__(self, cfg: Config, resolver: ManifestAliasResolver) -> None:
        self.cfg = cfg
        self.resolver = resolver
        self.llm_classifier = LlmIntentClassifier(cfg, resolver)

    def route(self, text: str, corr_id: str) -> RouteDecision:
        if self.cfg.require_wake_word and not has_wake_word(text, self.cfg.wake_words):
            return RouteDecision(topic=None, payload=None, log_line=f"[intent] no wake word: {text}")

        llm_decision = self.llm_classifier.classify(text)
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

        # Semantic/phonetic fallback for BACK_HOME (no exact keyword hard-match).
        # This keeps "back", "go back", "return home" stable even when LLM is off
        # or confidence is below threshold.
        exit_score = _best_exit_similarity(text, self.cfg.exit_keywords)
        exit_threshold = max(50.0, min(95.0, float(self.cfg.back_home_similarity_threshold)))
        if exit_score >= exit_threshold:
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
        # Soft fallback without hard keyword rules: relevance + phonetic similarity only.
        game = self.resolver.resolve_best_name(text, self.cfg.fuzzy_threshold)
        if game:
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


class IntentService:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"intent-svc-{uuid.uuid4().hex[:8]}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self._stopping = False
        self._resolver = ManifestAliasResolver(cfg.manifest_path)
        self._router = IntentRouterEngine(cfg, self._resolver)

    def start(self) -> None:
        print(f"[intent] connecting to mqtt {self.cfg.host}:{self.cfg.port}")
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
        decision = self._router.route(text, corr_id)
        if decision.log_line:
            print(decision.log_line)
        if decision.topic and decision.payload is not None:
            self.client.publish(decision.topic, json.dumps(decision.payload))


def main() -> int:
    cfg = load_config()
    svc = IntentService(cfg)
    return run_service_loop(
        service_name="intent",
        start=svc.start,
        stop=svc.stop,
        interval_sec=0.5,
    )


if __name__ == "__main__":
    sys.exit(main())
