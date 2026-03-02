from __future__ import annotations

import asyncio
import base64
import io
import os
import re
import time
import wave
from collections import OrderedDict
from collections import deque
from typing import Dict, Optional, Tuple

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

app = FastAPI(title="Qwen3-TTS HTTP Wrapper")

# NucBox M6 (AMD CPU) friendly defaults.
# These apply even when no environment variables are set.
_DEFAULT_MAX_CONCURRENCY = 1
_DEFAULT_CACHE_SIZE = 32
_DEFAULT_TORCH_NUM_THREADS = 8
_DEFAULT_TORCH_NUM_INTEROP = 1
_DEFAULT_TORCH_MATMUL_PRECISION = "high"
_DEFAULT_MIN_NEW_TOKENS = 160
_DEFAULT_MAX_NEW_TOKENS = 900
_DEFAULT_NEW_TOKENS_BASE = 220
_DEFAULT_NEW_TOKENS_PER_CHAR = 1.2
_DEFAULT_DO_SAMPLE = False
_DEFAULT_TOP_P = 0.90
_DEFAULT_TOP_K = 40
_DEFAULT_TEMPERATURE = 0.85
_DEFAULT_SPEED_PROFILE = "fast"

_WHITESPACE_RE = re.compile(r"\s+")
_REPEATED_PUNCT_RE = re.compile(r"([!?.,;:])\1+")


class TtsRequest(BaseModel):
    text: str = Field(..., min_length=1)
    speaker: Optional[str] = None
    voice: Optional[str] = None  # compatibility alias
    instruct: Optional[str] = None
    language: Optional[str] = None


class TtsResponse(BaseModel):
    audio_wav_base64: str
    sample_rate: int


def _env(key: str, default: str = "") -> str:
    v = os.getenv(key)
    return v.strip() if v else default


def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    n = v.strip().lower()
    if n in {"1", "true", "yes", "on"}:
        return True
    if n in {"0", "false", "no", "off"}:
        return False
    return default


def _env_int(key: str, default: int, floor: int = 0) -> int:
    v = os.getenv(key)
    if v is None:
        return max(floor, default)
    try:
        return max(floor, int(v))
    except Exception:
        return max(floor, default)


def _env_float(key: str, default: float, floor: float = 0.0) -> float:
    v = os.getenv(key)
    if v is None:
        return max(floor, default)
    try:
        return max(floor, float(v))
    except Exception:
        return max(floor, default)


def _pick_speaker(speaker: Optional[str], voice: Optional[str]) -> str:
    # Prefer explicit speaker, then voice alias, then env default.
    s = (speaker or "").strip() or (voice or "").strip()
    if s:
        return s
    return _env("QWEN_TTS_SPEAKER", "").strip()


def _pick_instruct(instruct: Optional[str]) -> str:
    s = (instruct or "").strip()
    if s:
        return s
    return _env("QWEN_TTS_INSTRUCT", "").strip()


def _pick_language(language: Optional[str]) -> str:
    s = (language or "").strip()
    if s:
        return s
    return _env("QWEN_TTS_LANGUAGE", "English").strip()


def _normalize_text_for_tts(text: str) -> str:
    t = _WHITESPACE_RE.sub(" ", (text or "").strip())
    t = _REPEATED_PUNCT_RE.sub(r"\1", t)
    max_chars = _env_int("QWEN_TTS_MAX_TEXT_CHARS", 0, floor=0)
    if max_chars > 0 and len(t) > max_chars:
        # Prefer a natural boundary so clipped tails do not sound like
        # missing half-words. Keep near-max length to preserve latency intent.
        clipped = t[:max_chars]
        boundary_candidates = [
            clipped.rfind(" "),
            clipped.rfind("."),
            clipped.rfind("!"),
            clipped.rfind("?"),
            clipped.rfind(","),
            clipped.rfind(";"),
            clipped.rfind(":"),
        ]
        boundary = max(boundary_candidates)
        min_boundary = max(12, int(max_chars * 0.65))
        if boundary >= min_boundary:
            clipped = clipped[:boundary]
        t = clipped.strip()
    return t


def _speed_profile() -> str:
    profile = _env("QWEN_TTS_SPEED_PROFILE", _DEFAULT_SPEED_PROFILE).strip().lower()
    if profile not in {"fast", "balanced", "quality"}:
        return _DEFAULT_SPEED_PROFILE
    return profile


