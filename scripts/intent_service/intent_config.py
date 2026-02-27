#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from common.config_utils import load_yaml_file, resolve_optional_path


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
    use_moonshine_intent_recognizer: bool = False
    moonshine_intent_threshold: float = 0.52
    moonshine_embedding_model: str = "embeddinggemma-300m"
    moonshine_embedding_variant: str = "q4"

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

    rules_use_moonshine = rules.get("use_moonshine_intent_recognizer")
    if rules_use_moonshine is not None:
        cfg.use_moonshine_intent_recognizer = _parse_bool_like(
            rules_use_moonshine,
            cfg.use_moonshine_intent_recognizer,
        )

    rules_moonshine_threshold = rules.get("moonshine_intent_threshold")
    if rules_moonshine_threshold is not None:
        cfg.moonshine_intent_threshold = _parse_float(
            str(rules_moonshine_threshold),
            cfg.moonshine_intent_threshold,
        )

    rules_moonshine_model = rules.get("moonshine_embedding_model")
    if rules_moonshine_model is not None:
        cfg.moonshine_embedding_model = (
            str(rules_moonshine_model).strip() or cfg.moonshine_embedding_model
        )

    rules_moonshine_variant = rules.get("moonshine_embedding_variant")
    if rules_moonshine_variant is not None:
        cfg.moonshine_embedding_variant = (
            str(rules_moonshine_variant).strip() or cfg.moonshine_embedding_variant
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
    cfg.use_moonshine_intent_recognizer = _parse_bool(
        os.environ.get("INTENT_USE_MOONSHINE_RECOGNIZER"),
        cfg.use_moonshine_intent_recognizer,
    )
    cfg.moonshine_intent_threshold = _parse_float(
        os.environ.get("INTENT_MOONSHINE_THRESHOLD"),
        cfg.moonshine_intent_threshold,
    )
    cfg.moonshine_embedding_model = (
        os.environ.get("INTENT_MOONSHINE_EMBEDDING_MODEL", cfg.moonshine_embedding_model).strip()
        or cfg.moonshine_embedding_model
    )
    cfg.moonshine_embedding_variant = (
        os.environ.get("INTENT_MOONSHINE_EMBEDDING_VARIANT", cfg.moonshine_embedding_variant).strip()
        or cfg.moonshine_embedding_variant
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
