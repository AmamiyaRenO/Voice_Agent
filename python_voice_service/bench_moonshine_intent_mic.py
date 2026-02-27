#!/usr/bin/env python3
"""Quick Moonshine IntentRecognizer benchmark with manifest game names."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Iterable, List, Optional

from moonshine_voice import (
    EmbeddingModelArch,
    IntentRecognizer,
    LineCompleted,
    MicTranscriber,
    ModelArch,
    TranscriptEventListener,
    get_embedding_model,
    get_model_for_language,
    string_to_model_arch,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Moonshine IntentRecognizer using game names from manifest."
    )
    parser.add_argument(
        "--manifest",
        default=str(Path(__file__).resolve().parents[1] / "scripts" / "intent_service" / "manifest.json"),
        help="Path to intent manifest.json (games[].name + games[].synonyms).",
    )
    parser.add_argument(
        "--intents",
        default="",
        help="Optional extra intents, comma-separated.",
    )
    parser.add_argument(
        "--language",
        default="en",
        help="ASR language for Moonshine transcriber.",
    )
    parser.add_argument(
        "--asr-arch",
        default="",
        help="Optional Moonshine ASR arch string (e.g. small-streaming, medium-streaming).",
    )
    parser.add_argument(
        "--embedding-model",
        default="embeddinggemma-300m",
        help="Embedding model name for IntentRecognizer.",
    )
    parser.add_argument(
        "--embedding-variant",
        default="q4",
        help="Embedding variant: fp32/fp16/q8/q4/q4f16.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.58,
        help="Intent similarity threshold (0-1).",
    )
    parser.add_argument(
        "--utterance",
        action="append",
        default=[],
        help="Optional text-only test (can be passed multiple times). If set, mic is not used.",
    )
    return parser


def _unique_keep_order(items: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for raw in items:
        text = str(raw or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _load_manifest_intents(manifest_path: Path) -> List[str]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise RuntimeError(f"Failed to load manifest {manifest_path}: {exc}") from exc

    intents: List[str] = []
    for game in payload.get("games", []) or []:
        name = str(game.get("name", "") or "").strip()
        if name:
            intents.append(name)
        for synonym in game.get("synonyms", []) or []:
            s = str(synonym or "").strip()
            if s:
                intents.append(s)
    return _unique_keep_order(intents)


def _parse_arch(value: str) -> Optional[ModelArch]:
    text = str(value or "").strip().lower()
    if not text:
        return None
    return string_to_model_arch(text)


class _PrinterListener(TranscriptEventListener):
    def on_line_completed(self, event: LineCompleted) -> None:
        line = getattr(event, "line", None)
        if line and line.text:
            print(f"[asr] {line.text}")


def _build_intent_recognizer(
    *,
    embedding_model: str,
    embedding_variant: str,
    threshold: float,
) -> IntentRecognizer:
    model_path, model_arch = get_embedding_model(embedding_model, embedding_variant)
    if not isinstance(model_arch, EmbeddingModelArch):
        raise RuntimeError("Unexpected embedding model arch type")
    recognizer = IntentRecognizer(
        model_path=model_path,
        model_arch=model_arch,
        model_variant=embedding_variant,
        threshold=float(threshold),
    )
    return recognizer


def _run_text_mode(recognizer: IntentRecognizer, utterances: List[str]) -> int:
    hit_holder: List[tuple[str, str, float]] = []

    # Register callback for all intents via generic callback.
    recognizer.set_on_intent(
        lambda m: hit_holder.append((m.trigger_phrase, m.utterance, float(m.similarity)))
    )

    print(f"[intent] text mode, utterances={len(utterances)}")
    for raw in utterances:
        utterance = str(raw).strip()
        hit_holder.clear()
        matched = recognizer.process_utterance(utterance)
        if matched and hit_holder:
            best = max(hit_holder, key=lambda x: x[2])
            print(
                f"[intent] '{utterance}' -> HIT trigger='{best[0]}' similarity={best[2]:.3f}"
            )
        else:
            print(f"[intent] '{utterance}' -> NO_MATCH")
    return 0


def main() -> int:
    args = _parser().parse_args()

    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest_intents = _load_manifest_intents(manifest_path)
    extra_intents = _unique_keep_order(args.intents.split(","))
    intents = _unique_keep_order([*manifest_intents, *extra_intents])
    if not intents:
        print("[intent] no intents found", file=sys.stderr)
        return 2

    recognizer = _build_intent_recognizer(
        embedding_model=args.embedding_model,
        embedding_variant=args.embedding_variant,
        threshold=args.threshold,
    )

    try:
        # Register intents first, then choose text or mic mode.
        for intent in intents:
            recognizer.register_intent(intent, lambda *_: None)

        print(f"[intent] loaded intents={len(intents)} threshold={recognizer.threshold:.2f}")
        for text in intents:
            print(f"  - {text}")

        if args.utterance:
            return _run_text_mode(recognizer, args.utterance)

        wanted_arch = _parse_arch(args.asr_arch)
        asr_model_path, asr_model_arch = get_model_for_language(args.language, wanted_arch)
        transcriber = MicTranscriber(model_path=asr_model_path, model_arch=asr_model_arch)
        transcriber.add_listener(_PrinterListener())
        transcriber.add_listener(recognizer)

        # Print intent hits from live stream.
        recognizer.set_on_intent(
            lambda m: print(
                f"[intent] HIT trigger='{m.trigger_phrase}' "
                f"utterance='{m.utterance}' similarity={m.similarity:.3f}"
            )
        )

        print("[intent] listening... press Ctrl+C to stop")
        transcriber.start()
        try:
            while True:
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            transcriber.stop()
            transcriber.close()
    finally:
        recognizer.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
