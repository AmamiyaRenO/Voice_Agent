#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
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
DEFAULT_SAMPLE_DIR = REPO_ROOT / "runtime" / "eval_assets" / "asr_samples"
DEFAULT_USER_CONFIG = REPO_ROOT / "scripts" / "local_services.user.json"
DEFAULT_BASE_CONFIG = REPO_ROOT / "scripts" / "local_services.default.json"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run repeatable streaming ASR regression scenarios.")
    parser.add_argument("--panel-url", default=DEFAULT_PANEL_URL, help="Desktop runtime base URL.")
    parser.add_argument("--piper-url", default=DEFAULT_PIPER_URL, help="Piper HTTP base URL for synthetic speech.")
    parser.add_argument(
        "--scenarios",
        default=str(Path(__file__).resolve().with_name("asr_regression_scenarios.sample.json")),
        help="Scenario JSON file.",
    )
    parser.add_argument(
        "--output",
        default=str(REPO_ROOT / "runtime" / "evals" / "latest_asr_regression.json"),
        help="Where to write the JSON report.",
    )
    parser.add_argument("--mode", default="moonshine-medium", help="Streaming ASR mode to evaluate.")
    parser.add_argument("--model-dir", default=str(DEFAULT_SAMPLE_DIR), help="Base directory for bundled sample WAVs.")
    parser.add_argument("--stable-partial-repeats", type=int, default=1, help="Stable partial repeat threshold.")
    parser.add_argument("--fail-on-error", action="store_true", help="Return non-zero when any case fails.")
    return parser.parse_args()


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def _save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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
    effective_manifest = str(paths.get("game_manifest") or paths.get("intent_manifest") or (REPO_ROOT / "scripts" / "intent_service" / "manifest.json"))
    return {
        "launch_triggers": ", ".join(intent.get("launch_triggers", ["open", "start", "launch", "play", "begin", "load"])),
        "exit_keywords": ", ".join(intent.get("exit_keywords", ["back home", "go home", "return home", "go back", "quit", "exit", "stop", "cancel", "close", "close game"])),
        "effective_game_manifest_path": str((REPO_ROOT / effective_manifest).resolve()) if not Path(effective_manifest).is_absolute() else effective_manifest,
        "asr_hotword_strategy": str(env.get("VOICE_ASR_HOTWORD_STRATEGY") or "commands_games_memory").strip(),
    }


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

    memory_path = REPO_ROOT / "scripts" / "dialog_service" / "user_memory.json"
    if memory_path.exists():
        memory_root = _load_json(memory_path)
        profiles = memory_root.get("profiles", {}) if isinstance(memory_root, dict) else {}
        if isinstance(profiles, dict):
            for profile in profiles.values():
                if not isinstance(profile, dict):
                    continue
                add_entry(str(profile.get("name") or "").strip())
                add_entry(str(profile.get("display_name") or "").strip())
                add_entry(str(profile.get("favorite_game") or "").strip())

    return HotwordPack(entries=entries)


def _load_command_grammar(runtime_cfg: Dict[str, Any]) -> CommandGrammarMatcher:
    return CommandGrammarMatcher.from_sources(
        launch_triggers=runtime_cfg.get("launch_triggers"),
        exit_keywords=runtime_cfg.get("exit_keywords"),
        manifest_path=str(runtime_cfg.get("effective_game_manifest_path") or "").strip(),
    )


async def _load_runtime_config(client: httpx.AsyncClient, panel_url: str) -> Dict[str, Any]:
    try:
        response = await client.get(f"{panel_url.rstrip('/')}/api/runtime/config")
        response.raise_for_status()
        return response.json()
    except Exception:
        return _load_runtime_config_fallback()


