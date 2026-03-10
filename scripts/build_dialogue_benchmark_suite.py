#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a text-only dialogue benchmark suite from public datasets.")
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().with_name("dialogue_benchmark_scenarios.sample.json")),
        help="Where to write the generated scenario JSON.",
    )
    parser.add_argument("--seed", type=int, default=42, help="Deterministic shuffle seed.")
    parser.add_argument("--empathetic-count", type=int, default=8, help="How many EmpatheticDialogues cases to export.")
    parser.add_argument("--persona-count", type=int, default=8, help="How many PersonaChat-derived memory cases to export.")
    parser.add_argument("--bst-count", type=int, default=8, help="How many BlendedSkillTalk free-response cases to export.")
    parser.add_argument("--clinc-count", type=int, default=8, help="How many CLINC OOS cases to export.")
    return parser.parse_args()


def _load_dataset(*args: Any, split: str) -> Any:
    try:
        from datasets import load_dataset
    except Exception as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError(
            "The 'datasets' package is required. Install it with "
            "`python_voice_service\\.venv_asr\\Scripts\\python.exe -m pip install datasets==2.21.0`."
        ) from exc
    return load_dataset(*args, split=split, trust_remote_code=True)


def _save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _clean_text(text: str) -> str:
    value = str(text or "").strip()
    value = value.replace("_comma_", ",")
    value = value.replace("_period_", ".")
    value = value.replace("_question_", "?")
    value = value.replace("_exclamation_", "!")
    value = re.sub(r"\s+", " ", value).strip()
    if value and value[-1] not in ".!?":
        value = f"{value}."
    return value


def _normalize_fact_statement(text: str) -> str:
    value = _clean_text(text)
    if value.startswith("i "):
        value = "I" + value[1:]
    elif value.startswith("my "):
        value = "My" + value[2:]
    return value


def _extract_like_value(statement: str) -> str:
    match = re.search(r"\bi like\s+(.+?)(?:[.!?]|$)", statement, flags=re.IGNORECASE)
    return (match.group(1).strip() if match else "").strip(" .,!?:;'\"")


def _extract_origin_value(statement: str) -> str:
    match = re.search(r"\bi live in\s+(.+?)(?:[.!?]|$)", statement, flags=re.IGNORECASE)
    return (match.group(1).strip() if match else "").strip(" .,!?:;'\"")


def _unique_by_key(rows: Iterable[Dict[str, Any]], key_fn) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for row in rows:
        key = key_fn(row)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _build_empathetic_cases(count: int, rng: random.Random) -> List[Dict[str, Any]]:
    ds = _load_dataset("empathetic_dialogues", split="validation")
    rows = []
    for row in ds:
        prompt = _clean_text(row.get("prompt", ""))
        if len(prompt) < 24:
            continue
        rows.append({"prompt": prompt, "context": str(row.get("context") or "").strip()})
    rows = _unique_by_key(rows, lambda item: item["prompt"].casefold())
    rng.shuffle(rows)
    cases: List[Dict[str, Any]] = []
    for index, row in enumerate(rows[: max(0, count)]):
        cases.append(
            {
                "name": f"benchmark_empathetic_{index+1:02d}",
                "category": "benchmark_empathy",
                "source_dataset": "empathetic_dialogues",
                "user_id": f"benchmark_empathetic_user_{index+1:02d}",
                "source_context": row["context"],
                "text": row["prompt"],
                "expect_route": "QUERY",
                "require_final_text": True,
                "max_sentences": 3,
                "max_chars": 220,
                "max_similarity_to_user": 0.82,
                "max_token_overlap_to_user": 0.72,
                "reject_contains": [
                    "what would you like to explore next",
                    "how would you like to proceed",
                    "ready to help",
                ],
            }
        )
    return cases


def _build_persona_memory_cases(count: int, rng: random.Random) -> List[Dict[str, Any]]:
    ds = _load_dataset("bavard/personachat_truecased", "sample", split="train")
    candidates: List[Dict[str, str]] = []
    for row in ds:
        for raw in row.get("personality", []) or []:
            statement = _normalize_fact_statement(str(raw or ""))
            lowered = statement.casefold()
            if "i like " in lowered:
                value = _extract_like_value(statement)
                if value:
                    candidates.append({"field": "like", "statement": statement, "value": value})
            elif "i live in " in lowered:
                value = _extract_origin_value(statement)
                if value:
                    candidates.append({"field": "origin", "statement": statement, "value": value})
    candidates = _unique_by_key(candidates, lambda item: f"{item['field']}|{item['value'].casefold()}")
    rng.shuffle(candidates)
    cases: List[Dict[str, Any]] = []
    for index, item in enumerate(candidates[: max(0, count)]):
        if item["field"] == "like":
            follow_up = "What do I like?"
        else:
            follow_up = "Where am I from?"
        cases.append(
            {
                "name": f"benchmark_persona_memory_{index+1:02d}",
                "category": "benchmark_persona_memory",
                "source_dataset": "bavard/personachat_truecased",
                "user_id": f"benchmark_persona_memory_user_{index+1:02d}",
                "steps": [
                    {
                        "name": "share_fact",
                        "text": item["statement"],
                        "expect_route": "QUERY",
                        "allowed_providers": ["memory_write"],
                        "expect_contains": ["got it"],
                        "reject_contains": ["what would you like to explore next"],
                    },
                    {
                        "name": "recall_fact",
                        "text": follow_up,
                        "expect_route": "QUERY",
                        "allowed_providers": ["memory"],
                        "expect_contains": [item["value"]],
                    },
                ],
            }
        )
    return cases


