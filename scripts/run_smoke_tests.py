#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run smoke tests for core local services.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--pytest",
        default=sys.executable,
        help="Python executable used to run pytest.",
    )
    parser.add_argument(
        "--extra",
        nargs="*",
        default=[],
        help="Extra pytest args.",
    )
    parser.add_argument(
        "--no-panel-live",
        action="store_true",
        help="Skip live User Panel API smoke checks.",
    )
    parser.add_argument(
        "--panel-url",
        default="",
        help="Optional override for VOICE_AGENT_PANEL_URL for live panel smoke checks.",
    )
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    smoke_targets = [
        str(repo_root / "tests" / "test_smoke_services.py"),
        str(repo_root / "tests" / "test_smoke_userpanel_contract.py"),
    ]
    if not args.no_panel_live:
        smoke_targets.append(str(repo_root / "tests" / "test_smoke_userpanel_live.py"))

    cmd = [
        args.pytest,
        "-m",
        "pytest",
        *smoke_targets,
        "-q",
    ]
    cmd.extend(args.extra)
    print("[smoke]", " ".join(cmd))
    env = dict(os.environ)
    if not args.no_panel_live:
        env["VOICE_AGENT_PANEL_LIVE"] = "1"
        if args.panel_url:
            env["VOICE_AGENT_PANEL_URL"] = args.panel_url
    result = subprocess.run(cmd, cwd=str(repo_root), env=env)
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
