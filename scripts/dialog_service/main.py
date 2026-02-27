#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from common.service_runtime import run_service_loop

try:
    from .dialog_config import load_config
    from .dialog_service_impl import DialogService
except Exception:
    from dialog_config import load_config
    from dialog_service_impl import DialogService


def main() -> int:
    cfg = load_config()
    svc = DialogService(cfg)
    return run_service_loop(
        service_name="dialog",
        start=svc.start,
        stop=svc.stop,
        interval_sec=0.5,
    )


if __name__ == "__main__":
    sys.exit(main())
