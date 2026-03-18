#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx


DEFAULT_PANEL_URL = "http://127.0.0.1:8787"
DEFAULT_VOICE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT = httpx.Timeout(90.0)


def _normalize_compare_text(value: str) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff\s]+", " ", text)
    return " ".join(text.split())


def _text_similarity(left: str, right: str) -> float:
    lhs = _normalize_compare_text(left)
    rhs = _normalize_compare_text(right)
    if not lhs or not rhs:
        return 0.0
    if lhs == rhs:
        return 1.0
    return difflib.SequenceMatcher(None, lhs, rhs).ratio()


def _token_overlap_ratio(left: str, right: str) -> float:
    lhs_tokens = _normalize_compare_text(left).split()
    rhs_tokens = _normalize_compare_text(right).split()
    if not lhs_tokens or not rhs_tokens:
        return 0.0
    lhs_set = set(lhs_tokens)
    rhs_set = set(rhs_tokens)
    overlap = len(lhs_set & rhs_set)
    return overlap / float(max(1, min(len(lhs_set), len(rhs_set))))


def _sentence_count(text: str) -> int:
    compact = str(text or "").strip()
    if not compact:
        return 0
    parts = [part.strip() for part in re.split(r"[.!?。！？]+", compact) if part.strip()]
    return max(1, len(parts)) if compact else 0


def _word_count(text: str) -> int:
    return len([part for part in re.split(r"\s+", str(text or "").strip()) if part])


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def _save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run repeatable conversation evaluation scenarios.")
    parser.add_argument("--panel-url", default=DEFAULT_PANEL_URL, help="Desktop runtime base URL.")
    parser.add_argument("--voice-url", default=DEFAULT_VOICE_URL, help="Voice service base URL.")
    parser.add_argument(
        "--scenarios",
        default=str(Path(__file__).resolve().with_name("conversation_eval_scenarios.sample.json")),
        help="Scenario JSON file.",
    )
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parents[1] / "runtime" / "evals" / "latest_conversation_eval.json"),
        help="Where to write the JSON report.",
    )
    parser.add_argument(
        "--profile",
        choices=("local", "cloud"),
        default="",
        help="Force a single conversation profile for every scenario.",
    )
    parser.add_argument(
        "--fail-on-error",
        action="store_true",
        help="Return non-zero when any scenario fails.",
    )
    return parser.parse_args()


async def _load_runtime_config(client: httpx.AsyncClient, panel_url: str) -> Dict[str, Any]:
    response = await client.get(f"{panel_url.rstrip('/')}/api/runtime/config")
    response.raise_for_status()
    return response.json()


async def _apply_runtime_profile(
    client: httpx.AsyncClient,
    panel_url: str,
    runtime_cfg: Dict[str, Any],
    profile: str,
) -> Dict[str, Any]:
    payload = {
        "conversation_profile": profile,
        "conversation_pipeline_mode": runtime_cfg.get("conversation_pipeline_mode", "direct_unified"),
        "local_asr_mode": runtime_cfg.get("local_asr_mode", "moonshine-medium"),
        "cloud_asr_mode": runtime_cfg.get("cloud_asr_mode", "api"),
        "openai_api_key": runtime_cfg.get("openai_api_key", ""),
        "openai_base_url": runtime_cfg.get("openai_base_url", ""),
        "openai_transcribe_model": runtime_cfg.get("openai_transcribe_model", ""),
        "openai_transcribe_prompt": runtime_cfg.get("openai_transcribe_prompt", ""),
        "openai_response_model": runtime_cfg.get("openai_response_model", ""),
        "ollama_model": runtime_cfg.get("ollama_model", ""),
        "launch_triggers": runtime_cfg.get("launch_triggers", ""),
        "exit_keywords": runtime_cfg.get("exit_keywords", ""),
        "use_llm_intent_classifier": runtime_cfg.get("use_llm_intent_classifier", False),
        "use_moonshine_intent_recognizer": runtime_cfg.get("use_moonshine_intent_recognizer", False),
    }
    response = await client.post(f"{panel_url.rstrip('/')}/api/runtime/config", json=payload)
    response.raise_for_status()
    return response.json()


