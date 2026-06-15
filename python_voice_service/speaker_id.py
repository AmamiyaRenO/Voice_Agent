from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

try:
    import onnxruntime as ort
except Exception:  # pragma: no cover - optional at runtime
    ort = None

try:
    from scipy.signal import resample_poly
except Exception:  # pragma: no cover - optional at runtime
    resample_poly = None

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MEMORY_PATH = REPO_ROOT / "scripts" / "dialog_service" / "user_memory.json"
DEFAULT_MODEL_PATH = REPO_ROOT / "runtime" / "models" / "speaker_id" / "voxceleb_ECAPA512_LM.onnx"
SPEAKER_ID_TARGET_SAMPLE_RATE = 16000
SPEAKER_ID_FBANK_BINS = 80
SPEAKER_ID_FRAME_LENGTH = 400
SPEAKER_ID_FRAME_SHIFT = 160
SPEAKER_ID_NFFT = 512


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if not raw:
        return bool(default)
    return raw not in {"0", "false", "no", "off", "disabled"}


def _safe_float(raw: Any, default: float) -> float:
    try:
        return float(raw)
    except Exception:
        return float(default)


def _safe_int(raw: Any, default: int) -> int:
    try:
        return int(raw)
    except Exception:
        return int(default)


def _normalize_embedding(value: np.ndarray) -> np.ndarray:
    arr = np.asarray(value, dtype=np.float32).reshape(-1)
    if arr.size <= 0:
        return np.zeros(0, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm <= 1e-8:
        return np.zeros(arr.shape, dtype=np.float32)
    return (arr / norm).astype(np.float32, copy=False)


def _cosine_similarity(left: np.ndarray, right: np.ndarray) -> float:
    lhs = _normalize_embedding(left)
    rhs = _normalize_embedding(right)
    if lhs.size <= 0 or rhs.size <= 0 or lhs.size != rhs.size:
        return 0.0
    return float(np.dot(lhs, rhs))


def _resample_audio(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    if samples.size <= 0 or src_rate <= 0 or dst_rate <= 0 or src_rate == dst_rate:
        return samples.astype(np.float32, copy=False)
    if resample_poly is not None:
        try:
            return np.asarray(resample_poly(samples, dst_rate, src_rate), dtype=np.float32)
        except Exception:
            pass
    duration = samples.size / float(src_rate)
    dst_count = max(1, int(round(duration * dst_rate)))
    src_x = np.linspace(0.0, 1.0, num=samples.size, endpoint=False)
    dst_x = np.linspace(0.0, 1.0, num=dst_count, endpoint=False)
    return np.asarray(np.interp(dst_x, src_x, samples), dtype=np.float32)


def _hz_to_mel(value: np.ndarray) -> np.ndarray:
    arr = np.asarray(value, dtype=np.float32)
    return 2595.0 * np.log10(1.0 + (arr / 700.0))


def _mel_to_hz(value: np.ndarray) -> np.ndarray:
    arr = np.asarray(value, dtype=np.float32)
    return 700.0 * (np.power(10.0, arr / 2595.0) - 1.0)


def _build_mel_filterbank(
    *,
    sample_rate: int,
    n_fft: int,
    num_mels: int,
    low_freq_hz: float = 20.0,
    high_freq_hz: Optional[float] = None,
) -> np.ndarray:
    high = float(high_freq_hz) if high_freq_hz is not None else float(sample_rate) / 2.0
    mel_points = np.linspace(
        _hz_to_mel(np.asarray([low_freq_hz], dtype=np.float32))[0],
        _hz_to_mel(np.asarray([high], dtype=np.float32))[0],
        num_mels + 2,
        dtype=np.float32,
    )
    hz_points = _mel_to_hz(mel_points)
    bins = np.floor((n_fft + 1) * hz_points / float(sample_rate)).astype(np.int32)
    filters = np.zeros((num_mels, n_fft // 2 + 1), dtype=np.float32)
    for index in range(num_mels):
        left = int(max(0, bins[index]))
        center = int(max(left + 1, bins[index + 1]))
        right = int(max(center + 1, bins[index + 2]))
        if right <= left:
            continue
        for pos in range(left, min(center, filters.shape[1])):
            filters[index, pos] = (pos - left) / float(max(1, center - left))
        for pos in range(center, min(right, filters.shape[1])):
            filters[index, pos] = (right - pos) / float(max(1, right - center))
    return filters


def _extract_log_mel_fbank(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    samples = _resample_audio(audio, sample_rate, SPEAKER_ID_TARGET_SAMPLE_RATE)
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    if samples.size <= 0:
        return np.zeros((0, SPEAKER_ID_FBANK_BINS), dtype=np.float32)
    peak = float(np.max(np.abs(samples))) if samples.size > 0 else 0.0
    if peak > 1.0:
        samples = samples / peak
    if samples.size < SPEAKER_ID_FRAME_LENGTH:
        pad = SPEAKER_ID_FRAME_LENGTH - samples.size
        samples = np.pad(samples, (0, pad), mode="constant")
    samples = np.append(samples[0], samples[1:] - (0.97 * samples[:-1]))
    frame_count = 1 + max(0, (samples.size - SPEAKER_ID_FRAME_LENGTH) // SPEAKER_ID_FRAME_SHIFT)
    total_length = ((frame_count - 1) * SPEAKER_ID_FRAME_SHIFT) + SPEAKER_ID_FRAME_LENGTH
    if samples.size < total_length:
        samples = np.pad(samples, (0, total_length - samples.size), mode="constant")
    strides = (
        samples.strides[0] * SPEAKER_ID_FRAME_SHIFT,
        samples.strides[0],
    )
    frames = np.lib.stride_tricks.as_strided(
        samples,
        shape=(frame_count, SPEAKER_ID_FRAME_LENGTH),
        strides=strides,
        writeable=False,
    ).copy()
    window = np.hamming(SPEAKER_ID_FRAME_LENGTH).astype(np.float32)
    frames *= window[None, :]
    spectrum = np.fft.rfft(frames, n=SPEAKER_ID_NFFT, axis=1)
    power = np.abs(spectrum).astype(np.float32) ** 2
    filters = _build_mel_filterbank(
        sample_rate=SPEAKER_ID_TARGET_SAMPLE_RATE,
        n_fft=SPEAKER_ID_NFFT,
        num_mels=SPEAKER_ID_FBANK_BINS,
    )
    mel = np.matmul(power, filters.T)
    mel = np.log(np.maximum(mel, 1e-10)).astype(np.float32)
    if mel.size > 0:
        mel = mel - np.mean(mel, axis=0, keepdims=True)
    return mel


def resolve_speaker_profiles_path(
    *,
    memory_path: Optional[str] = None,
    profiles_path: Optional[str] = None,
) -> Path:
    configured = str(profiles_path or os.getenv("VOICE_SPEAKER_ID_PROFILES_PATH", "") or "").strip()
    if configured:
        return Path(os.path.expandvars(configured)).expanduser().resolve()
    memory_raw = str(memory_path or os.getenv("DIALOG_USER_MEMORY_PATH", "") or "").strip()
    memory_file = Path(os.path.expandvars(memory_raw)).expanduser() if memory_raw else DEFAULT_MEMORY_PATH
    return memory_file.resolve().with_name("speaker_profiles.json")


@dataclass
class SpeakerIdConfig:
    enabled: bool = True
    model_path: str = str(DEFAULT_MODEL_PATH)
    profiles_path: str = ""
    match_threshold: float = 0.38
    match_margin: float = 0.08
    min_match_seconds: float = 1.2
    enroll_min_seconds: float = 0.5
    enroll_max_seconds: float = 6.0
    enroll_min_clips: int = 3

    @classmethod
    def from_env(cls, *, memory_path: Optional[str] = None) -> "SpeakerIdConfig":
        resolved_profiles = resolve_speaker_profiles_path(memory_path=memory_path)
        return cls(
            enabled=_env_bool("VOICE_SPEAKER_ID_ENABLED", True),
            model_path=str(
                Path(
                    os.path.expandvars(
                        os.getenv("VOICE_SPEAKER_ID_MODEL_PATH", str(DEFAULT_MODEL_PATH))
                    )
                ).expanduser()
            ),
            profiles_path=str(resolved_profiles),
            match_threshold=_safe_float(os.getenv("VOICE_SPEAKER_ID_MATCH_THRESHOLD"), 0.38),
            match_margin=_safe_float(os.getenv("VOICE_SPEAKER_ID_MATCH_MARGIN"), 0.08),
            min_match_seconds=max(0.1, _safe_float(os.getenv("VOICE_SPEAKER_ID_MIN_MATCH_SECONDS"), 1.2)),
            enroll_min_seconds=max(0.1, _safe_float(os.getenv("VOICE_SPEAKER_ID_ENROLL_MIN_SECONDS"), 0.5)),
            enroll_max_seconds=max(0.1, _safe_float(os.getenv("VOICE_SPEAKER_ID_ENROLL_MAX_SECONDS"), 6.0)),
            enroll_min_clips=max(1, _safe_int(os.getenv("VOICE_SPEAKER_ID_ENROLL_MIN_CLIPS"), 3)),
        )


@dataclass
class SpeakerMatchResult:
    user_id: str = ""
    matched: bool = False
    score: float = 0.0
    margin: float = 0.0
    top1_user_id: str = ""
    top2_user_id: str = ""
    top1_score: float = 0.0
    top2_score: float = 0.0
    candidate_count: int = 0
    duration_seconds: float = 0.0
    reason: str = ""

    def to_payload(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "matched": bool(self.matched),
            "score": round(float(self.score), 4),
            "margin": round(float(self.margin), 4),
            "top1_user_id": str(self.top1_user_id or ""),
            "top2_user_id": str(self.top2_user_id or ""),
            "top1_score": round(float(self.top1_score), 4),
            "top2_score": round(float(self.top2_score), 4),
            "candidate_count": int(self.candidate_count),
            "duration_seconds": round(float(self.duration_seconds), 4),
            "reason": str(self.reason or ""),
        }


class SpeakerIdService:
    def __init__(self, config: Optional[SpeakerIdConfig] = None) -> None:
        self._lock = threading.RLock()
        self._config = config or SpeakerIdConfig.from_env()
        self._profiles: Dict[str, Dict[str, Any]] = {}
        self._pending: Dict[str, List[Dict[str, Any]]] = {}
        self._session: Any = None
        self._input_name = ""
        self._output_dim = 0
        self._mel_cache: Dict[Tuple[int, int, int], np.ndarray] = {}
        self.error = ""
        self._load_profiles()
        self.reload_model()

    @property
    def config(self) -> SpeakerIdConfig:
        return self._config

    @property
    def enabled(self) -> bool:
        return bool(self._config.enabled)

    @property
    def ready(self) -> bool:
        return self.enabled and self._session is not None and not self.error

    def reload_from_env(self, *, memory_path: Optional[str] = None) -> None:
        with self._lock:
            self._config = SpeakerIdConfig.from_env(memory_path=memory_path)
            self._load_profiles()
            self.reload_model()

    def reload_model(self) -> None:
        with self._lock:
            self._session = None
            self._input_name = ""
            self._output_dim = 0
            self.error = ""
            if not self._config.enabled:
                return
            if ort is None:
                self.error = "onnxruntime is not installed"
                return
            model_path = Path(self._config.model_path).expanduser()
            if not model_path.exists():
                self.error = f"speaker model not found: {model_path}"
                return
            try:
                session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
                inputs = session.get_inputs()
                outputs = session.get_outputs()
                if not inputs or not outputs:
                    raise RuntimeError("speaker model has no inputs or outputs")
                self._session = session
                self._input_name = str(inputs[0].name)
                output_shape = list(outputs[0].shape or [])
                if output_shape:
                    try:
                        self._output_dim = int(output_shape[-1] or 0)
                    except Exception:
                        self._output_dim = 0
            except Exception as exc:
                self.error = str(exc)
                self._session = None
                self._input_name = ""
                self._output_dim = 0

    def _default_db(self) -> Dict[str, Any]:
        return {"version": 1, "users": {}}

    def _load_profiles(self) -> None:
        path = Path(self._config.profiles_path).expanduser()
        self._profiles = {}
        if not path.exists():
            return
        try:
            raw = path.read_text(encoding="utf-8-sig")
            node = json.loads(raw)
            users = node.get("users") if isinstance(node, dict) else {}
            if not isinstance(users, dict):
                return
            for user_id, payload in users.items():
                normalized_id = str(user_id or "").strip()
                if not normalized_id or not isinstance(payload, dict):
                    continue
                centroid_raw = payload.get("centroid")
                centroid = _normalize_embedding(np.asarray(centroid_raw or [], dtype=np.float32))
                if centroid.size <= 0:
                    continue
                self._profiles[normalized_id] = {
                    "centroid": centroid,
                    "clip_count": max(0, _safe_int(payload.get("clip_count"), 0)),
                    "created_ts": _safe_float(payload.get("created_ts"), time.time()),
                    "updated_ts": _safe_float(payload.get("updated_ts"), time.time()),
                    "embedding_dim": int(centroid.size),
                }
        except Exception as exc:
            self.error = f"profile load failed: {exc}"

    def _save_profiles(self) -> None:
        path = Path(self._config.profiles_path).expanduser()
        payload = self._default_db()
        for user_id, profile in sorted(self._profiles.items()):
            centroid_raw = profile.get("centroid")
            centroid = np.asarray(centroid_raw if centroid_raw is not None else [], dtype=np.float32).reshape(-1)
            if centroid.size <= 0:
                continue
            payload["users"][user_id] = {
                "centroid": [round(float(value), 8) for value in centroid.tolist()],
                "clip_count": int(profile.get("clip_count") or 0),
                "created_ts": float(profile.get("created_ts") or time.time()),
                "updated_ts": float(profile.get("updated_ts") or time.time()),
                "embedding_dim": int(centroid.size),
            }
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(f"{path.suffix}.tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(path)

    def _profile_centroid(self, user_id: str) -> np.ndarray:
        profile = self._profiles.get(str(user_id or "").strip()) or {}
        centroid_raw = profile.get("centroid")
        centroid = np.asarray(centroid_raw if centroid_raw is not None else [], dtype=np.float32).reshape(-1)
        return centroid

    def _embedding_for_audio(self, audio: np.ndarray, sample_rate: int) -> Tuple[np.ndarray, float]:
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if samples.size <= 0:
            return np.zeros(0, dtype=np.float32), 0.0
        duration_seconds = samples.size / float(max(1, sample_rate))
        if self._session is None or not self._input_name:
            return np.zeros(0, dtype=np.float32), duration_seconds
        features = _extract_log_mel_fbank(samples, sample_rate)
        if features.shape[0] <= 0:
            return np.zeros(0, dtype=np.float32), duration_seconds
        output = self._session.run(None, {self._input_name: features[np.newaxis, :, :].astype(np.float32, copy=False)})
        if not output:
            return np.zeros(0, dtype=np.float32), duration_seconds
        embedding = _normalize_embedding(np.asarray(output[0], dtype=np.float32).reshape(-1))
        return embedding, duration_seconds

    def match_audio(self, audio: np.ndarray, sample_rate: int) -> SpeakerMatchResult:
        with self._lock:
            if not self.enabled:
                return SpeakerMatchResult(reason="disabled")
            if not self.ready:
                return SpeakerMatchResult(reason=self.error or "not_ready")
            embedding, duration_seconds = self._embedding_for_audio(audio, sample_rate)
            if duration_seconds < self._config.min_match_seconds:
                return SpeakerMatchResult(duration_seconds=duration_seconds, reason="too_short")
            if embedding.size <= 0:
                return SpeakerMatchResult(duration_seconds=duration_seconds, reason="embedding_failed")
            scored: List[Tuple[str, float]] = []
            for user_id, profile in self._profiles.items():
                centroid_raw = profile.get("centroid")
                centroid = np.asarray(centroid_raw if centroid_raw is not None else [], dtype=np.float32).reshape(-1)
                if centroid.size != embedding.size or centroid.size <= 0:
                    continue
                scored.append((user_id, _cosine_similarity(embedding, centroid)))
            if not scored:
                return SpeakerMatchResult(duration_seconds=duration_seconds, reason="no_profiles")
            scored.sort(key=lambda item: item[1], reverse=True)
            user_id, top1 = scored[0]
            top2_user_id = str(scored[1][0]) if len(scored) > 1 else ""
            top2 = float(scored[1][1]) if len(scored) > 1 else 0.0
            margin = float(top1 - top2)
            score_passed = top1 >= self._config.match_threshold
            margin_passed = margin >= self._config.match_margin
            matched = score_passed and margin_passed
            if matched:
                reason = "matched"
            elif not score_passed:
                reason = "below_score_threshold"
            else:
                reason = "below_margin_threshold"
            return SpeakerMatchResult(
                user_id=user_id if matched else "",
                matched=matched,
                score=float(top1),
                margin=margin,
                top1_user_id=str(user_id or ""),
                top2_user_id=top2_user_id,
                top1_score=float(top1),
                top2_score=float(top2),
                candidate_count=len(scored),
                duration_seconds=duration_seconds,
                reason=reason,
            )

    def add_pending_clip(self, user_id: str, audio: np.ndarray, sample_rate: int) -> Dict[str, Any]:
        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            raise ValueError("user_id is required")
        with self._lock:
            if not self.enabled:
                raise RuntimeError("speaker id is disabled")
            if not self.ready:
                raise RuntimeError(self.error or "speaker id is not ready")
            embedding, duration_seconds = self._embedding_for_audio(audio, sample_rate)
            if duration_seconds < self._config.enroll_min_seconds:
                raise ValueError("enrollment clip is too short")
            if duration_seconds > self._config.enroll_max_seconds:
                raise ValueError("enrollment clip is too long")
            if embedding.size <= 0:
                raise RuntimeError("failed to compute speaker embedding")
            clips = self._pending.setdefault(normalized_user_id, [])
            clips.append(
                {
                    "embedding": embedding,
                    "captured_ts": float(time.time()),
                    "duration_seconds": float(duration_seconds),
                }
            )
            return self.pending_summary(normalized_user_id)

    def pending_summary(self, user_id: str) -> Dict[str, Any]:
        normalized_user_id = str(user_id or "").strip()
        clips = list(self._pending.get(normalized_user_id) or [])
        last_ts = 0.0
        total_seconds = 0.0
        for clip in clips:
            last_ts = max(last_ts, _safe_float(clip.get("captured_ts"), 0.0))
            total_seconds += max(0.0, _safe_float(clip.get("duration_seconds"), 0.0))
        return {
            "user_id": normalized_user_id,
            "pending_clip_count": len(clips),
            "pending_total_seconds": round(total_seconds, 4),
            "last_captured_ts": float(last_ts),
            "required_clip_count": int(self._config.enroll_min_clips),
            "can_commit": len(clips) >= self._config.enroll_min_clips,
        }

    def commit_pending_clips(self, user_id: str) -> Dict[str, Any]:
        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            raise ValueError("user_id is required")
        with self._lock:
            clips = list(self._pending.get(normalized_user_id) or [])
            if len(clips) < self._config.enroll_min_clips:
                raise ValueError("not enough enrollment clips")
            embeddings = []
            for item in clips:
                raw_embedding = item.get("embedding")
                embeddings.append(
                    _normalize_embedding(
                        np.asarray(raw_embedding if raw_embedding is not None else [], dtype=np.float32)
                    )
                )
            valid_embeddings = [item for item in embeddings if item.size > 0]
            if len(valid_embeddings) < self._config.enroll_min_clips:
                raise RuntimeError("not enough valid enrollment embeddings")
            stacked = np.stack(valid_embeddings, axis=0)
            centroid = _normalize_embedding(np.mean(stacked, axis=0))
            now_ts = float(time.time())
            existing = self._profiles.get(normalized_user_id) or {}
            created_ts = _safe_float(existing.get("created_ts"), now_ts)
            self._profiles[normalized_user_id] = {
                "centroid": centroid,
                "clip_count": len(valid_embeddings),
                "created_ts": created_ts,
                "updated_ts": now_ts,
                "embedding_dim": int(centroid.size),
            }
            self._pending.pop(normalized_user_id, None)
            self._save_profiles()
            return self.user_summary(normalized_user_id)

    def commit_pending_clips_as(self, source_user_id: str, target_user_id: str) -> Dict[str, Any]:
        normalized_source_id = str(source_user_id or "").strip()
        normalized_target_id = str(target_user_id or "").strip()
        if not normalized_source_id:
            raise ValueError("source_user_id is required")
        if not normalized_target_id:
            raise ValueError("target_user_id is required")
        with self._lock:
            clips = list(self._pending.get(normalized_source_id) or [])
            if len(clips) < self._config.enroll_min_clips:
                raise ValueError("not enough enrollment clips")
            embeddings = []
            for item in clips:
                raw_embedding = item.get("embedding")
                embeddings.append(
                    _normalize_embedding(
                        np.asarray(raw_embedding if raw_embedding is not None else [], dtype=np.float32)
                    )
                )
            valid_embeddings = [item for item in embeddings if item.size > 0]
            if len(valid_embeddings) < self._config.enroll_min_clips:
                raise RuntimeError("not enough valid enrollment embeddings")

            existing = self._profiles.get(normalized_target_id) or {}
            existing_centroid_raw = existing.get("centroid")
            existing_centroid = _normalize_embedding(
                np.asarray(existing_centroid_raw if existing_centroid_raw is not None else [], dtype=np.float32)
            )
            existing_clip_count = max(0, int(existing.get("clip_count") or 0))
            weighted_embeddings = list(valid_embeddings)
            if existing_centroid.size > 0 and existing_clip_count > 0:
                weighted_embeddings.append(existing_centroid * float(existing_clip_count))
            centroid = _normalize_embedding(np.sum(np.stack(weighted_embeddings, axis=0), axis=0))
            now_ts = float(time.time())
            self._profiles[normalized_target_id] = {
                "centroid": centroid,
                "clip_count": existing_clip_count + len(valid_embeddings),
                "created_ts": _safe_float(existing.get("created_ts"), now_ts),
                "updated_ts": now_ts,
                "embedding_dim": int(centroid.size),
            }
            self._pending.pop(normalized_source_id, None)
            self._save_profiles()
            return self.user_summary(normalized_target_id)

    def clear_pending(self, user_id: str) -> Dict[str, Any]:
        normalized_user_id = str(user_id or "").strip()
        with self._lock:
            self._pending.pop(normalized_user_id, None)
            return self.pending_summary(normalized_user_id)

    def clear_profile(self, user_id: str) -> Dict[str, Any]:
        normalized_user_id = str(user_id or "").strip()
        with self._lock:
            self._profiles.pop(normalized_user_id, None)
            self._pending.pop(normalized_user_id, None)
            self._save_profiles()
            return self.user_summary(normalized_user_id)

    def user_summary(self, user_id: str) -> Dict[str, Any]:
        normalized_user_id = str(user_id or "").strip()
        with self._lock:
            profile = self._profiles.get(normalized_user_id) or {}
            pending = self.pending_summary(normalized_user_id)
            centroid_raw = profile.get("centroid")
            has_profile = bool(
                centroid_raw is not None and np.asarray(centroid_raw, dtype=np.float32).reshape(-1).size > 0
            )
            return {
                "user_id": normalized_user_id,
                "has_profile": has_profile,
                "clip_count": int(profile.get("clip_count") or 0),
                "updated_ts": float(profile.get("updated_ts") or 0.0),
                "created_ts": float(profile.get("created_ts") or 0.0),
                "embedding_dim": int(profile.get("embedding_dim") or 0),
                "pending": pending,
            }

    def list_user_summaries(self) -> List[Dict[str, Any]]:
        with self._lock:
            user_ids = sorted(set(self._profiles.keys()) | set(self._pending.keys()), key=str.lower)
            return [self.user_summary(user_id) for user_id in user_ids]

    def pending_state(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "users": [self.pending_summary(user_id) for user_id in sorted(self._pending.keys(), key=str.lower)],
                "pending_user_count": len(self._pending),
            }

    def status_payload(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "enabled": bool(self.enabled),
                "ready": bool(self.ready),
                "error": str(self.error or ""),
                "model_path": str(Path(self._config.model_path).expanduser()),
                "profiles_path": str(Path(self._config.profiles_path).expanduser()),
                "profile_count": len(self._profiles),
                "match_threshold": float(self._config.match_threshold),
                "match_margin": float(self._config.match_margin),
                "min_match_seconds": float(self._config.min_match_seconds),
                "enroll_min_seconds": float(self._config.enroll_min_seconds),
                "enroll_max_seconds": float(self._config.enroll_max_seconds),
                "enroll_min_clips": int(self._config.enroll_min_clips),
                "users": self.list_user_summaries(),
                "pending": self.pending_state(),
            }
