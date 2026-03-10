#!/usr/bin/env python3
from __future__ import annotations

import os

import uvicorn

import desktop_runtime


def main() -> int:
    port = int((os.getenv("PORT", "8787") or "8787").strip())
    uvicorn.run(desktop_runtime.app, host="0.0.0.0", port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
