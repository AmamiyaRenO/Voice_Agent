#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import io
import json
import math
import sys
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON_VOICE_DIR = REPO_ROOT / "python_voice_service"
if str(PYTHON_VOICE_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_VOICE_DIR))

from command_grammar import CommandGrammarMatcher  # noqa: E402
from desktop_audio_agent import AudioFrontEndProcessor, AudioInputBuffer, WebRtcApm  # noqa: E402
from streaming_asr import (  # noqa: E402
    AsrEvent,
    HotwordEntry,
    HotwordPack,
    create_streaming_asr_backend,
    normalize_streaming_asr_mode,
)


DEFAULT_PANEL_URL = "http://127.0.0.1:8787"
DEFAULT_PIPER_URL = "http://127.0.0.1:5005"
DEFAULT_TIMEOUT = httpx.Timeout(90.0)
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_FRAME_SIZE = 160
DEFAULT_SAMPLE_DIR = REPO_ROOT / "runtime" / "eval_assets" / "asr_samples"
DEFAULT_USER_CONFIG = REPO_ROOT / "scripts" / "local_services.user.json"
DEFAULT_BASE_CONFIG = REPO_ROOT / "scripts" / "local_services.default.json"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run system audio frontend regression scenarios.")
    parser.add_argument("--panel-url", default=DEFAULT_PANEL_URL, help="Desktop runtime base URL.")
    parser.add_argument("--piper-url", default=DEFAULT_PIPER_URL, help="Piper HTTP base URL for synthetic speech.")
    parser.add_argument(
        "--scenarios",
        default=str(Path(__file__).resolve().with_name("audio_frontend_regression_scenarios.sample.json")),
        help="Scenario JSON file.",
    )
    parser.add_argument(
        "--output",
        default=str(REPO_ROOT / "runtime" / "evals" / "latest_audio_frontend_regression.json"),
        help="Where to write the JSON report.",
    )
    parser.add_argument("--mode", default="moonshine-medium", help="Streaming ASR mode for pipeline scenarios.")
    parser.add_argument("--model-dir", default=str(DEFAULT_SAMPLE_DIR), help="Base directory for bundled sample WAVs.")
    parser.add_argument("--stable-partial-repeats", type=int, default=2, help="Stable partial repeat threshold.")
    parser.add_argument("--fail-on-error", action="store_true", help="Return non-zero when any scenario fails.")
    return parser.parse_args()


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def _save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _decode_wav_bytes(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        channels = max(1, int(wf.getnchannels()))
        sample_rate = max(1, int(wf.getframerate()))
        sample_width = int(wf.getsampwidth())
        frames = wf.readframes(wf.getnframes())
    if sample_width != 2:
        raise RuntimeError(f"unsupported WAV sample width: {sample_width}")
    pcm = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1)
    return np.asarray(pcm, dtype=np.float32), sample_rate


def _resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate:
        return np.asarray(audio, dtype=np.float32)
    duration = audio.size / float(src_rate)
    dst_count = max(1, int(round(duration * dst_rate)))
    src_x = np.linspace(0.0, 1.0, num=audio.size, endpoint=False)
    dst_x = np.linspace(0.0, 1.0, num=dst_count, endpoint=False)
    return np.asarray(np.interp(dst_x, src_x, audio), dtype=np.float32)


def _normalize_compare_text(value: str) -> str:
    text = str(value or "").casefold()
    text = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text)
    return " ".join(text.split())


def _coerce_string_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item or "").strip() for item in value if str(item or "").strip()]
    if isinstance(value, str):
        merged = value.replace("\r\n", "\n").replace(";", ",").replace("\n", ",")
        return [part.strip() for part in merged.split(",") if part.strip()]
    return []


