#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON_VOICE_DIR = REPO_ROOT / "python_voice_service"
if str(PYTHON_VOICE_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_VOICE_DIR))

from streaming_asr import (  # noqa: E402
    HotwordEntry,
    HotwordNormalizer,
    HotwordPack,
    WAKE_WORD,
    WAKE_WORD_ALIASES,
)

DEFAULT_RECORDINGS_DIR = Path(r"C:\Users\tianj\OneDrive\文档\Sound Recordings")
DEFAULT_OUTPUT = REPO_ROOT / "runtime" / "evals" / "latest_recording_asr_eval.json"
DEFAULT_VOICE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT = httpx.Timeout(180.0)
DEFAULT_MODES = ["moonshine-medium", "api"]
GENERIC_STEMS = {"recording", "audio", "new recording", "untitled"}
SUPPORTED_SUFFIXES = {".m4a", ".wav", ".mp3", ".flac", ".aac", ".ogg", ".wma"}


@dataclass
class ModeResult:
    mode: str
    success: bool
    text: str
    normalized_text: str
    exact_match: Optional[bool]
    expected_contains_actual: Optional[bool]
    actual_contains_expected: Optional[bool]
    word_error_rate: Optional[float]
    char_error_rate: Optional[float]
    token_overlap: Optional[float]
    latency_ms: Optional[float]
    provider: Optional[str]
    detail: Optional[str]
    canonicalized_text: Optional[str]
    canonicalized_text_normalized: Optional[str]
    canonicalized_exact_match: Optional[bool]
    canonicalized_word_error_rate: Optional[float]
    canonicalized_token_overlap: Optional[float]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate local recordings against /transcribe for multiple ASR modes.")
    parser.add_argument(
        "--recordings-dir",
        default=str(DEFAULT_RECORDINGS_DIR),
        help="Directory containing source recordings. Expected text is derived from filename stem.",
    )
    parser.add_argument("--voice-url", default=DEFAULT_VOICE_URL, help="Voice service base URL.")
    parser.add_argument(
        "--modes",
        nargs="+",
        default=DEFAULT_MODES,
        help="ASR modes to evaluate, e.g. moonshine-medium api",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="Path to write the JSON report.",
    )
    parser.add_argument(
        "--language",
        default="en",
        help="Language passed to /transcribe.",
    )
    parser.add_argument(
        "--keep-generic",
        action="store_true",
        help="Include generic stems like Recording.m4a in accuracy aggregates.",
    )
    return parser.parse_args()


def _normalize_text(value: str) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"[^0-9a-z\s]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _levenshtein(seq_a: List[str], seq_b: List[str]) -> int:
    if not seq_a:
        return len(seq_b)
    if not seq_b:
        return len(seq_a)
    prev = list(range(len(seq_b) + 1))
    for i, token_a in enumerate(seq_a, start=1):
        curr = [i]
        for j, token_b in enumerate(seq_b, start=1):
            insert_cost = curr[j - 1] + 1
            delete_cost = prev[j] + 1
            replace_cost = prev[j - 1] + (0 if token_a == token_b else 1)
            curr.append(min(insert_cost, delete_cost, replace_cost))
        prev = curr
    return prev[-1]


def _word_error_rate(expected: str, actual: str) -> Optional[float]:
    exp_tokens = expected.split()
    act_tokens = actual.split()
    if not exp_tokens:
        return None
    return _levenshtein(exp_tokens, act_tokens) / float(len(exp_tokens))


def _char_error_rate(expected: str, actual: str) -> Optional[float]:
    if not expected:
        return None
    return _levenshtein(list(expected), list(actual)) / float(len(expected))


def _token_overlap(expected: str, actual: str) -> Optional[float]:
    exp_tokens = expected.split()
    act_tokens = actual.split()
    if not exp_tokens:
        return None
    exp_set = set(exp_tokens)
    act_set = set(act_tokens)
    if not exp_set:
        return None
    return len(exp_set & act_set) / float(len(exp_set))


def _looks_generic(stem: str) -> bool:
    compact = _normalize_text(stem)
    return compact in GENERIC_STEMS


def _expected_from_path(path: Path) -> Optional[str]:
    stem = path.stem.strip()
    if not stem or _looks_generic(stem):
        return None
    return stem