def _profile_token_defaults(profile: str) -> Tuple[int, int, int, float]:
    if profile == "quality":
        return 240, 1200, 320, 1.8
    if profile == "balanced":
        return 180, 900, 240, 1.4
    # fast
    return 96, 420, 120, 0.75


def _to_wav_bytes(mono_float: np.ndarray, sample_rate: int) -> bytes:
    if mono_float.ndim != 1:
        mono_float = mono_float.reshape(-1)
    if mono_float.size == 0:
        raise HTTPException(status_code=500, detail="TTS returned empty audio")

    x = np.asarray(mono_float, dtype=np.float32)
    x = np.clip(x, -1.0, 1.0)
    pcm16 = (x * 32767.0).astype(np.int16)

    with io.BytesIO() as bio:
        with wave.open(bio, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(int(sample_rate))
            wf.writeframes(pcm16.tobytes())
        return bio.getvalue()


# Gate synthesis concurrency: CPU-only mini PCs will saturate easily.
_SYNTH_SEM = asyncio.Semaphore(
    max(1, int(os.getenv("QWEN_TTS_MAX_CONCURRENCY", str(_DEFAULT_MAX_CONCURRENCY)) or "1"))
)
_CACHE_SIZE = max(0, int(os.getenv("QWEN_TTS_CACHE_SIZE", str(_DEFAULT_CACHE_SIZE)) or "0"))
_CACHE: "OrderedDict[Tuple[str, str, str, str], Tuple[bytes, int]]" = OrderedDict()
_CACHE_LOCK = asyncio.Lock()
_INFLIGHT: Dict[Tuple[str, str, str, str], "asyncio.Future[Tuple[bytes, int]]"] = {}
_INFLIGHT_LOCK = asyncio.Lock()
_TORCH_CONFIGURED = False
_METRICS_SIZE = max(10, int(os.getenv("QWEN_TTS_METRICS_SIZE", "200") or "200"))
_METRICS = deque(maxlen=_METRICS_SIZE)
_METRICS_LOCK = asyncio.Lock()
_METRICS_STARTED_AT = time.perf_counter()
_METRICS_TOTAL = 0
_METRICS_CACHE_HIT = 0
_METRICS_INFLIGHT_WAIT = 0
_METRICS_ERROR = 0


def _configure_torch() -> None:
    global _TORCH_CONFIGURED
    if _TORCH_CONFIGURED:
        return
    try:
        import torch  # type: ignore
    except Exception:
        _TORCH_CONFIGURED = True
        return

    # CPU-first defaults (NucBox M6). Still allow env overrides if present.
    try:
        torch.set_num_threads(
            int(os.getenv("QWEN_TTS_NUM_THREADS", str(_DEFAULT_TORCH_NUM_THREADS)) or _DEFAULT_TORCH_NUM_THREADS)
        )
    except Exception:
        pass

    try:
        torch.set_num_interop_threads(
            int(
                os.getenv("QWEN_TTS_NUM_INTEROP", str(_DEFAULT_TORCH_NUM_INTEROP)) or _DEFAULT_TORCH_NUM_INTEROP
            )
        )
    except Exception:
        pass

    precision = os.getenv("QWEN_TTS_MATMUL_PRECISION", _DEFAULT_TORCH_MATMUL_PRECISION).strip()
    if precision:
        try:
            torch.set_float32_matmul_precision(precision)
        except Exception:
            pass

    _TORCH_CONFIGURED = True


def _load_model():
    _configure_torch()
    try:
        from qwen_tts import Qwen3TTSModel  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "qwen-tts is not installed. Install it in python_voice_service venv:\n"
            "  pip install -U qwen-tts\n"
            "Note: CPU inference can be slow; start with the 0.6B model."
        ) from exc

    model_id = _env("QWEN_TTS_MODEL", "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice")
    # CPU-only: use float32 for compatibility/stability.
    # qwen-tts uses a HuggingFace-style loader with device_map support.
    model = Qwen3TTSModel.from_pretrained(
        model_id,
        device_map=_env("QWEN_TTS_DEVICE_MAP", "cpu"),
        dtype=_env("QWEN_TTS_DTYPE", "float32"),
        attn_implementation=_env("QWEN_TTS_ATTN", "eager"),
    )
    return model


_MODEL = None


