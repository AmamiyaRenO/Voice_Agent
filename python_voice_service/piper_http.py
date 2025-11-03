from __future__ import annotations

import base64
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

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


def _run_piper_subprocess(text: str) -> bytes:
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

        return out_path.read_bytes()


async def _request_piper_server(text: str) -> bytes:
    server_url = _env("PIPER_SERVER_URL")
    if not server_url:
        raise HTTPException(status_code=500, detail="PIPER_SERVER_URL is not configured")

    timeout = _env("PIPER_SERVER_TIMEOUT")
    timeout_value: Optional[float] = None
    if timeout:
        try:
            timeout_value = float(timeout)
        except ValueError:
            raise HTTPException(status_code=500, detail="PIPER_SERVER_TIMEOUT must be a number")

    endpoint = server_url.rstrip("/") + "/synthesize"
    payload: dict[str, object] = {"text": text, "audio_format": "wav"}
    speaker = _env("PIPER_SPEAKER")
    if speaker:
        try:
            payload["speaker_id"] = int(speaker)
        except ValueError:
            payload["speaker"] = speaker

    async with httpx.AsyncClient(timeout=timeout_value) as client:
        try:
            response = await client.post(endpoint, json=payload)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Failed to reach Piper server: {exc}") from exc

    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Piper server error: {response.status_code} {response.text}")

    if not response.content:
        raise HTTPException(status_code=500, detail="Piper server returned empty response")

    return response.content


async def _synthesize_audio(text: str) -> tuple[bytes, int]:
    sample_rate = int(_env("PIPER_SAMPLE_RATE", "22050"))
    server_url = _env("PIPER_SERVER_URL")
    if server_url:
        audio_bytes = await _request_piper_server(text)
    else:
        audio_bytes = _run_piper_subprocess(text)
    return audio_bytes, sample_rate


@app.post("/speak", response_model=TtsResponse)
async def speak(payload: TtsRequest) -> TtsResponse:
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty text")

    audio_bytes, sample_rate = await _synthesize_audio(text)
    audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
    return TtsResponse(audio_wav_base64=audio_b64, sample_rate=sample_rate)


@app.get("/speak")
async def speak_get(text: str) -> Response:
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty text")

    audio_bytes, _ = await _synthesize_audio(text)
    return Response(content=audio_bytes, media_type="audio/wav")