def _decode_to_pcm16(path: Path) -> bytes:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-",
    ]
    completed = subprocess.run(command, capture_output=True, check=False)
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(stderr or f"ffmpeg failed with exit code {completed.returncode}")
    if not completed.stdout:
        raise RuntimeError("ffmpeg produced empty PCM output")
    return completed.stdout


def _collect_recordings(directory: Path) -> List[Path]:
    if not directory.exists():
        raise FileNotFoundError(f"recordings directory not found: {directory}")
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def _build_normalizer() -> HotwordNormalizer:
    entries: List[HotwordEntry] = []
    if WAKE_WORD:
        entries.append(HotwordEntry(phrase=WAKE_WORD, aliases=list(WAKE_WORD_ALIASES)))

    manifest_path = REPO_ROOT / "scripts" / "intent_service" / "manifest.json"
    if manifest_path.exists():
        manifest = _load_json(manifest_path)
        for item in manifest.get("games", []) if isinstance(manifest, dict) else []:
            if not isinstance(item, dict):
                continue
            phrase = str(item.get("name") or item.get("id") or "").strip()
            aliases = [str(value).strip() for value in item.get("synonyms", []) or [] if str(value).strip()]
            if phrase:
                entries.append(HotwordEntry(phrase=phrase, aliases=aliases))

    return HotwordNormalizer(HotwordPack(entries=entries))


def _save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def _transcribe_one(
    client: httpx.AsyncClient,
    voice_url: str,
    pcm_bytes: bytes,
    language: str,
    mode: str,
) -> ModeResult:
    try:
        response = await client.post(
            f"{voice_url.rstrip('/')}/transcribe",
            params={"sample_rate": 16000, "language": language, "mode": mode},
            content=pcm_bytes,
        )
        latency_ms = response.elapsed.total_seconds() * 1000.0 if response.elapsed else None
        response.raise_for_status()
        payload = response.json()
        text = str(payload.get("text") or "").strip()
        return ModeResult(
            mode=mode,
            success=True,
            text=text,
            normalized_text=_normalize_text(text),
            exact_match=None,
            expected_contains_actual=None,
            actual_contains_expected=None,
            word_error_rate=None,
            char_error_rate=None,
            token_overlap=None,
            latency_ms=latency_ms,
            provider=str(payload.get("provider") or ""),
            detail=None,
            canonicalized_text=None,
            canonicalized_text_normalized=None,
            canonicalized_exact_match=None,
            canonicalized_word_error_rate=None,
            canonicalized_token_overlap=None,
        )
    except Exception as exc:
        detail = str(exc)
        return ModeResult(
            mode=mode,
            success=False,
            text="",
            normalized_text="",
            exact_match=None,
            expected_contains_actual=None,
            actual_contains_expected=None,
            word_error_rate=None,
            char_error_rate=None,
            token_overlap=None,
            latency_ms=None,
            provider=None,
            detail=detail,
            canonicalized_text=None,
            canonicalized_text_normalized=None,
            canonicalized_exact_match=None,
            canonicalized_word_error_rate=None,
            canonicalized_token_overlap=None,
        )


def _apply_reference_metrics(result: ModeResult, expected: Optional[str], normalizer: HotwordNormalizer) -> ModeResult:
    if not expected or not result.success:
        if result.success:
            canonicalized_text = normalizer.rewrite_aggressive(result.text)
            result.canonicalized_text = canonicalized_text
            result.canonicalized_text_normalized = _normalize_text(canonicalized_text)
        return result
    normalized_expected = _normalize_text(expected)
    normalized_actual = result.normalized_text
    result.exact_match = normalized_actual == normalized_expected
    result.expected_contains_actual = bool(normalized_actual) and normalized_expected.find(normalized_actual) >= 0
    result.actual_contains_expected = bool(normalized_expected) and normalized_actual.find(normalized_expected) >= 0
    result.word_error_rate = _word_error_rate(normalized_expected, normalized_actual)
    result.char_error_rate = _char_error_rate(normalized_expected, normalized_actual)
    result.token_overlap = _token_overlap(normalized_expected, normalized_actual)
    canonicalized_text = normalizer.rewrite_aggressive(result.text)
    result.canonicalized_text = canonicalized_text
    result.canonicalized_text_normalized = _normalize_text(canonicalized_text)
    result.canonicalized_exact_match = result.canonicalized_text_normalized == normalized_expected
    result.canonicalized_word_error_rate = _word_error_rate(normalized_expected, result.canonicalized_text_normalized)
    result.canonicalized_token_overlap = _token_overlap(normalized_expected, result.canonicalized_text_normalized)
    return result


