from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import unicodedata

try:
    import numpy as np
except Exception:
    np = None  # type: ignore[assignment]

try:
    from .game_grounding import GameCard, GameCatalog, _GOAL_RULES, _LIMITATION_RULES, _ACTIVITY_LEVEL_SCORE
except Exception:
    from game_grounding import GameCard, GameCatalog, _GOAL_RULES, _LIMITATION_RULES, _ACTIVITY_LEVEL_SCORE

try:
    from onnx_embedder import OnnxTextEmbedder, cosine_similarity
except Exception:
    try:
        from .onnx_embedder import OnnxTextEmbedder, cosine_similarity
    except Exception:
        _ONNX_EMBEDDER_DIR = Path(__file__).resolve().parents[1] / "scripts" / "dialog_service"
        if _ONNX_EMBEDDER_DIR.exists():
            _dir_text = str(_ONNX_EMBEDDER_DIR)
            if _dir_text not in sys.path:
                sys.path.insert(0, _dir_text)
            try:
                from onnx_embedder import OnnxTextEmbedder, cosine_similarity
            except Exception:
                OnnxTextEmbedder = None  # type: ignore[assignment]
                cosine_similarity = None  # type: ignore[assignment]
        else:
            OnnxTextEmbedder = None  # type: ignore[assignment]
            cosine_similarity = None  # type: ignore[assignment]

try:
    from scripts.intent_service.match_utils import candidate_variants as _match_candidate_variants, similarity_score as _match_similarity_score
