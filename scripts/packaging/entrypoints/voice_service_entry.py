#!/usr/bin/env python3
from __future__ import annotations

import os

import uvicorn

import main as voice_service_main


def main() -> int:
    port = int((os.getenv("PORT", "8000") or "8000").strip())
    uvicorn.run(voice_service_main.app, host="0.0.0.0", port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

