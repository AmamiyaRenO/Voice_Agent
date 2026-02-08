from __future__ import annotations

import argparse
import io
import time
import wave

import httpx


def wav_duration_seconds(wav_bytes: bytes) -> float:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        return 0.0 if rate <= 0 else frames / float(rate)


def main() -> int:
    ap = argparse.ArgumentParser(description="Benchmark /speak TTS HTTP endpoint.")
    ap.add_argument("--url", default="http://127.0.0.1:5005/speak", help="Base /speak URL")
    ap.add_argument("--text", default="Hello! Let's get started with your plan.", help="Text to synthesize")
    ap.add_argument("--speaker", default="", help="Optional speaker/voice")
    ap.add_argument("--instruct", default="", help="Optional instruct/style string")
    ap.add_argument("--runs", type=int, default=3, help="Number of runs")
    args = ap.parse_args()

    params = {"text": args.text}
    if args.speaker:
        params["voice"] = args.speaker  # works for both piper_http (ignored) and qwen_tts_http (alias)
    if args.instruct:
        params["instruct"] = args.instruct

    with httpx.Client(timeout=120.0) as client:
        # Warm-up
        print(f"[bench] GET {args.url} (warm-up)")
        client.get(args.url, params=params).raise_for_status()

        for i in range(args.runs):
            start = time.perf_counter()
            r = client.get(args.url, params=params)
            r.raise_for_status()
            elapsed = time.perf_counter() - start
            dur = wav_duration_seconds(r.content)
            rtf = (elapsed / dur) if dur > 1e-6 else 0.0
            print(f"[bench] run={i+1} elapsed={elapsed:0.3f}s audio={dur:0.3f}s RTF={rtf:0.2f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

