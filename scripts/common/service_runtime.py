from __future__ import annotations

import signal
import sys
import time
from typing import Callable, Optional


def run_service_loop(
    *,
    service_name: str,
    start: Callable[[], None],
    stop: Callable[[], None],
    poll: Optional[Callable[[], None]] = None,
    interval_sec: float = 0.5,
) -> int:
    start()

    def _term(signum, frame):  # type: ignore[override]
        print(f"[{service_name}] signal {signum}, stopping...")
        stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _term)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _term)

    try:
        while True:
            if poll is not None:
                poll()
            time.sleep(interval_sec)
    except KeyboardInterrupt:
        _term(signal.SIGINT, None)
    return 0