async def _record_metric(
    *,
    text_len: int,
    elapsed_ms: float,
    cache_hit: bool,
    inflight_wait: bool,
    sem_wait_ms: float,
    synth_ms: float,
    max_new_tokens: int,
    speed_profile: str,
    ok: bool,
) -> None:
    global _METRICS_TOTAL, _METRICS_CACHE_HIT, _METRICS_INFLIGHT_WAIT, _METRICS_ERROR
    async with _METRICS_LOCK:
        _METRICS_TOTAL += 1
        if cache_hit:
            _METRICS_CACHE_HIT += 1
        if inflight_wait:
            _METRICS_INFLIGHT_WAIT += 1
        if not ok:
            _METRICS_ERROR += 1
        _METRICS.append(
            {
                "ts": time.time(),
                "text_len": int(text_len),
                "elapsed_ms": round(float(elapsed_ms), 2),
                "sem_wait_ms": round(float(sem_wait_ms), 2),
                "synth_ms": round(float(synth_ms), 2),
                "max_new_tokens": int(max_new_tokens),
                "speed_profile": speed_profile,
                "cache_hit": bool(cache_hit),
                "inflight_wait": bool(inflight_wait),
                "ok": bool(ok),
            }
        )


def _build_generate_kwargs(text: str) -> Tuple[dict, int, str]:
    # Dynamic cap keeps short replies from over-generating codec tokens.
    profile = _speed_profile()
    prof_min, prof_max, prof_base, prof_per_char = _profile_token_defaults(profile)
    min_new_tokens = _env_int("QWEN_TTS_MIN_NEW_TOKENS", prof_min, floor=64)
    max_new_tokens_limit = _env_int("QWEN_TTS_MAX_NEW_TOKENS", prof_max, floor=min_new_tokens)
    base = _env_int("QWEN_TTS_NEW_TOKENS_BASE", prof_base, floor=0)
    per_char = _env_float("QWEN_TTS_NEW_TOKENS_PER_CHAR", prof_per_char, floor=0.0)

    estimated = int(base + (len(text) * per_char))
    max_new_tokens = max(min_new_tokens, min(max_new_tokens_limit, estimated))
    if profile == "fast":
        short_text_limit = _env_int("QWEN_TTS_FAST_SHORT_TEXT_LIMIT", 100, floor=1)
        short_text_token_cap = _env_int("QWEN_TTS_FAST_SHORT_MAX_NEW_TOKENS", 280, floor=64)
        if len(text) <= short_text_limit:
            max_new_tokens = min(max_new_tokens, short_text_token_cap)

    do_sample = _env_bool("QWEN_TTS_DO_SAMPLE", _DEFAULT_DO_SAMPLE)
    top_p = _env_float("QWEN_TTS_TOP_P", _DEFAULT_TOP_P, floor=0.0)
    top_k = _env_int("QWEN_TTS_TOP_K", _DEFAULT_TOP_K, floor=0)
    temperature = _env_float("QWEN_TTS_TEMPERATURE", _DEFAULT_TEMPERATURE, floor=0.0)

    kwargs = {
        "max_new_tokens": max_new_tokens,
        "do_sample": do_sample,
    }
    if do_sample:
        kwargs["top_p"] = top_p
        kwargs["top_k"] = top_k
        kwargs["temperature"] = temperature
    return kwargs, max_new_tokens, profile


@app.on_event("startup")
async def _startup() -> None:
    # Load once during startup to avoid first-request latency spikes.
    global _MODEL
    if _MODEL is None:
        _MODEL = await asyncio.to_thread(_load_model)
    # Optional startup warmup to reduce first-request latency spikes.
    warmup_text = _env("QWEN_TTS_WARMUP_TEXT", "")
    if warmup_text:
        try:
            await _synthesize(
                warmup_text,
                _env("QWEN_TTS_SPEAKER", ""),
                _env("QWEN_TTS_INSTRUCT", ""),
                _env("QWEN_TTS_LANGUAGE", "English"),
            )
        except Exception:
            pass