def _load_runtime_config_fallback() -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for path in [DEFAULT_BASE_CONFIG, DEFAULT_USER_CONFIG]:
        if not path.exists():
            continue
        payload = _load_json(path)
        if isinstance(payload, dict):
            merged.update(payload)
            for key in ("openai", "intent", "env", "paths"):
                if isinstance(payload.get(key), dict):
                    merged.setdefault(key, {})
                    merged[key].update(payload[key])
    intent = merged.get("intent", {}) if isinstance(merged.get("intent"), dict) else {}
    env = merged.get("env", {}) if isinstance(merged.get("env"), dict) else {}
    paths = merged.get("paths", {}) if isinstance(merged.get("paths"), dict) else {}
    effective_manifest = str(
        paths.get("game_manifest")
        or paths.get("intent_manifest")
        or (REPO_ROOT / "scripts" / "intent_service" / "manifest.json")
    )
    return {
        "launch_triggers": ", ".join(intent.get("launch_triggers", ["open", "start", "launch", "play", "begin", "load"])),
        "exit_keywords": ", ".join(intent.get("exit_keywords", ["back home", "go home", "return home", "go back", "quit", "exit", "stop", "cancel", "close", "close game"])),
        "effective_game_manifest_path": str((REPO_ROOT / effective_manifest).resolve()) if not Path(effective_manifest).is_absolute() else effective_manifest,
        "asr_hotword_strategy": str(env.get("VOICE_ASR_HOTWORD_STRATEGY") or "commands_games_memory").strip(),
    }


async def _load_runtime_config(client: httpx.AsyncClient, panel_url: str) -> Dict[str, Any]:
    try:
        response = await client.get(f"{panel_url.rstrip('/')}/api/runtime/config")
        response.raise_for_status()
        return response.json()
    except Exception:
        return _load_runtime_config_fallback()


def _should_include_hotword(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    compact = text.casefold().replace(" ", "").replace("_", "").replace("-", "")
    if compact.startswith("user") and compact[4:].isdigit():
        return False
    return any(ch.isalpha() for ch in text)


def _load_hotword_pack(runtime_cfg: Dict[str, Any]) -> HotwordPack:
    entries: List[HotwordEntry] = []
    seen = set()

    def add_entry(phrase: str, aliases: Optional[List[str]] = None) -> None:
        canonical = str(phrase or "").strip()
        if not _should_include_hotword(canonical):
            return
        key = canonical.casefold()
        if key in seen:
            return
        seen.add(key)
        entries.append(
            HotwordEntry(
                phrase=canonical,
                aliases=[item for item in (aliases or []) if _should_include_hotword(item)],
            )
        )

    for phrase in [part.strip() for part in str(runtime_cfg.get("launch_triggers") or "").split(",") if part.strip()]:
        add_entry(phrase)
    for phrase in [part.strip() for part in str(runtime_cfg.get("exit_keywords") or "").split(",") if part.strip()]:
        add_entry(phrase)

    manifest_path = str(runtime_cfg.get("effective_game_manifest_path") or "").strip()
    if manifest_path and Path(manifest_path).exists():
        manifest = _load_json(Path(manifest_path))
        for item in manifest.get("games", []) if isinstance(manifest, dict) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("id") or "").strip()
            aliases = [str(value).strip() for value in item.get("synonyms", []) or [] if str(value).strip()]
            if name:
                if " " in name:
                    aliases.append(name.replace(" ", ""))
                add_entry(name, aliases)
    return HotwordPack(entries=entries)


def _load_command_grammar(runtime_cfg: Dict[str, Any]) -> CommandGrammarMatcher:
    return CommandGrammarMatcher.from_sources(
        launch_triggers=runtime_cfg.get("launch_triggers"),
        exit_keywords=runtime_cfg.get("exit_keywords"),
        manifest_path=str(runtime_cfg.get("effective_game_manifest_path") or "").strip(),
    )