def _build_bst_cases(count: int, rng: random.Random) -> List[Dict[str, Any]]:
    ds = _load_dataset("blended_skill_talk", split="validation")
    rows: List[Dict[str, str]] = []
    for row in ds:
        messages = row.get("free_messages") or []
        if not isinstance(messages, list) or not messages:
            continue
        text = _clean_text(str(messages[0] or ""))
        if len(text) < 18:
            continue
        rows.append({"text": text, "context": str(row.get("context") or "").strip()})
    rows = _unique_by_key(rows, lambda item: item["text"].casefold())
    rng.shuffle(rows)
    cases: List[Dict[str, Any]] = []
    for index, row in enumerate(rows[: max(0, count)]):
        cases.append(
            {
                "name": f"benchmark_smalltalk_{index+1:02d}",
                "category": "benchmark_smalltalk",
                "source_dataset": "blended_skill_talk",
                "user_id": f"benchmark_smalltalk_user_{index+1:02d}",
                "source_context": row["context"],
                "text": row["text"],
                "expect_route": "QUERY",
                "require_final_text": True,
                "max_sentences": 3,
                "max_chars": 220,
                "max_similarity_to_user": 0.8,
                "max_token_overlap_to_user": 0.7,
                "reject_contains": [
                    "what would you like to explore next",
                    "how would you like to proceed",
                ],
            }
        )
    return cases


def _build_clinc_cases(count: int, rng: random.Random) -> List[Dict[str, Any]]:
    ds = _load_dataset("clinc_oos", "small", split="validation")
    label_names = list(ds.features["intent"].names)
    rows: List[Dict[str, str]] = []
    for row in ds:
        text = _clean_text(str(row.get("text") or ""))
        if len(text) < 12:
            continue
        intent_id = int(row.get("intent"))
        intent_name = label_names[intent_id]
        if intent_name != "oos":
            continue
        rows.append({"text": text, "intent": intent_name})
    rows = _unique_by_key(rows, lambda item: item["text"].casefold())
    rng.shuffle(rows)
    cases: List[Dict[str, Any]] = []
    for index, row in enumerate(rows[: max(0, count)]):
        cases.append(
            {
                "name": f"benchmark_oos_{index+1:02d}",
                "category": "benchmark_oos",
                "source_dataset": "clinc_oos",
                "user_id": f"benchmark_oos_user_{index+1:02d}",
                "text": row["text"],
                "expect_route": "QUERY",
                "require_final_text": True,
                "max_sentences": 2,
                "max_chars": 180,
                "reject_contains": [
                    "Opening Bean Bag Toss",
                    "Opening Disc Golf",
                    "Going back home",
                ],
            }
        )
    return cases


def _build_local_guard_cases() -> List[Dict[str, Any]]:
    return [
        {
            "name": "benchmark_guard_weather_01",
            "category": "benchmark_general_guard",
            "source_dataset": "local_regression",
            "user_id": "benchmark_general_guard_user_01",
            "text": "How's the weather today?",
            "expect_route": "QUERY",
            "require_final_text": True,
            "max_sentences": 2,
            "max_chars": 120,
            "max_similarity_to_user": 0.65,
            "max_token_overlap_to_user": 0.6,
            "reject_contains": [
                "how's the weather today",
                "hows the weather today",
                "weather today?",
            ],
        }
    ]


def main() -> int:
    args = _parse_args()
    rng = random.Random(int(args.seed))
    scenarios: List[Dict[str, Any]] = []
    scenarios.extend(_build_empathetic_cases(args.empathetic_count, rng))
    scenarios.extend(_build_persona_memory_cases(args.persona_count, rng))
    scenarios.extend(_build_bst_cases(args.bst_count, rng))
    scenarios.extend(_build_clinc_cases(args.clinc_count, rng))
    scenarios.extend(_build_local_guard_cases())
    output_path = Path(args.output).resolve()
    _save_json(output_path, scenarios)
    print(json.dumps({"total": len(scenarios)}, ensure_ascii=False))
    print(str(output_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
