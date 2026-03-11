from __future__ import annotations

import difflib
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

try:
    from moonshine_voice import (
        Transcriber as MoonshineTranscriber,
        TranscriptEventListener as MoonshineTranscriptEventListener,
        get_model_for_language as moonshine_get_model_for_language,
        string_to_model_arch as moonshine_string_to_model_arch,
    )
except Exception:  # pragma: no cover - optional at runtime
    MoonshineTranscriber = None
    MoonshineTranscriptEventListener = object
    moonshine_get_model_for_language = None
    moonshine_string_to_model_arch = None


STREAMING_ASR_MODE_MOONSHINE_SMALL = "moonshine-small"
STREAMING_ASR_MODE_MOONSHINE_MEDIUM = "moonshine-medium"
STREAMING_ASR_MODE_LIVE_CAPTIONS = "live-captions"
STREAMING_ASR_MODE_API = "api"
STREAMING_ASR_MODE_GEMINI_LIVE = "gemini-live"
STREAMING_ASR_SUPPORTED_MODES = [
    STREAMING_ASR_MODE_MOONSHINE_SMALL,
    STREAMING_ASR_MODE_MOONSHINE_MEDIUM,
    STREAMING_ASR_MODE_LIVE_CAPTIONS,
    STREAMING_ASR_MODE_API,
    STREAMING_ASR_MODE_GEMINI_LIVE,
]
WAKE_WORD = (os.getenv("WAKE_WORD", "rachel") or "rachel").strip().lower()
WAKE_WORD_ALIASES = [
    item.strip().lower()
    for item in os.getenv(
        "WAKE_WORD_ALIASES",
        "rachel, rachael, richel, richelle, rachal, raychel, ra chel, rach el, rita, ritu",
    ).split(",")
    if item.strip()
]
WAKE_WORD_GREETING_PREFIXES = [
    "hey",
    "hi",
    "hello",
    "yo",
    "what's up",
    "whats up",
    "good morning",
    "good afternoon",
    "good evening",
]
WAKE_WORD_CONTEXT_MISHEARINGS = [
    item.strip().lower()
    for item in os.getenv("WAKE_WORD_CONTEXT_MISHEARINGS", "32, thirty two, thirty-two").split(",")
    if item.strip()
]

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9']+|[\u4e00-\u9fff]+")
_COMMAND_PREFIX_TOKENS = {"open", "start", "launch", "play", "begin", "load"}


def normalize_streaming_asr_mode(value: Optional[str]) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {
        STREAMING_ASR_MODE_MOONSHINE_SMALL,
        "moonshine_small",
        "small",
    }:
        return STREAMING_ASR_MODE_MOONSHINE_SMALL
    if normalized in {
        STREAMING_ASR_MODE_LIVE_CAPTIONS,
        "live_captions",
        "windows-live-captions",
        "windows_captions",
        "captions",
        "livecaptions",
    }:
        return STREAMING_ASR_MODE_LIVE_CAPTIONS
    if normalized in {
        STREAMING_ASR_MODE_API,
        "cloud-api",
        "service-api",
        "openai",
        "online",
    }:
        return STREAMING_ASR_MODE_API
    if normalized in {
        STREAMING_ASR_MODE_GEMINI_LIVE,
        "gemini-live-native-audio",
        "gemini-native-audio",
        "native-audio",
        "native_audio",
        "gemini",
    }:
        return STREAMING_ASR_MODE_GEMINI_LIVE
    if normalized in {
        "sherpa",
        "sherpa-onnx-en",
        "sherpa-onnx",
        "zipformer",
        "zipformer-en",
        "sherpa_en",
    }:
        return STREAMING_ASR_MODE_MOONSHINE_MEDIUM
    if normalized in {
        "sherpa-onnx-bilingual",
        "sherpa-bilingual",
        "sherpa-onnx-zh-en",
        "sherpa_zh_en",
        "bilingual",
        "zh-en",
    }:
        return STREAMING_ASR_MODE_MOONSHINE_MEDIUM
    return STREAMING_ASR_MODE_MOONSHINE_MEDIUM


