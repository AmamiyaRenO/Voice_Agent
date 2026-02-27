#!/usr/bin/env python3
"""Record microphone audio and benchmark ASR /transcribe quickly."""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Optional

import httpx
import numpy as np

try:
    import sounddevice as sd
except Exception as exc:  # pragma: no cover - optional dependency at runtime
    sd = None
    _sounddevice_import_error = exc
else:
    _sounddevice_import_error = None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record from microphone and send PCM to /transcribe."
    )
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000/transcribe",
        help="Transcribe endpoint URL.",
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=4.0,
        help="Recording duration in seconds per run.",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=16000,
        help="Microphone sample rate.",
    )
    parser.add_argument(
        "--mode",
        default=None,
        help="Optional mode query (whisper-large-v3/api/moonshine-small/moonshine-medium).",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="Optional language query (e.g., en, zh).",
    )
    parser.add_argument(
        "--beam-size",
        type=int,
        default=None,
        help="Optional beam_size query.",
    )
    parser.add_argument(
        "--device",
        default=None,
        help="Input device index or name. Default uses system default mic.",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List available audio devices and exit.",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Number of repeated recordings.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="HTTP timeout seconds.",
    )
    return parser


def _resolve_device(device: Optional[str]) -> Optional[object]:
    if device is None:
        return None
    stripped = str(device).strip()
    if not stripped:
        return None
    try:
        return int(stripped)
    except ValueError:
        return stripped


def _audio_metrics(samples: np.ndarray) -> tuple[float, float]:
    if samples.size == 0:
        return 0.0, 0.0
    normalized = samples.astype(np.float32) / 32768.0
    peak = float(np.max(np.abs(normalized)))
    rms = float(np.sqrt(np.mean(np.square(normalized))))
    return peak, rms


def main() -> int:
    args = _build_parser().parse_args()

    if sd is None:
        print(
            "[asr-mic] sounddevice not available. "
            "Install dependencies in your venv first: pip install -r requirements.txt",
            file=sys.stderr,
        )
        if _sounddevice_import_error is not None:
            print(f"[asr-mic] import error: {_sounddevice_import_error}", file=sys.stderr)
        return 2

    if args.list_devices:
        print(sd.query_devices())
        return 0

    if args.seconds <= 0:
        print("[asr-mic] --seconds must be > 0", file=sys.stderr)
        return 2
    if args.sample_rate < 8000:
        print("[asr-mic] --sample-rate should be >= 8000", file=sys.stderr)
        return 2
    if args.runs < 1:
        print("[asr-mic] --runs must be >= 1", file=sys.stderr)
        return 2

    device = _resolve_device(args.device)
    frames = max(1, int(args.seconds * args.sample_rate))

    params = {"sample_rate": int(args.sample_rate)}
    if args.mode:
        params["mode"] = args.mode
    if args.language:
        params["language"] = args.language
    if args.beam_size is not None:
        params["beam_size"] = int(args.beam_size)

    with httpx.Client(timeout=args.timeout) as client:
        for run in range(1, args.runs + 1):
            print(f"[asr-mic] run {run}/{args.runs}: recording {args.seconds:.2f}s...")
            recording = sd.rec(
                frames=frames,
                samplerate=args.sample_rate,
                channels=1,
                dtype="int16",
                device=device,
                blocking=True,
            )
            audio = np.asarray(recording, dtype=np.int16).reshape(-1)
            peak, rms = _audio_metrics(audio)
            print(f"[asr-mic] captured samples={audio.size} peak={peak:.4f} rms={rms:.4f}")

            t0 = time.perf_counter()
            response = client.post(
                args.url,
                params=params,
                content=audio.tobytes(),
                headers={"Content-Type": "application/octet-stream"},
            )
            elapsed = time.perf_counter() - t0

            if response.status_code != 200:
                print(f"[asr-mic] HTTP {response.status_code}: {response.text}", file=sys.stderr)
                return 1

            try:
                payload = response.json()
            except json.JSONDecodeError:
                print("[asr-mic] invalid JSON response:", file=sys.stderr)
                print(response.text, file=sys.stderr)
                return 1

            print(
                "[asr-mic] provider={provider} mode={mode} elapsed={elapsed:.3f}s "
                "service={service:.3f}s".format(
                    provider=payload.get("provider"),
                    mode=payload.get("mode"),
                    elapsed=elapsed,
                    service=float(payload.get("processing_seconds", 0.0) or 0.0),
                )
            )
            text = str(payload.get("text", "") or "")
            print(f"[asr-mic] text: {text}")
            if payload.get("avg_logprob") is not None:
                print(f"[asr-mic] avg_logprob={payload.get('avg_logprob')}")
            print(
                "[asr-mic] signal rms={rms} amp={amp} speech_fraction={sf}".format(
                    rms=payload.get("rms"),
                    amp=payload.get("max_amplitude"),
                    sf=payload.get("speech_fraction"),
                )
            )
            print("")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
