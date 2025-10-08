from __future__ import annotations

import base64
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from fastapi.responses import Response

app = FastAPI(title="Piper TTS Wrapper")


class TtsRequest(BaseModel):
    text: str = Field(..., min_length=1)


class TtsResponse(BaseModel):
    audio_wav_base64: str
    sample_rate: int


def _env(key: str, default: str = "") -> str:
    v = os.getenv(key)
    return v.strip() if v else default


def _build_command(out_path: Path) -> list[str]:
    exe = _env("PIPER_EXECUTABLE", "piper")
    model = _env("PIPER_MODEL_PATH")
    if not model:
        raise HTTPException(status_code=500, detail="PIPER_MODEL_PATH is not configured")
    cmd = [exe, "--model", model, "--output_file", str(out_path)]
    cfg = _env("PIPER_CONFIG_PATH")
    if cfg:
        cmd += ["--config", cfg]
    speaker = _env("PIPER_SPEAKER")
    if speaker:
        cmd += ["--speaker", speaker]
    return cmd


@app.post("/speak", response_model=TtsResponse)
async def speak(payload: TtsRequest) -> TtsResponse:
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty text")

    with tempfile.TemporaryDirectory(prefix="piper-http-") as tmp:
        out_path = Path(tmp) / "out.wav"
        cmd = _build_command(out_path)
        try:
            completed = subprocess.run(
                cmd,
                input=text.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to launch Piper: {exc}") from exc

        if completed.returncode != 0:
            raise HTTPException(status_code=500, detail=completed.stderr.decode("utf-8", errors="ignore"))
        if not out_path.exists():
            raise HTTPException(status_code=500, detail="Piper did not produce output")

        audio_bytes = out_path.read_bytes()
        audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
        # Piper 默认 22050Hz，如模型不同可通过环境变量传回；这里固定 22050，或未来加读 WAV 头
        return TtsResponse(audio_wav_base64=audio_b64, sample_rate=int(_env("PIPER_SAMPLE_RATE", "22050")))


@app.get("/speak")
async def speak_get(text: str) -> Response:
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty text")

    with tempfile.TemporaryDirectory(prefix="piper-http-") as tmp:
        out_path = Path(tmp) / "out.wav"
        cmd = _build_command(out_path)
        try:
            completed = subprocess.run(
                cmd,
                input=text.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to launch Piper: {exc}") from exc

        if completed.returncode != 0:
            raise HTTPException(status_code=500, detail=completed.stderr.decode("utf-8", errors="ignore"))
        if not out_path.exists():
            raise HTTPException(status_code=500, detail="Piper did not produce output")

        audio_bytes = out_path.read_bytes()
        return Response(content=audio_bytes, media_type="audio/wav")