def _summarize(records: List[Dict[str, Any]], modes: List[str], keep_generic: bool) -> Dict[str, Any]:
    summary: Dict[str, Any] = {"total_files": len(records), "modes": {}}
    for mode in modes:
        mode_rows: List[ModeResult] = []
        for item in records:
            if not keep_generic and not item.get("expected_text"):
                continue
            raw = item.get("results", {}).get(mode)
            if not isinstance(raw, dict):
                continue
            mode_rows.append(ModeResult(**raw))

        successes = [row for row in mode_rows if row.success]
        with_reference = [row for row in successes if row.exact_match is not None]
        exact = [row for row in with_reference if row.exact_match]
        canonical_exact = [row for row in with_reference if row.canonicalized_exact_match]
        avg_latency = (
            sum(row.latency_ms for row in successes if row.latency_ms is not None) / max(1, len([row for row in successes if row.latency_ms is not None]))
            if successes
            else None
        )
        avg_wer = (
            sum(row.word_error_rate for row in with_reference if row.word_error_rate is not None)
            / max(1, len([row for row in with_reference if row.word_error_rate is not None]))
            if with_reference
            else None
        )
        avg_overlap = (
            sum(row.token_overlap for row in with_reference if row.token_overlap is not None)
            / max(1, len([row for row in with_reference if row.token_overlap is not None]))
            if with_reference
            else None
        )
        avg_canonical_wer = (
            sum(row.canonicalized_word_error_rate for row in with_reference if row.canonicalized_word_error_rate is not None)
            / max(1, len([row for row in with_reference if row.canonicalized_word_error_rate is not None]))
            if with_reference
            else None
        )
        avg_canonical_overlap = (
            sum(row.canonicalized_token_overlap for row in with_reference if row.canonicalized_token_overlap is not None)
            / max(1, len([row for row in with_reference if row.canonicalized_token_overlap is not None]))
            if with_reference
            else None
        )
        failures = [row for row in mode_rows if not row.success]
        summary["modes"][mode] = {
            "evaluated_files": len(mode_rows),
            "successful_files": len(successes),
            "failed_files": len(failures),
            "referenced_files": len(with_reference),
            "exact_matches": len(exact),
            "exact_match_rate": (len(exact) / len(with_reference)) if with_reference else None,
            "canonicalized_exact_matches": len(canonical_exact),
            "canonicalized_exact_match_rate": (len(canonical_exact) / len(with_reference)) if with_reference else None,
            "average_word_error_rate": avg_wer,
            "average_token_overlap": avg_overlap,
            "average_canonicalized_word_error_rate": avg_canonical_wer,
            "average_canonicalized_token_overlap": avg_canonical_overlap,
            "average_latency_ms": avg_latency,
            "failures": [
                {
                    "detail": row.detail,
                }
                for row in failures
            ],
        }
    return summary


async def _main() -> int:
    args = _parse_args()
    recordings_dir = Path(args.recordings_dir)
    output_path = Path(args.output)
    modes = [str(mode).strip() for mode in args.modes if str(mode).strip()]

    recordings = _collect_recordings(recordings_dir)
    normalizer = _build_normalizer()
    report_rows: List[Dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        health = await client.get(f"{args.voice_url.rstrip('/')}/healthz")
        health.raise_for_status()

        for path in recordings:
            expected = _expected_from_path(path)
            pcm_bytes = _decode_to_pcm16(path)
            row: Dict[str, Any] = {
                "file_name": path.name,
                "file_path": str(path),
                "expected_text": expected,
                "expected_text_normalized": _normalize_text(expected or "") if expected else None,
                "results": {},
            }
            for mode in modes:
                result = await _transcribe_one(
                    client=client,
                    voice_url=args.voice_url,
                    pcm_bytes=pcm_bytes,
                    language=args.language,
                    mode=mode,
                )
                result = _apply_reference_metrics(result, expected, normalizer)
                row["results"][mode] = asdict(result)
            report_rows.append(row)

    report = {
        "recordings_dir": str(recordings_dir),
        "voice_url": args.voice_url,
        "modes": modes,
        "language": args.language,
        "kept_generic": bool(args.keep_generic),
        "summary": _summarize(report_rows, modes, args.keep_generic),
        "records": report_rows,
    }
    _save_json(output_path, report)
    print(json.dumps(report["summary"], ensure_ascii=False))
    print(f"report_written={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
