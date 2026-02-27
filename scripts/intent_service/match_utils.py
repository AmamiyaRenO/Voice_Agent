#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import uuid
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional


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


def normalize_match_text(text: str) -> str:
    value = (text or "").strip().lower()
    if not value:
        return ""
    value = value.strip(_PUNCT_TRIM_CHARS)
    value = re.sub(r"[\/_|]+", " ", value)
    value = re.sub(r"[\s]+", " ", value)
    return value.strip()


def clean_game_candidate(text: str) -> str:
    value = normalize_match_text(text)
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
        "b": "1",
        "f": "1",
        "p": "1",
        "v": "1",
        "c": "2",
        "g": "2",
        "j": "2",
        "k": "2",
        "q": "2",
        "s": "2",
        "x": "2",
        "z": "2",
        "d": "3",
        "t": "3",
        "l": "4",
        "m": "5",
        "n": "5",
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
    tokens = [t for t in normalize_match_text(text).split(" ") if t]
    codes = [_phonetic_code(t) for t in tokens]
    return " ".join([c for c in codes if c])


def _consonant_skeleton(text: str) -> str:
    value = re.sub(r"[^a-z]", "", (text or "").lower())
    if not value:
        return ""
    return re.sub(r"[aeiou]", "", value)


def _token_jaccard(a: str, b: str) -> float:
    sa = {t for t in normalize_match_text(a).split(" ") if t}
    sb = {t for t in normalize_match_text(b).split(" ") if t}
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    if union == 0:
        return 0.0
    return float(inter) / float(union)


def similarity_score(candidate: str, alias: str) -> float:
    cand = normalize_match_text(candidate)
    ali = normalize_match_text(alias)
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


def candidate_variants(text: str) -> List[str]:
    cleaned = clean_game_candidate(text)
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
        normalized = normalize_match_text(value)
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


def best_exit_similarity(text: str, exit_keywords: List[str]) -> float:
    variants = candidate_variants(text)
    if not variants:
        normalized = normalize_match_text(text)
        if normalized:
            variants = [normalized]
    if not variants:
        return 0.0

    best = 0.0
    for variant in variants:
        for keyword in exit_keywords or []:
            target = normalize_match_text(keyword)
            if not target:
                continue
            score = similarity_score(variant, target)
            if score > best:
                best = score
    return best


def new_corr_id() -> str:
    return uuid.uuid4().hex


def extract_first_json_object(text: str) -> Optional[Dict[str, Any]]:
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


def normalize_intent_label(value: str) -> str:
    key = (value or "").strip().upper()
    if key in {"BACK_HOME", "EXIT", "EXIT_GAME", "QUIT", "STOP", "GO_HOME"}:
        return "BACK_HOME"
    if key in {"LAUNCH", "LAUNCH_GAME", "OPEN_GAME", "START_GAME", "PLAY_GAME"}:
        return "LAUNCH_GAME"
    return "QUERY"