async def _ensure_runtime_profile(
    client: httpx.AsyncClient,
    panel_url: str,
    runtime_cfg: Dict[str, Any],
    profile: str,
) -> Dict[str, Any]:
    desired = str(profile or "").strip().lower()
    if desired not in {"local", "cloud"}:
        return runtime_cfg
    current = str(runtime_cfg.get("conversation_profile") or "local").strip().lower()
    if current == desired:
        return runtime_cfg
    return await _apply_runtime_profile(client, panel_url, runtime_cfg, desired)


def _string_list(node: Dict[str, Any], key: str) -> List[str]:
    value = node.get(key)
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        merged = value.replace("\r\n", "\n").replace(";", ",").replace("\n", ",")
        return [part.strip() for part in merged.split(",") if part.strip()]
    return []


def _scenario_steps(scenario: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_steps = scenario.get("steps")
    if isinstance(raw_steps, list) and raw_steps:
        steps = [step for step in raw_steps if isinstance(step, dict)]
        if steps:
            return steps
    return [scenario]


def _resolve_expected_provider(node: Dict[str, Any], profile: str) -> str:
    value = node.get("expect_provider")
    if isinstance(value, dict):
        candidate = value.get(profile) or value.get("default") or ""
        return str(candidate or "").strip()
    return str(value or "").strip()


def _resolve_allowed_providers(node: Dict[str, Any], profile: str) -> List[str]:
    value = node.get("allowed_providers")
    if isinstance(value, dict):
        selected = value.get(profile)
        if isinstance(selected, list):
            return [str(item).strip() for item in selected if str(item).strip()]
        if isinstance(selected, str) and selected.strip():
            return [selected.strip()]
        fallback = value.get("default")
        if isinstance(fallback, list):
            return [str(item).strip() for item in fallback if str(item).strip()]
        if isinstance(fallback, str) and fallback.strip():
            return [fallback.strip()]
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _profile_readiness(runtime_cfg: Dict[str, Any], profile: str) -> Tuple[bool, str]:
    normalized = str(profile or "").strip().lower()
    if normalized != "cloud":
        return True, ""
    api_key = str(runtime_cfg.get("openai_api_key") or "").strip()
    base_url = str(runtime_cfg.get("openai_base_url") or "").strip()
    if api_key or base_url:
        return True, ""
    return False, "cloud profile skipped: OPENAI API key or base URL is not configured"


async def _run_turn_stream(
    client: httpx.AsyncClient,
    voice_url: str,
    request_payload: Dict[str, Any],
) -> Dict[str, Any]:
    started = time.perf_counter()
    route = ""
    provider = ""
    final_text = ""
    error_text = ""
    chunks: List[str] = []
    first_event_ms: Optional[float] = None
    first_chunk_ms: Optional[float] = None
    events: List[Dict[str, Any]] = []
    final_event: Dict[str, Any] = {}

    try:
        async with client.stream(
            "POST",
            f"{voice_url.rstrip('/')}/conversation/turn/stream",
            json=request_payload,
            timeout=DEFAULT_TIMEOUT,
        ) as response:
            response.raise_for_status()
            async for raw_line in response.aiter_lines():
                line = str(raw_line or "").strip()
                if not line:
                    continue
                event = json.loads(line)
                now_ms = (time.perf_counter() - started) * 1000.0
                if first_event_ms is None:
                    first_event_ms = now_ms
                event_type = str(event.get("type") or "").strip().lower()
                route = str(event.get("route") or route).strip().upper()
                provider = str(event.get("provider") or provider).strip()
                events.append(event)
                if event_type == "chunk":
                    chunk_text = str(event.get("text") or "").strip()
                    if chunk_text:
                        chunks.append(chunk_text)
                        if first_chunk_ms is None:
                            first_chunk_ms = now_ms
                elif event_type == "final":
                    final_text = str(event.get("text") or "").strip()
                    final_event = dict(event)
                elif event_type == "error":
                    error_text = str(event.get("message") or "").strip()
                    final_event = dict(event)
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        error_text = str(exc).strip() or exc.__class__.__name__

    return {
        "route": route,
        "provider": provider,
        "first_event_ms": round(first_event_ms, 1) if first_event_ms is not None else None,
        "first_chunk_ms": round(first_chunk_ms, 1) if first_chunk_ms is not None else None,
        "final_ms": round((time.perf_counter() - started) * 1000.0, 1),
        "chunks": chunks,
        "final_text": final_text,
        "error": error_text,
        "events": events,
        "doc_probe": final_event.get("doc_probe") if isinstance(final_event.get("doc_probe"), dict) else {},
        "fallback_reason": str(final_event.get("fallback_reason") or "").strip(),
        "structured_type": str(final_event.get("structured_type") or "").strip(),
        "domain": str(final_event.get("domain") or "").strip(),
        "answer_mode": str(final_event.get("answer_mode") or "").strip(),
        "general_focus": str(final_event.get("general_focus") or "").strip(),
        "clarify_kind": str(final_event.get("clarify_kind") or "").strip(),
        "summary_used": bool(final_event.get("summary_used")),
        "summary_model": str(final_event.get("summary_model") or "").strip(),
        "summary_fallback_reason": str(final_event.get("summary_fallback_reason") or "").strip(),
        "general_doc_kinds": list(final_event.get("general_doc_kinds") or []),
        "focus_domain": str(final_event.get("focus_domain") or "").strip(),
        "focus_general_focus": str(final_event.get("focus_general_focus") or "").strip(),
        "clarification_resume_strategy": str(final_event.get("clarification_resume_strategy") or "").strip(),
    }


def _evaluate_turn_result(result: Dict[str, Any], expectations: Dict[str, Any], profile: str) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    expected_route = str(expectations.get("expect_route") or "").strip().upper()
    if expected_route:
        checks.append(
            {
                "name": "route",
                "passed": str(result.get("route") or "").strip().upper() == expected_route,
                "expected": expected_route,
                "actual": result.get("route", ""),
            }
        )

    expected_provider = _resolve_expected_provider(expectations, profile)
    if expected_provider:
        checks.append(
            {
                "name": "provider",
                "passed": str(result.get("provider") or "").strip() == expected_provider,
                "expected": expected_provider,
                "actual": result.get("provider", ""),
            }
        )

    allowed_providers = _resolve_allowed_providers(expectations, profile)
    if allowed_providers:
        actual_provider = str(result.get("provider") or "").strip()
        checks.append(
            {
                "name": "allowed_provider",
                "passed": actual_provider in allowed_providers,
                "expected": allowed_providers,
                "actual": actual_provider,
            }
        )

    haystack = "\n".join([*result.get("chunks", []), str(result.get("final_text") or "")]).lower()
    for phrase in _string_list(expectations, "expect_contains"):
        checks.append({"name": f"contains:{phrase}", "passed": phrase.lower() in haystack})
    for phrase in _string_list(expectations, "reject_contains"):
        checks.append({"name": f"reject:{phrase}", "passed": phrase.lower() not in haystack})
    if expectations.get("require_final_text"):
        checks.append(
            {
                "name": "final_text_present",
                "passed": bool(str(result.get("final_text") or "").strip()),
            }
        )
    final_text = str(result.get("final_text") or "").strip()
    user_text = str(expectations.get("text") or "").strip()
    max_chars = int(expectations.get("max_chars") or 0)
    if max_chars > 0:
        checks.append(
            {
                "name": "max_chars",
                "passed": len(final_text) <= max_chars,
                "expected": max_chars,
                "actual": len(final_text),
            }
        )
    max_words = int(expectations.get("max_words") or 0)
    if max_words > 0:
        actual_words = _word_count(final_text)
        checks.append(
            {
                "name": "max_words",
                "passed": actual_words <= max_words,
                "expected": max_words,
                "actual": actual_words,
            }
        )
    max_sentences = int(expectations.get("max_sentences") or 0)
    if max_sentences > 0:
        actual_sentences = _sentence_count(final_text)
        checks.append(
            {
                "name": "max_sentences",
                "passed": actual_sentences <= max_sentences,
                "expected": max_sentences,
                "actual": actual_sentences,
            }
        )
    max_similarity = expectations.get("max_similarity_to_user")
    if max_similarity is not None and user_text:
        actual_similarity = round(_text_similarity(user_text, final_text), 4)
        checks.append(
            {
                "name": "max_similarity_to_user",
                "passed": actual_similarity <= float(max_similarity),
                "expected": float(max_similarity),
                "actual": actual_similarity,
            }
        )
    max_overlap = expectations.get("max_token_overlap_to_user")
    if max_overlap is not None and user_text:
        actual_overlap = round(_token_overlap_ratio(user_text, final_text), 4)
        checks.append(
            {
                "name": "max_token_overlap_to_user",
                "passed": actual_overlap <= float(max_overlap),
                "expected": float(max_overlap),
                "actual": actual_overlap,
            }
        )
    if result.get("error"):
        checks.append({"name": "no_error", "passed": False, "actual": result.get("error", "")})
    return checks


async def _run_scenario(
    client: httpx.AsyncClient,
    panel_url: str,
    voice_url: str,
    scenario: Dict[str, Any],
    index: int,
    runtime_cfg: Dict[str, Any],
    forced_profile: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    base_name = str(scenario.get("name") or f"scenario_{index+1}").strip() or f"scenario_{index+1}"
    desired_profile = forced_profile or str(scenario.get("profile") or "").strip().lower()
    ready, skip_reason = _profile_readiness(runtime_cfg, desired_profile)
    if not ready:
        fallback_profile = str(runtime_cfg.get("conversation_profile") or "local").strip().lower() or "local"
        return (
            {
                "name": f"{base_name}[{desired_profile}]",
                "base_name": base_name,
                "category": str(scenario.get("category") or "uncategorized").strip() or "uncategorized",
                "profile": desired_profile or fallback_profile,
                "passed": False,
                "skipped": True,
                "route": "",
                "provider": "",
                "first_event_ms": None,
                "first_chunk_ms": None,
                "final_ms": None,
                "final_text": "",
                "error": "",
                "skip_reason": skip_reason,
                "steps": [],
            },
            runtime_cfg,
        )
    runtime_cfg = await _ensure_runtime_profile(client, panel_url, runtime_cfg, desired_profile)
    active_profile = str(runtime_cfg.get("conversation_profile") or "local").strip().lower() or "local"
    category = str(scenario.get("category") or "uncategorized").strip() or "uncategorized"
    display_name = f"{base_name}[{active_profile}]" if desired_profile else base_name
    steps = _scenario_steps(scenario)
    default_user_id = str(scenario.get("user_id") or "").strip()
    default_source = str(scenario.get("source") or "conversation_eval").strip() or "conversation_eval"
    step_results: List[Dict[str, Any]] = []

    for step_index, step in enumerate(steps):
        text = str(step.get("text") or "").strip()
        if not text:
            step_results.append(
                {
                    "name": f"step_{step_index+1}",
                    "passed": False,
                    "error": "scenario text is required",
                    "checks": [{"name": "text", "passed": False, "actual": ""}],
                }
            )
            continue

        corr_id = (
            str(step.get("corr_id") or "").strip()
            or f"eval-{index+1}-{step_index+1}-{int(time.time() * 1000)}"
        )
        request_payload: Dict[str, Any] = {
            "text": text,
            "corr_id": corr_id,
            "source": str(step.get("source") or scenario.get("source") or default_source).strip() or default_source,
        }
        user_id = str(step.get("user_id") or default_user_id).strip()
        if user_id:
            request_payload["user_id"] = user_id
        if "barge_in" in step or "barge_in" in scenario:
            request_payload["barge_in"] = bool(step.get("barge_in", scenario.get("barge_in", False)))
        interrupted_text = str(step.get("interrupted_tts_text") or scenario.get("interrupted_tts_text") or "").strip()
        if interrupted_text:
            request_payload["interrupted_tts_text"] = interrupted_text
        interrupted_corr_id = str(step.get("interrupted_tts_corr_id") or scenario.get("interrupted_tts_corr_id") or "").strip()
        if interrupted_corr_id:
            request_payload["interrupted_tts_corr_id"] = interrupted_corr_id

        result = await _run_turn_stream(client, voice_url, request_payload)
        checks = _evaluate_turn_result(result, step, active_profile)
        passed = all(check.get("passed", False) for check in checks) if checks else not result.get("error")
        step_result = {
            "name": str(step.get("name") or f"step_{step_index+1}").strip() or f"step_{step_index+1}",
            "text": text,
            "corr_id": corr_id,
            "user_id": user_id,
            "barge_in": bool(request_payload.get("barge_in")),
            "route": result.get("route", ""),
            "provider": result.get("provider", ""),
            "first_event_ms": result.get("first_event_ms"),
            "first_chunk_ms": result.get("first_chunk_ms"),
            "final_ms": result.get("final_ms"),
            "chunks": result.get("chunks", []),
            "final_text": result.get("final_text", ""),
            "error": result.get("error", ""),
            "doc_probe": result.get("doc_probe", {}),
            "fallback_reason": result.get("fallback_reason", ""),
            "structured_type": result.get("structured_type", ""),
            "domain": result.get("domain", ""),
            "answer_mode": result.get("answer_mode", ""),
            "general_focus": result.get("general_focus", ""),
            "clarify_kind": result.get("clarify_kind", ""),
            "checks": checks,
            "passed": passed,
        }
        step_results.append(step_result)

        sleep_ms = int(step.get("sleep_ms") or 0)
        if sleep_ms > 0:
            await asyncio.sleep(sleep_ms / 1000.0)

    passed = all(step.get("passed", False) for step in step_results)
    aggregate = step_results[-1] if step_results else {}
    return (
        {
            "name": display_name,
            "base_name": base_name,
            "category": category,
            "profile": active_profile,
            "passed": passed,
            "skipped": False,
            "route": aggregate.get("route", ""),
            "provider": aggregate.get("provider", ""),
            "first_event_ms": aggregate.get("first_event_ms"),
            "first_chunk_ms": aggregate.get("first_chunk_ms"),
            "final_ms": aggregate.get("final_ms"),
            "final_text": aggregate.get("final_text", ""),
            "error": aggregate.get("error", ""),
            "doc_probe": aggregate.get("doc_probe", {}),
            "fallback_reason": aggregate.get("fallback_reason", ""),
            "structured_type": aggregate.get("structured_type", ""),
            "domain": aggregate.get("domain", ""),
            "answer_mode": aggregate.get("answer_mode", ""),
            "general_focus": aggregate.get("general_focus", ""),
            "clarify_kind": aggregate.get("clarify_kind", ""),
            "steps": step_results,
        },
        runtime_cfg,
    )


def _category_summary(results: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    summary: Dict[str, Dict[str, int]] = {}
    for item in results:
        category = str(item.get("category") or "uncategorized").strip() or "uncategorized"
        bucket = summary.setdefault(category, {"total": 0, "passed": 0, "failed": 0, "skipped": 0})
        bucket["total"] += 1
        if item.get("skipped"):
            bucket["skipped"] += 1
        elif item.get("passed"):
            bucket["passed"] += 1
        else:
            bucket["failed"] += 1
    return summary


async def _async_main(args: argparse.Namespace) -> int:
    scenario_path = Path(args.scenarios).resolve()
    output_path = Path(args.output).resolve()
    scenarios_payload = _load_json(scenario_path)
    if not isinstance(scenarios_payload, list):
        raise RuntimeError("scenario file must contain a JSON array")

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        original_runtime = await _load_runtime_config(client, args.panel_url)
        active_runtime = original_runtime
        results: List[Dict[str, Any]] = []
        try:
            for index, raw in enumerate(scenarios_payload):
                if not isinstance(raw, dict):
                    results.append(
                        {
                            "name": f"scenario_{index+1}",
                            "category": "uncategorized",
                            "profile": str(active_runtime.get("conversation_profile") or "local"),
                            "passed": False,
                            "error": "scenario must be an object",
                            "steps": [],
                        }
                    )
                    continue
                result, active_runtime = await _run_scenario(
                    client,
                    args.panel_url,
                    args.voice_url,
                    raw,
                    index,
                    active_runtime,
                    args.profile,
                )
                results.append(result)
        finally:
            original_profile = str(original_runtime.get("conversation_profile") or "local").strip().lower() or "local"
            active_profile = str(active_runtime.get("conversation_profile") or "local").strip().lower() or "local"
            if active_profile != original_profile:
                await _apply_runtime_profile(client, args.panel_url, active_runtime, original_profile)

    passed = sum(1 for item in results if item.get("passed"))
    skipped = sum(1 for item in results if item.get("skipped"))
    failed = len(results) - passed - skipped
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "panel_url": args.panel_url,
        "voice_url": args.voice_url,
        "profile_override": args.profile or "",
        "scenario_file": str(scenario_path),
        "summary": {"total": len(results), "passed": passed, "failed": failed, "skipped": skipped},
        "category_summary": _category_summary(results),
        "results": results,
    }
    _save_json(output_path, report)

    print(json.dumps(report["summary"], ensure_ascii=False))
    print(str(output_path))
    if args.fail_on_error and failed:
        return 1
    return 0


def main() -> int:
    args = _parse_args()
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