async def _synthesize(text: str, speaker: str, instruct: str, language: str) -> Tuple[bytes, int]:
    global _MODEL
    started_at = time.perf_counter()
    text_len = len(text or "")
    cache_hit = False
    inflight_wait = False
    sem_wait_ms = 0.0
    synth_ms = 0.0
    max_new_tokens = 0
    speed_profile = _speed_profile()
    if _MODEL is None:
        _MODEL = await asyncio.to_thread(_load_model)

    key = (text, speaker or "", instruct or "", language or "")
    if _CACHE_SIZE > 0:
        async with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if cached:
                _CACHE.move_to_end(key)
                cache_hit = True
                await _record_metric(
                    text_len=text_len,
                    elapsed_ms=(time.perf_counter() - started_at) * 1000.0,
                    cache_hit=cache_hit,
                    inflight_wait=inflight_wait,
                    sem_wait_ms=sem_wait_ms,
                    synth_ms=synth_ms,
                    max_new_tokens=max_new_tokens,
                    speed_profile=speed_profile,
                    ok=True,
                )
                return cached

    async with _INFLIGHT_LOCK:
        existing = _INFLIGHT.get(key)
        if existing is not None:
            inflight_wait = True
            try:
                result = await existing
                await _record_metric(
                    text_len=text_len,
                    elapsed_ms=(time.perf_counter() - started_at) * 1000.0,
                    cache_hit=cache_hit,
                    inflight_wait=inflight_wait,
                    sem_wait_ms=sem_wait_ms,
                    synth_ms=synth_ms,
                    max_new_tokens=max_new_tokens,
                    speed_profile=speed_profile,
                    ok=True,
                )
                return result
            except Exception:
                await _record_metric(
                    text_len=text_len,
                    elapsed_ms=(time.perf_counter() - started_at) * 1000.0,
                    cache_hit=cache_hit,
                    inflight_wait=inflight_wait,
                    sem_wait_ms=sem_wait_ms,
                    synth_ms=synth_ms,
                    max_new_tokens=max_new_tokens,
                    speed_profile=speed_profile,
                    ok=False,
                )
                raise
        loop = asyncio.get_running_loop()
        future: "asyncio.Future[Tuple[bytes, int]]" = loop.create_future()
        _INFLIGHT[key] = future

    try:
        sem_wait_start = time.perf_counter()
        async with _SYNTH_SEM:
            sem_wait_ms = (time.perf_counter() - sem_wait_start) * 1000.0
            kwargs, max_new_tokens, speed_profile = _build_generate_kwargs(text)

            def _run(generate_kwargs: dict):
                try:
                    import torch  # type: ignore
                except Exception:
                    torch = None  # type: ignore[assignment]

                if torch:
                    with torch.inference_mode():
                        wavs, sr = _MODEL.generate_custom_voice(
                            text=text,
                            language=language,
                            speaker=speaker if speaker else None,
                            instruct=instruct if instruct else None,
                            **generate_kwargs,
                        )
                else:
                    wavs, sr = _MODEL.generate_custom_voice(
                        text=text,
                        language=language,
                        speaker=speaker if speaker else None,
                        instruct=instruct if instruct else None,
                        **generate_kwargs,
                    )
                if not wavs:
                    raise RuntimeError("Qwen3-TTS returned no waveforms")
                wav0 = wavs[0]
                return _to_wav_bytes(np.asarray(wav0), int(sr)), int(sr)

            synth_start = time.perf_counter()
            result = await asyncio.to_thread(_run, kwargs)
            synth_ms = (time.perf_counter() - synth_start) * 1000.0
            if _CACHE_SIZE > 0:
                async with _CACHE_LOCK:
                    _CACHE[key] = result
                    _CACHE.move_to_end(key)
                    while len(_CACHE) > _CACHE_SIZE:
                        _CACHE.popitem(last=False)
            future.set_result(result)
            await _record_metric(
                text_len=text_len,
                elapsed_ms=(time.perf_counter() - started_at) * 1000.0,
                cache_hit=cache_hit,
                inflight_wait=inflight_wait,
                sem_wait_ms=sem_wait_ms,
                synth_ms=synth_ms,
                max_new_tokens=max_new_tokens,
                speed_profile=speed_profile,
                ok=True,
            )
            return result
    except HTTPException as exc:
        if not future.done():
            future.set_exception(exc)
        await _record_metric(
            text_len=text_len,
            elapsed_ms=(time.perf_counter() - started_at) * 1000.0,
            cache_hit=cache_hit,
            inflight_wait=inflight_wait,
            sem_wait_ms=sem_wait_ms,
            synth_ms=synth_ms,
            max_new_tokens=max_new_tokens,
            speed_profile=speed_profile,
            ok=False,
        )
        raise
    except Exception as exc:
        wrapped = HTTPException(status_code=500, detail=f"Qwen3-TTS failed: {exc}")
        if not future.done():
            future.set_exception(wrapped)
        await _record_metric(
            text_len=text_len,
            elapsed_ms=(time.perf_counter() - started_at) * 1000.0,
            cache_hit=cache_hit,
            inflight_wait=inflight_wait,
            sem_wait_ms=sem_wait_ms,
            synth_ms=synth_ms,
            max_new_tokens=max_new_tokens,
            speed_profile=speed_profile,
            ok=False,
        )
        raise wrapped from exc
    finally:
        async with _INFLIGHT_LOCK:
            current = _INFLIGHT.get(key)
            if current is future:
                _INFLIGHT.pop(key, None)


