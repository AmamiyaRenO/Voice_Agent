#!/usr/bin/env python3
from __future__ import annotations

import re

_WHITESPACE_RE = re.compile(r"\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[\.\!\?])\s+")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.;:!?])")
_TRAILING_CONNECTOR_RE = re.compile(
    r"(?:\b(?:and|or|but|to|of|with|for|in|on|at|through|about|into|from)\b[\s,;:]*)+$",
    re.IGNORECASE,
)
_LEADING_STAGE_CUE_RE = re.compile(
    r"^\s*(?:\([^)]{1,180}\)|\[[^\]]{1,180}\]|\*[^*]{1,180}\*)[\s,;:!\.\-]*"
)
_WRAPPED_STAGE_RE = re.compile(r"^\s*(?:\([^)]{1,260}\)|\[[^\]]{1,260}\]|\*[^*]{1,260}\*)\s*$")
_STAGE_HINT_RE = re.compile(
    r"\b("
    r"chuckle|chuckles|laugh|laughs|sigh|sighs|whisper|whispers|"
    r"tone|voice|rasp|raspy|smile|smiles|grin|grins|giggle|giggles|"
    r"narrat|stage|emotion|mood|sarcastic|dramatic|warmly|softly"
    r")\b",
    re.IGNORECASE,
)
_EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\u2600-\u27BF"
    "]+",
    re.UNICODE,
)
_KEYCAP_EMOJI_RE = re.compile(r"[#*0-9]\ufe0f?\u20e3")
_EMOJI_SYMBOL_RE = re.compile(r"[\u00a9\u00ae\u2122]")
_EMOJI_MODIFIER_RE = re.compile(r"[\u200d\ufe0e\ufe0f\u20e3\U0001F3FB-\U0001F3FF]")


def compress_reply_for_latency(text: str, max_sentences: int, max_chars: int) -> str:
    normalized = _WHITESPACE_RE.sub(" ", (text or "").strip())
    if not normalized:
        return ""

    compact = normalized
    if max_sentences > 0:
        parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(compact) if p and p.strip()]
        if parts:
            compact = " ".join(parts[:max_sentences]).strip()

    if max_chars > 0 and len(compact) > max_chars:
        search_start = max(16, max_chars - 20)
        split = -1
        punctuation = ".!?;: "
        for i in range(max_chars, search_start - 1, -1):
            if compact[i] in punctuation:
                split = i + 1
                break
        if split <= 0:
            split = max_chars
        compact = compact[:split].strip()

    return compact


def trim_trailing_connectors(text: str) -> str:
    if not text:
        return ""
    trimmed = _TRAILING_CONNECTOR_RE.sub("", text).strip()
    return trimmed if trimmed else text.strip()


def compress_reply_by_words(text: str, max_words: int) -> str:
    if max_words <= 0:
        return (text or "").strip()
    words = [w for w in (text or "").strip().split(" ") if w]
    if len(words) <= max_words:
        return (text or "").strip()
    return " ".join(words[:max_words]).strip()


def _strip_leading_stage_cues(text: str) -> str:
    current = (text or "").strip()
    while current:
        match = _LEADING_STAGE_CUE_RE.match(current)
        if not match:
            break
        cue = match.group(0) or ""
        if not _STAGE_HINT_RE.search(cue):
            break
        next_value = current[match.end() :].lstrip()
        if next_value == current:
            break
        current = next_value
    return current


def sanitize_tts_text(text: str) -> str:
    current = (text or "").strip()
    if not current:
        return ""

    current = _strip_leading_stage_cues(current)
    if not current:
        return ""

    # Drop pure stage-direction outputs such as "(A low chuckle, ...)".
    if _WRAPPED_STAGE_RE.match(current) and _STAGE_HINT_RE.search(current):
        return ""

    current = _KEYCAP_EMOJI_RE.sub(" ", current)
    current = _EMOJI_SYMBOL_RE.sub(" ", current)
    current = _EMOJI_RE.sub(" ", current)
    current = _EMOJI_MODIFIER_RE.sub("", current)
    current = _WHITESPACE_RE.sub(" ", current).strip()
    current = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", current)

    return current.strip()