def supported_streaming_asr_modes() -> List[str]:
    return list(STREAMING_ASR_SUPPORTED_MODES)


def moonshine_streaming_available() -> bool:
    return MoonshineTranscriber is not None and moonshine_get_model_for_language is not None

def _compact_key(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value or "").casefold())


def _collapse_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _build_wake_word_pattern(terms: Sequence[str]) -> re.Pattern[str]:
    patterns: List[str] = []
    for term in terms:
        stripped = str(term or "").strip()
        if not stripped:
            continue
        pieces = [re.escape(piece) for piece in stripped.split() if piece]
        if not pieces:
            continue
        patterns.append(r"\s*".join(pieces))
    if not patterns:
        patterns.append(re.escape(WAKE_WORD))
    combined = "|".join(sorted(set(patterns), key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{combined})(?!\w)", re.IGNORECASE)


_WAKE_WORD_REGEX = _build_wake_word_pattern([WAKE_WORD, *WAKE_WORD_ALIASES])
_WAKE_WORD_CONTEXT_PATTERNS = [
    re.compile(
        rf"(?P<prefix>(?<!\w){re.escape(prefix)}(?:\s|[,;:!\-])+)(?P<wake>{pattern})(?!\w)",
        re.IGNORECASE,
    )
    for prefix in WAKE_WORD_GREETING_PREFIXES
    for pattern in sorted({re.escape(term) for term in WAKE_WORD_CONTEXT_MISHEARINGS if term}, key=len, reverse=True)
]


def _canonicalize_wake_word_context(text: str) -> str:
    normalized = _collapse_spaces(text)
    if not normalized:
        return normalized
    normalized = _WAKE_WORD_REGEX.sub(WAKE_WORD, normalized)
    for pattern in _WAKE_WORD_CONTEXT_PATTERNS:
        normalized = pattern.sub(lambda match: f"{match.group('prefix')}{WAKE_WORD}", normalized)
    return _collapse_spaces(normalized)


_GAME_TERM_PATTERNS = [
    (re.compile(r"(?<!\w)corn[\s\-]*hole(?!\w)", re.IGNORECASE), "cornhole"),
    (re.compile(r"(?<!\w)kong[\s\-]*ho(?:u)?(?!\w)", re.IGNORECASE), "cornhole"),
    (re.compile(r"(?<!\w)disc[\s\-]*golf(?!\w)", re.IGNORECASE), "disc golf"),
]

_COMMON_AGENT_PHRASE_PATTERNS = [
    (re.compile(r"(?<!\w)holstein\s+screen(?!\w)", re.IGNORECASE), "how's things going"),
    (re.compile(r"(?<!\w)recommend(?:ed)?\s+game\s+to\s+me(?!\w)", re.IGNORECASE), "recommend a game to me"),
]


def _canonicalize_game_terms(text: str) -> str:
    normalized = _collapse_spaces(text)
    if not normalized:
        return normalized
    for pattern, replacement in _GAME_TERM_PATTERNS:
        normalized = pattern.sub(replacement, normalized)
    return _collapse_spaces(normalized)


def _canonicalize_common_agent_phrases(text: str) -> str:
    normalized = _collapse_spaces(text)
    if not normalized:
        return normalized
    for pattern, replacement in _COMMON_AGENT_PHRASE_PATTERNS:
        normalized = pattern.sub(replacement, normalized)
    return _collapse_spaces(normalized)


def _canonicalize_agent_text(text: str) -> str:
    return _canonicalize_common_agent_phrases(_canonicalize_game_terms(_canonicalize_wake_word_context(text)))


def _common_prefix_text(left: str, right: str) -> str:
    max_len = min(len(left), len(right))
    idx = 0
    while idx < max_len and left[idx] == right[idx]:
        idx += 1
    prefix = left[:idx].rstrip()
    if not prefix:
        return ""
    split = max(prefix.rfind(" "), prefix.rfind("\n"))
    if split >= max(4, len(prefix) // 2):
        return prefix[: split + 1].rstrip()
    return prefix


def _tokenize_text(value: str) -> List[str]:
    return [match.group(0) for match in _TOKEN_PATTERN.finditer(str(value or ""))]


def _token_similarity(left: str, right: str) -> float:
    lhs = _collapse_spaces(left).casefold()
    rhs = _collapse_spaces(right).casefold()
    if not lhs or not rhs:
        return 0.0
    if lhs == rhs:
        return 1.0
    lhs_compact = _compact_key(lhs)
    rhs_compact = _compact_key(rhs)
    if lhs_compact and lhs_compact == rhs_compact:
        return 1.0
    shorter = min(len(lhs), len(rhs))
    longer = max(len(lhs), len(rhs))
    if shorter >= 3 and (lhs.startswith(rhs) or rhs.startswith(lhs)):
        return 0.84 + (0.16 * shorter / max(1, longer))
    return difflib.SequenceMatcher(None, lhs, rhs).ratio()


@dataclass
class HotwordEntry:
    phrase: str
    aliases: List[str] = field(default_factory=list)
    weight: float = 1.0


@dataclass
class HotwordPack:
    entries: List[HotwordEntry] = field(default_factory=list)

    def canonical_phrases(self) -> List[str]:
        out: List[str] = []
        seen = set()
        for entry in self.entries:
            for phrase in [entry.phrase, *entry.aliases]:
                text = _collapse_spaces(phrase)
                if not text:
                    continue
                key = text.casefold()
                if key in seen:
                    continue
                seen.add(key)
                out.append(text)
        return out


class HotwordNormalizer:
    def __init__(self, pack: Optional[HotwordPack] = None) -> None:
        self._entries: List[Tuple[str, List[str]]] = []
        self._phrase_alias_map: Dict[str, str] = {}
        self._compact_alias_map: Dict[str, str] = {}
        self._token_entries: List[Tuple[str, str, List[str]]] = []
        self.update(pack or HotwordPack())

    def update(self, pack: HotwordPack) -> None:
        entries: List[Tuple[str, List[str]]] = []
        phrase_alias_map: Dict[str, str] = {}
        compact_alias_map: Dict[str, str] = {}
        token_entries: List[Tuple[str, str, List[str]]] = []
        for item in pack.entries:
            canonical = _collapse_spaces(item.phrase)
            if not canonical:
                continue
            aliases = [canonical, *item.aliases]
            seen_local = set()
            normalized_aliases: List[str] = []
            for alias in aliases:
                alias_text = _collapse_spaces(alias)
                if not alias_text:
                    continue
                key = alias_text.casefold()
                if key in seen_local:
                    continue
                seen_local.add(key)
                normalized_aliases.append(alias_text)
                phrase_alias_map.setdefault(alias_text.casefold(), canonical)
                compact = _compact_key(alias_text)
                if compact:
                    compact_alias_map.setdefault(compact, canonical)
                alias_tokens = _tokenize_text(alias_text)
                if alias_tokens:
                    token_entries.append((canonical, alias_text, alias_tokens))
            entries.append((canonical, normalized_aliases))
        self._entries = sorted(entries, key=lambda item: max((len(alias) for alias in item[1]), default=0), reverse=True)
        self._phrase_alias_map = phrase_alias_map
        self._compact_alias_map = compact_alias_map
        self._token_entries = sorted(
            token_entries,
            key=lambda item: (len(item[2]), max((len(token) for token in item[2]), default=0)),
            reverse=True,
        )

    def rewrite(self, value: str) -> str:
        text = _collapse_spaces(value)
        if not text or not self._entries:
            return text
        rewritten = text
        for canonical, aliases in self._entries:
            for alias in aliases:
                if alias.casefold() == canonical.casefold():
                    continue
                pattern = re.compile(rf"(?<!\w){re.escape(alias)}(?!\w)", re.IGNORECASE)
                rewritten = pattern.sub(canonical, rewritten)
        token_matches = list(_TOKEN_PATTERN.finditer(rewritten))
        if not token_matches:
            exact = self._phrase_alias_map.get(rewritten.casefold())
            if exact:
                return exact
            compact = self._compact_alias_map.get(_compact_key(rewritten))
            return compact or rewritten
        replacements: List[Tuple[int, int, str]] = []
        token_texts = [match.group(0) for match in token_matches]
        max_window = min(5, len(token_texts))
        index = 0
        while index < len(token_texts):
            replacement: Optional[Tuple[int, int, str]] = None
            for width in range(max_window, 0, -1):
                if index + width > len(token_texts):
                    continue
                span_text = " ".join(token_texts[index : index + width])
                canonical = self._phrase_alias_map.get(span_text.casefold())
                if canonical is None:
                    canonical = self._compact_alias_map.get(_compact_key(span_text))
                if canonical is None:
                    continue
                start = token_matches[index].start()
                end = token_matches[index + width - 1].end()
                replacement = (start, end, canonical)
                break
            if replacement is None:
                index += 1
                continue
            replacements.append(replacement)
            consumed = 0
            while index + consumed < len(token_texts) and token_matches[index + consumed].end() <= replacement[1]:
                consumed += 1
            index += max(1, consumed)
        if not replacements:
            return rewritten
        parts: List[str] = []
        cursor = 0
        for start, end, canonical in replacements:
            if start < cursor:
                continue
            parts.append(rewritten[cursor:start])
            parts.append(canonical)
            cursor = end
        parts.append(rewritten[cursor:])
        return _collapse_spaces("".join(parts))

    def rewrite_aggressive(self, value: str) -> str:
        text = self.rewrite(value)
        token_matches = list(_TOKEN_PATTERN.finditer(text))
        if not token_matches or len(token_matches) > 4 or not self._token_entries:
            return text
        token_texts = [match.group(0) for match in token_matches]
        replacements: List[Tuple[int, int, str]] = []
        index = 0
        while index < len(token_texts):
            best_span: Optional[Tuple[int, int, str]] = None
            best_score = 0.0
            max_window = min(3, len(token_texts) - index)
            previous_token = token_texts[index - 1].casefold() if index > 0 else ""
            for width in range(max_window, 0, -1):
                span_tokens = token_texts[index : index + width]
                candidate = self._best_fuzzy_replacement(
                    span_tokens,
                    previous_token=previous_token,
                    is_terminal=index + width == len(token_texts),
                    total_tokens=len(token_texts),
                )
                if candidate is None:
                    continue
                canonical, score = candidate
                if score <= best_score:
                    continue
                start = token_matches[index].start()
                end = token_matches[index + width - 1].end()
                best_span = (start, end, canonical)
                best_score = score
            if best_span is None:
                index += 1
                continue
            replacements.append(best_span)
            index += len(_tokenize_text(text[best_span[0] : best_span[1]])) or 1
        if not replacements:
            return text
        parts: List[str] = []
        cursor = 0
        for start, end, canonical in replacements:
            if start < cursor:
                continue
            parts.append(text[cursor:start])
            parts.append(canonical)
            cursor = end
        parts.append(text[cursor:])
        return _collapse_spaces("".join(parts))

    def _best_fuzzy_replacement(
        self,
        candidate_tokens: List[str],
        *,
        previous_token: str,
        is_terminal: bool,
        total_tokens: int,
    ) -> Optional[Tuple[str, float]]:
        best: Optional[Tuple[str, float]] = None
        candidate_compact = _compact_key(" ".join(candidate_tokens))
        for canonical, alias_text, alias_tokens in self._token_entries:
            if not alias_tokens or len(candidate_tokens) > len(alias_tokens):
                continue
            if candidate_compact and candidate_compact == _compact_key(alias_text):
                return canonical, 1.0
            shortened = len(candidate_tokens) < len(alias_tokens)
            if shortened and not (
                is_terminal and (total_tokens <= 2 or previous_token in _COMMAND_PREFIX_TOKENS)
            ):
                continue
            similarities = [_token_similarity(left, right) for left, right in zip(candidate_tokens, alias_tokens)]
            if not similarities or min(similarities) < 0.68:
                continue
            score = sum(similarities) / float(len(similarities))
            if shortened:
                score -= 0.08 * (len(alias_tokens) - len(candidate_tokens))
            threshold = 0.8 if shortened else 0.76
            if score < threshold:
                continue
            if best is None or score > best[1]:
                best = (canonical, score)
        return best


class StablePartialTracker:
    def __init__(self, repeats: int = 2) -> None:
        self.repeats = max(1, int(repeats))
        self._last_text = ""
        self._stable_text = ""
        self._repeat_count = 0

    def reset(self) -> None:
        self._last_text = ""
        self._stable_text = ""
        self._repeat_count = 0

    def observe(self, text: str) -> str:
        current = _collapse_spaces(text)
        if not current:
            return self._stable_text
        if not self._last_text:
            self._last_text = current
            self._repeat_count = 1
            return self._stable_text
        prefix = _common_prefix_text(self._last_text, current)
        if len(prefix) > len(self._stable_text):
            self._stable_text = prefix
        if current == self._last_text:
            self._repeat_count += 1
        else:
            self._repeat_count = 1
            self._last_text = current
        if self._repeat_count >= self.repeats:
            self._stable_text = current
        return self._stable_text


@dataclass
class AsrEvent:
    event_type: str
    backend: str
    text: str = ""
    raw_text: str = ""
    stable_text: str = ""
    avg_logprob: Optional[float] = None
    speaker_index: Optional[int] = None
    speaker_id: Optional[int] = None
    has_speaker_id: bool = False
    audio_data: Optional[List[float]] = None
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


class StreamingAsrBackend:
    def __init__(
        self,
        *,
        mode: str,
        stable_partial_repeats: int,
        hotword_pack: HotwordPack,
        on_event: Callable[[AsrEvent], None],
        on_error: Callable[[str], None],
    ) -> None:
        self.mode = normalize_streaming_asr_mode(mode)
        self._on_event = on_event
        self._on_error = on_error
        self._stable_tracker = StablePartialTracker(stable_partial_repeats)
        self._normalizer = HotwordNormalizer(hotword_pack)
        self._last_error = ""
        self._started = False

    @property
    def backend_name(self) -> str:
        return self.mode

    @property
    def last_error(self) -> str:
        return self._last_error

    def supports_hotwords(self) -> bool:
        return False

    def is_available(self) -> bool:
        return True

    def start(self) -> None:
        self._started = True

    def stop(self) -> None:
        self._started = False
        self._stable_tracker.reset()

    def push_audio(self, audio: np.ndarray, sample_rate: int) -> None:
        _ = audio
        _ = sample_rate

    def finish(self) -> None:
        return None

    def update_hotwords(self, hotword_pack: HotwordPack) -> None:
        self._normalizer.update(hotword_pack)

    def status_payload(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "backend": self.backend_name,
            "started": self._started,
            "available": self.is_available(),
            "supports_hotwords": self.supports_hotwords(),
            "last_error": self._last_error,
        }

    def _rewrite_text(self, text: str) -> str:
        return _canonicalize_agent_text(self._normalizer.rewrite(text))

    def _rewrite_final_text(self, text: str) -> str:
        return _canonicalize_agent_text(self._normalizer.rewrite_aggressive(text))

    def _emit(self, event: AsrEvent) -> None:
        self._on_event(event)

    def _emit_error(self, message: str) -> None:
        self._last_error = str(message or "").strip()
        self._on_error(self._last_error)


class _MoonshineListener(MoonshineTranscriptEventListener):
    def __init__(self, owner: "MoonshineStreamingBackend") -> None:
        self._owner = owner

    def on_line_started(self, event) -> None:
        self._owner._handle_line_event("started", getattr(event, "line", None))

    def on_line_text_changed(self, event) -> None:
        self._owner._handle_line_event("partial", getattr(event, "line", None))

    def on_line_completed(self, event) -> None:
        self._owner._handle_line_event("final", getattr(event, "line", None))

    def on_error(self, event) -> None:
        self._owner._emit_error(str(getattr(event, "error", "") or "Moonshine stream error"))


class MoonshineStreamingBackend(StreamingAsrBackend):
    def __init__(
        self,
        *,
        mode: str,
        stable_partial_repeats: int,
        hotword_pack: HotwordPack,
        on_event: Callable[[AsrEvent], None],
        on_error: Callable[[str], None],
    ) -> None:
        super().__init__(
            mode=mode,
            stable_partial_repeats=stable_partial_repeats,
            hotword_pack=hotword_pack,
            on_event=on_event,
            on_error=on_error,
        )
        self._transcriber = None
        self._stream = None
        self._listener = _MoonshineListener(self)

    @property
    def backend_name(self) -> str:
        return "moonshine"

    def is_available(self) -> bool:
        return MoonshineTranscriber is not None and moonshine_get_model_for_language is not None

    def start(self) -> None:
        if not self.is_available():
            raise RuntimeError("moonshine-voice is not installed")
        self.stop()
        wanted_arch = "small-streaming" if self.mode == STREAMING_ASR_MODE_MOONSHINE_SMALL else "medium-streaming"
        model_arch = moonshine_string_to_model_arch(wanted_arch) if moonshine_string_to_model_arch is not None else None
        model_path, resolved_arch = moonshine_get_model_for_language("en", model_arch)
        transcriber = MoonshineTranscriber(model_path=model_path, model_arch=resolved_arch)
        stream = transcriber.create_stream(update_interval=0.15)
        stream.add_listener(self._listener)
        stream.start()
        self._transcriber = transcriber
        self._stream = stream
        self._stable_tracker.reset()
        self._started = True

    def stop(self) -> None:
        stream = self._stream
        transcriber = self._transcriber
        self._stream = None
        self._transcriber = None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        if transcriber is not None:
            try:
                transcriber.close()
            except Exception:
                pass
        super().stop()

    def push_audio(self, audio: np.ndarray, sample_rate: int) -> None:
        if not self._started or self._stream is None:
            return
        try:
            self._stream.add_audio(np.asarray(audio, dtype=np.float32), int(sample_rate))
        except Exception as exc:
            self._emit_error(f"moonshine stream add_audio failed: {exc}")

    def _handle_line_event(self, event_type: str, line: Any) -> None:
        if line is None:
            return
        raw_text = _collapse_spaces(getattr(line, "text", ""))
        text = self._rewrite_final_text(raw_text)
        speaker_index = int(getattr(line, "speaker_index", 0) or 0)
        speaker_id = int(getattr(line, "speaker_id", 0) or 0)
        has_speaker_id = bool(getattr(line, "has_speaker_id", False))
        if event_type == "started":
            self._stable_tracker.reset()
            self._emit(AsrEvent(event_type="started", backend=self.backend_name))
            return
        if event_type == "partial":
            stable = self._stable_tracker.observe(text)
            self._emit(
                AsrEvent(
                    event_type="partial",
                    backend=self.backend_name,
                    text=text,
                    raw_text=raw_text,
                    stable_text=stable,
                    speaker_index=speaker_index if has_speaker_id else None,
                    speaker_id=speaker_id if has_speaker_id else None,
                    has_speaker_id=has_speaker_id,
                )
            )
            return
        self._emit(
            AsrEvent(
                event_type="final",
                backend=self.backend_name,
                text=text,
                raw_text=raw_text,
                stable_text=text,
                speaker_index=speaker_index if has_speaker_id else None,
                speaker_id=speaker_id if has_speaker_id else None,
                has_speaker_id=has_speaker_id,
                audio_data=list(getattr(line, "audio_data", []) or []),
            )
        )
        self._stable_tracker.reset()


def create_streaming_asr_backend(
    *,
    mode: str,
    stable_partial_repeats: int,
    hotword_pack: HotwordPack,
    on_event: Callable[[AsrEvent], None],
    on_error: Callable[[str], None],
):
    normalized = normalize_streaming_asr_mode(mode)
    return MoonshineStreamingBackend(
        mode=normalized,
        stable_partial_repeats=stable_partial_repeats,
        hotword_pack=hotword_pack,
        on_event=on_event,
        on_error=on_error,
    )
