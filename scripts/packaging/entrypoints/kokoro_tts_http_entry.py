#!/usr/bin/env python3
from __future__ import annotations

import os

import uvicorn

import kokoro_tts_http


def main() -> int:
    port = int((os.getenv("PORT", "5007") or "5007").strip())
    uvicorn.run(kokoro_tts_http.app, host="0.0.0.0", port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