except Exception:
    _MATCH_UTILS_PATH = Path(__file__).resolve().parents[1] / "scripts" / "intent_service" / "match_utils.py"
    if _MATCH_UTILS_PATH.exists():
        _spec = importlib.util.spec_from_file_location("doc_rag_match_utils", str(_MATCH_UTILS_PATH))
        if _spec is not None and _spec.loader is not None:
            _module = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_module)
            _match_candidate_variants = getattr(_module, "candidate_variants", None)
            _match_similarity_score = getattr(_module, "similarity_score", None)
        else:
            _match_candidate_variants = None
            _match_similarity_score = None
    else:
        _match_candidate_variants = None
        _match_similarity_score = None


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_FRONT_MATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)
_QMD_JSON_RE = re.compile(r"<!--\s*QMD-DATA-BEGIN\s*-->.*?<!--\s*QMD-DATA-END\s*-->", re.DOTALL)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_WHITESPACE_RE = re.compile(r"\s+")
_ENTITY_REFERENCE_RE = re.compile(
    r"\b(it|its|them|their|they|his|her|that one|this one|the game|that game|this game|the other one|the lab|his lab|her lab|the research|their research|the team|their team)\b",
    re.IGNORECASE,
)
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "at",
    "be",
    "do",
    "does",
    "for",
    "games",
    "have",
    "how",
    "i",
    "in",
    "is",
    "manual",
    "me",
    "of",
    "or",
    "play",
    "please",
    "rules",
    "tell",
    "the",
    "to",
    "what",
    "which",
    "you",
}
_DOC_FORCE_PHRASES = (
    ("according to the docs", "docs_reference"),
    ("according to the doc", "docs_reference"),
    ("in the manual", "manual_reference"),
    ("what games do you have", "availability_query"),
    ("which games do you have", "availability_query"),
    ("available games", "availability_query"),
    ("recommend", "recommend_query"),
    ("suggest", "recommend_query"),
    ("compare", "compare_query"),
    ("difference", "compare_query"),
    ("versus", "compare_query"),
    (" vs ", "compare_query"),
    ("how to play", "how_to_query"),
    ("rules", "how_to_query"),
)
_SOCIAL_PATTERNS = (
    "hello",
    "hi",
    "can you hear me",
    "how are you",
    "thank you",
    "thanks",
    "tell me something fun",
    "introduce yourself",
    "who are you",
)
_AVAILABILITY_HINTS = (
    "what games",
    "which games",
    "games do you have",
    "available games",
    "what game do you have",
)
_RECOMMEND_HINTS = ("recommend", "suggest", "good for", "which should", "best game")
_COMPARE_HINTS = ("compare", "difference", "versus", " vs ", "better than")
_HOW_TO_HINTS = ("how to play", "rules", "how do i play", "how do you play")
_FACTUAL_HINTS = (
    "launchable",
    "available",
    "how many players",
    "player",
    "players",
    "tag",
    "tags",
    "activity level",
)
_INTRODUCE_HINTS = ("tell me about", "what is", "what's", "describe", "introduce", "what do you know about", "do you know")
_GENERAL_FACTUAL_QUERY_PREFIXES = (
    "what is ",
    "what's ",
    "who is ",
    "where is ",
    "what does ",
    "does ",
    "what projects",
    "what equipment",
    "what tools",
    "which devices",
    "tell me about ",
    "what do you know about ",
    "do you know ",
)
_GENERAL_DOC_TERMS = (
    "lab",
    "laboratory",
    "project",
    "projects",
    "publication",
    "publications",
    "equipment",
    "tool",
    "tools",
    "device",
    "devices",
    "director",
    "member",
    "members",
    "team",
    "research",
    "researcher",
    "researchers",
    "robotics",
    "contact",
    "location",
)
_GENERAL_TOPIC_TERMS = (
    "equipment",
    "tool",
    "tools",
    "device",
    "devices",
    "project",
    "projects",
    "publication",
    "publications",
    "director",
    "member",
    "members",
    "team",
    "robotics",
    "contact",
    "location",
    "researcher",
    "researchers",
)
_GENERAL_FOCUS_VALUES = ("overview", "people", "research", "equipment", "location_contact", "news")
_GENERAL_KEYWORD_RULES = (
    {
        "name": "equipment",
        "query_terms": (" equipment ", " tool ", " tools ", " device ", " devices "),
        "field_terms": ("equipment", "tools", "devices"),
        "snippet_terms": ("eeg", "emg", "sensor", "headset", "kinect", "device", "kit", "tools"),
        "affinity_boost": 0.18,
        "binding_boost": 0.18,
        "answer_boost": 0.26,
    },
    {
        "name": "robotics",
        "query_terms": (" robotics ", " social robotics ", " hri ", " human robot interaction "),
        "field_terms": ("robotics", "hri", "research"),
        "snippet_terms": ("social robotics", "human robot interaction", "robo ludens", "robot"),
        "affinity_boost": 0.14,
        "binding_boost": 0.16,
        "answer_boost": 0.18,
    },
    {
        "name": "people",
        "query_terms": (
            " director ",
            " team ",
            " member ",
            " members ",
            " collaborator ",
            " collaborators ",
            " alumni ",
            " researcher ",
            " researchers ",
            " who works there ",
            " who do they have ",
        ),
        "field_terms": ("team", "director", "member", "collaborator", "alumni"),
        "snippet_terms": ("director", "assistant professor", "member", "collaborator", "alumni", "researcher"),
        "affinity_boost": 0.14,
        "binding_boost": 0.14,
        "answer_boost": 0.2,
    },
    {
        "name": "projects",
        "query_terms": (" project ", " projects ", " publication ", " publications ", " research "),
        "field_terms": ("research", "project", "projects", "publication", "publications"),
        "snippet_terms": ("research", "project", "publication", "study", "theme"),
        "affinity_boost": 0.14,
        "binding_boost": 0.14,
        "answer_boost": 0.2,
    },
    {
        "name": "contact",
        "query_terms": (" contact ", " email ", " phone "),
        "field_terms": ("contact", "email", "phone"),
        "snippet_terms": ("contact", "email", "phone"),
        "affinity_boost": 0.16,
        "binding_boost": 0.14,
        "answer_boost": 0.2,
    },
    {
        "name": "location",
        "query_terms": (" location ", " where is ", " address ", " located "),
        "field_terms": ("location", "address"),
        "snippet_terms": ("location", "address", "brantford", "ontario", "darling"),
        "affinity_boost": 0.16,
        "binding_boost": 0.14,
        "answer_boost": 0.22,
    },
)
_GAME_DOMAINS = {"game", "mixed"}
_DEFAULT_DOC_ROOT = Path(__file__).resolve().parents[1] / "runtime" / "qmd"
_DEFAULT_TOP_K = 4
_DEFAULT_FTS_LIMIT = 20
_DEFAULT_DENSE_LIMIT = 20
_DEFAULT_ROUTING_THRESHOLD = 0.56
_DEFAULT_ANSWER_THRESHOLD = 0.54
_ENTITY_REGISTRY_SCHEMA_VERSION = "resolver-v3"
_PERSON_LOOKUP_PREFIXES = ("who is ", "tell me about ", "what do you know about ", "do you know ")
_PERSON_DOC_HINTS = ("team", "director", "member", "people", "person", "biography")
_GENERAL_DOC_FAILURE_TEXT = "I could not confirm that from my local documents."
_ENTITY_FOCUS_SOURCES = {"answer", "launch_command"}
_GENERAL_DOC_KINDS = {"identity", "research", "team", "equipment", "news", "location_contact", "general"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _env_int(name: str, default: int, *, floor: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None:
        return max(floor, default)
    try:
        return max(floor, int(raw))
    except Exception:
        return max(floor, default)


def _env_float(name: str, default: float, *, floor: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None:
        return max(floor, default)
    try:
        return max(floor, float(raw))
    except Exception:
        return max(floor, default)


def _clean_text(value: str, *, max_len: int = 240) -> str:
    compact = " ".join(str(value or "").strip().split())
    if not compact:
        return ""
    if len(compact) <= max_len:
        return compact
    return compact[: max(1, int(max_len))].rstrip()


def _ascii_fold(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text)
    return normalized.encode("ascii", "ignore").decode("ascii", errors="ignore")


def _normalize_text(value: str) -> str:
    lowered = _clean_text(_ascii_fold(value), max_len=512).lower()
    lowered = _NON_ALNUM_RE.sub(" ", lowered)
    return _WHITESPACE_RE.sub(" ", lowered).strip()


def _tokenize(value: str) -> List[str]:
    normalized = _normalize_text(value)
    if not normalized:
        return []
    return [token for token in normalized.split(" ") if token]


def _query_keywords(query_text: str) -> List[str]:
    tokens = [token for token in _tokenize(query_text) if len(token) >= 2 and token not in _STOPWORDS]
    if not tokens:
        tokens = [token for token in _tokenize(query_text) if len(token) >= 2]
    unique: List[str] = []
    seen = set()
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        unique.append(token)
    return unique[:10]


def _match_similarity(candidate: str, alias: str) -> float:
    if callable(_match_similarity_score):
        try:
            return float(_match_similarity_score(candidate, alias)) / 100.0
        except Exception:
            return 0.0
    left = _normalize_text(candidate)
    right = _normalize_text(alias)
    if not left or not right:
        return 0.0
    try:
        import difflib

        return float(difflib.SequenceMatcher(None, left, right).ratio())
    except Exception:
        return 0.0


def _candidate_variants(text: str) -> List[str]:
    if callable(_match_candidate_variants):
        try:
            return [str(item).strip() for item in _match_candidate_variants(text) if str(item).strip()]
        except Exception:
            pass
    normalized = _normalize_text(text)
    return [normalized] if normalized else []


def _looks_social(query_text: str) -> bool:
    normalized = _normalize_text(query_text)
    return any(pattern in normalized for pattern in _SOCIAL_PATTERNS)


def _force_doc_reason(query_text: str, *, explicit_entities: Sequence[str], has_property_question: bool) -> str:
    normalized = f" {_normalize_text(query_text)} "
    if explicit_entities and has_property_question:
        return "entity_property_query"
    for pattern, reason in _DOC_FORCE_PHRASES:
        if pattern in normalized:
            return reason
    return ""


def _is_factual_query(query_text: str) -> bool:
    normalized = f" {_normalize_text(query_text)} "
    return any(hint in normalized for hint in _FACTUAL_HINTS)


def _is_interrogative_query(query_text: str) -> bool:
    raw = str(query_text or "").strip()
    normalized = _normalize_text(query_text)
    if not normalized:
        return False
    if "?" in raw:
        return True
    return any(
        normalized.startswith(prefix)
        for prefix in ("what ", "who ", "where ", "when ", "why ", "how ", "does ", "do ", "which ", "tell me ")
    )


def _general_doc_query_shape_score(query_text: str) -> float:
    normalized = _normalize_text(query_text)
    padded = f" {normalized} "
    score = 0.0
    if any(normalized.startswith(prefix) for prefix in _GENERAL_FACTUAL_QUERY_PREFIXES):
        score += 0.26
    if any(term in padded for term in (f" {term} " for term in _GENERAL_DOC_TERMS)):
        score += 0.12
    if _is_interrogative_query(query_text):
        score += 0.08
    return min(0.4, score)


def _general_topic_terms(query_text: str) -> List[str]:
    normalized = f" {_normalize_text(query_text)} "
    return [term for term in _GENERAL_TOPIC_TERMS if f" {term} " in normalized]


def _general_keyword_rule_hits(query_text: str) -> List[Dict[str, Any]]:
    normalized = f" {_normalize_text(query_text)} "
    hits: List[Dict[str, Any]] = []
    for rule in _GENERAL_KEYWORD_RULES:
        if any(term in normalized for term in rule.get("query_terms", ())):
            hits.append(rule)
    return hits


def _should_skip_general_doc_path(path: Path) -> bool:
    stem = _normalize_text(path.stem)
    name = _normalize_text(path.name)
    if stem == "readme" or name == "readme md":
        return True
    if "query examples" in stem or "query example" in stem:
        return True
    return False


def _is_news_like_text(value: str) -> bool:
    normalized = f" {_normalize_text(value)} "
    return any(marker in normalized for marker in (" news ", " update ", " updates "))


def _strip_person_suffixes(value: str) -> str:
    text = _clean_text(value, max_len=120)
    if not text:
        return ""
    text = re.sub(r",?\s*(?:ph\.?d\.?|msc|m\.sc\.?|ma|m\.a\.?|bsc|b\.sc\.?|professor|prof\.?)\b.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)(?:\.\s*(?:role|academic position|background|project|projects)\b.*$|\s*-\s*(?:master|undergraduate|assistant|distinguished)\b.*$)",
        "",
        text,
    )
    return _clean_text(text, max_len=120)


def _looks_like_named_entity(value: str) -> bool:
    text = _clean_text(value, max_len=120)
    if not text:
        return False
    tokens = [token for token in re.split(r"\s+", text) if token]
    if not tokens:
        return False
    return any(token[:1].isupper() for token in tokens)


def _looks_like_person_name(value: str) -> bool:
    text = _strip_person_suffixes(value)
    if not text:
        return False
    normalized = f" {_normalize_text(text)} "
    if any(term in normalized for term in (" lab ", " interface ", " research ", " project ", " equipment ", " team ")):
        return False
    tokens = [token for token in re.split(r"\s+", text) if token]
    if len(tokens) < 2:
        return False
    alphabetic = [token for token in tokens if re.search(r"[A-Za-z]", token)]
    return len(alphabetic) >= 2 and all(token[:1].isupper() or len(token) == 1 for token in alphabetic[:3])


def _resolver_query_key(value: str) -> str:
    return _normalize_text(value)


def _replace_query_span(query_text: str, replacement: str) -> str:
    raw = _clean_text(query_text, max_len=240)
    normalized = _normalize_text(raw)
    if normalized.startswith("who is "):
        return f"Who is {replacement}?"
    if normalized.startswith("tell me about "):
        return f"Tell me about {replacement}."
    if normalized.startswith("what is "):
        return f"What is {replacement}?"
    if normalized.startswith("where is "):
        return f"Where is {replacement}?"
    if normalized.startswith("does "):
        return raw
    return replacement


def _extract_person_query_fragment(query_text: str) -> str:
    normalized = _normalize_text(query_text)
    for prefix in _PERSON_LOOKUP_PREFIXES:
        if normalized.startswith(prefix):
            fragment = normalized[len(prefix) :]
            fragment = re.sub(r"\b(from|at|in)\b.*$", "", fragment).strip()
            return fragment
    return ""


def _person_lookup_fragment(query_text: str) -> str:
    fragment = _extract_person_query_fragment(query_text)
    if not fragment:
        return ""
    tokens = [token for token in _tokenize(fragment) if token]
    if not tokens or len(tokens) > 4:
        return ""
    padded = f" {fragment} "
    if any(term in padded for term in (" lab ", " interface ", " game ", " project ", " equipment ", " device ", " research ", " robotics ")):
        return ""
    return fragment


def _person_lookup_query(query_text: str) -> bool:
    return bool(_person_lookup_fragment(query_text))


def _entity_lookup_fragment(query_text: str) -> str:
    normalized = _normalize_text(query_text)
    for prefix in ("what is ", "what's ", "who is ", "where is ", "tell me about ", "what do you know about ", "do you know "):
        if normalized.startswith(prefix):
            fragment = normalized[len(prefix) :]
            fragment = re.sub(r"\b(from|at|in)\b.*$", "", fragment).strip()
            return fragment
    return ""


def _entity_lookup_query(query_text: str) -> bool:
    return bool(_entity_lookup_fragment(query_text))


def _should_attempt_entity_resolution(query_text: str) -> bool:
    normalized = _normalize_text(query_text)
    if not normalized or _looks_social(query_text):
        return False
    if _person_lookup_query(query_text):
        return True
    if _entity_lookup_query(query_text):
        fragment = _entity_lookup_fragment(query_text)
        if not fragment:
            return False
        padded_fragment = f" {fragment} "
        if any(f" {term} " in padded_fragment for term in _GENERAL_TOPIC_TERMS):
            return False
        return True
    raw = _clean_text(query_text, max_len=200)
    raw_tokens = [token for token in re.split(r"\s+", raw) if token]
    inner_capitalized = sum(1 for token in raw_tokens[1:] if token[:1].isupper())
    if inner_capitalized >= 1 and _is_interrogative_query(query_text):
        return True
    return False


def _strip_front_matter(text: str) -> str:
    return _FRONT_MATTER_RE.sub("", text or "", count=1)


def _strip_qmd_json(text: str) -> str:
    return _QMD_JSON_RE.sub("", text or "")


def _parse_front_matter(text: str) -> Dict[str, str]:
    raw = str(text or "")
    if not raw.startswith("---"):
        return {}
    match = _FRONT_MATTER_RE.match(raw)
    if not match:
        return {}
    payload = raw[3 : match.end() - 4]
    out: Dict[str, str] = {}
    for line in payload.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        clean_key = _normalize_text(key).replace(" ", "_")
        clean_value = _clean_text(value, max_len=240)
        if clean_key and clean_value:
            out[clean_key] = clean_value
    return out


def _safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        try:
            return path.read_text(encoding="utf-8-sig")
        except Exception:
            return ""


def _general_doc_kind(path: Path, *, front_matter: Optional[Dict[str, str]] = None, title: str = "") -> str:
    front_matter = front_matter or {}
    combined = " ".join(
        (
            str(path.stem or ""),
            str(front_matter.get("section") or ""),
            str(front_matter.get("title") or ""),
            str(title or ""),
        )
    )
    normalized = f" {_normalize_text(combined)} "
    if any(term in normalized for term in (" news ", " updates ", " update ")):
        return "news"
    if any(term in normalized for term in (" team ", " director ", " member ", " members ", " collaborator ", " alumni ")):
        return "team"
    if any(term in normalized for term in (" equipment ", " tool ", " tools ", " device ", " devices ", " sensor ", " kit ")):
        return "equipment"
    if any(term in normalized for term in (" contact ", " location ", " address ", " phone ", " email ")):
        return "location_contact"
    if any(term in normalized for term in (" identity ", " overview ", " lab identity ", " lab overview ")):
        return "identity"
    if any(term in normalized for term in (" research ", " project ", " projects ", " publication ", " publications ")):
        return "research"
    return "general"


def _general_doc_kind_for_hit(hit: RetrievalHit) -> str:
    metadata = getattr(hit, "metadata", {}) or {}
    kind = _clean_text(str(metadata.get("general_doc_kind") or ""), max_len=32).lower().replace(" ", "_")
    if kind in _GENERAL_DOC_KINDS:
        return kind
    return _general_doc_kind(
        Path(str(hit.source_path or "")),
        front_matter={"section": str(metadata.get("section") or ""), "title": str(metadata.get("title") or "")},
        title=str(hit.title or ""),
    )


def _extract_person_names(value: str) -> List[str]:
    text = _ascii_fold(str(value or "")).replace("鈥", "-").replace("?", " ")
    out: List[str] = []
    seen = set()
    patterns = (
        r"\bDirector:\s*([A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){1,3})",
        r"(?:^|\n)\s*Name:\s*([A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){1,3})",
        r"(?:^|\n|\s-\s)([A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){1,3})\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            name = _strip_person_suffixes(match.group(1))
            if not _looks_like_person_name(name):
                continue
            folded = name.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            out.append(name)
    return out[:12]


def _extract_lab_names(value: str) -> List[str]:
    text = str(value or "")
    out: List[str] = []
    seen = set()
    explicit = _clean_text(_extract_label_value(text, "Lab name"), max_len=120)
    if explicit:
        seen.add(explicit.casefold())
        out.append(explicit)
    for match in re.finditer(r"\b([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,5}\s+Lab)\b", text):
        name = _clean_text(match.group(1), max_len=120)
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        out.append(name)
    return out[:6]


def _extract_project_names(value: str) -> List[str]:
    text = str(value or "")
    out: List[str] = []
    seen = set()
    for match in re.finditer(r"\bProject:\s*([A-Z][A-Za-z0-9&'.-]+(?:\s+[A-Z][A-Za-z0-9&'.-]+){0,6})", text):
        name = _clean_text(match.group(1), max_len=120)
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        out.append(name)
    return out[:6]


def _extract_label_value(text: str, label: str) -> str:
    raw = str(text or "")
    patterns = (
        rf"\b{re.escape(label)}:\s*(.+?)(?=\s+[A-Z][A-Za-z ]+:\s|\s-\s[A-Z]|$)",
        rf"\b{re.escape(label)}:\s*([^.]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if match:
            return _clean_text(match.group(1).strip(" ."), max_len=220)
    return ""


def _extract_field_sentence(text: str, label: str) -> str:
    value = _extract_label_value(text, label)
    if not value:
        return ""
    value = re.sub(r"\s*-\s*$", "", value).strip()
    return value


def _split_markdown_sections(text: str, *, fallback_title: str) -> List[Tuple[str, str]]:
    clean = _strip_qmd_json(_strip_front_matter(text))
    lines = clean.splitlines()
    sections: List[Tuple[str, str]] = []
    stack: List[str] = []
    current: List[str] = []

    def _flush(buffer: List[str]) -> None:
        body = "\n".join(buffer).strip()
        if not body:
            return
        section_path = " > ".join(stack) if stack else fallback_title
        sections.append((section_path or fallback_title, body))

    for raw_line in lines:
        line = raw_line.rstrip()
        match = _HEADING_RE.match(line)
        if match:
            _flush(current)
            current = []
            level = len(match.group(1))
            title = _clean_text(match.group(2), max_len=120)
            if not title:
                continue
            while len(stack) >= level:
                stack.pop()
            stack.append(title)
            continue
        current.append(line)
    _flush(current)
    if sections:
        return sections
    plain = clean.strip()
    return [(fallback_title, plain)] if plain else []


def _chunk_text(text: str, *, target_chars: int = 900, overlap_chars: int = 150) -> List[str]:
    clean = _clean_text(text, max_len=12000)
    if not clean:
        return []
    if len(clean) <= target_chars:
        return [clean]
    out: List[str] = []
    start = 0
    while start < len(clean):
        end = min(len(clean), start + target_chars)
        if end < len(clean):
            boundary = clean.rfind(" ", start + max(40, target_chars - 160), end)
            if boundary > start + 120:
                end = boundary
        chunk = clean[start:end].strip()
        if chunk:
            out.append(chunk)
        if end >= len(clean):
            break
        start = max(end - overlap_chars, start + 1)
    return out


def _build_match_query(query_text: str) -> str:
    tokens = _query_keywords(query_text)
    if not tokens:
        return ""
    unique: List[str] = []
    seen = set()
    for token in tokens[:8]:
        if token in seen:
            continue
        seen.add(token)
        unique.append(token)
    if not unique:
        return ""
    return " OR ".join(f'"{token}"' for token in unique)


def _players_text(card: GameCard) -> str:
    if int(card.players_min) == int(card.players_max):
        return f"{int(card.players_min)} player" + ("" if int(card.players_min) == 1 else "s")
    return f"{int(card.players_min)} to {int(card.players_max)} players"


def _normalized_tags(card: GameCard) -> set[str]:
    return {_normalize_text(tag) for tag in getattr(card, "tags", []) or [] if _normalize_text(tag)}


def _activity_level(card: GameCard) -> str:
    value = _normalize_text(str(getattr(card, "activity_level", "") or ""))
    return value if value in _ACTIVITY_LEVEL_SCORE else ""


def _card_matches_values(card: GameCard, values: Sequence[str]) -> bool:
    normalized_pool = {
        _normalize_text(str(card.name or "")),
        _normalize_text(str(card.game_id or "")),
        *[_normalize_text(alias) for alias in getattr(card, "aliases", []) or []],
        *[_normalize_text(tag) for tag in getattr(card, "tags", []) or []],
    }
    for value in values:
        normalized_value = _normalize_text(str(value or ""))
        if normalized_value and normalized_value in normalized_pool:
            return True
    return False


def _text_contains_keyword(text: str, keyword: str) -> bool:
    normalized_text = f" {_normalize_text(text)} "
    normalized_keyword = f" {_normalize_text(keyword)} "
    return bool(normalized_keyword.strip()) and normalized_keyword in normalized_text


def _matching_rules(profile_texts: Sequence[str], rules: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    hits: List[Dict[str, Any]] = []
    for rule in rules:
        keywords = {str(keyword).strip().lower() for keyword in rule.get("keywords", set()) if str(keyword).strip()}
        if not keywords:
            continue
        if any(_text_contains_keyword(text, keyword) for text in profile_texts for keyword in keywords):
            hits.append(dict(rule))
    return hits


def _score_goal_or_limitation_rules_local(
    *,
    card: GameCard,
    rules: Sequence[Dict[str, Any]],
    profile_texts: Sequence[str],
) -> Tuple[float, List[Tuple[float, str]]]:
    tags = _normalized_tags(card)
    level = _activity_level(card)
    score = 0.0
    reason_candidates: List[Tuple[float, str]] = []
    for rule in _matching_rules(profile_texts, rules):
        boost_tags = {_normalize_text(tag) for tag in rule.get("boost_tags", set()) if _normalize_text(tag)}
        penalty_tags = {_normalize_text(tag) for tag in rule.get("penalty_tags", set()) if _normalize_text(tag)}
        boost_levels = {_normalize_text(item) for item in rule.get("boost_levels", set()) if _normalize_text(item)}
        penalty_levels = {_normalize_text(item) for item in rule.get("penalty_levels", set()) if _normalize_text(item)}
        weight = float(rule.get("weight") or 0.0)
        hit = False
        if boost_tags and tags.intersection(boost_tags):
            score += weight
            hit = True
        if boost_levels and level in boost_levels:
            score += max(0.8, weight * 0.7)
            hit = True
        if penalty_tags and tags.intersection(penalty_tags):
            score -= max(0.8, weight)
        if penalty_levels and level in penalty_levels:
            score -= max(0.8, weight * 0.8)
        if hit:
            reason = str(rule.get("reason") or "").strip()
            if reason:
                reason_candidates.append((weight, reason))
    return score, reason_candidates


def _history_adjustment(card: GameCard, recent_games: Sequence[Dict[str, Any]], wants_variety: bool) -> Tuple[float, List[Tuple[float, str]]]:
    if not recent_games:
        return 0.0, []
    score = 0.0
    reasons: List[Tuple[float, str]] = []
    recent_names = [_normalize_text(str(item.get("game_name") or "")) for item in recent_games if isinstance(item, dict)]
    recent_names = [item for item in recent_names if item]
    if not recent_names:
        return 0.0, []
    card_names = {
        _normalize_text(str(card.name or "")),
        _normalize_text(str(card.game_id or "")),
        *[_normalize_text(alias) for alias in getattr(card, "aliases", []) or []],
    }
    last_game = recent_names[-1]
    if last_game in card_names:
        penalty = 2.8 if wants_variety else 1.9
        score -= penalty
    else:
        bonus = 1.1 if wants_variety else 0.8
        score += bonus
        reason = "it gives you a change from the last game" if wants_variety else "it gives you a change from your most recent game"
        reasons.append((bonus, reason))
    if sum(1 for item in recent_names[-3:] if item in card_names) >= 2:
        score -= 1.2
    return score, reasons


def _contrast_reason_local(candidate: GameCard, reference: GameCard) -> str:
    candidate_tags = _normalized_tags(candidate)
    reference_tags = _normalized_tags(reference)
    candidate_level = _ACTIVITY_LEVEL_SCORE.get(_activity_level(candidate), 0.0)
    reference_level = _ACTIVITY_LEVEL_SCORE.get(_activity_level(reference), 0.0)
    if "walking" in candidate_tags and "stationary" in reference_tags:
        return f"It adds more walking and movement than {reference.name}."
    if candidate_level > reference_level:
        return f"It is the more active option compared with {reference.name}."
    if candidate_level < reference_level:
        return f"It is the gentler option compared with {reference.name}."
    if "balance" in candidate_tags and "balance" not in reference_tags:
        return f"It adds more balance and movement than {reference.name}."
    return f"It is the other local option besides {reference.name}."


@dataclass
class DocChunk:
    chunk_id: str
    doc_id: str
    source_path: str
    domain: str
    doc_type: str
    title: str
    section_path: str
    entity_name: str
    search_text: str
    snippet_text: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RetrievalHit:
    chunk_id: str
    doc_id: str
    source_path: str
    domain: str
    doc_type: str
    title: str
    section_path: str
    entity_name: str
    snippet_text: str
    metadata: Dict[str, Any]
    fused_score: float = 0.0
    sparse_rank: int = 0
    dense_rank: int = 0
    sparse_score: float = 0.0
    dense_score: float = 0.0


@dataclass
class EntityAliasSpec:
    value: str
    alias_strength: str = "strong"
    alias_origin: str = "auto_generated"


@dataclass
class EntityRegistryEntry:
    canonical: str
    domain: str
    entity_type: str
    prominence_score: float = 0.0
    source_kind: str = ""
    aliases: List[EntityAliasSpec] = field(default_factory=list)


@dataclass
class EntityResolverCandidate:
    canonical: str
    domain: str
    entity_type: str
    fused_score: float
    string_similarity: float = 0.0
    partial_name_score: float = 0.0
    embedding_similarity: float = 0.0
    retrieval_corroboration: float = 0.0
    prominence_score: float = 0.0
    alias_strength: str = "strong"
    resolution_mode: str = "none"
    resolver_reason: str = ""


@dataclass
class DocProbe:
    query: str
    stage1_result: str = "not_doc"
    stage2_result: str = ""
    query_doc_affinity: float = 0.0
    retrieval_support: float = 0.0
    entity_binding_strength: float = 0.0
    stage1_reason: str = ""
    force_doc_reason: Optional[str] = None
    domain: str = "unknown"
    answer_mode: str = ""
    general_focus: str = ""
    routing_confidence: float = 0.0
    answerability_confidence: float = 0.0
    doc_confidence: float = 0.0
    top_hit_ids: List[str] = field(default_factory=list)
    selected_evidence_ids: List[str] = field(default_factory=list)
    fallback_reason: str = ""
    clarify_kind: str = ""
    candidate_entities: List[str] = field(default_factory=list)
    payload: Dict[str, Any] = field(default_factory=dict)
    response_text: str = ""
    resolver_attempted: bool = False
    normalized_query: str = ""
    retrieval_queries: List[str] = field(default_factory=list)
    entity_candidates: List[str] = field(default_factory=list)
    entity_registry_hits: List[Dict[str, Any]] = field(default_factory=list)
    candidate_entity_rewrites: List[str] = field(default_factory=list)
    entity_similarity_score: float = 0.0
    entity_resolution_confidence: float = 0.0
    rewritten_query: str = ""
    resolution_mode: str = "none"
    resolver_reason: str = ""
    open_world_fallback_blocked: bool = False
    doc_failure_mode: str = ""
    general_doc_kinds: List[str] = field(default_factory=list)

    def telemetry(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "stage1_result": self.stage1_result,
            "query_doc_affinity": round(float(self.query_doc_affinity), 4),
            "retrieval_support": round(float(self.retrieval_support), 4),
            "entity_binding_strength": round(float(self.entity_binding_strength), 4),
            "stage1_reason": self.stage1_reason,
            "force_doc_reason": self.force_doc_reason or "",
            "stage2_result": self.stage2_result,
            "domain": self.domain,
            "answer_mode": self.answer_mode,
            "general_focus": self.general_focus,
            "routing_confidence": round(float(self.routing_confidence), 4),
            "answerability_confidence": round(float(self.answerability_confidence), 4),
            "doc_confidence": round(float(self.doc_confidence), 4),
            "top_hit_ids": list(self.top_hit_ids),
            "selected_evidence_ids": list(self.selected_evidence_ids),
            "fallback_reason": self.fallback_reason,
            "clarify_kind": self.clarify_kind,
            "resolver_attempted": bool(self.resolver_attempted),
            "normalized_query": self.normalized_query,
            "retrieval_queries": list(self.retrieval_queries),
            "entity_candidates": list(self.entity_candidates),
            "entity_registry_hits": list(self.entity_registry_hits),
            "candidate_entity_rewrites": list(self.candidate_entity_rewrites),
            "entity_similarity_score": round(float(self.entity_similarity_score), 4),
            "entity_resolution_confidence": round(float(self.entity_resolution_confidence), 4),
            "rewritten_query": self.rewritten_query,
            "resolution_mode": self.resolution_mode,
            "resolver_reason": self.resolver_reason,
            "open_world_fallback_blocked": bool(self.open_world_fallback_blocked),
            "doc_failure_mode": self.doc_failure_mode,
            "general_doc_kinds": list(self.general_doc_kinds),
        }


class LocalDocsRAG:
    def __init__(
        self,
        *,
        manifest_path: Path,
        game_catalog: Optional[GameCatalog],
        doc_root: Optional[Path] = None,
        embedder: Optional[OnnxTextEmbedder] = None,
    ) -> None:
        self.manifest_path = Path(manifest_path)
        self.game_catalog = game_catalog
        self.doc_root = Path(doc_root) if doc_root is not None else Path(os.getenv("DOC_RAG_ROOT", "") or _DEFAULT_DOC_ROOT)
        self.doc_root = self.doc_root.expanduser()
        self.index_dir = self.doc_root / ".doc_rag"
        self.sqlite_path = self.index_dir / "chunks.sqlite3"
        self.chunk_path = self.index_dir / "chunks.json"
        self.vector_path = self.index_dir / "vectors.npy"
        self.meta_path = self.index_dir / "meta.json"
        self.entity_registry_path = self.index_dir / "entity_registry.json"
        self.alias_override_path = self.doc_root / "docs" / "entity_aliases.json"
        self.enabled = _env_bool("DOC_RAG_ENABLE", True)
        self.include_games = _env_bool("DOC_RAG_INCLUDE_GAMES", True)
        self.top_k = _env_int("DOC_RAG_TOP_K", _DEFAULT_TOP_K, floor=1)
        self.fts_limit = _env_int("DOC_RAG_FTS_LIMIT", _DEFAULT_FTS_LIMIT, floor=4)
        self.dense_limit = _env_int("DOC_RAG_DENSE_LIMIT", _DEFAULT_DENSE_LIMIT, floor=4)
        self.routing_threshold = _env_float("DOC_RAG_ROUTING_THRESHOLD", _DEFAULT_ROUTING_THRESHOLD, floor=0.0)
        self.answer_threshold = _env_float("DOC_RAG_ANSWER_THRESHOLD", _DEFAULT_ANSWER_THRESHOLD, floor=0.0)
        self.embedder = embedder or self._build_embedder()
        self.ready = False
        self.error = ""
        self._conn: Optional[sqlite3.Connection] = None
        self._chunks_by_id: Dict[str, DocChunk] = {}
        self._vectors = None
        self._entity_registry: List[EntityRegistryEntry] = []
        self._entity_vectors: Dict[str, Any] = {}
        self._build_or_load()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None

    def _dense_runtime_ready(self) -> bool:
        return bool(
            self.embedder is not None
            and getattr(self.embedder, "ready", False)
            and np is not None
            and cosine_similarity is not None
        )

    def _dense_index_ready(self) -> bool:
        return bool(self._dense_runtime_ready() and self._vectors is not None)

    def diagnostics(self) -> Dict[str, Any]:
        general_chunks = sum(1 for chunk in self._chunks_by_id.values() if chunk.domain == "general")
        game_chunks = sum(1 for chunk in self._chunks_by_id.values() if chunk.domain in _GAME_DOMAINS)
        docs_dir = self.doc_root / "docs"
        general_source_files = list(self._iter_general_doc_paths())
        dense_error = ""
        if not self._dense_index_ready():
            dense_error = getattr(self.embedder, "error", "") or (
                "doc_rag embedder unavailable" if not self._dense_runtime_ready() else "doc_rag dense index unavailable"
            )
        return {
            "doc_root": str(self.doc_root),
            "docs_dir": str(docs_dir),
            "docs_dir_exists": bool(docs_dir.exists()),
            "general_source_files": len(general_source_files),
            "general_chunk_count": int(general_chunks),
            "game_chunk_count": int(game_chunks),
            "entity_registry_count": len(self._entity_registry),
            "dense_ready": bool(self._dense_index_ready()),
            "dense_error": dense_error,
            "ready": bool(self.ready),
            "error": str(self.error or ""),
        }

    def game_names(self) -> List[str]:
        if self.game_catalog is None:
            return []
        return [str(card.name).strip() for card in getattr(self.game_catalog, "cards", []) if str(card.name).strip()]

    def _build_embedder(self) -> Optional[OnnxTextEmbedder]:
        if not self.enabled or OnnxTextEmbedder is None:
            return None
        try:
            embedder_name = (os.getenv("DOC_RAG_EMBEDDER", "bge") or "bge").strip().lower()
            return OnnxTextEmbedder(
                embedder=embedder_name,
                repo_id=(os.getenv("DOC_RAG_EMBEDDING_REPO_ID", "") or "").strip(),
                model_dir=(os.getenv("DOC_RAG_EMBEDDING_MODEL_DIR", "") or "").strip(),
                model_file=(os.getenv("DOC_RAG_EMBEDDING_MODEL_FILE", "") or "").strip(),
                tokenizer_file=(os.getenv("DOC_RAG_EMBEDDING_TOKENIZER_FILE", "") or "").strip(),
                max_length=_env_int("DOC_RAG_EMBEDDING_MAX_LENGTH", 256, floor=16),
                auto_download=_env_bool("DOC_RAG_EMBEDDING_AUTO_DOWNLOAD", True),
                cache_dir=(os.getenv("DOC_RAG_EMBEDDING_CACHE_DIR", "") or "").strip(),
                query_prefix=(os.getenv("DOC_RAG_QUERY_PREFIX", "") or "").strip(),
                doc_prefix=(os.getenv("DOC_RAG_DOC_PREFIX", "") or "").strip(),
            )
        except Exception:
            return None

    def _iter_general_doc_paths(self) -> Iterable[Path]:
        base = self.doc_root / "docs"
        if not base.exists():
            return []
        paths: List[Path] = []
        for pattern in ("*.qmd", "*.md", "*.txt"):
            for path in sorted(base.rglob(pattern)):
                if _should_skip_general_doc_path(path):
                    continue
                paths.append(path)
        return paths

    def _iter_game_doc_paths(self) -> Iterable[Path]:
        seen = set()
        paths: List[Path] = []
        for candidate_root in (self.doc_root / "games", getattr(self.game_catalog, "qmd_games_dir", None)):
            if candidate_root is None:
                continue
            root_path = Path(candidate_root)
            if not root_path.exists():
                continue
            for pattern in ("*.qmd", "*.md", "*.txt"):
                for path in sorted(root_path.rglob(pattern)):
                    key = str(path.resolve())
                    if key in seen:
                        continue
                    seen.add(key)
                    paths.append(path)
        return paths

    @staticmethod
    def _entity_key(canonical: str, domain: str, entity_type: str) -> str:
        return "|".join((_normalize_text(canonical), _normalize_text(domain), _normalize_text(entity_type)))

    @staticmethod
    def _alias_value_set(entry: EntityRegistryEntry) -> set[str]:
        return {_normalize_text(alias.value) for alias in entry.aliases if _normalize_text(alias.value)}

    def _register_entity(
        self,
        registry: Dict[str, EntityRegistryEntry],
        *,
        canonical: str,
        domain: str,
        entity_type: str,
        prominence_score: float,
        source_kind: str,
    ) -> EntityRegistryEntry:
        clean_canonical = _clean_text(canonical, max_len=120)
        if not clean_canonical:
            raise ValueError("canonical entity required")
        key = self._entity_key(clean_canonical, domain, entity_type)
        existing = registry.get(key)
        if existing is not None:
            existing.prominence_score = max(float(existing.prominence_score or 0.0), float(prominence_score or 0.0))
            if source_kind and not existing.source_kind:
                existing.source_kind = source_kind
            return existing
        entry = EntityRegistryEntry(
            canonical=clean_canonical,
            domain=_clean_text(domain, max_len=24) or "general",
            entity_type=_clean_text(entity_type, max_len=32) or "doc",
            prominence_score=max(0.0, min(1.0, float(prominence_score or 0.0))),
            source_kind=_clean_text(source_kind, max_len=48),
            aliases=[],
        )
        registry[key] = entry
        return entry

    def _add_entity_alias(
        self,
        entry: EntityRegistryEntry,
        alias: str,
        *,
        alias_strength: str = "strong",
        alias_origin: str = "auto_generated",
    ) -> None:
        clean_alias = _clean_text(alias, max_len=120)
        if not clean_alias:
            return
        normalized_alias = _normalize_text(clean_alias)
        if not normalized_alias:
            return
        if normalized_alias in self._alias_value_set(entry):
            return
        entry.aliases.append(
            EntityAliasSpec(
                value=clean_alias,
                alias_strength=_clean_text(alias_strength, max_len=24) or "strong",
                alias_origin=_clean_text(alias_origin, max_len=32) or "auto_generated",
            )
        )

    def _camel_split_alias(self, text: str) -> str:
        return _clean_text(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(text or "")), max_len=120)

    def _add_auto_aliases(self, entry: EntityRegistryEntry) -> None:
        self._add_entity_alias(entry, entry.canonical, alias_strength="strong", alias_origin="canonical")
        folded = _ascii_fold(entry.canonical)
        if folded and _normalize_text(folded) != _normalize_text(entry.canonical):
            self._add_entity_alias(entry, folded, alias_strength="strong", alias_origin="auto_generated")
        camel_split = self._camel_split_alias(entry.canonical)
        if camel_split and _normalize_text(camel_split) != _normalize_text(entry.canonical):
            self._add_entity_alias(entry, camel_split, alias_strength="strong", alias_origin="auto_generated")
        if entry.entity_type == "person":
            stripped = _strip_person_suffixes(entry.canonical)
            if stripped:
                self._add_entity_alias(entry, stripped, alias_strength="strong", alias_origin="auto_generated")
            tokens = [token for token in re.split(r"\s+", stripped or entry.canonical) if token]
            if len(tokens) >= 2:
                self._add_entity_alias(entry, f"{tokens[0]} {tokens[-1]}", alias_strength="strong", alias_origin="auto_generated")
                self._add_entity_alias(entry, tokens[0], alias_strength="weak", alias_origin="auto_generated")
        if entry.entity_type == "lab":
            tokens = [token for token in re.split(r"\s+", entry.canonical) if token]
            if len(tokens) >= 2:
                condensed = " ".join(token for token in tokens if token.lower() not in {"interface", "laboratory"})
                if condensed and _normalize_text(condensed) != _normalize_text(entry.canonical):
                    self._add_entity_alias(entry, condensed, alias_strength="weak", alias_origin="auto_generated")
            self._add_entity_alias(entry, "the lab", alias_strength="weak", alias_origin="auto_generated")

    def _load_alias_overrides(self) -> List[Dict[str, Any]]:
        if not self.alias_override_path.exists():
            return []
        try:
            payload = json.loads(self.alias_override_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if isinstance(payload, dict):
            items = payload.get("entries") or payload.get("aliases") or []
        else:
            items = payload
        out: List[Dict[str, Any]] = []
        for item in items or []:
            if isinstance(item, dict):
                out.append(dict(item))
        return out

    def _apply_alias_overrides(self, registry: Dict[str, EntityRegistryEntry]) -> None:
        for item in self._load_alias_overrides():
            canonical = _clean_text(str(item.get("canonical") or ""), max_len=120)
            if not canonical:
                continue
            domain = _clean_text(str(item.get("domain") or ""), max_len=24) or "general"
            entity_type = _clean_text(str(item.get("entity_type") or ""), max_len=32) or "doc"
            prominence_score = float(item.get("prominence_score") or 0.0)
            entry = self._register_entity(
                registry,
                canonical=canonical,
                domain=domain,
                entity_type=entity_type,
                prominence_score=prominence_score,
                source_kind="override",
            )
            for raw_alias in item.get("aliases", []) or []:
                self._add_entity_alias(
                    entry,
                    str(raw_alias),
                    alias_strength=str(item.get("alias_strength") or "strong"),
                    alias_origin="curated_override",
                )

    def _extract_general_entities_from_chunk(self, chunk: DocChunk) -> List[Tuple[str, str, float, str]]:
        text = str(chunk.search_text or chunk.snippet_text or "")
        out: List[Tuple[str, str, float, str]] = []
        doc_kind = _clean_text(str((chunk.metadata or {}).get("general_doc_kind") or ""), max_len=32).lower().replace(" ", "_")
        lab_name = _extract_field_sentence(text, "Lab name")
        if lab_name:
            out.append((lab_name, "lab", 0.72, "extracted_lab"))
        director_name = _strip_person_suffixes(_extract_field_sentence(text, "Director"))
        if _looks_like_person_name(director_name):
            out.append((director_name, "person", 0.68, "extracted_person"))
        named_person = _strip_person_suffixes(_extract_field_sentence(text, "Name"))
        if _looks_like_person_name(named_person):
            out.append((named_person, "person", 0.68, "extracted_person"))
        if doc_kind == "team":
            for name in _extract_person_names(text):
                out.append((name, "person", 0.52, "extracted_person"))
        if doc_kind == "identity" and _looks_like_named_entity(chunk.title) and "lab" in _normalize_text(chunk.title):
            title_entity = _clean_text(str(chunk.title or "").split(" - ")[0], max_len=120)
            if title_entity:
                out.append((title_entity, "lab", 0.6, "doc_title"))
        section_leaf = _clean_text(str(chunk.section_path or "").split(">")[-1], max_len=120)
        if (
            doc_kind == "research"
            and _looks_like_named_entity(section_leaf)
            and _normalize_text(section_leaf) not in {"team director", "lab identity", "news and updates", "research"}
        ):
            entity_type = "project"
            out.append((section_leaf, entity_type, 0.44, "heading"))
        return out

    def _build_entity_registry(self, chunks: Sequence[DocChunk]) -> List[EntityRegistryEntry]:
        registry: Dict[str, EntityRegistryEntry] = {}
        if self.game_catalog is not None:
            for card in getattr(self.game_catalog, "cards", []) or []:
                name = _clean_text(str(getattr(card, "name", "") or ""), max_len=120)
                if not name:
                    continue
                entry = self._register_entity(
                    registry,
                    canonical=name,
                    domain="game",
                    entity_type="game",
                    prominence_score=0.82,
                    source_kind="manifest",
                )
                self._add_auto_aliases(entry)
                for alias in getattr(card, "aliases", []) or []:
                    self._add_entity_alias(entry, str(alias), alias_strength="strong", alias_origin="auto_generated")
                game_id = _clean_text(str(getattr(card, "game_id", "") or ""), max_len=80)
                if game_id:
                    self._add_entity_alias(entry, game_id, alias_strength="strong", alias_origin="auto_generated")
        for chunk in chunks:
            if chunk.domain != "general":
                continue
            for canonical, entity_type, prominence_score, source_kind in self._extract_general_entities_from_chunk(chunk):
                entry = self._register_entity(
                    registry,
                    canonical=canonical,
                    domain="general",
                    entity_type=entity_type,
                    prominence_score=prominence_score,
                    source_kind=source_kind,
                )
                self._add_auto_aliases(entry)
        self._apply_alias_overrides(registry)
        entries = list(registry.values())
        entries.sort(key=lambda item: (_normalize_text(item.domain), -float(item.prominence_score or 0.0), _normalize_text(item.canonical)))
        return entries

    def _build_entity_vectors(self) -> None:
        self._entity_vectors = {}
        if not self._dense_runtime_ready():
            return
        for entry in self._entity_registry:
            try:
                vector = self.embedder.query_embedding(entry.canonical)
            except Exception:
                vector = None
            if vector is None:
                continue
            self._entity_vectors[self._entity_key(entry.canonical, entry.domain, entry.entity_type)] = vector

    def _signature(self) -> str:
        parts: List[str] = [str(self.manifest_path.resolve()), _ENTITY_REGISTRY_SCHEMA_VERSION]
        tracked_paths = [self.manifest_path, *list(self._iter_general_doc_paths()), *list(self._iter_game_doc_paths())]
        if self.alias_override_path.exists():
            tracked_paths.append(self.alias_override_path)
        for path in tracked_paths:
            try:
                stat = path.stat()
                parts.append(f"{path.resolve()}:{int(getattr(stat, 'st_mtime_ns', int(stat.st_mtime * 1e9)))}:{int(stat.st_size)}")
            except Exception:
                parts.append(str(path.resolve()))
        digest = hashlib.sha1("\n".join(parts).encode("utf-8", errors="ignore")).hexdigest()
        return digest

    def _load_existing(self, signature: str) -> bool:
        if not (
            self.meta_path.exists()
            and self.chunk_path.exists()
            and self.vector_path.exists()
            and self.sqlite_path.exists()
            and self.entity_registry_path.exists()
        ):
            return False
        try:
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            if str(meta.get("signature") or "").strip() != signature:
                return False
            chunk_data = json.loads(self.chunk_path.read_text(encoding="utf-8"))
            chunks = [DocChunk(**item) for item in chunk_data if isinstance(item, dict)]
            registry_data = json.loads(self.entity_registry_path.read_text(encoding="utf-8"))
            registry_entries = []
            for item in registry_data.get("entries", []) if isinstance(registry_data, dict) else []:
                if not isinstance(item, dict):
                    continue
                aliases = [EntityAliasSpec(**alias) for alias in item.get("aliases", []) or [] if isinstance(alias, dict)]
                registry_entries.append(
                    EntityRegistryEntry(
                        canonical=str(item.get("canonical") or "").strip(),
                        domain=str(item.get("domain") or "").strip(),
                        entity_type=str(item.get("entity_type") or "").strip(),
                        prominence_score=float(item.get("prominence_score") or 0.0),
                        source_kind=str(item.get("source_kind") or "").strip(),
                        aliases=aliases,
                    )
                )
            if not chunks:
                return False
            if np is None:
                return False
            vectors = np.load(str(self.vector_path))
            if len(chunks) != int(vectors.shape[0]):
                return False
            self._chunks_by_id = {item.chunk_id: item for item in chunks}
            self._vectors = vectors
            self._entity_registry = registry_entries
            self._build_entity_vectors()
            self._conn = sqlite3.connect(str(self.sqlite_path), check_same_thread=False)
            self.ready = True
            self.error = ""
            return True
        except Exception:
            return False

    def _build_or_load(self) -> None:
        self.close()
        self.ready = False
        self.error = ""
        if not self.enabled:
            self.error = "doc_rag disabled"
            return
        if self.embedder is None or not getattr(self.embedder, "ready", False):
            self.error = getattr(self.embedder, "error", "") or "doc_rag embedder unavailable"
            return
        if np is None or cosine_similarity is None:
            self.error = "numpy or cosine_similarity unavailable"
            return
        signature = self._signature()
        if self._load_existing(signature):
            return
        try:
            self.index_dir.mkdir(parents=True, exist_ok=True)
            chunks = self._build_chunks()
            if not chunks:
                self.error = "doc_rag corpus is empty"
                return
            registry_entries = self._build_entity_registry(chunks)
            vectors = self._build_vectors(chunks)
            self._write_index(chunks, vectors, registry_entries, signature)
            self._chunks_by_id = {item.chunk_id: item for item in chunks}
            self._vectors = vectors
            self._entity_registry = registry_entries
            self._build_entity_vectors()
            self._conn = sqlite3.connect(str(self.sqlite_path), check_same_thread=False)
            self.ready = True
            self.error = ""
        except Exception as exc:
            self.error = f"doc_rag init failed: {exc}"
            self.ready = False

    def _build_chunks(self) -> List[DocChunk]:
        chunks: List[DocChunk] = []
        for path in self._iter_general_doc_paths():
            chunks.extend(self._chunks_from_general_doc(path))
        if self.include_games and self.game_catalog is not None:
            for card in getattr(self.game_catalog, "cards", []) or []:
                chunk = self._chunk_from_game_card(card)
                if chunk is not None:
                    chunks.append(chunk)
            for path in self._iter_game_doc_paths():
                chunks.extend(self._chunks_from_game_doc(path))
        return chunks

    def _chunk_from_game_card(self, card: GameCard) -> Optional[DocChunk]:
        name = _clean_text(card.name, max_len=80)
        if not name:
            return None
        aliases = [str(alias).strip() for alias in getattr(card, "aliases", []) if str(alias).strip()]
        visible_lines = [
            f"Game: {name}.",
            ("Aliases: " + ", ".join(aliases[:6]) + ".") if aliases else "",
            f"Description: {_clean_text(card.description, max_len=220)}." if str(card.description).strip() else "",
            f"How to play: {_clean_text(card.how_to_play, max_len=220)}." if str(card.how_to_play).strip() else "",
            f"Players: {_players_text(card)}.",
            ("Tags: " + ", ".join(str(tag).strip() for tag in getattr(card, "tags", []) if str(tag).strip()) + ".") if getattr(card, "tags", None) else "",
            f"Activity level: {_clean_text(card.activity_level, max_len=32)}." if str(card.activity_level).strip() else "",
            f"Launchable: {'yes' if str(getattr(card, 'exec_path', '') or '').strip() else 'no'}.",
        ]
        snippet = " ".join(line for line in visible_lines if line).strip()
        chunk_id = f"game_card:{_normalize_text(card.game_id or card.name)}"
        return DocChunk(
            chunk_id=chunk_id,
            doc_id=chunk_id,
            source_path=str(self.manifest_path),
            domain="game",
            doc_type="game_card",
            title=name,
            section_path="game_card",
            entity_name=name,
            search_text=snippet,
            snippet_text=snippet,
            metadata={
                "game_id": str(card.game_id or "").strip(),
                "players_min": int(getattr(card, "players_min", 1) or 1),
                "players_max": int(getattr(card, "players_max", 1) or 1),
                "tags": [str(tag).strip() for tag in getattr(card, "tags", []) or [] if str(tag).strip()],
                "activity_level": str(getattr(card, "activity_level", "") or "").strip(),
                "launchable": bool(str(getattr(card, "exec_path", "") or "").strip()),
                "recommendation_weight": float(getattr(card, "recommendation_weight", 0.0) or 0.0),
            },
        )

    def _chunks_from_general_doc(self, path: Path) -> List[DocChunk]:
        text = _safe_read_text(path)
        front_matter = _parse_front_matter(text)
        title = (
            _clean_text(str(front_matter.get("title") or ""), max_len=120)
            or _clean_text(path.stem.replace("_", " ").replace("-", " "), max_len=120)
            or path.stem
        )
        doc_kind = _general_doc_kind(path, front_matter=front_matter, title=title)
        chunks: List[DocChunk] = []
        for section_idx, (section_path, body) in enumerate(_split_markdown_sections(text, fallback_title=title)):
            for chunk_idx, chunk_text in enumerate(_chunk_text(body)):
                chunk_id = f"general:{path.stem}:{section_idx}:{chunk_idx}"
                chunks.append(
                    DocChunk(
                        chunk_id=chunk_id,
                        doc_id=f"general:{path.stem}",
                        source_path=str(path),
                        domain="general",
                        doc_type="doc",
                        title=title,
                        section_path=section_path,
                        entity_name="",
                        search_text=chunk_text,
                        snippet_text=chunk_text,
                        metadata={
                            "general_doc_kind": doc_kind,
                            "section": str(front_matter.get("section") or ""),
                            "title": title,
                            "source": str(front_matter.get("source") or ""),
                        },
                    )
                )
        return chunks

    def _chunks_from_game_doc(self, path: Path) -> List[DocChunk]:
        text = _safe_read_text(path)
        title = _clean_text(path.stem.replace("_", " ").replace("-", " "), max_len=120) or path.stem
        resolved = self.game_catalog.resolve_card(title) if self.game_catalog is not None else None
        entity_name = str(getattr(resolved, "name", "") or "").strip()
        if not entity_name and self.game_catalog is not None:
            try:
                mentioned = self.game_catalog.extract_game_mentions(title, limit=1)
                entity_name = str(mentioned[0] if mentioned else "").strip()
            except Exception:
                entity_name = ""
        chunks: List[DocChunk] = []
        for section_idx, (section_path, body) in enumerate(_split_markdown_sections(text, fallback_title=title)):
            for chunk_idx, chunk_text in enumerate(_chunk_text(body)):
                chunk_id = f"game_doc:{path.stem}:{section_idx}:{chunk_idx}"
                chunks.append(
                    DocChunk(
                        chunk_id=chunk_id,
                        doc_id=f"game_doc:{path.stem}",
                        source_path=str(path),
                        domain="game",
                        doc_type="game_doc",
                        title=entity_name or title,
                        section_path=section_path,
                        entity_name=entity_name or title,
                        search_text=chunk_text,
                        snippet_text=chunk_text,
                        metadata={},
                    )
                )
        return chunks

    def _build_vectors(self, chunks: Sequence[DocChunk]):
        assert np is not None
        rows: List[Any] = []
        for chunk in chunks:
            vector = self.embedder.doc_embedding(chunk.search_text)
            if vector is None:
                raise RuntimeError("doc_rag embedding failed")
            rows.append(vector.astype(np.float32))
        return np.stack(rows)

    def _write_index(self, chunks: Sequence[DocChunk], vectors: Any, registry_entries: Sequence[EntityRegistryEntry], signature: str) -> None:
        chunk_payload = [asdict(item) for item in chunks]
        self.chunk_path.write_text(json.dumps(chunk_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        registry_payload = {
            "schema_version": _ENTITY_REGISTRY_SCHEMA_VERSION,
            "entries": [asdict(item) for item in registry_entries],
        }
        self.entity_registry_path.write_text(json.dumps(registry_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        assert np is not None
        np.save(str(self.vector_path), vectors)
        conn = sqlite3.connect(str(self.sqlite_path), check_same_thread=False)
        try:
            conn.execute("DROP TABLE IF EXISTS doc_chunks")
            conn.execute(
                "CREATE VIRTUAL TABLE doc_chunks USING fts5("
                "chunk_id UNINDEXED, "
                "doc_id UNINDEXED, "
                "domain UNINDEXED, "
                "doc_type UNINDEXED, "
                "entity_name UNINDEXED, "
                "title UNINDEXED, "
                "section_path UNINDEXED, "
                "source_path UNINDEXED, "
                "search_text)"
            )
            rows = [
                (
                    item.chunk_id,
                    item.doc_id,
                    item.domain,
                    item.doc_type,
                    item.entity_name,
                    item.title,
                    item.section_path,
                    item.source_path,
                    item.search_text,
                )
                for item in chunks
            ]
            conn.executemany(
                "INSERT INTO doc_chunks(chunk_id, doc_id, domain, doc_type, entity_name, title, section_path, source_path, search_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()
        self.meta_path.write_text(json.dumps({"signature": signature, "count": len(chunks)}, ensure_ascii=False, indent=2), encoding="utf-8")

    def _search_sparse(self, query_text: str) -> List[Tuple[str, float, int]]:
        if self._conn is None:
            return []
        match_query = _build_match_query(query_text)
        if not match_query:
            return []
        try:
            cursor = self._conn.execute(
                "SELECT chunk_id, bm25(doc_chunks) AS score "
                "FROM doc_chunks WHERE doc_chunks MATCH ? ORDER BY score LIMIT ?",
                (match_query, int(self.fts_limit)),
            )
            rows = cursor.fetchall()
        except Exception:
            return []
        out: List[Tuple[str, float, int]] = []
        for rank, row in enumerate(rows, start=1):
            chunk_id = str(row[0] or "").strip()
            raw_score = float(row[1] or 0.0)
            score = 1.0 / float(max(1, rank))
            if raw_score < 0.0:
                score = min(1.0, score + min(abs(raw_score), 5.0) * 0.02)
            out.append((chunk_id, score, rank))
        return out

    def _sparse_variant_map(self, queries: Sequence[str]) -> Dict[str, Tuple[float, int]]:
        aggregate: Dict[str, Tuple[float, int]] = {}
        seen_queries = set()
        for query in queries:
            normalized_query = _normalize_text(query)
            if not normalized_query or normalized_query in seen_queries:
                continue
            seen_queries.add(normalized_query)
            for chunk_id, score, rank in self._search_sparse(query):
                existing_score, existing_rank = aggregate.get(chunk_id, (0.0, 0))
                aggregate[chunk_id] = (
                    float(existing_score) + float(score),
                    rank if existing_rank <= 0 else min(existing_rank, rank),
                )
        return aggregate

    def _search_dense(self, query_text: str) -> List[Tuple[str, float, int]]:
        if self._vectors is None or not self._chunks_by_id or np is None:
            return []
        query_vec = self.embedder.query_embedding(query_text)
        if query_vec is None:
            return []
        scores: List[Tuple[str, float]] = []
        chunk_ids = list(self._chunks_by_id.keys())
        for idx, chunk_id in enumerate(chunk_ids):
            try:
                vec = self._vectors[idx]
            except Exception:
                continue
            score = float(cosine_similarity(query_vec, vec))
            scores.append((chunk_id, score))
        scores.sort(key=lambda item: item[1], reverse=True)
        out: List[Tuple[str, float, int]] = []
        for rank, (chunk_id, score) in enumerate(scores[: self.dense_limit], start=1):
            out.append((chunk_id, score, rank))
        return out

    def _fuse_hits(
        self,
        query_text: str,
        *,
        normalized_query: str = "",
        rewritten_query: str = "",
    ) -> List[RetrievalHit]:
        sparse_queries = [query_text]
        if normalized_query and _normalize_text(normalized_query) != _normalize_text(query_text):
            sparse_queries.append(normalized_query)
        if rewritten_query and _normalize_text(rewritten_query) not in {_normalize_text(query_text), _normalize_text(normalized_query)}:
            sparse_queries.append(rewritten_query)
        sparse_map = self._sparse_variant_map(sparse_queries)
        dense_query = normalized_query or query_text
        dense_hits = self._search_dense(dense_query)
        dense_map = {chunk_id: (score, rank) for chunk_id, score, rank in dense_hits}
        chunk_ids = set(sparse_map) | set(dense_map)
        fused: List[RetrievalHit] = []
        for chunk_id in chunk_ids:
            chunk = self._chunks_by_id.get(chunk_id)
            if chunk is None:
                continue
            sparse_score, sparse_rank = sparse_map.get(chunk_id, (0.0, 0))
            dense_score, dense_rank = dense_map.get(chunk_id, (0.0, 0))
            fused_score = float(sparse_score or 0.0)
            if dense_rank > 0:
                fused_score += 1.0 / (60.0 + float(dense_rank))
            fused.append(
                RetrievalHit(
                    chunk_id=chunk.chunk_id,
                    doc_id=chunk.doc_id,
                    source_path=chunk.source_path,
                    domain=chunk.domain,
                    doc_type=chunk.doc_type,
                    title=chunk.title,
                    section_path=chunk.section_path,
                    entity_name=chunk.entity_name,
                    snippet_text=chunk.snippet_text,
                    metadata=dict(chunk.metadata),
                    fused_score=fused_score,
                    sparse_rank=sparse_rank,
                    dense_rank=dense_rank,
                    sparse_score=sparse_score,
                    dense_score=dense_score,
                )
            )
        fused.sort(key=lambda item: (item.fused_score, item.dense_score, item.sparse_score), reverse=True)
        return fused

    def _explicit_game_entities(self, query_text: str) -> List[str]:
        if self.game_catalog is None:
            return []
        normalized = f" {_normalize_text(query_text)} "
        explicit: List[str] = []
        seen = set()
        for card in getattr(self.game_catalog, "cards", []) or []:
            aliases = [str(card.name or "").strip(), str(card.game_id or "").strip(), *[str(alias).strip() for alias in getattr(card, "aliases", []) or []]]
            for alias in aliases:
                alias_key = _normalize_text(alias)
                if not alias_key or f" {alias_key} " not in normalized:
                    continue
                canonical = str(card.name or "").strip()
                if canonical and canonical.casefold() not in seen:
                    seen.add(canonical.casefold())
                    explicit.append(canonical)
                break
        try:
            for item in self.game_catalog.extract_game_mentions(query_text, limit=4):
                clean = str(item).strip()
                if not clean or clean.casefold() in seen:
                    continue
                seen.add(clean.casefold())
                explicit.append(clean)
        except Exception:
            pass
        return explicit[:4]

    @staticmethod
    def _focus_candidates(focus_state: Any) -> List[str]:
        source = _clean_text(str(getattr(focus_state, "focus_source", "") or ""), max_len=32) if focus_state is not None else ""
        if source and source not in _ENTITY_FOCUS_SOURCES:
            return []
        values = getattr(focus_state, "candidate_entities", []) if focus_state is not None else []
        out: List[str] = []
        seen = set()
        for raw in values or []:
            clean = _clean_text(str(raw or ""), max_len=80)
            if not clean:
                continue
            folded = clean.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            out.append(clean)
        focused = _clean_text(str(getattr(focus_state, "focused_entity", "") or ""), max_len=80) if focus_state is not None else ""
        if focused and focused.casefold() not in seen:
            out.insert(0, focused)
        return out[:4]

    def _bound_entities(self, query_text: str, *, focus_state: Any = None) -> List[str]:
        explicit = self._explicit_game_entities(query_text)
        if explicit:
            return explicit
        if _ENTITY_REFERENCE_RE.search(query_text or ""):
            return self._focus_candidates(focus_state)
        return []

    def _domain_from_hits(self, hits: Sequence[RetrievalHit]) -> str:
        if not hits:
            return "unknown"
        domain_scores: Dict[str, float] = {}
        for hit in hits[: min(5, len(hits))]:
            if not hit.domain:
                continue
            weight = float(hit.fused_score or 0.0)
            if int(hit.sparse_rank or 0) > 0:
                weight += 0.18
            if float(hit.dense_score or 0.0) >= 0.75:
                weight += 0.08
            domain_scores[hit.domain] = domain_scores.get(hit.domain, 0.0) + weight
        if not domain_scores:
            return "unknown"
        game_score = float(domain_scores.get("game", 0.0))
        general_score = float(domain_scores.get("general", 0.0))
        if game_score and general_score:
            if abs(game_score - general_score) <= 0.08:
                return "mixed"
            return "game" if game_score > general_score else "general"
        if game_score:
            return "game"
        if general_score:
            return "general"
        return "unknown"

    def _query_is_doc_like(self, query_text: str, *, force_doc_reason: str = "") -> bool:
        if force_doc_reason:
            return True
        if _looks_social(query_text):
            return False
        if _person_lookup_query(query_text):
            return True
        if _general_doc_query_shape_score(query_text) >= 0.18:
            return True
        if _is_factual_query(query_text):
            return True
        return False

    def _resolver_variants(self, query_text: str) -> List[str]:
        variants: List[str] = []
        for item in [query_text, _normalize_text(query_text), *_candidate_variants(query_text)]:
            clean = _clean_text(item, max_len=160)
            if not clean:
                continue
            variants.append(clean)
        fragment = _extract_person_query_fragment(query_text)
        if fragment:
            variants.append(fragment)
        normalized = _normalize_text(query_text)
        for prefix in ("what is ", "tell me about ", "where is ", "what does ", "does "):
            if normalized.startswith(prefix):
                tail = normalized[len(prefix) :].strip()
                tail = re.sub(r"\b(?:have|work on|works on|is|are)\b.*$", "", tail).strip()
                if tail:
                    variants.append(tail)
        deduped: List[str] = []
        seen = set()
        for value in variants:
            key = _resolver_query_key(value)
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(value)
        return deduped[:12]

    def _entry_vector(self, entry: EntityRegistryEntry):
        return self._entity_vectors.get(self._entity_key(entry.canonical, entry.domain, entry.entity_type))

    def _entity_embedding_similarity(self, query_fragment: str, entry: EntityRegistryEntry) -> float:
        if self.embedder is None or np is None or cosine_similarity is None:
            return 0.0
        entry_vec = self._entry_vector(entry)
        if entry_vec is None:
            return 0.0
        try:
            query_vec = self.embedder.query_embedding(query_fragment)
        except Exception:
            query_vec = None
        if query_vec is None:
            return 0.0
        try:
            return max(0.0, min(1.0, float(cosine_similarity(query_vec, entry_vec))))
        except Exception:
            return 0.0

    def _person_doc_concentration(self, hits: Sequence[RetrievalHit]) -> float:
        if not hits:
            return 0.0
        score = 0.0
        top_hits = list(hits[:4])
        if not top_hits:
            return 0.0
        for hit in top_hits:
            combined = _normalize_text(" ".join((hit.title, hit.section_path, hit.source_path, hit.snippet_text)))
            if any(hint in combined for hint in _PERSON_DOC_HINTS):
                score += 1.0
        return max(0.0, min(1.0, score / float(len(top_hits))))

    def _retrieval_corroboration(self, entry: EntityRegistryEntry, hits: Sequence[RetrievalHit]) -> float:
        if not hits:
            return 0.0
        best = 0.0
        aliases = [entry.canonical] + [alias.value for alias in entry.aliases]
        for hit in hits[:6]:
            combined = _normalize_text(" ".join((hit.title, hit.section_path, hit.entity_name, hit.source_path, hit.snippet_text)))
            if not combined:
                continue
            alias_best = 0.0
            for alias in aliases[:8]:
                alias_tokens = _query_keywords(alias)
                if not alias_tokens:
                    continue
                overlap = sum(1 for token in alias_tokens if token in combined)
                coverage = overlap / float(max(1, len(alias_tokens)))
                alias_best = max(alias_best, coverage)
            if entry.entity_type == "person" and self._person_doc_concentration([hit]) > 0.0:
                alias_best = max(alias_best, 0.42)
            best = max(best, alias_best)
        return max(0.0, min(1.0, best))

    def _partial_name_score(
        self,
        query_text: str,
        entry: EntityRegistryEntry,
        *,
        hits: Sequence[RetrievalHit],
        fragment: str,
    ) -> float:
        if entry.entity_type != "person":
            return 0.0
        if not _person_lookup_query(query_text):
            return 0.0
        if self._person_doc_concentration(hits) < 0.45:
            return 0.0
        query_tokens = [token for token in _tokenize(fragment or query_text) if token]
        entity_tokens = [token for token in _tokenize(entry.canonical) if token]
        if not query_tokens or not entity_tokens:
            return 0.0
        overlap = sum(1 for token in query_tokens if token in entity_tokens)
        if overlap <= 0:
            return 0.0
        if len(query_tokens) == 1 and query_tokens[0] == entity_tokens[0]:
            return 0.62
        return max(0.0, min(1.0, 0.46 + (0.18 * float(overlap))))

    def _resolve_entities(
        self,
        query_text: str,
        *,
        initial_hits: Sequence[RetrievalHit],
    ) -> Tuple[str, List[str], List[EntityResolverCandidate], str]:
        normalized_query = _normalize_text(query_text)
        if not self._entity_registry:
            return normalized_query, [], [], ""
        variants = self._resolver_variants(query_text)
        if not variants:
            return normalized_query, [], [], ""
        fragment = _extract_person_query_fragment(query_text)
        candidates: List[EntityResolverCandidate] = []
        for entry in self._entity_registry:
            best_alias = ""
            best_strength = "strong"
            best_similarity = 0.0
            for alias_spec in entry.aliases:
                for variant in variants:
                    similarity = _match_similarity(variant, alias_spec.value)
                    if similarity > best_similarity:
                        best_similarity = similarity
                        best_alias = alias_spec.value
                        best_strength = alias_spec.alias_strength
            if best_similarity < 0.42:
                continue
            partial_score = self._partial_name_score(query_text, entry, hits=initial_hits, fragment=fragment)
            embedding_score = self._entity_embedding_similarity(fragment or query_text, entry)
            corroboration = self._retrieval_corroboration(entry, initial_hits)
            prominence = min(0.12, max(0.0, float(entry.prominence_score or 0.0)) * 0.14)
            fused = (
                (0.48 * best_similarity)
                + (0.14 * embedding_score)
                + (0.2 * corroboration)
                + (0.18 * partial_score)
                + prominence
            )
            if best_strength == "weak":
                fused = min(fused, 0.78)
            resolution_mode = "exact" if _normalize_text(best_alias) in {_normalize_text(query_text), normalized_query, _normalize_text(fragment)} else "fuzzy"
            if partial_score >= 0.5:
                resolution_mode = "partial_name"
            candidates.append(
                EntityResolverCandidate(
                    canonical=entry.canonical,
                    domain=entry.domain,
                    entity_type=entry.entity_type,
                    fused_score=max(0.0, min(1.0, fused)),
                    string_similarity=best_similarity,
                    partial_name_score=partial_score,
                    embedding_similarity=embedding_score,
                    retrieval_corroboration=corroboration,
                    prominence_score=min(1.0, float(entry.prominence_score or 0.0)),
                    alias_strength=best_strength,
                    resolution_mode=resolution_mode if best_similarity >= 0.58 else "none",
                    resolver_reason=f"{resolution_mode}:{best_strength}:{best_alias or entry.canonical}",
                )
            )
        candidates.sort(key=lambda item: (item.fused_score, item.retrieval_corroboration, item.string_similarity), reverse=True)
        rewrite_query = ""
        rewrite_candidates: List[str] = []
        if candidates:
            rewrite_candidates = [item.canonical for item in candidates[:3] if item.fused_score >= 0.56]
            top = candidates[0]
            if (
                top.alias_strength != "weak"
                and top.resolution_mode != "partial_name"
                and top.fused_score >= 0.72
                and (top.string_similarity >= 0.78 or top.embedding_similarity >= 0.88)
            ):
                rewrite_query = _replace_query_span(query_text, top.canonical)
        return normalized_query, rewrite_candidates, candidates[:3], rewrite_query

    def _query_doc_affinity(self, query_text: str, *, explicit_entities: Sequence[str], force_doc_reason: str, entity_resolution_confidence: float = 0.0) -> float:
        normalized = f" {_normalize_text(query_text)} "
        if force_doc_reason:
            return 0.95
        if _looks_social(query_text):
            return 0.08
        score = 0.22
        keyword_rules = _general_keyword_rule_hits(query_text)
        if explicit_entities:
            score += 0.25
        score += _general_doc_query_shape_score(query_text)
        if any(hint in normalized for hint in _AVAILABILITY_HINTS):
            score += 0.3
        if any(hint in normalized for hint in _RECOMMEND_HINTS):
            score += 0.28
        if any(hint in normalized for hint in _COMPARE_HINTS):
            score += 0.28
        if any(hint in normalized for hint in _HOW_TO_HINTS):
            score += 0.24
        if any(hint in normalized for hint in _INTRODUCE_HINTS):
            score += 0.18
        if _is_factual_query(query_text):
            score += 0.22
        if "?" in str(query_text or ""):
            score += 0.08
        score += min(0.24, sum(float(rule.get("affinity_boost") or 0.0) for rule in keyword_rules))
        score += min(0.18, max(0.0, float(entity_resolution_confidence or 0.0)) * 0.18)
        return max(0.0, min(1.0, score))

    def _top_hit_concentration(self, hits: Sequence[RetrievalHit]) -> float:
        if not hits:
            return 0.0
        top_hits = list(hits[: min(4, len(hits))])
        if not top_hits:
            return 0.0
        doc_counts = Counter(hit.doc_id for hit in top_hits if hit.doc_id)
        title_counts = Counter(_normalize_text(hit.title) for hit in top_hits if _normalize_text(hit.title))
        strongest = 0.0
        if doc_counts:
            strongest = max(strongest, float(doc_counts.most_common(1)[0][1]) / float(len(top_hits)))
        if title_counts:
            strongest = max(strongest, float(title_counts.most_common(1)[0][1]) / float(len(top_hits)))
        same_domain = {hit.domain for hit in top_hits if hit.domain}
        if len(same_domain) == 1:
            strongest = max(strongest, 0.72)
        return max(0.0, min(1.0, strongest))

    def _strict_entity_binding_strength(
        self,
        query_text: str,
        *,
        explicit_entities: Sequence[str],
        hits: Sequence[RetrievalHit],
        focus_state: Any = None,
    ) -> float:
        if explicit_entities:
            normalized = _normalize_text(query_text)
            if _is_factual_query(query_text) or any(hint in normalized for hint in _INTRODUCE_HINTS + _HOW_TO_HINTS + _COMPARE_HINTS):
                return 0.95
            return 0.82
        if _ENTITY_REFERENCE_RE.search(query_text or ""):
            focus = self._focus_candidates(focus_state)
            if focus:
                return 0.6
        entity_counts = Counter(hit.entity_name.casefold() for hit in hits[:4] if hit.entity_name)
        if entity_counts:
            _, count = entity_counts.most_common(1)[0]
            return min(0.75, 0.2 + 0.18 * float(count))
        return 0.08

    def _semantic_subject_binding_strength(self, query_text: str, *, hits: Sequence[RetrievalHit]) -> float:
        if not hits:
            return 0.0
        keywords = _query_keywords(query_text)
        keyword_rules = _general_keyword_rule_hits(query_text)
        if not keywords:
            return 0.08
        top_hits = list(hits[: min(4, len(hits))])
        best = 0.08
        for hit in top_hits:
            path_name = Path(str(hit.source_path or "")).stem.replace("_", " ")
            field_text = _normalize_text(" ".join((hit.title, hit.section_path, hit.entity_name, path_name, hit.snippet_text)))
            if not field_text:
                continue
            overlap = sum(1 for token in keywords if token in field_text)
            coverage = overlap / float(max(1, len(keywords)))
            score = 0.08 + min(0.56, coverage * 0.72)
            for rule in keyword_rules:
                rule_field_terms = tuple(str(item).strip().lower() for item in rule.get("field_terms", ()) if str(item).strip())
                rule_snippet_terms = tuple(str(item).strip().lower() for item in rule.get("snippet_terms", ()) if str(item).strip())
                if any(term in field_text for term in rule_field_terms):
                    score += float(rule.get("binding_boost") or 0.0)
                elif any(term in field_text for term in rule_snippet_terms):
                    score += float(rule.get("binding_boost") or 0.0) * 0.7
            if overlap >= 2:
                score += 0.08
            best = max(best, score)
        best += min(0.18, self._top_hit_concentration(top_hits) * 0.18)
        return max(0.0, min(1.0, best))

    def _retrieval_support(self, hits: Sequence[RetrievalHit]) -> float:
        if not hits:
            return 0.0
        top1 = hits[0]
        score = 0.22
        if top1.sparse_rank > 0:
            score += 0.18
        if top1.dense_rank > 0:
            score += 0.2
        if top1.dense_score >= 0.42:
            score += 0.18
        if len(hits) >= 2:
            score += 0.1
        if len(hits) >= 4:
            score += 0.08
        top_domains = {hit.domain for hit in hits[:3] if hit.domain}
        if len(top_domains) == 1:
            score += 0.1
        entity_counts = Counter(hit.entity_name.casefold() for hit in hits[:4] if hit.entity_name)
        if entity_counts and entity_counts.most_common(1)[0][1] >= 2:
            score += 0.08
        return max(0.0, min(1.0, score))

    def _entity_binding_strength(self, query_text: str, *, explicit_entities: Sequence[str], hits: Sequence[RetrievalHit], focus_state: Any = None) -> float:
        domain_hint = self._domain_from_hits(hits[:4])
        strict_binding = self._strict_entity_binding_strength(
            query_text,
            explicit_entities=explicit_entities,
            hits=hits,
            focus_state=focus_state,
        )
        semantic_binding = self._semantic_subject_binding_strength(query_text, hits=hits)
        if domain_hint in _GAME_DOMAINS:
            return max(strict_binding, min(0.62, semantic_binding))
        return max(strict_binding, semantic_binding)

    def _retrieval_rescue_applies(
        self,
        query_text: str,
        *,
        support: float,
        hits: Sequence[RetrievalHit],
        affinity: float,
    ) -> bool:
        if support < 0.82:
            return False
        if _looks_social(query_text):
            return False
        if not (_is_interrogative_query(query_text) or _is_factual_query(query_text) or _general_doc_query_shape_score(query_text) >= 0.2):
            return False
        if self._top_hit_concentration(hits) < 0.5:
            return False
        return affinity >= 0.28

    def _stage1_reason(
        self,
        *,
        routing_confidence: float,
        support: float,
        affinity: float,
        entity_binding: float,
        force_doc_reason: str,
        rescue_applied: bool,
    ) -> str:
        if force_doc_reason:
            return f"force_doc:{force_doc_reason}"
        if rescue_applied:
            return "retrieval_rescue_doc_candidate"
        if routing_confidence >= self.routing_threshold:
            if support >= 0.55 and affinity >= 0.55:
                return "stable_doc_candidate"
            return "borderline_doc_candidate"
        if support < 0.24:
            return "weak_retrieval_support"
        if affinity < 0.28:
            return "low_query_doc_affinity"
        if entity_binding < 0.18:
            return "weak_entity_binding"
        return "stage1_mixed_but_below_threshold"

    def _lexical_coverage(self, query_text: str, hits: Sequence[RetrievalHit]) -> float:
        keywords = _query_keywords(query_text)
        if not keywords:
            return 0.0
        combined = " ".join(
            _normalize_text(" ".join((hit.title, hit.section_path, hit.entity_name, hit.snippet_text)))
            for hit in hits[: max(1, min(len(hits), self.top_k))]
        )
        if not combined:
            return 0.0
        covered = sum(1 for token in keywords if token in combined)
        return max(0.0, min(1.0, covered / float(len(keywords))))

    def _hit_query_relevance(self, query_text: str, hit: RetrievalHit) -> float:
        keywords = _query_keywords(query_text)
        if not keywords:
            return float(hit.fused_score or 0.0)
        path_name = Path(str(hit.source_path or "")).stem.replace("_", " ")
        fields_text = _normalize_text(" ".join((hit.title, hit.section_path, hit.entity_name, path_name)))
        snippet_text = _normalize_text(hit.snippet_text)
        field_overlap = sum(1 for token in keywords if token in fields_text)
        snippet_overlap = sum(1 for token in keywords if token in snippet_text)
        topic_terms = _general_topic_terms(query_text)
        topic_field_overlap = sum(1 for term in topic_terms if term in fields_text)
        topic_snippet_overlap = sum(1 for term in topic_terms if term in snippet_text)
        score = float(hit.fused_score or 0.0)
        score += min(0.56, 0.22 * float(field_overlap))
        score += min(0.24, 0.06 * float(snippet_overlap))
        score += min(0.42, 0.26 * float(topic_field_overlap))
        score += min(0.14, 0.07 * float(topic_snippet_overlap))
        if topic_terms and topic_field_overlap <= 0 and topic_snippet_overlap <= 0:
            score -= 0.08
        if int(hit.sparse_rank or 0) > 0:
            score += 0.08
        if float(hit.dense_score or 0.0) >= 0.55:
            score += 0.05
        return score

    def _general_answer_hit_score(
        self,
        query_text: str,
        answer_mode: str,
        hit: RetrievalHit,
        *,
        general_focus: str = "",
        candidate_entities: Sequence[str] = (),
    ) -> float:
        score = self._hit_query_relevance(query_text, hit)
        normalized = f" {_normalize_text(query_text)} "
        fields_text = _normalize_text(" ".join((hit.title, hit.section_path, Path(str(hit.source_path or '')).stem.replace('_', ' '))))
        snippet_text = _normalize_text(hit.snippet_text)
        keyword_rules = _general_keyword_rule_hits(query_text)
        news_like = _is_news_like_text(" ".join((hit.title, hit.section_path, hit.source_path)))
        asks_latest = any(term in normalized for term in (" latest ", " recent ", " news ", " update ", " updates "))
        asks_person = normalized.startswith(" who is ")
        asks_introduce = normalized.startswith(" what is ") or normalized.startswith(" tell me about ")
        asks_yes_no = normalized.startswith(" does ") or normalized.startswith(" do ")
        asks_equipment = any(term in normalized for term in (" equipment ", " tools ", " tool ", " devices ", " device "))
        doc_kind = _general_doc_kind_for_hit(hit)
        entity_terms = [_normalize_text(item) for item in candidate_entities if _normalize_text(item)]

        if asks_person:
            if any(term in fields_text for term in ("team", "director", "member")):
                score += 0.28
            if " name " in f" {snippet_text} " or " role " in f" {snippet_text} ":
                score += 0.18
        if asks_introduce and any(term in fields_text for term in ("identity", "overview", "lab identity")):
            score += 0.24
        if asks_yes_no and any(term in snippet_text for term in ("works on", "research", "theme", "social robotics", "interface lab")):
            score += 0.18
        if asks_equipment and any(term in fields_text for term in ("equipment", "tools", "devices")):
            score += 0.32
        if asks_equipment and any(term in snippet_text for term in ("eeg", "emg", "headset", "kinect", "device", "kit")):
            score += 0.14
        for rule in keyword_rules:
            rule_field_terms = tuple(str(item).strip().lower() for item in rule.get("field_terms", ()) if str(item).strip())
            rule_snippet_terms = tuple(str(item).strip().lower() for item in rule.get("snippet_terms", ()) if str(item).strip())
            if any(term in fields_text for term in rule_field_terms):
                score += float(rule.get("answer_boost") or 0.0)
            elif any(term in snippet_text for term in rule_snippet_terms):
                score += float(rule.get("answer_boost") or 0.0) * 0.55
        if entity_terms and any(term in fields_text or term in snippet_text for term in entity_terms):
            score += 0.14
        if general_focus == "overview":
            if doc_kind == "identity":
                score += 0.42
            elif doc_kind == "research":
                score += 0.1
            elif doc_kind == "team":
                score += 0.06
            elif doc_kind == "news":
                score -= 0.42
        elif general_focus == "people":
            if doc_kind == "team":
                score += 0.46
            elif doc_kind == "identity":
                score += 0.18
            elif doc_kind == "news":
                score -= 0.5
        elif general_focus == "research":
            if doc_kind == "research":
                score += 0.4
            elif doc_kind == "identity":
                score += 0.14
            elif doc_kind == "news":
                score -= 0.36
        elif general_focus == "equipment":
            if doc_kind == "equipment":
                score += 0.44
            elif doc_kind == "identity":
                score += 0.1
            elif doc_kind == "news":
                score -= 0.42
        elif general_focus == "location_contact":
            if doc_kind in {"location_contact", "identity"}:
                score += 0.34
            elif doc_kind == "news":
                score -= 0.38
        elif general_focus == "news":
            if doc_kind == "news":
                score += 0.36
            else:
                score -= 0.08
        if news_like and not asks_latest:
            score -= 0.24
        if answer_mode == "introduce" and news_like:
            score -= 0.08
        return score

    def _select_evidence(
        self,
        hits: Sequence[RetrievalHit],
        *,
        query_text: str,
        answer_mode: str,
        domain: str,
        candidate_entities: Sequence[str],
        general_focus: str = "",
    ) -> List[RetrievalHit]:
        selected: List[RetrievalHit] = []
        seen = set()
        entity_keys = {item.casefold() for item in candidate_entities if item}
        if domain in _GAME_DOMAINS and answer_mode in {"availability", "recommend"} and self.game_catalog is not None:
            allowed = entity_keys
            for card in getattr(self.game_catalog, "cards", []) or []:
                if not str(card.exec_path or "").strip():
                    continue
                if allowed and str(card.name or "").strip().casefold() not in allowed:
                    continue
                chunk = self._chunks_by_id.get(f"game_card:{_normalize_text(card.game_id or card.name)}")
                if chunk is None:
                    continue
                selected.append(
                    RetrievalHit(
                        chunk_id=chunk.chunk_id,
                        doc_id=chunk.doc_id,
                        source_path=chunk.source_path,
                        domain=chunk.domain,
                        doc_type=chunk.doc_type,
                        title=chunk.title,
                        section_path=chunk.section_path,
                        entity_name=chunk.entity_name,
                        snippet_text=chunk.snippet_text,
                        metadata=dict(chunk.metadata),
                    )
                )
                seen.add(chunk.chunk_id)
                if len(selected) >= max(self.top_k, len(candidate_entities), 3):
                    break
        candidate_hits = list(hits)
        if domain not in _GAME_DOMAINS:
            candidate_hits.sort(
                key=lambda item: (
                    self._general_answer_hit_score(
                        query_text,
                        answer_mode,
                        item,
                        general_focus=general_focus,
                        candidate_entities=candidate_entities,
                    ),
                    item.fused_score,
                    item.dense_score,
                    item.sparse_score,
                ),
                reverse=True,
            )
            kind_limits = self._general_kind_limits(general_focus)
            kind_counts: Counter[str] = Counter()
            asks_latest = any(term in f" {_normalize_text(query_text)} " for term in (" latest ", " recent ", " news ", " update ", " updates "))
            deferred: List[RetrievalHit] = []
            for hit in candidate_hits:
                if hit.chunk_id in seen:
                    continue
                kind = _general_doc_kind_for_hit(hit)
                limit = int(kind_limits.get(kind, 1))
                if kind == "news" and not asks_latest and general_focus != "news":
                    deferred.append(hit)
                    continue
                if limit <= 0 or kind_counts[kind] >= limit:
                    deferred.append(hit)
                    continue
                selected.append(hit)
                seen.add(hit.chunk_id)
                kind_counts[kind] += 1
                if len(selected) >= self.top_k:
                    break
            if len(selected) < self.top_k:
                for hit in deferred:
                    if hit.chunk_id in seen:
                        continue
                    selected.append(hit)
                    seen.add(hit.chunk_id)
                    if len(selected) >= self.top_k:
                        break
            return selected[: self.top_k]
        for hit in candidate_hits:
            if hit.chunk_id in seen:
                continue
            if entity_keys and hit.entity_name and hit.entity_name.casefold() not in entity_keys:
                if answer_mode in {"compare", "introduce", "how_to", "factual"} and domain in _GAME_DOMAINS:
                    continue
            selected.append(hit)
            seen.add(hit.chunk_id)
            if len(selected) >= self.top_k:
                break
        return selected[: self.top_k]

    def _answer_mode(self, query_text: str, *, domain: str, candidate_entities: Sequence[str], hits: Sequence[RetrievalHit]) -> str:
        normalized = f" {_normalize_text(query_text)} "
        if len(candidate_entities) >= 2 and "?" in str(query_text or "") and not any(
            hint in normalized for hint in _COMPARE_HINTS + _RECOMMEND_HINTS + _AVAILABILITY_HINTS + _HOW_TO_HINTS + _INTRODUCE_HINTS + _FACTUAL_HINTS
        ):
            return ""
        if any(hint in normalized for hint in _AVAILABILITY_HINTS):
            return "availability"
        if any(hint in normalized for hint in _RECOMMEND_HINTS):
            return "recommend"
        if any(hint in normalized for hint in _COMPARE_HINTS):
            return "compare"
        if any(hint in normalized for hint in _HOW_TO_HINTS):
            return "how_to"
        if domain == "general" and any(
            term in normalized
            for term in (
                " research ",
                " works on ",
                " work on ",
                " social robotics ",
                " researchers ",
                " collaborators ",
                " members ",
                " who works there ",
                " equipment ",
                " tools ",
                " devices ",
                " contact ",
                " email ",
                " phone ",
                " where is ",
            )
        ):
            return "factual"
        if domain == "general" and (
            normalized.startswith(" what do you know about ")
            or normalized.startswith(" do you know ")
        ):
            return "introduce"
        if _is_factual_query(query_text):
            return "factual"
        if any(hint in normalized for hint in _INTRODUCE_HINTS):
            return "introduce"
        if domain == "general" and hits:
            return "factual"
        if len(candidate_entities) == 1:
            return "introduce"
        return ""

    def _general_focus(
        self,
        query_text: str,
        *,
        answer_mode: str,
        resolver_candidates: Sequence[EntityResolverCandidate],
        candidate_entities: Sequence[str],
    ) -> str:
        normalized = f" {_normalize_text(query_text)} "
        top_candidate = resolver_candidates[0] if resolver_candidates else None
        top_type = str(getattr(top_candidate, "entity_type", "") or "").strip().lower()
        if any(term in normalized for term in (" latest ", " recent ", " news ", " update ", " updates ")):
            return "news"
        if any(term in normalized for term in (" contact ", " email ", " phone ", " where is ", " address ", " located ")):
            return "location_contact"
        if any(term in normalized for term in (" equipment ", " tools ", " tool ", " devices ", " device ", " sensors ", " sensor ")):
            return "equipment"
        if (
            top_type == "person"
            or normalized.startswith(" who is ")
            or any(term in normalized for term in (" team ", " member ", " members ", " collaborator ", " collaborators ", " researcher ", " researchers ", " who works there "))
        ):
            return "people"
        if any(term in normalized for term in (" what projects ", " what research ", " works on ", " work on ", " social robotics ", " hri ", " research ")):
            return "research"
        if answer_mode == "introduce":
            return "people" if top_type == "person" else "overview"
        if answer_mode == "factual" and top_type == "person":
            return "people"
        if candidate_entities:
            return "overview"
        return "overview"

    def _game_candidates_from_hits(self, hits: Sequence[RetrievalHit], *, explicit_entities: Sequence[str], focus_state: Any = None) -> List[str]:
        out: List[str] = []
        seen = set()
        for name in list(explicit_entities) + self._focus_candidates(focus_state):
            clean = _clean_text(name, max_len=80)
            if not clean or clean.casefold() in seen:
                continue
            seen.add(clean.casefold())
            out.append(clean)
        for hit in hits:
            clean = _clean_text(hit.entity_name or "", max_len=80)
            if not clean or clean.casefold() in seen:
                continue
            seen.add(clean.casefold())
            out.append(clean)
        return out[:4]

    @staticmethod
    def _general_kind_limits(general_focus: str) -> Dict[str, int]:
        focus = str(general_focus or "").strip().lower()
        if focus == "people":
            return {"team": 3, "identity": 1, "research": 1, "equipment": 1, "location_contact": 1, "news": 0}
        if focus == "research":
            return {"research": 3, "identity": 1, "team": 1, "equipment": 1, "location_contact": 1, "news": 0}
        if focus == "equipment":
            return {"equipment": 3, "identity": 1, "research": 1, "team": 1, "location_contact": 1, "news": 0}
        if focus == "location_contact":
            return {"location_contact": 2, "identity": 2, "research": 1, "team": 1, "equipment": 1, "news": 0}
        if focus == "news":
            return {"news": 3, "identity": 1, "research": 1, "team": 1, "equipment": 1, "location_contact": 1}
        return {"identity": 2, "research": 1, "team": 1, "equipment": 1, "location_contact": 1, "news": 0}

    def _game_cards_for_entities(self, entity_names: Sequence[str]) -> List[GameCard]:
        if self.game_catalog is None:
            return []
        cards: List[GameCard] = []
        seen = set()
        for raw in entity_names:
            card = self.game_catalog.resolve_card(str(raw))
            if card is None:
                continue
            key = str(card.name or "").strip().casefold()
            if not key or key in seen:
                continue
            seen.add(key)
            cards.append(card)
        return cards

    def _desired_player_count(self, query_text: str) -> Optional[int]:
        normalized = _normalize_text(query_text)
        match = re.search(r"\b(?:for|with)\s+([0-9]+)\s*(?:player|players|people)\b", normalized)
        if match:
            try:
                return max(1, int(match.group(1)))
            except Exception:
                return None
        if " two player " in f" {normalized} " or " two people " in f" {normalized} ":
            return 2
        if " single player " in f" {normalized} " or " solo " in f" {normalized} ":
            return 1
        return None

    def _profile_texts(self, user_profile: Optional[Dict[str, Any]]) -> List[str]:
        profile = user_profile or {}
        values: List[str] = []
        for key in ("goals", "likes", "dislikes", "recent_notes"):
            for raw in profile.get(key, []) or []:
                clean = _clean_text(str(raw), max_len=120)
                if clean:
                    values.append(clean)
        for episode in profile.get("episodes", []) or []:
            if not isinstance(episode, dict):
                continue
            if str(episode.get("role") or "").strip().lower() != "user":
                continue
            clean = _clean_text(str(episode.get("text") or ""), max_len=140)
            if clean:
                values.append(clean)
        return values

    def _rank_recommendations(self, cards: Sequence[GameCard], *, query_text: str, user_profile: Optional[Dict[str, Any]]) -> List[Tuple[GameCard, float, str]]:
        if self.game_catalog is None:
            return []
        profile = user_profile or {}
        desired_players = self._desired_player_count(query_text)
        likes = [str(value).strip() for value in profile.get("likes", []) or [] if str(value).strip()]
        dislikes = [str(value).strip() for value in profile.get("dislikes", []) or [] if str(value).strip()]
        favorite_game = str(profile.get("favorite_game") or "").strip()
        profile_texts = self._profile_texts(profile)
        recent_games = [item for item in profile.get("recent_games", []) or [] if isinstance(item, dict)]
        wants_variety = "different" in _normalize_text(query_text) or "another" in _normalize_text(query_text)
        any_player_fit = any(card.players_min <= desired_players <= card.players_max for card in cards) if desired_players is not None else False
        ranked: List[Tuple[GameCard, float, str]] = []
        for card in cards:
            score = float(getattr(card, "recommendation_weight", 0.0) or 0.0)
            reason = "it is available locally"
            if desired_players is not None:
                if int(card.players_min) <= int(desired_players) <= int(card.players_max):
                    score += 3.0
                    reason = f"it fits {desired_players} player" + ("" if desired_players == 1 else "s")
                else:
                    score -= 2.0 if any_player_fit else 0.8
            if favorite_game and _card_matches_values(card, [favorite_game]):
                score += 4.2
                reason = "it matches your favorite game"
            if likes and _card_matches_values(card, likes):
                score += 2.2
                reason = "it matches what you like"
            if dislikes and _card_matches_values(card, dislikes):
                score -= 4.5
            if _GOAL_RULES:
                goal_score, goal_reasons = _score_goal_or_limitation_rules_local(
                    card=card,
                    rules=_GOAL_RULES,
                    profile_texts=profile_texts,
                )
                score += goal_score
                if goal_reasons:
                    reason = max(goal_reasons, key=lambda item: item[0])[1]
            if _LIMITATION_RULES:
                limit_score, limit_reasons = _score_goal_or_limitation_rules_local(
                    card=card,
                    rules=_LIMITATION_RULES,
                    profile_texts=profile_texts,
                )
                score += limit_score
                if limit_reasons:
                    reason = max(limit_reasons, key=lambda item: item[0])[1]
            history_score, history_reasons = _history_adjustment(
                card=card,
                recent_games=recent_games,
                wants_variety=wants_variety,
            )
            score += history_score
            if history_reasons:
                reason = max(history_reasons, key=lambda item: item[0])[1]
            if str(card.exec_path or "").strip():
                score += 0.5
            ranked.append((card, score, reason))
        ranked.sort(key=lambda item: (item[1], str(item[0].name).casefold()), reverse=True)
        return ranked

    def _compare_reason(self, left: GameCard, right: GameCard) -> str:
        return _contrast_reason_local(left, right)

    def _build_game_payload(
        self,
        *,
        query_text: str,
        answer_mode: str,
        selected_hits: Sequence[RetrievalHit],
        candidate_entities: Sequence[str],
        bound_entities: Sequence[str],
        focus_state: Any,
        user_profile: Optional[Dict[str, Any]],
    ) -> Tuple[Dict[str, Any], float, str, str]:
        cards = self._game_cards_for_entities(candidate_entities)
        bound_cards = self._game_cards_for_entities(bound_entities)
        launchable_cards = [card for card in cards if str(card.exec_path or "").strip()]
        launchable_names = [card.name for card in launchable_cards]
        session_focus_game = _clean_text(str(getattr(focus_state, "focused_entity", "") or ""), max_len=80) if focus_state is not None else ""
        snippets = [_clean_text(hit.snippet_text, max_len=260) for hit in selected_hits if _clean_text(hit.snippet_text, max_len=260)]
        source_ids = [hit.chunk_id for hit in selected_hits]
        recommendation_reason = ""
        primary_entity = ""
        required_terms: List[str] = []
        clarify_kind = ""
        binding_state = "doc_grounded"

        if answer_mode == "availability":
            if not bound_entities and self.game_catalog is not None:
                launchable_cards = [card for card in getattr(self.game_catalog, "cards", []) or [] if str(card.exec_path or "").strip()]
                launchable_names = [card.name for card in launchable_cards]
            if not launchable_cards and self.game_catalog is not None:
                launchable_cards = [card for card in getattr(self.game_catalog, "cards", []) or [] if str(card.exec_path or "").strip()]
                launchable_names = [card.name for card in launchable_cards]
            if not launchable_cards:
                return {}, 0.18, "no_launchable_games", ""
            primary_entity = launchable_cards[0].name
            required_terms = launchable_names[:]
            if len(launchable_names) == 1:
                text = f"Right now I have {launchable_names[0]} available."
            elif len(launchable_names) == 2:
                text = f"Right now I have {launchable_names[0]} and {launchable_names[1]} available."
            else:
                text = "Right now I have " + ", ".join(launchable_names[:-1]) + f", and {launchable_names[-1]} available."
            payload = {
                "type": "doc_answer",
                "domain": "game",
                "answer_mode": "availability",
                "text": text,
                "doc_snippets": snippets or [text],
                "doc_source_ids": source_ids,
                "required_terms": required_terms[:4],
                "allowed_entities": launchable_names[:],
                "primary_entity": primary_entity,
                "candidate_entities": launchable_names[:],
                "binding_state": binding_state,
                "game_names": launchable_names[:],
                "launchable_games": launchable_names[:],
                "session_focus_game": session_focus_game,
                "recommendation_reason": "",
                "max_sentences": 2,
            }
            return payload, 0.86, "", ""

        if answer_mode == "recommend":
            if not launchable_cards:
                return {}, 0.2, "no_launchable_recommendation_candidates", ""
            ranked = self._rank_recommendations(launchable_cards, query_text=query_text, user_profile=user_profile)
            if not ranked:
                return {}, 0.2, "recommendation_rank_failed", ""
            winner, _, recommendation_reason = ranked[0]
            primary_entity = winner.name
            required_terms = [winner.name]
            text = f"I recommend {winner.name} because {recommendation_reason}."
            payload = {
                "type": "doc_answer",
                "domain": "game",
                "answer_mode": "recommend",
                "text": text,
                "doc_snippets": snippets or [text],
                "doc_source_ids": source_ids,
                "required_terms": required_terms,
                "allowed_entities": launchable_names[:],
                "primary_entity": primary_entity,
                "candidate_entities": [card.name for card, _, _ in ranked[:4]],
                "binding_state": binding_state,
                "game_names": [card.name for card, _, _ in ranked[:4]],
                "launchable_games": launchable_names[:],
                "session_focus_game": session_focus_game,
                "recommendation_reason": recommendation_reason,
                "max_sentences": 2,
            }
            return payload, 0.8, "", ""

        if answer_mode == "compare":
            if len(cards) < 2:
                clarify_kind = "clarify_missing_entity"
                text = "Which two games do you want me to compare?"
                payload = {
                    "type": "doc_clarify",
                    "domain": "game",
                    "answer_mode": "compare",
                    "clarify_kind": clarify_kind,
                    "text": text,
                    "doc_snippets": snippets[:2],
                    "doc_source_ids": source_ids,
                    "required_terms": [],
                    "allowed_entities": [card.name for card in cards],
                    "primary_entity": "",
                    "candidate_entities": [card.name for card in cards],
                    "binding_state": binding_state,
                    "game_names": [card.name for card in cards],
                    "launchable_games": launchable_names[:],
                    "session_focus_game": session_focus_game,
                    "recommendation_reason": "",
                    "max_sentences": 1,
                }
                return payload, 0.45, "", clarify_kind
            first, second = cards[0], cards[1]
            left_reason = self._compare_reason(first, second)
            text = f"{first.name} and {second.name} are current options. {left_reason}"
            payload = {
                "type": "doc_answer",
                "domain": "game",
                "answer_mode": "compare",
                "text": text,
                "doc_snippets": snippets or [text],
                "doc_source_ids": source_ids,
                "required_terms": [first.name, second.name],
                "allowed_entities": [card.name for card in cards],
                "primary_entity": first.name,
                "candidate_entities": [card.name for card in cards],
                "binding_state": binding_state,
                "game_names": [card.name for card in cards],
                "launchable_games": launchable_names[:],
                "session_focus_game": session_focus_game,
                "recommendation_reason": left_reason,
                "max_sentences": 2,
            }
            return payload, 0.76, "", ""

        if answer_mode in {"introduce", "how_to", "factual"}:
            target_cards = bound_cards if bound_cards else cards
            if not target_cards:
                clarify_kind = "clarify_missing_entity"
                if answer_mode == "how_to":
                    text = "Which game do you want the rules for?"
                elif answer_mode == "factual":
                    text = "Which game are you asking about?"
                else:
                    text = "Which game do you want me to describe?"
                payload = {
                    "type": "doc_clarify",
                    "domain": "game",
                    "answer_mode": answer_mode,
                    "clarify_kind": clarify_kind,
                    "text": text,
                    "doc_snippets": snippets[:1],
                    "doc_source_ids": source_ids,
                    "required_terms": [],
                    "allowed_entities": list(candidate_entities),
                    "primary_entity": "",
                    "candidate_entities": list(candidate_entities),
                    "binding_state": binding_state,
                    "game_names": list(candidate_entities),
                    "launchable_games": launchable_names[:],
                    "session_focus_game": session_focus_game,
                    "recommendation_reason": "",
                    "max_sentences": 1,
                }
                return payload, 0.42, "", clarify_kind
            if not bound_cards and len(cards) > 1:
                clarify_kind = "clarify_missing_entity"
                if answer_mode == "how_to":
                    text = "Which game do you want the rules for?"
                elif answer_mode == "factual":
                    text = "Which game are you asking about?"
                else:
                    text = "Which game do you want me to describe?"
                payload = {
                    "type": "doc_clarify",
                    "domain": "game",
                    "answer_mode": answer_mode,
                    "clarify_kind": clarify_kind,
                    "text": text,
                    "doc_snippets": snippets[:1],
                    "doc_source_ids": source_ids,
                    "required_terms": [],
                    "allowed_entities": [card.name for card in cards],
                    "primary_entity": "",
                    "candidate_entities": [card.name for card in cards],
                    "binding_state": binding_state,
                    "game_names": [card.name for card in cards],
                    "launchable_games": launchable_names[:],
                    "session_focus_game": session_focus_game,
                    "recommendation_reason": "",
                    "max_sentences": 1,
                }
                return payload, 0.46, "", clarify_kind
            card = target_cards[0]
            primary_entity = card.name
            allowed_entities = [card.name]
            if answer_mode == "how_to":
                text = _clean_text(card.how_to_play or card.description or "", max_len=240)
            elif answer_mode == "factual":
                normalized = f" {_normalize_text(query_text)} "
                if "launchable" in normalized or (" available " in normalized and len(cards) == 1):
                    text = f"{card.name} is {'launchable' if str(card.exec_path or '').strip() else 'not launchable'} right now."
                elif "how many players" in normalized or " players " in normalized or " player " in normalized:
                    text = f"{card.name} supports {_players_text(card)}."
                elif "tag" in normalized:
                    tags = [str(tag).strip() for tag in getattr(card, "tags", []) or [] if str(tag).strip()]
                    text = f"{card.name} is tagged as {', '.join(tags[:4])}." if tags else f"I do not have tag details for {card.name}."
                elif "activity level" in normalized:
                    text = f"{card.name} has a {str(card.activity_level or 'unknown').strip() or 'unknown'} activity level."
                else:
                    text = _clean_text(card.description or card.how_to_play or "", max_len=220)
            else:
                text = _clean_text(card.description or card.how_to_play or "", max_len=240)
            if not text:
                return {}, 0.18, "missing_game_fact_text", ""
            payload = {
                "type": "doc_answer",
                "domain": "game",
                "answer_mode": answer_mode,
                "text": text,
                "doc_snippets": snippets or [text],
                "doc_source_ids": source_ids,
                "required_terms": [card.name],
                "allowed_entities": allowed_entities,
                "primary_entity": primary_entity,
                "candidate_entities": [card.name],
                "binding_state": binding_state,
                "game_names": [card.name],
                "launchable_games": launchable_names[:],
                "session_focus_game": session_focus_game,
                "recommendation_reason": "",
                "max_sentences": 2 if answer_mode == "introduce" else 1,
            }
            return payload, 0.72, "", ""

        if cards and len(cards) >= 2:
            clarify_kind = "clarify_ambiguous_intent"
            text = f"Do you want me to compare {cards[0].name} and {cards[1].name}, or recommend one?"
            payload = {
                "type": "doc_clarify",
                "domain": "game",
                "answer_mode": "",
                "clarify_kind": clarify_kind,
                "text": text,
                "doc_snippets": snippets[:2],
                "doc_source_ids": source_ids,
                "required_terms": [],
                "allowed_entities": [card.name for card in cards],
                "primary_entity": "",
                "candidate_entities": [card.name for card in cards],
                "binding_state": binding_state,
                "game_names": [card.name for card in cards],
                "launchable_games": launchable_names[:],
                "session_focus_game": session_focus_game,
                "recommendation_reason": "",
                "max_sentences": 1,
            }
            return payload, 0.44, "", clarify_kind
        return {}, 0.18, "unsupported_game_answer_mode", ""

    def _general_kind_texts(self, selected_hits: Sequence[RetrievalHit]) -> Dict[str, List[str]]:
        buckets: Dict[str, List[str]] = {kind: [] for kind in _GENERAL_DOC_KINDS}
        for hit in selected_hits:
            text = _clean_text(hit.snippet_text, max_len=320)
            if not text:
                continue
            buckets.setdefault(_general_doc_kind_for_hit(hit), []).append(text)
        return buckets

    @staticmethod
    def _summary_points(text: str, facet_items: Sequence[str]) -> List[str]:
        points: List[str] = []
        seen = set()
        for sentence in re.split(r"(?<=[.!?])\s+", str(text or "").strip()):
            clean = _clean_text(sentence.rstrip(".!?"), max_len=140)
            key = _normalize_text(clean)
            if not clean or not key or key in seen:
                continue
            seen.add(key)
            points.append(clean)
            if len(points) >= 2:
                break
        for item in facet_items:
            clean = _clean_text(item, max_len=120)
            key = _normalize_text(clean)
            if not clean or not key or key in seen:
                continue
            seen.add(key)
            points.append(clean)
            if len(points) >= 3:
                break
        return points

    @staticmethod
    def _related_entity_roles(related_entities: Dict[str, List[str]]) -> Dict[str, str]:
        roles: Dict[str, str] = {}
        for role, entities in (related_entities or {}).items():
            for entity in entities or []:
                clean = _clean_text(entity, max_len=80)
                if clean and clean not in roles:
                    roles[clean] = str(role or "").strip()
        return roles

    def _general_related_entities(
        self,
        *,
        general_focus: str,
        primary_entity: str,
        kind_texts: Dict[str, List[str]],
        facet_items: Sequence[str],
        resolver_candidates: Sequence[EntityResolverCandidate],
    ) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
        combined_text = " ".join(sum(kind_texts.values(), []))
        related: Dict[str, List[str]] = {}
        top_candidate = resolver_candidates[0] if resolver_candidates else None
        top_type = str(getattr(top_candidate, "entity_type", "") or "").strip().lower()

        lab_names = self._unique_names(
            [
                *_extract_lab_names(combined_text),
                primary_entity if top_type == "lab" else "",
            ],
            limit=2,
        )
        if lab_names:
            related["lab"] = lab_names

        person_names = self._unique_names(
            [
                primary_entity if top_type == "person" else "",
                _strip_person_suffixes(_extract_field_sentence(" ".join(kind_texts.get("identity", [])[:2]), "Director")),
                *facet_items,
                *_extract_person_names(" ".join(kind_texts.get("team", [])[:3])),
            ],
            limit=4,
            exclude=lab_names,
        )
        if person_names:
            related["person"] = person_names
            if len(person_names) >= 1:
                related["director"] = person_names[:1]

        project_names = self._unique_names(
            [
                *_extract_project_names(" ".join(kind_texts.get("research", [])[:3])),
            ],
            limit=3,
        )
        if project_names:
            related["project"] = project_names

        if general_focus == "people" and primary_entity and top_type == "person" and primary_entity not in related.get("person", []):
            related["person"] = self._unique_names([primary_entity, *(related.get("person", []))], limit=4)
        if general_focus in {"overview", "research", "equipment", "location_contact", "news"} and primary_entity and top_type == "lab":
            related["lab"] = self._unique_names([primary_entity, *(related.get("lab", []))], limit=2)

        return related, self._related_entity_roles(related)

    @staticmethod
    def _unique_names(names: Sequence[str], *, limit: int = 6, exclude: Sequence[str] = ()) -> List[str]:
        out: List[str] = []
        seen = {_normalize_text(item) for item in exclude if _normalize_text(item)}
        for raw in names:
            clean = _clean_text(raw, max_len=80)
            normalized = _normalize_text(clean)
            if not clean or not normalized or normalized in seen:
                continue
            seen.add(normalized)
            out.append(clean)
            if len(out) >= limit:
                break
        return out

    def _general_overview_text(self, *, primary_entity: str, kind_texts: Dict[str, List[str]]) -> Tuple[str, List[str]]:
        identity_text = " ".join(kind_texts.get("identity", [])[:2])
        research_text = " ".join(kind_texts.get("research", [])[:2])
        name = _extract_field_sentence(identity_text, "Lab name") or primary_entity or "The lab"
        institution = _extract_field_sentence(identity_text, "Institution")
        location = _extract_field_sentence(identity_text, "Location")
        director = _strip_person_suffixes(_extract_field_sentence(identity_text, "Director"))
        vision = _extract_field_sentence(identity_text, "Research vision") or _extract_field_sentence(research_text, "Research vision") or research_text
        vision = _clean_text(vision, max_len=220)
        sentence1 = f"{name} is a research lab"
        if institution:
            sentence1 += f" at {institution}"
        elif location:
            sentence1 += f" in {location}"
        if director:
            sentence1 += f" directed by {director}"
        sentence1 = sentence1.rstrip(".") + "."
        sentence2 = ""
        if vision:
            sentence2 = vision
            if not sentence2.endswith("."):
                sentence2 += "."
        elif research_text:
            sentence2 = _clean_text(research_text, max_len=220)
            if sentence2 and not sentence2.endswith("."):
                sentence2 += "."
        return _clean_text(f"{sentence1} {sentence2}".strip(), max_len=320), self._unique_names([name, director], limit=2)

    def _general_person_intro_text(self, *, primary_entity: str, kind_texts: Dict[str, List[str]]) -> Tuple[str, List[str]]:
        team_text = " ".join(kind_texts.get("team", [])[:3])
        identity_text = " ".join(kind_texts.get("identity", [])[:2])
        role = _extract_field_sentence(team_text, "Role") or _extract_field_sentence(identity_text, "Director role")
        academic = _extract_field_sentence(team_text, "Academic position")
        subject = primary_entity or _strip_person_suffixes(_extract_field_sentence(team_text, "Name"))
        sentence1 = f"{subject} is {role}" if role else f"{subject} is part of the BioAdaptive Interface Lab team"
        if academic and academic.lower() not in sentence1.lower():
            sentence2 = f"{subject} is also {academic}."
        else:
            background = _clean_text(team_text, max_len=220)
            sentence2 = ""
            if "Background:" in background:
                background = background.split("Background:", 1)[-1].strip(" .-")
                background = _clean_text(background, max_len=180)
                if background:
                    sentence2 = f"{subject} has a background in {background}."
        text = sentence1.rstrip(".") + "."
        if sentence2:
            text = f"{text} {sentence2}"
        return _clean_text(text, max_len=320), self._unique_names([subject], limit=1)

    def _general_people_text(self, *, primary_entity: str, kind_texts: Dict[str, List[str]]) -> Tuple[str, List[str]]:
        identity_text = " ".join(kind_texts.get("identity", [])[:2])
        team_texts = kind_texts.get("team", [])
        director = _strip_person_suffixes(_extract_field_sentence(identity_text, "Director")) or _strip_person_suffixes(_extract_field_sentence(" ".join(team_texts[:2]), "Name"))
        names = self._unique_names(_extract_person_names(" ".join(team_texts[:3])), limit=6, exclude=[director])
        listed = names[:3]
        if director:
            sentence1 = f"The lab is directed by {director}."
        else:
            sentence1 = "The lab has an active research team."
        if listed:
            if len(listed) == 1:
                sentence2 = f"Current researchers and collaborators include {listed[0]}."
            elif len(listed) == 2:
                sentence2 = f"Current researchers and collaborators include {listed[0]} and {listed[1]}."
            else:
                sentence2 = f"Current researchers and collaborators include {listed[0]}, {listed[1]}, and {listed[2]}."
        else:
            sentence2 = ""
        return _clean_text(f"{sentence1} {sentence2}".strip(), max_len=320), self._unique_names([director, *listed], limit=4)

    def _general_research_text(self, *, query_text: str, primary_entity: str, kind_texts: Dict[str, List[str]]) -> Tuple[str, List[str]]:
        identity_text = " ".join(kind_texts.get("identity", [])[:2])
        research_text = " ".join(kind_texts.get("research", [])[:3])
        subject = primary_entity or _extract_field_sentence(identity_text, "Lab name") or "The lab"
        normalized = f" {_normalize_text(query_text)} "
        combined = _normalize_text(" ".join((identity_text, research_text)))
        if (" social robotics " in normalized or " robotics " in normalized) and (" social robotics " in f" {combined} " or " robotics " in f" {combined} "):
            return f"Yes, {subject} works on social robotics.", self._unique_names([subject], limit=1)
        vision = _extract_field_sentence(identity_text, "Research vision")
        if vision:
            if vision.lower().startswith("the lab ") or vision.lower().startswith(subject.lower()):
                return vision.rstrip(".") + ".", self._unique_names([subject], limit=1)
            return _clean_text(f"{subject} works on {vision}", max_len=320).rstrip(".") + ".", self._unique_names([subject], limit=1)
        summary = _clean_text(research_text, max_len=260)
        if summary:
            if normalized.startswith(" does ") or normalized.startswith(" do "):
                return _clean_text(f"Yes, {summary}", max_len=320).rstrip(".") + ".", self._unique_names([subject], limit=1)
            return summary.rstrip(".") + ".", self._unique_names([subject], limit=1)
        return "", []

    def _general_equipment_text(self, *, primary_entity: str, kind_texts: Dict[str, List[str]]) -> Tuple[str, List[str]]:
        equipment_text = " ".join(kind_texts.get("equipment", [])[:3])
        subject = primary_entity or "The lab"
        found: List[str] = []
        labels = (
            ("EEG systems", (" eeg ",)),
            ("EMG armbands", (" emg ",)),
            ("VR headsets", (" vr headset ", " vr headsets ")),
            ("Kinect devices", (" kinect ",)),
            ("exergaming kits", (" exergaming ", " kit ", " kits ")),
        )
        haystack = f" {_normalize_text(equipment_text)} "
        for label, markers in labels:
            if any(marker in haystack for marker in markers):
                found.append(label)
        unique_items = self._unique_names(found, limit=5)
        if not unique_items:
            summary = _clean_text(equipment_text, max_len=240)
            return (summary.rstrip(".") + ".") if summary else "", []
        if len(unique_items) == 1:
            text = f"{subject} has {unique_items[0]}."
        elif len(unique_items) == 2:
            text = f"{subject} has {unique_items[0]} and {unique_items[1]}."
        else:
            text = f"{subject} has {', '.join(unique_items[:-1])}, and {unique_items[-1]}."
        return _clean_text(text, max_len=320), unique_items

    def _general_location_text(self, *, primary_entity: str, kind_texts: Dict[str, List[str]]) -> Tuple[str, List[str]]:
        identity_text = " ".join(kind_texts.get("identity", [])[:2] + kind_texts.get("location_contact", [])[:2])
        name = _extract_field_sentence(identity_text, "Lab name") or primary_entity or "The lab"
        institution = _extract_field_sentence(identity_text, "Institution")
        location = _extract_field_sentence(identity_text, "Location")
        if institution and location:
            return f"{name} is at {institution} in {location}.", self._unique_names([name], limit=1)
        if location:
            return f"{name} is located at {location}.", self._unique_names([name], limit=1)
        if institution:
            return f"{name} is part of {institution}.", self._unique_names([name], limit=1)
        return "", []

    def _general_news_text(self, *, primary_entity: str, kind_texts: Dict[str, List[str]]) -> Tuple[str, List[str]]:
        news_items = []
        for text in kind_texts.get("news", [])[:2]:
            clean = _clean_text(text, max_len=200)
            if clean:
                news_items.append(clean.rstrip(".") + ".")
        if not news_items:
            return "", []
        if len(news_items) == 1:
            return _clean_text(f"Recent news for {primary_entity or 'the lab'} includes {news_items[0]}", max_len=320), self._unique_names([primary_entity], limit=1)
        return _clean_text(f"Recent news for {primary_entity or 'the lab'} includes {news_items[0]} {news_items[1]}", max_len=320), self._unique_names([primary_entity], limit=1)

    def _build_general_payload(
        self,
        *,
        query_text: str,
        answer_mode: str,
        general_focus: str,
        selected_hits: Sequence[RetrievalHit],
        candidate_entities: Sequence[str],
        resolver_candidates: Sequence[EntityResolverCandidate],
        rewrite_candidates: Sequence[str],
        resolver_reason: str,
    ) -> Tuple[Dict[str, Any], float, str, str]:
        snippets = [_clean_text(hit.snippet_text, max_len=260) for hit in selected_hits if _clean_text(hit.snippet_text, max_len=260)]
        if not snippets:
            return {}, 0.12, "no_general_snippets", ""
        candidate_entity_list = list(candidate_entities[:3]) if candidate_entities else [item.canonical for item in resolver_candidates if item.canonical][:3]
        primary_entity = resolver_candidates[0].canonical if resolver_candidates else (candidate_entity_list[0] if candidate_entity_list else "")
        coverage = self._lexical_coverage(query_text, selected_hits)
        top_dense = max((float(hit.dense_score or 0.0) for hit in selected_hits), default=0.0)
        has_sparse = any(int(hit.sparse_rank or 0) > 0 for hit in selected_hits)
        query_keywords = [token for token in _query_keywords(query_text) if token not in {"according", "docs", "doc", "manual", "know"}]
        matched_keywords = sum(
            1
            for token in query_keywords
            if any(token in _normalize_text(hit.snippet_text) or token in _normalize_text(hit.title) for hit in selected_hits)
        )
        if coverage < 0.34 and not has_sparse and top_dense < 0.75:
            return {}, 0.18, "low_general_coverage", ""
        if query_keywords and matched_keywords <= 0 and not candidate_entity_list:
            return {}, 0.16, "general_keywords_unmatched", ""

        kind_texts = self._general_kind_texts(selected_hits)
        doc_kinds = [kind for kind in (_general_doc_kind_for_hit(hit) for hit in selected_hits) if kind]
        required_terms: List[str] = []
        facet_items: List[str] = []
        text = ""
        if general_focus == "people" and primary_entity and resolver_candidates and resolver_candidates[0].entity_type == "person":
            text, facet_items = self._general_person_intro_text(primary_entity=primary_entity, kind_texts=kind_texts)
            required_terms = [primary_entity] if primary_entity else []
        elif general_focus == "people":
            text, facet_items = self._general_people_text(primary_entity=primary_entity, kind_texts=kind_texts)
            required_terms = facet_items[:1]
        elif general_focus == "research":
            text, facet_items = self._general_research_text(query_text=query_text, primary_entity=primary_entity, kind_texts=kind_texts)
        elif general_focus == "equipment":
            text, facet_items = self._general_equipment_text(primary_entity=primary_entity, kind_texts=kind_texts)
        elif general_focus == "location_contact":
            text, facet_items = self._general_location_text(primary_entity=primary_entity, kind_texts=kind_texts)
        elif general_focus == "news":
            text, facet_items = self._general_news_text(primary_entity=primary_entity, kind_texts=kind_texts)
        else:
            text, facet_items = self._general_overview_text(primary_entity=primary_entity, kind_texts=kind_texts)
            required_terms = [primary_entity] if primary_entity else []

        text = _clean_text(text, max_len=320)
        if not text:
            return {}, 0.2, f"general_focus_{general_focus or 'overview'}_empty", ""
        summary_points = self._summary_points(text, facet_items)
        related_entities, related_entity_roles = self._general_related_entities(
            general_focus=general_focus or "overview",
            primary_entity=primary_entity,
            kind_texts=kind_texts,
            facet_items=facet_items,
            resolver_candidates=resolver_candidates,
        )

        answerability = 0.28
        answerability += min(0.42, coverage * 0.52)
        if has_sparse:
            answerability += 0.18
        if top_dense >= 0.75:
            answerability += 0.14
        elif top_dense >= 0.58:
            answerability += 0.08
        if len(selected_hits) >= 2:
            answerability += 0.06
        if any(kind in {"identity", "team", "research", "equipment"} for kind in doc_kinds):
            answerability += 0.08
        answerability = max(0.0, min(1.0, answerability))
        payload = {
            "type": "doc_answer",
            "domain": "general",
            "answer_mode": answer_mode or "factual",
            "general_focus": general_focus or "overview",
            "text": text,
            "summary_text": text,
            "summary_points": summary_points[:3],
            "reason_text": summary_points[0] if summary_points else "",
            "doc_snippets": snippets,
            "doc_source_ids": [hit.chunk_id for hit in selected_hits],
            "required_terms": required_terms[:2],
            "allowed_entities": candidate_entity_list[:],
            "primary_entity": primary_entity,
            "candidate_entities": candidate_entity_list[:],
            "related_entities": related_entities,
            "related_entity_roles": related_entity_roles,
            "binding_state": "doc_grounded",
            "max_sentences": 2,
            "candidate_entity_rewrites": list(rewrite_candidates),
            "resolver_reason": resolver_reason,
            "facet_items": facet_items[:4],
            "general_doc_kinds": doc_kinds[:4],
        }
        return payload, answerability, "", ""

    def _build_general_clarify_payload(
        self,
        *,
        query_text: str,
        general_focus: str,
        selected_hits: Sequence[RetrievalHit],
        candidate_entities: Sequence[str],
        clarify_kind: str,
        message: str,
        resolver_candidates: Sequence[EntityResolverCandidate],
        rewrite_candidates: Sequence[str],
        resolver_reason: str,
    ) -> Dict[str, Any]:
        snippets = [_clean_text(hit.snippet_text, max_len=220) for hit in selected_hits[:2] if _clean_text(hit.snippet_text, max_len=220)]
        candidate_entity_list = list(candidate_entities[:3]) if candidate_entities else [item.canonical for item in resolver_candidates if item.canonical][:3]
        primary_entity = resolver_candidates[0].canonical if resolver_candidates else (candidate_entity_list[0] if candidate_entity_list else "")
        related_entities, related_entity_roles = self._general_related_entities(
            general_focus=general_focus or "overview",
            primary_entity=primary_entity,
            kind_texts=self._general_kind_texts(selected_hits),
            facet_items=candidate_entity_list,
            resolver_candidates=resolver_candidates,
        )
        resume_strategy = "confirm_candidate"
        if clarify_kind == "clarify_typo_correction":
            resume_strategy = "confirm_rewrite"
        payload: Dict[str, Any] = {
            "type": "doc_clarify",
            "domain": "general",
            "answer_mode": "factual",
            "general_focus": general_focus or "overview",
            "clarify_kind": clarify_kind,
            "text": _clean_text(message, max_len=180),
            "summary_text": _clean_text(message, max_len=180),
            "summary_points": [],
            "doc_snippets": snippets,
            "doc_source_ids": [hit.chunk_id for hit in selected_hits[:2]],
            "required_terms": [],
            "allowed_entities": candidate_entity_list[:],
            "primary_entity": primary_entity if clarify_kind == "clarify_typo_correction" else "",
            "candidate_entities": candidate_entity_list[:],
            "target_general_focus": general_focus or "overview",
            "target_entities": candidate_entity_list[:],
            "related_entities": related_entities,
            "related_entity_roles": related_entity_roles,
            "resume_strategy": resume_strategy,
            "binding_state": "doc_grounded",
            "rewrite_suggestion": primary_entity if clarify_kind == "clarify_typo_correction" else "",
            "candidate_entity_rewrites": list(rewrite_candidates),
            "resolver_reason": resolver_reason,
            "max_sentences": 1,
            "general_doc_kinds": [_general_doc_kind_for_hit(hit) for hit in selected_hits[:2]],
        }
        return payload

    def probe(
        self,
        query_text: str,
        *,
        focus_state: Any = None,
        session_state: Any = None,
        user_profile: Optional[Dict[str, Any]] = None,
    ) -> DocProbe:
        probe = DocProbe(query=_clean_text(query_text, max_len=240))
        if not self.ready or not query_text.strip():
            probe.stage1_reason = self.error or "doc_rag_not_ready"
            probe.fallback_reason = probe.stage1_reason
            return probe

        normalized_query = _normalize_text(query_text)
        initial_hits = self._fuse_hits(query_text, normalized_query=normalized_query)
        resolver_attempted = bool(self._entity_registry) and _should_attempt_entity_resolution(query_text)
        rewrite_candidates: List[str] = []
        resolver_candidates: List[EntityResolverCandidate] = []
        rewritten_query = ""
        if resolver_attempted:
            normalized_query, rewrite_candidates, resolver_candidates, rewritten_query = self._resolve_entities(
                query_text,
                initial_hits=initial_hits,
            )
        resolver_top = resolver_candidates[0] if resolver_candidates else None
        rewrite_query = rewritten_query if resolver_top is not None and resolver_top.fused_score >= 0.84 else ""
        retrieval_queries = [query_text]
        if normalized_query and _normalize_text(normalized_query) != _normalize_text(query_text):
            retrieval_queries.append(normalized_query)
        if rewrite_query:
            retrieval_queries.append(rewrite_query)
        hits = self._fuse_hits(query_text, normalized_query=normalized_query, rewritten_query=rewrite_query)
        probe.top_hit_ids = [hit.chunk_id for hit in hits[:6]]
        probe.resolver_attempted = resolver_attempted
        probe.normalized_query = normalized_query
        probe.retrieval_queries = retrieval_queries[:]
        probe.entity_candidates = [item.canonical for item in resolver_candidates]
        probe.entity_registry_hits = [
            {
                "canonical": item.canonical,
                "domain": item.domain,
                "entity_type": item.entity_type,
                "fused_score": round(float(item.fused_score), 4),
                "string_similarity": round(float(item.string_similarity), 4),
                "partial_name_score": round(float(item.partial_name_score), 4),
                "retrieval_corroboration": round(float(item.retrieval_corroboration), 4),
                "alias_strength": item.alias_strength,
                "resolution_mode": item.resolution_mode,
            }
            for item in resolver_candidates[:3]
        ]
        probe.candidate_entity_rewrites = list(rewrite_candidates)
        probe.entity_similarity_score = float(resolver_top.string_similarity) if resolver_top is not None else 0.0
        probe.entity_resolution_confidence = float(resolver_top.fused_score) if resolver_top is not None else 0.0
        probe.rewritten_query = rewrite_query
        probe.resolution_mode = str(resolver_top.resolution_mode) if resolver_top is not None else "none"
        probe.resolver_reason = str(resolver_top.resolver_reason) if resolver_top is not None else ""
        explicit_entities = self._explicit_game_entities(query_text)
        bound_entities = self._bound_entities(query_text, focus_state=focus_state)
        force_doc_reason = _force_doc_reason(query_text, explicit_entities=explicit_entities, has_property_question=_is_factual_query(query_text))
        probe.force_doc_reason = force_doc_reason or None
        probe.query_doc_affinity = self._query_doc_affinity(
            query_text,
            explicit_entities=explicit_entities,
            force_doc_reason=force_doc_reason,
            entity_resolution_confidence=probe.entity_resolution_confidence,
        )
        probe.retrieval_support = self._retrieval_support(hits)
        probe.entity_binding_strength = self._entity_binding_strength(query_text, explicit_entities=explicit_entities, hits=hits, focus_state=focus_state)
        if resolver_top is not None:
            if resolver_top.alias_strength != "weak" and resolver_top.resolution_mode in {"exact", "fuzzy"}:
                probe.entity_binding_strength = max(probe.entity_binding_strength, min(0.95, 0.45 + (0.5 * resolver_top.fused_score)))
            elif resolver_top.resolution_mode == "partial_name":
                probe.entity_binding_strength = max(probe.entity_binding_strength, min(0.72, 0.3 + (0.4 * resolver_top.partial_name_score)))
        rescue_applied = self._retrieval_rescue_applies(
            query_text,
            support=probe.retrieval_support,
            hits=hits,
            affinity=probe.query_doc_affinity,
        )
        probe.routing_confidence = max(
            0.0,
            min(
                1.0,
                (0.45 * probe.query_doc_affinity)
                + (0.35 * probe.retrieval_support)
                + (0.20 * probe.entity_binding_strength)
                + (0.15 if force_doc_reason else 0.0),
            ),
        )
        if resolver_top is not None and rewrite_query:
            probe.routing_confidence = max(probe.routing_confidence, min(1.0, self.routing_threshold + 0.08))
        if rescue_applied:
            probe.routing_confidence = max(
                probe.routing_confidence,
                min(1.0, self.routing_threshold + 0.04),
            )
        probe.stage1_reason = self._stage1_reason(
            routing_confidence=probe.routing_confidence,
            support=probe.retrieval_support,
            affinity=probe.query_doc_affinity,
            entity_binding=probe.entity_binding_strength,
            force_doc_reason=force_doc_reason,
            rescue_applied=rescue_applied,
        )
        doc_like_query = self._query_is_doc_like(query_text, force_doc_reason=force_doc_reason) or bool(resolver_attempted)
        resolver_gap = 0.0
        if len(resolver_candidates) >= 2:
            resolver_gap = max(0.0, float(resolver_candidates[0].fused_score) - float(resolver_candidates[1].fused_score))
        if probe.routing_confidence < self.routing_threshold and not force_doc_reason and not doc_like_query:
            probe.stage1_result = "not_doc"
            probe.doc_confidence = probe.routing_confidence
            probe.fallback_reason = probe.stage1_reason
            return probe

        probe.stage1_result = "doc_candidate"
        probe.domain = self._domain_from_hits(hits)
        if probe.domain == "unknown" and resolver_top is not None:
            probe.domain = resolver_top.domain
        if force_doc_reason in {"docs_reference", "manual_reference"} and not explicit_entities:
            if any(hit.domain == "general" for hit in hits[:3]):
                probe.domain = "general"
        if force_doc_reason in {"availability_query", "recommend_query", "compare_query", "how_to_query"} and self.game_catalog is not None:
            probe.domain = "game"
        candidate_entities = (
            bound_entities[:]
            if bound_entities
            else self._game_candidates_from_hits(hits, explicit_entities=explicit_entities, focus_state=focus_state)
        ) if probe.domain in _GAME_DOMAINS else []
        if probe.domain == "general":
            general_focus_candidates = self._focus_candidates(focus_state)
            if bound_entities:
                candidate_entities = list(bound_entities[:3])
            elif resolver_candidates:
                candidate_entities = [item.canonical for item in resolver_candidates[:3]]
            elif general_focus_candidates:
                candidate_entities = list(general_focus_candidates[:3])
        probe.candidate_entities = candidate_entities[:]
        answer_mode = self._answer_mode(query_text, domain=probe.domain, candidate_entities=candidate_entities, hits=hits)
        probe.answer_mode = answer_mode
        if probe.domain == "general":
            probe.general_focus = self._general_focus(
                query_text,
                answer_mode=answer_mode or "factual",
                resolver_candidates=resolver_candidates,
                candidate_entities=candidate_entities,
            )
        selected_hits = self._select_evidence(
            hits,
            query_text=rewrite_query or normalized_query or query_text,
            answer_mode=answer_mode or "factual",
            domain=probe.domain,
            candidate_entities=candidate_entities,
            general_focus=probe.general_focus,
        )
        probe.selected_evidence_ids = [hit.chunk_id for hit in selected_hits]
        if probe.domain == "general":
            probe.general_doc_kinds = [_general_doc_kind_for_hit(hit) for hit in selected_hits]
            referential_general_clarify = (
                _ENTITY_REFERENCE_RE.search(query_text or "")
                and not candidate_entities
                and resolver_top is None
                and probe.general_focus in {"overview", "people"}
                and not any(
                    marker in f" {_normalize_text(query_text)} "
                    for marker in (
                        " equipment ",
                        " tools ",
                        " devices ",
                        " sensors ",
                        " research ",
                        " social robotics ",
                        " researchers ",
                        " team ",
                        " members ",
                        " collaborators ",
                        " contact ",
                        " email ",
                        " phone ",
                        " where is ",
                    )
                )
            )
            if referential_general_clarify:
                message = "Could you clarify which lab or person you mean?"
                payload = self._build_general_clarify_payload(
                    query_text=query_text,
                    general_focus=probe.general_focus,
                    selected_hits=selected_hits,
                    candidate_entities=candidate_entities,
                    clarify_kind="clarify_missing_entity",
                    message=message,
                    resolver_candidates=resolver_candidates,
                    rewrite_candidates=rewrite_candidates,
                    resolver_reason=probe.resolver_reason,
                )
                probe.payload = payload
                probe.stage2_result = "doc_clarify"
                probe.answerability_confidence = 0.4
                probe.doc_confidence = min(probe.routing_confidence, probe.answerability_confidence)
                probe.clarify_kind = "clarify_missing_entity"
                probe.response_text = message
                probe.fallback_reason = "clarify_missing_entity"
                probe.open_world_fallback_blocked = True
                probe.doc_failure_mode = "clarify_missing_entity"
                return probe
            if _person_lookup_query(query_text) and (
                resolver_top is None
                or resolver_top.entity_type != "person"
                or resolver_top.fused_score < 0.54
            ):
                probe.stage2_result = "no_evidence"
                probe.answerability_confidence = 0.18
                probe.doc_confidence = min(probe.routing_confidence, probe.answerability_confidence)
                probe.response_text = _GENERAL_DOC_FAILURE_TEXT
                probe.fallback_reason = "person_not_grounded"
                probe.open_world_fallback_blocked = True
                probe.doc_failure_mode = "person_not_grounded"
                return probe

        if probe.domain == "general" and resolver_top is not None:
            rewrite_differs = bool(rewrite_query) and _normalize_text(rewrite_query) != _normalize_text(query_text)
            person_fragment_tokens = [token for token in _tokenize(_extract_person_query_fragment(query_text)) if len(token) >= 2]
            typo_clarify = (
                _entity_lookup_query(query_text)
                and resolver_top.alias_strength != "weak"
                and resolver_top.resolution_mode == "fuzzy"
                and not rewrite_query
                and (rewrite_differs or resolver_top.string_similarity < 0.82)
                and 0.62 <= resolver_top.fused_score < 0.84
            )
            missing_entity_clarify = (
                _person_lookup_query(query_text)
                and resolver_top.entity_type == "person"
                and resolver_top.fused_score >= 0.54
                and len(person_fragment_tokens) < 2
                and (
                    resolver_top.alias_strength == "weak"
                    or resolver_top.resolution_mode == "partial_name"
                    or resolver_gap < 0.18
                )
            )
            if typo_clarify:
                message = f"Did you mean {resolver_top.canonical}?"
                payload = self._build_general_clarify_payload(
                    query_text=query_text,
                    general_focus=probe.general_focus,
                    selected_hits=selected_hits,
                    candidate_entities=candidate_entities,
                    clarify_kind="clarify_typo_correction",
                    message=message,
                    resolver_candidates=resolver_candidates,
                    rewrite_candidates=rewrite_candidates,
                    resolver_reason=probe.resolver_reason,
                )
                probe.payload = payload
                probe.stage2_result = "doc_clarify"
                probe.answerability_confidence = 0.46
                probe.doc_confidence = min(probe.routing_confidence, probe.answerability_confidence)
                probe.clarify_kind = "clarify_typo_correction"
                probe.response_text = message
                probe.fallback_reason = "clarify_typo_correction"
                probe.open_world_fallback_blocked = True
                probe.doc_failure_mode = "clarify_typo_correction"
                return probe
            if missing_entity_clarify:
                message = f"Do you mean {resolver_top.canonical}?"
                payload = self._build_general_clarify_payload(
                    query_text=query_text,
                    general_focus=probe.general_focus,
                    selected_hits=selected_hits,
                    candidate_entities=candidate_entities,
                    clarify_kind="clarify_missing_entity",
                    message=message,
                    resolver_candidates=resolver_candidates,
                    rewrite_candidates=rewrite_candidates,
                    resolver_reason=probe.resolver_reason,
                )
                probe.payload = payload
                probe.stage2_result = "doc_clarify"
                probe.answerability_confidence = 0.44
                probe.doc_confidence = min(probe.routing_confidence, probe.answerability_confidence)
                probe.clarify_kind = "clarify_missing_entity"
                probe.response_text = message
                probe.fallback_reason = "clarify_missing_entity"
                probe.open_world_fallback_blocked = True
                probe.doc_failure_mode = "clarify_missing_entity"
                return probe

        if probe.domain == "unknown":
            probe.stage2_result = "no_evidence"
            probe.answerability_confidence = 0.14
            probe.doc_confidence = min(probe.routing_confidence, probe.answerability_confidence)
            probe.response_text = _GENERAL_DOC_FAILURE_TEXT
            probe.fallback_reason = "unknown_domain"
            probe.open_world_fallback_blocked = True
            probe.doc_failure_mode = "unknown_domain"
            return probe

        if probe.domain in _GAME_DOMAINS:
            payload, answerability, fallback_reason, clarify_kind = self._build_game_payload(
                query_text=query_text,
                answer_mode=answer_mode,
                selected_hits=selected_hits,
                candidate_entities=candidate_entities,
                bound_entities=bound_entities,
                focus_state=focus_state,
                user_profile=user_profile,
            )
        else:
            payload, answerability, fallback_reason, clarify_kind = self._build_general_payload(
                query_text=rewrite_query or normalized_query or query_text,
                answer_mode=answer_mode or "factual",
                general_focus=probe.general_focus,
                selected_hits=selected_hits,
                candidate_entities=candidate_entities,
                resolver_candidates=resolver_candidates,
                rewrite_candidates=rewrite_candidates,
                resolver_reason=probe.resolver_reason,
            )
        probe.answerability_confidence = answerability
        probe.doc_confidence = min(probe.routing_confidence, probe.answerability_confidence)
        probe.fallback_reason = fallback_reason
        probe.clarify_kind = clarify_kind

        if payload:
            payload_type = str(payload.get("type") or "").strip()
            snippets = [str(item).strip() for item in payload.get("doc_snippets", []) or [] if str(item).strip()]
            if payload_type == "doc_answer" and not snippets:
                probe.stage2_result = "no_evidence"
                probe.response_text = _GENERAL_DOC_FAILURE_TEXT
                probe.fallback_reason = fallback_reason or "no_valid_snippets"
                probe.open_world_fallback_blocked = True
                probe.doc_failure_mode = "no_valid_snippets"
                return probe
            if payload_type == "doc_answer" and answerability < self.answer_threshold:
                probe.stage2_result = "no_evidence"
                probe.response_text = _GENERAL_DOC_FAILURE_TEXT
                probe.fallback_reason = fallback_reason or "answerability_below_threshold"
                probe.open_world_fallback_blocked = True
                probe.doc_failure_mode = "answerability_below_threshold"
                return probe
            payload["doc_confidence"] = round(float(probe.doc_confidence), 4)
            probe.payload = payload
            if str(payload.get("type") or "").strip() == "doc_clarify":
                probe.stage2_result = "doc_clarify"
                probe.open_world_fallback_blocked = True
                probe.doc_failure_mode = probe.clarify_kind or "doc_clarify"
            else:
                probe.stage2_result = "doc_answer"
            probe.response_text = str(payload.get("text") or "").strip()
            return probe

        probe.stage2_result = "no_evidence"
        probe.response_text = _GENERAL_DOC_FAILURE_TEXT
        if fallback_reason:
            probe.fallback_reason = fallback_reason
        probe.open_world_fallback_blocked = True
        probe.doc_failure_mode = fallback_reason or "no_evidence"
        return probe