async def _tts_audio(client: httpx.AsyncClient, piper_url: str, text: str) -> tuple[np.ndarray, int]:
    response = await client.get(f"{piper_url.rstrip('/')}/speak", params={"text": text}, timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()
    return _decode_wav_bytes(response.content)


def _model_audio(path: Path) -> tuple[np.ndarray, int]:
    return _decode_wav_bytes(path.read_bytes())


async def _run_case(
    client: httpx.AsyncClient,
    piper_url: str,
    mode: str,
    model_dir: str,
    stable_partial_repeats: int,
    hotword_pack: HotwordPack,
    command_grammar: CommandGrammarMatcher,
    case: Dict[str, Any],
) -> Dict[str, Any]:
    events: List[AsrEvent] = []
    errors: List[str] = []

    backend = create_streaming_asr_backend(
        mode=mode,
        stable_partial_repeats=stable_partial_repeats,
        hotword_pack=hotword_pack,
        on_event=events.append,
        on_error=errors.append,
    )
    backend.start()
    try:
        source = str(case.get("source") or "").strip().lower()
        if source == "model_wav":
            rel = str(case.get("wav_path") or "").strip()
            audio, sample_rate = _model_audio(Path(model_dir) / rel)
        else:
            audio, sample_rate = await _tts_audio(client, piper_url, str(case.get("text") or ""))
        audio = _resample(audio, sample_rate, 16000)
        block = 160
        for offset in range(0, audio.size, block):
            chunk = np.asarray(audio[offset : offset + block], dtype=np.float32)
            if chunk.size < block:
                fixed = np.zeros(block, dtype=np.float32)
                fixed[: chunk.size] = chunk
                chunk = fixed
            backend.push_audio(chunk, 16000)
        backend.finish()
        final_events = [event for event in events if event.event_type == "final" and str(event.text or "").strip()]
        partial_events = [event for event in events if event.event_type == "partial" and str(event.text or "").strip()]
        final_text = str(final_events[-1].text if final_events else "").strip()
        stable_text = str(partial_events[-1].stable_text if partial_events else "").strip()
        partial_text = str(partial_events[-1].text if partial_events else "").strip()
        candidate_text = final_text or stable_text or partial_text
        grammar_match = command_grammar.canonicalize(candidate_text)
        effective_text = str(grammar_match.canonical_text or candidate_text).strip()
        checks: List[Dict[str, Any]] = []
        if case.get("expect_exact"):
            expected = _normalize_compare_text(str(case["expect_exact"]))
            actual = _normalize_compare_text(effective_text)
            checks.append({"name": "exact", "passed": actual == expected, "expected": expected, "actual": actual})
        for phrase in case.get("expect_contains", []) or []:
            expected = _normalize_compare_text(str(phrase))
            actual_haystack = _normalize_compare_text(effective_text)
            checks.append({"name": f"contains:{phrase}", "passed": expected in actual_haystack})
        for phrase in case.get("reject_contains", []) or []:
            rejected = _normalize_compare_text(str(phrase))
            actual_haystack = _normalize_compare_text(effective_text)
            checks.append({"name": f"reject:{phrase}", "passed": rejected not in actual_haystack})
        passed = all(bool(check.get("passed")) for check in checks) and not errors
        return {
            "name": case.get("name", ""),
            "source": source,
            "input_text": case.get("text", ""),
            "wav_path": case.get("wav_path", ""),
            "final_text": final_text,
            "stable_text": stable_text,
            "partial_text": partial_text,
            "effective_text": effective_text,
            "grammar_route": grammar_match.route_type,
            "grammar_game_name": grammar_match.game_name,
            "grammar_applied": grammar_match.applied,
            "partial_count": len(partial_events),
            "final_count": len(final_events),
            "checks": checks,
            "errors": errors,
            "passed": passed,
        }
    finally:
        backend.stop()


async def _run() -> int:
    args = _parse_args()
    scenarios = _load_json(Path(args.scenarios))
    if not isinstance(scenarios, list):
        raise SystemExit("scenario file root must be a JSON array")
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        runtime_cfg = await _load_runtime_config(client, args.panel_url)
        hotword_pack = _load_hotword_pack(runtime_cfg)
        command_grammar = _load_command_grammar(runtime_cfg)
        results: List[Dict[str, Any]] = []
        passed = 0
        failed = 0
        for case in scenarios:
            if not isinstance(case, dict):
                continue
            result = await _run_case(
                client=client,
                piper_url=args.piper_url,
                mode=normalize_streaming_asr_mode(args.mode),
                model_dir=str(Path(args.model_dir).resolve()),
                stable_partial_repeats=max(1, int(args.stable_partial_repeats)),
                hotword_pack=hotword_pack,
                command_grammar=command_grammar,
                case=case,
            )
            results.append(result)
            if result["passed"]:
                passed += 1
            else:
                failed += 1
    report = {
        "status": "ok",
        "mode": normalize_streaming_asr_mode(args.mode),
        "model_dir": str(Path(args.model_dir).resolve()),
        "total": passed + failed,
        "passed": passed,
        "failed": failed,
        "results": results,
    }
    _save_json(Path(args.output), report)
    print(json.dumps({"total": report["total"], "passed": passed, "failed": failed}, ensure_ascii=False))
    if args.fail_on_error and failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
