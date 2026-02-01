#!/usr/bin/env python3
"""
Download a Faster-Whisper (CTranslate2) model from HuggingFace to a local folder.

Why:
- `python_voice_service` uses `faster-whisper`, which loads CTranslate2 models.
- If your environment is offline (HF_HUB_OFFLINE / outgoing traffic disabled),
  you must pre-download the model to disk and then set WHISPER_MODEL_PATH to that folder.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def _default_out_dir(repo_id: str) -> Path:
    # Store under repo_root/models/whisper/<org>__<name>
    safe = repo_id.replace("/", "__")
    repo_root = Path(__file__).resolve().parents[1]
    return repo_root / "models" / "whisper" / safe


def main() -> int:
    parser = argparse.ArgumentParser(description="Download Faster-Whisper model to local folder.")
    parser.add_argument(
        "--repo",
        default="Systran/faster-distil-whisper-large-v3",
        help="HuggingFace repo id (CTranslate2 / faster-whisper model).",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output directory. Default: <repo_root>/models/whisper/<org>__<name>",
    )
    parser.add_argument(
        "--revision",
        default=None,
        help="Optional revision/commit/tag.",
    )
    args = parser.parse_args()

    try:
        from huggingface_hub import snapshot_download
    except Exception as exc:  # pragma: no cover
        raise SystemExit(
            "Missing dependency: huggingface_hub. It is usually installed with faster-whisper.\n"
            "Try: pip install huggingface_hub"
        ) from exc

    repo_id = str(args.repo).strip()
    if not repo_id:
        raise SystemExit("--repo is required")

    out_dir = Path(args.out).resolve() if args.out else _default_out_dir(repo_id).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[download-whisper] repo: {repo_id}")
    print(f"[download-whisper] out : {out_dir}")
    print("[download-whisper] downloading... (first time can take a while)")

    # Ensure we are not forced offline for the download step.
    # Users can re-enable offline afterwards.
    os.environ.pop("HF_HUB_OFFLINE", None)
    os.environ.pop("HF_HUB_DISABLE_TELEMETRY", None)

    snapshot_download(
        repo_id=repo_id,
        local_dir=str(out_dir),
        local_dir_use_symlinks=False,
        revision=args.revision,
        resume_download=True,
    )

    print("[download-whisper] done.")
    print()
    print("Next steps (PowerShell):")
    print(f'$env:WHISPER_MODEL_PATH="{out_dir}"')
    print("# optional: run offline after downloaded")
    print('$env:HF_HUB_OFFLINE="1"')
    print('python D:\\unityproject\\Voice_Agent\\scripts\\start_local_services.py')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