@app.get("/metrics")
async def metrics() -> dict:
    async with _METRICS_LOCK:
        entries = list(_METRICS)
        total = _METRICS_TOTAL
        cache_hits = _METRICS_CACHE_HIT
        inflight_waits = _METRICS_INFLIGHT_WAIT
        errors = _METRICS_ERROR

    elapsed_values = [float(e.get("elapsed_ms", 0.0)) for e in entries]
    sem_wait_values = [float(e.get("sem_wait_ms", 0.0)) for e in entries]
    synth_values = [float(e.get("synth_ms", 0.0)) for e in entries]
    elapsed_sorted = sorted(elapsed_values)
    count = len(elapsed_sorted)

    def _percentile(p: float) -> float:
        if count == 0:
            return 0.0
        idx = int(round((count - 1) * p))
        idx = max(0, min(count - 1, idx))
        return float(elapsed_sorted[idx])

    avg_ms = (sum(elapsed_values) / len(elapsed_values)) if elapsed_values else 0.0
    avg_sem_wait_ms = (sum(sem_wait_values) / len(sem_wait_values)) if sem_wait_values else 0.0
    avg_synth_ms = (sum(synth_values) / len(synth_values)) if synth_values else 0.0
    uptime = time.perf_counter() - _METRICS_STARTED_AT
    return {
        "status": "ok",
        "uptime_seconds": round(float(uptime), 2),
        "totals": {
            "requests": int(total),
            "cache_hits": int(cache_hits),
            "inflight_waits": int(inflight_waits),
            "errors": int(errors),
            "cache_hit_ratio": round((cache_hits / total), 4) if total > 0 else 0.0,
        },
        "recent": {
            "window_size": int(count),
            "avg_ms": round(float(avg_ms), 2),
            "avg_sem_wait_ms": round(float(avg_sem_wait_ms), 2),
            "avg_synth_ms": round(float(avg_synth_ms), 2),
            "p50_ms": round(_percentile(0.50), 2),
            "p95_ms": round(_percentile(0.95), 2),
            "last": entries[-20:],
        },
    }


@app.get("/speak")
async def speak_get(
    text: str = Query(..., min_length=1),
    speaker: Optional[str] = Query(None),
    voice: Optional[str] = Query(None),  # compatibility alias
    instruct: Optional[str] = Query(None),
    language: Optional[str] = Query(None),
    # Compatibility no-ops (Unity currently sends these for Piper).
    model: Optional[str] = Query(None),
    config: Optional[str] = Query(None),
) -> Response:
    _ = (model, config)  # ignored
    t = _normalize_text_for_tts(text)
    if not t:
        raise HTTPException(status_code=400, detail="Empty text")

    spk = _pick_speaker(speaker, voice)
    ins = _pick_instruct(instruct)
    lang = _pick_language(language)

    wav_bytes, _sr = await _synthesize(t, spk, ins, lang)
    return Response(content=wav_bytes, media_type="audio/wav")


@app.post("/speak", response_model=TtsResponse)
async def speak_post(payload: TtsRequest) -> TtsResponse:
    t = _normalize_text_for_tts(payload.text)
    if not t:
        raise HTTPException(status_code=400, detail="Empty text")

    spk = _pick_speaker(payload.speaker, payload.voice)
    ins = _pick_instruct(payload.instruct)
    lang = _pick_language(payload.language)

    wav_bytes, sr = await _synthesize(t, spk, ins, lang)
    audio_b64 = base64.b64encode(wav_bytes).decode("ascii")
    return TtsResponse(audio_wav_base64=audio_b64, sample_rate=sr)
