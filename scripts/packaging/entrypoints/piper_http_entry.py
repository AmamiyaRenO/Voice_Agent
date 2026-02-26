#!/usr/bin/env python3
from __future__ import annotations

import os

import uvicorn

import piper_http


def main() -> int:
    port = int((os.getenv("PORT", "5005") or "5005").strip())
    uvicorn.run(piper_http.app, host="0.0.0.0", port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