async def _tts_audio(client: httpx.AsyncClient, piper_url: str, text: str) -> tuple[np.ndarray, int]:
    response = await client.get(f"{piper_url.rstrip('/')}/speak", params={"text": text}, timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()
    return _decode_wav_bytes(response.content)


def _model_audio(path: Path) -> tuple[np.ndarray, int]:
    return _decode_wav_bytes(path.read_bytes())


def _segment_audio(segment: Dict[str, Any], sample_rate: int, rng: np.random.Generator) -> np.ndarray:
    kind = str(segment.get("kind") or "silence").strip().lower()
    duration_ms = max(10.0, float(segment.get("duration_ms") or 200.0))
    gain = float(segment.get("gain") or 1.0)
    count = max(1, int(round(sample_rate * duration_ms / 1000.0)))
    timeline = np.arange(count, dtype=np.float32) / float(sample_rate)
    if kind == "silence":
        return np.zeros(count, dtype=np.float32)
    if kind == "noise":
        return np.asarray(rng.normal(0.0, float(segment.get("std") or 0.02), size=count), dtype=np.float32)
    if kind == "tone":
        freq = float(segment.get("freq_hz") or 220.0)
        return np.asarray(np.sin(2.0 * math.pi * freq * timeline), dtype=np.float32) * gain
    if kind == "voiced":
        f0 = float(segment.get("freq_hz") or 180.0)
        envelope = 0.55 + (0.45 * np.sin(2.0 * math.pi * 3.0 * timeline))
        voiced = (
            0.7 * np.sin(2.0 * math.pi * f0 * timeline)
            + 0.22 * np.sin(2.0 * math.pi * (f0 * 2.0) * timeline)
            + 0.08 * np.sin(2.0 * math.pi * (f0 * 3.0) * timeline)
        )
        return np.asarray(voiced * envelope, dtype=np.float32) * gain
    raise RuntimeError(f"unknown segment kind: {kind}")


def _synth_frontend_audio(segments: List[Dict[str, Any]], sample_rate: int, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    pieces = [_segment_audio(segment, sample_rate, rng) for segment in segments if isinstance(segment, dict)]
    if not pieces:
        return np.zeros(DEFAULT_FRAME_SIZE, dtype=np.float32)
    return np.concatenate(pieces, axis=0).astype(np.float32)


def _apply_pipeline_effects(audio: np.ndarray, sample_rate: int, case: Dict[str, Any], rng: np.random.Generator) -> np.ndarray:
    result = np.asarray(audio, dtype=np.float32).copy()
    prepend_ms = float(case.get("prepend_silence_ms") or 0.0)
    if prepend_ms > 0:
        prepend = np.zeros(max(1, int(round(sample_rate * prepend_ms / 1000.0))), dtype=np.float32)
        result = np.concatenate([prepend, result], axis=0)
    append_ms = float(case.get("append_silence_ms") or 0.0)
    if append_ms > 0:
        append = np.zeros(max(1, int(round(sample_rate * append_ms / 1000.0))), dtype=np.float32)
        result = np.concatenate([result, append], axis=0)
    gain = float(case.get("gain") or 1.0)
    result *= gain
    white_noise_std = float(case.get("white_noise_std") or case.get("white_noise") or 0.0)
    if white_noise_std > 0:
        result += rng.normal(0.0, white_noise_std, size=result.size).astype(np.float32)
    if case.get("hard_clip"):
        clip_level = max(0.1, min(1.0, float(case.get("hard_clip"))))
        result = np.clip(result, -clip_level, clip_level)
    return np.asarray(result, dtype=np.float32)


def _evaluate_checks(result_value: Dict[str, Any], expect: Dict[str, Any]) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []

    def add_check(name: str, passed: bool, expected: Any = None, actual: Any = None) -> None:
        item: Dict[str, Any] = {"name": name, "passed": bool(passed)}
        if expected is not None:
            item["expected"] = expected
        if actual is not None:
            item["actual"] = actual
        checks.append(item)

    if "speech_active" in expect:
        add_check("speech_active", bool(result_value.get("speech_active")) == bool(expect["speech_active"]), bool(expect["speech_active"]), bool(result_value.get("speech_active")))
    if "speech_frames_min" in expect:
        actual = int(result_value.get("speech_frames", 0))
        target = int(expect["speech_frames_min"])
        add_check("speech_frames_min", actual >= target, target, actual)
    if "clip_events_min" in expect:
        actual = int(result_value.get("clip_events", 0))
        target = int(expect["clip_events_min"])
        add_check("clip_events_min", actual >= target, target, actual)
    for key in ["input_level_dbfs", "input_peak_dbfs", "noise_floor_dbfs", "frontend_gain_db"]:
        min_key = f"{key}_min"
        max_key = f"{key}_max"
        if min_key in expect:
            actual = float(result_value.get(key, -999.0))
            target = float(expect[min_key])
            add_check(min_key, actual >= target, target, actual)
        if max_key in expect:
            actual = float(result_value.get(key, 999.0))
            target = float(expect[max_key])
            add_check(max_key, actual <= target, target, actual)
    if "queued_frames" in expect:
        actual = int(result_value.get("queued_frames", -1))
        target = int(expect["queued_frames"])
        add_check("queued_frames", actual == target, target, actual)
    if "dropped_frames" in expect:
        actual = int(result_value.get("dropped_frames", -1))
        target = int(expect["dropped_frames"])
        add_check("dropped_frames", actual == target, target, actual)
    if "pop_sizes" in expect:
        actual = list(result_value.get("pop_sizes", []))
        target = [int(item) for item in expect["pop_sizes"]]
        add_check("pop_sizes", actual == target, target, actual)
    if "expect_exact" in expect:
        actual = _normalize_compare_text(str(result_value.get("effective_text") or ""))
        target = _normalize_compare_text(str(expect["expect_exact"]))
        add_check("exact", actual == target, target, actual)
    for phrase in expect.get("expect_contains", []) or []:
        actual = _normalize_compare_text(str(result_value.get("effective_text") or ""))
        target = _normalize_compare_text(str(phrase))
        add_check(f"contains:{phrase}", target in actual, target, actual)
    return checks


def _run_frontend_case(case: Dict[str, Any]) -> Dict[str, Any]:
    frontend = AudioFrontEndProcessor(sample_rate_hz=DEFAULT_SAMPLE_RATE, frame_size=DEFAULT_FRAME_SIZE)
    segments = [item for item in case.get("segments", []) if isinstance(item, dict)]
    audio = _synth_frontend_audio(segments, DEFAULT_SAMPLE_RATE, seed=int(case.get("seed") or 7))
    speech_frames = 0
    for offset in range(0, audio.size, DEFAULT_FRAME_SIZE):
        frame = np.asarray(audio[offset : offset + DEFAULT_FRAME_SIZE], dtype=np.float32)
        if frame.size < DEFAULT_FRAME_SIZE:
            padded = np.zeros(DEFAULT_FRAME_SIZE, dtype=np.float32)
            padded[: frame.size] = frame
            frame = padded
        frontend.process(frame)
        status = frontend.status()
        if status.speech_active:
            speech_frames += 1
    final = frontend.status()
    result = {
        "input_level_dbfs": float(final.input_level_dbfs),
        "input_peak_dbfs": float(final.input_peak_dbfs),
        "noise_floor_dbfs": float(final.noise_floor_dbfs),
        "frontend_gain_db": float(final.frontend_gain_db),
        "speech_active": bool(final.speech_active),
        "clip_events": int(final.clip_events),
        "speech_frames": int(speech_frames),
    }
    checks = _evaluate_checks(result, case.get("expect", {}) if isinstance(case.get("expect"), dict) else {})
    result.update(
        {
            "kind": "frontend",
            "name": str(case.get("name") or ""),
            "checks": checks,
            "passed": all(bool(item.get("passed")) for item in checks),
        }
    )
    return result


def _run_buffer_case(case: Dict[str, Any]) -> Dict[str, Any]:
    frame_size = max(1, int(case.get("frame_size") or DEFAULT_FRAME_SIZE))
    max_frames = max(1, int(case.get("max_frames") or 4))
    buffer = AudioInputBuffer(frame_size=frame_size, max_frames=max_frames)
    push_sizes = [int(item) for item in case.get("push_sizes", []) if int(item) > 0]
    for size in push_sizes:
        buffer.push(np.ones(size, dtype=np.float32))
    pop_count = max(0, int(case.get("pop_count") or 0))
    pop_sizes: List[int] = []
    for _ in range(pop_count):
        frame = buffer.pop(timeout=0.01)
        pop_sizes.append(int(frame.size) if frame is not None else 0)
    result = {
        "queued_frames": int(buffer.queued_frames()),
        "dropped_frames": int(buffer.dropped_frames()),
        "pop_sizes": pop_sizes,
    }
    checks = _evaluate_checks(result, case.get("expect", {}) if isinstance(case.get("expect"), dict) else {})
    result.update(
        {
            "kind": "buffer",
            "name": str(case.get("name") or ""),
            "checks": checks,
            "passed": all(bool(item.get("passed")) for item in checks),
        }
    )
    return result


async def _run_pipeline_attempt(
    client: httpx.AsyncClient,
    piper_url: str,
    mode: str,
    model_dir: str,
    stable_partial_repeats: int,
    hotword_pack: HotwordPack,
    command_grammar: CommandGrammarMatcher,
    case: Dict[str, Any],
    *,
    attempt_index: int,
) -> Dict[str, Any]:
    source = str(case.get("source") or "").strip().lower()
    if source == "model_wav":
        rel = str(case.get("wav_path") or "").strip()
        audio, sample_rate = _model_audio(Path(model_dir) / rel)
    else:
        audio, sample_rate = await _tts_audio(client, piper_url, str(case.get("text") or ""))
    audio = _resample(audio, sample_rate, DEFAULT_SAMPLE_RATE)
    rng = np.random.default_rng(int(case.get("seed") or 11))
    capture = _apply_pipeline_effects(audio, DEFAULT_SAMPLE_RATE, case, rng)

    use_aec = bool(case.get("use_aec"))
    render_bleed = float(case.get("render_bleed") or 0.0)
    aec_available = False
    apm: Optional[WebRtcApm] = None
    if use_aec:
        apm = WebRtcApm(DEFAULT_SAMPLE_RATE)
        aec_available = bool(apm.available)
        if render_bleed > 0:
            capture = capture + (np.asarray(audio, dtype=np.float32)[: capture.size] * render_bleed)
        if use_aec and not aec_available:
            return {
                "kind": "pipeline",
                "name": str(case.get("name") or ""),
                "status": "skipped",
                "reason": "AEC DLL unavailable",
            }

    events: List[AsrEvent] = []
    errors: List[str] = []
    frontend = AudioFrontEndProcessor(sample_rate_hz=DEFAULT_SAMPLE_RATE, frame_size=DEFAULT_FRAME_SIZE)
    backend = create_streaming_asr_backend(
        mode=mode,
        stable_partial_repeats=stable_partial_repeats,
        hotword_pack=hotword_pack,
        on_event=events.append,
        on_error=errors.append,
    )
    backend.start()
    speech_frames = 0
    try:
        for offset in range(0, capture.size, DEFAULT_FRAME_SIZE):
            frame = np.asarray(capture[offset : offset + DEFAULT_FRAME_SIZE], dtype=np.float32)
            if frame.size < DEFAULT_FRAME_SIZE:
                padded = np.zeros(DEFAULT_FRAME_SIZE, dtype=np.float32)
                padded[: frame.size] = frame
                frame = padded
            if apm is not None and aec_available:
                reference = np.asarray(audio[offset : offset + DEFAULT_FRAME_SIZE], dtype=np.float32)
                if reference.size < DEFAULT_FRAME_SIZE:
                    padded_ref = np.zeros(DEFAULT_FRAME_SIZE, dtype=np.float32)
                    padded_ref[: reference.size] = reference
                    reference = padded_ref
                frame = apm.process(frame, reference)
            processed = frontend.process(frame)
            if frontend.status().speech_active:
                speech_frames += 1
            backend.push_audio(processed, DEFAULT_SAMPLE_RATE)
        backend.finish()
    finally:
        backend.stop()
        if apm is not None:
            apm.close()

    final_events = [event for event in events if event.event_type == "final" and str(event.text or "").strip()]
    partial_events = [event for event in events if event.event_type == "partial" and str(event.text or "").strip()]
    final_text = str(final_events[-1].text if final_events else "").strip()
    stable_text = str(partial_events[-1].stable_text if partial_events else "").strip()
    grammar_match = command_grammar.canonicalize(final_text or stable_text)
    effective_text = str(grammar_match.canonical_text or final_text or stable_text).strip()
    frontend_status = frontend.status()
    result = {
        "kind": "pipeline",
        "name": str(case.get("name") or ""),
        "attempt_index": int(attempt_index),
        "source": source,
        "input_text": str(case.get("text") or ""),
        "final_text": final_text,
        "stable_text": stable_text,
        "effective_text": effective_text,
        "grammar_route": grammar_match.route_type,
        "grammar_game_name": grammar_match.game_name,
        "grammar_applied": grammar_match.applied,
        "speech_frames": int(speech_frames),
        "clip_events": int(frontend_status.clip_events),
        "frontend_gain_db": float(frontend_status.frontend_gain_db),
        "noise_floor_dbfs": float(frontend_status.noise_floor_dbfs),
        "aec_used": bool(use_aec and aec_available),
        "errors": errors,
    }
    checks = _evaluate_checks(result, case.get("expect", {}) if isinstance(case.get("expect"), dict) else {})
    result["checks"] = checks
    result["passed"] = all(bool(item.get("passed")) for item in checks) and not errors
    return result


async def _run_pipeline_case(
    client: httpx.AsyncClient,
    piper_url: str,
    mode: str,
    model_dir: str,
    stable_partial_repeats: int,
    hotword_pack: HotwordPack,
    command_grammar: CommandGrammarMatcher,
    case: Dict[str, Any],
) -> Dict[str, Any]:
    attempts = max(1, int(case.get("attempts") or 3))
    required_successes = max(1, int(case.get("required_successes") or 1))
    attempt_results: List[Dict[str, Any]] = []
    passed_results: List[Dict[str, Any]] = []

    for attempt_index in range(1, attempts + 1):
        attempt = await _run_pipeline_attempt(
            client=client,
            piper_url=piper_url,
            mode=mode,
            model_dir=model_dir,
            stable_partial_repeats=stable_partial_repeats,
            hotword_pack=hotword_pack,
            command_grammar=command_grammar,
            case=case,
            attempt_index=attempt_index,
        )
        attempt_results.append(attempt)
        if attempt.get("passed"):
            passed_results.append(attempt)
        if len(passed_results) >= required_successes:
            break

    def _score_attempt(result: Dict[str, Any]) -> tuple[int, int, int]:
        checks = result.get("checks", []) if isinstance(result.get("checks"), list) else []
        passed_checks = sum(1 for item in checks if bool(item.get("passed")))
        total_checks = len(checks)
        error_count = len(result.get("errors", []) or [])
        return (passed_checks, total_checks, -error_count)

    chosen = passed_results[0] if passed_results else max(attempt_results, key=_score_attempt)
    final_result = dict(chosen)
    final_result["attempts"] = attempts
    final_result["required_successes"] = required_successes
    final_result["successful_attempts"] = len(passed_results)
    final_result["attempt_results"] = attempt_results
    final_result["passed"] = len(passed_results) >= required_successes
    if final_result["passed"]:
        final_result["status"] = "ok"
    return final_result


async def _run() -> int:
    args = _parse_args()
    payload = _load_json(Path(args.scenarios))
    if not isinstance(payload, dict):
        raise SystemExit("scenario file root must be a JSON object")

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        runtime_cfg = await _load_runtime_config(client, args.panel_url)
        hotword_pack = _load_hotword_pack(runtime_cfg)
        command_grammar = _load_command_grammar(runtime_cfg)

        frontend_results = [
            _run_frontend_case(case)
            for case in payload.get("frontend", [])
            if isinstance(case, dict)
        ]
        buffer_results = [
            _run_buffer_case(case)
            for case in payload.get("buffer", [])
            if isinstance(case, dict)
        ]

        pipeline_results: List[Dict[str, Any]] = []
        for case in payload.get("pipeline", []):
            if not isinstance(case, dict):
                continue
            pipeline_results.append(
                await _run_pipeline_case(
                    client=client,
                    piper_url=args.piper_url,
                    mode=normalize_streaming_asr_mode(args.mode),
                    model_dir=str(Path(args.model_dir).resolve()),
                    stable_partial_repeats=max(1, int(args.stable_partial_repeats)),
                    hotword_pack=hotword_pack,
                    command_grammar=command_grammar,
                    case=case,
                )
            )

    all_results = frontend_results + buffer_results + pipeline_results
    passed = sum(1 for item in all_results if item.get("passed"))
    failed = sum(1 for item in all_results if item.get("status") != "skipped" and not item.get("passed"))
    skipped = sum(1 for item in all_results if item.get("status") == "skipped")
    report = {
        "status": "ok",
        "mode": normalize_streaming_asr_mode(args.mode),
        "model_dir": str(Path(args.model_dir).resolve()),
        "total": len(all_results),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "results": all_results,
    }
    _save_json(Path(args.output), report)
    print(json.dumps({"total": report["total"], "passed": passed, "failed": failed, "skipped": skipped}, ensure_ascii=False))
    if args.fail_on_error and failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
