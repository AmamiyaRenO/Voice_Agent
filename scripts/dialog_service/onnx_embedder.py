#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import numpy as np
except Exception:
    np = None  # type: ignore[assignment]
try:
    import onnxruntime as ort
except Exception:
    ort = None  # type: ignore[assignment]
try:
    from tokenizers import Tokenizer
except Exception:
    Tokenizer = None  # type: ignore[assignment]
try:
    from huggingface_hub import snapshot_download
except Exception:
    snapshot_download = None  # type: ignore[assignment]

try:
    from .dialog_config import _default_doc_prefix, _default_embedder_repo, _default_query_prefix
except Exception:
    from dialog_config import _default_doc_prefix, _default_embedder_repo, _default_query_prefix


def _safe_vector_norm(vec: "np.ndarray") -> float:
    if np is None:
        return 0.0
    try:
        return float(np.linalg.norm(vec))
    except Exception:
        return 0.0


def cosine_similarity(a: "np.ndarray", b: "np.ndarray") -> float:
    if np is None:
        return 0.0
    if a.shape != b.shape:
        return 0.0
    norm_a = _safe_vector_norm(a)
    norm_b = _safe_vector_norm(b)
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


class OnnxTextEmbedder:
    def __init__(
        self,
        *,
        embedder: str,
        repo_id: str,
        model_dir: str,
        model_file: str,
        tokenizer_file: str,
        max_length: int,
        auto_download: bool,
        cache_dir: str,
        query_prefix: str,
        doc_prefix: str,
    ) -> None:
        self.embedder = (embedder or "minilm").strip().lower() or "minilm"
        self.repo_id = (repo_id or _default_embedder_repo(self.embedder)).strip()
        self.model_dir = (model_dir or "").strip()
        self.model_file = (model_file or "").strip()
        self.tokenizer_file = (tokenizer_file or "").strip()
        self.max_length = max(16, int(max_length))
        self.auto_download = bool(auto_download)
        self.cache_dir = (cache_dir or "").strip()
        self.query_prefix = query_prefix or _default_query_prefix(self.embedder)
        self.doc_prefix = doc_prefix or _default_doc_prefix(self.embedder)
        self.ready = False
        self.error = ""
        self._session = None
        self._tokenizer = None
        self._input_names: List[str] = []
        self._pad_token_id = 0
        self.dimension = 0
        self._initialize()

    @staticmethod
    def _deps_ready() -> bool:
        return np is not None and ort is not None and Tokenizer is not None

    def _initialize(self) -> None:
        if not self._deps_ready():
            self.error = (
                "ONNX memory dependencies unavailable. "
                "Install numpy, onnxruntime, tokenizers, huggingface-hub."
            )
            return

        root = self._resolve_model_root()
        if root is None:
            return

        model_path = self._resolve_model_path(root)
        tokenizer_path = self._resolve_tokenizer_path(root)
        if model_path is None or tokenizer_path is None:
            return

        try:
            self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
            pad_id = self._tokenizer.token_to_id("[PAD]")
            if pad_id is not None:
                self._pad_token_id = int(pad_id)
        except Exception as exc:
            self.error = f"Failed to load tokenizer: {exc}"
            return

        try:
            self._session = ort.InferenceSession(  # type: ignore[union-attr]
                str(model_path),
                providers=["CPUExecutionProvider"],
            )
            self._input_names = [item.name for item in self._session.get_inputs()]
        except Exception as exc:
            self.error = f"Failed to load ONNX model: {exc}"
            return

        # Best-effort embedding dimension probe.
        probe = self.encode("probe", is_query=True)
        if probe is not None:
            self.dimension = int(probe.shape[0])
            self.ready = True
            self.error = ""
            return

        self.error = self.error or "Failed to initialize embedding probe."

    def _resolve_model_root(self) -> Optional[Path]:
        if self.model_dir:
            path = Path(self.model_dir).expanduser()
            if path.exists():
                return path
            self.error = f"Embedding model dir not found: {path}"
            return None

        if snapshot_download is None:
            self.error = "huggingface_hub not available and no model dir configured."
            return None

        try:
            kwargs: Dict[str, Any] = {
                "repo_id": self.repo_id,
            }
            if self.cache_dir:
                kwargs["cache_dir"] = self.cache_dir
            kwargs["local_files_only"] = not self.auto_download
            downloaded = snapshot_download(**kwargs)
            return Path(downloaded)
        except Exception as exc:
            self.error = f"Failed to resolve embedding model '{self.repo_id}': {exc}"
            return None

    def _resolve_model_path(self, root: Path) -> Optional[Path]:
        if self.model_file:
            path = Path(self.model_file)
            if not path.is_absolute():
                path = root / path
            if path.exists():
                return path
            self.error = f"Embedding model file not found: {path}"
            return None

        candidates = [
            root / "model.onnx",
            root / "onnx" / "model.onnx",
        ]
        for path in candidates:
            if path.exists():
                return path

        onnx_files = sorted(root.rglob("*.onnx"))
        if onnx_files:
            return onnx_files[0]

        self.error = f"No ONNX model file found under: {root}"
        return None

    def _resolve_tokenizer_path(self, root: Path) -> Optional[Path]:
        if self.tokenizer_file:
            path = Path(self.tokenizer_file)
            if not path.is_absolute():
                path = root / path
            if path.exists():
                return path
            self.error = f"Embedding tokenizer file not found: {path}"
            return None

        candidates = [
            root / "tokenizer.json",
            root / "onnx" / "tokenizer.json",
        ]
        for path in candidates:
            if path.exists():
                return path

        tokenizers = sorted(root.rglob("tokenizer.json"))
        if tokenizers:
            return tokenizers[0]

        self.error = f"No tokenizer.json found under: {root}"
        return None

    def _build_inputs(self, text: str) -> Dict[str, "np.ndarray"]:
        if np is None or self._tokenizer is None:
            return {}

        encoded = self._tokenizer.encode(text)
        token_ids = list(encoded.ids[: self.max_length])
        attention = [1] * len(token_ids)

        if len(token_ids) < self.max_length:
            pad_size = self.max_length - len(token_ids)
            token_ids.extend([self._pad_token_id] * pad_size)
            attention.extend([0] * pad_size)

        input_ids = np.asarray([token_ids], dtype=np.int64)
        attention_mask = np.asarray([attention], dtype=np.int64)
        token_type_ids = np.zeros_like(input_ids, dtype=np.int64)

        feed: Dict[str, "np.ndarray"] = {}
        for name in self._input_names:
            lowered = name.lower()
            if "input" in lowered and "id" in lowered:
                feed[name] = input_ids
            elif "attention" in lowered:
                feed[name] = attention_mask
            elif "token_type" in lowered:
                feed[name] = token_type_ids

        if not feed and self._input_names:
            feed[self._input_names[0]] = input_ids
            if len(self._input_names) >= 2:
                feed[self._input_names[1]] = attention_mask
            if len(self._input_names) >= 3:
                feed[self._input_names[2]] = token_type_ids
        return feed

    @staticmethod
    def _normalize_memory_value(text: str, *, max_len: int = 256) -> str:
        compact = " ".join((text or "").strip().split())
        compact = compact.strip(" \t\r\n.,!?;:()[]{}\"'")
        if not compact:
            return ""
        if len(compact) > max_len:
            compact = compact[:max_len].rstrip()
        return compact

    def encode(self, text: str, *, is_query: bool) -> Optional["np.ndarray"]:
        if not self.ready and self._session is None:
            return None
        if np is None or self._session is None:
            return None

        normalized = self._normalize_memory_value(text, max_len=256)
        if not normalized:
            return None

        prefix = self.query_prefix if is_query else self.doc_prefix
        model_text = f"{prefix}{normalized}" if prefix else normalized
        feed = self._build_inputs(model_text)
        if not feed:
            return None

        try:
            outputs = self._session.run(None, feed)
        except Exception as exc:
            self.error = f"Embedding inference failed: {exc}"
            return None

        token_embeddings: Optional["np.ndarray"] = None
        sentence_embeddings: Optional["np.ndarray"] = None
        for value in outputs:
            if not hasattr(value, "ndim"):
                continue
            if value.ndim == 3 and token_embeddings is None:
                token_embeddings = value
            elif value.ndim == 2 and sentence_embeddings is None:
                sentence_embeddings = value

        vector: Optional["np.ndarray"] = None
        if token_embeddings is not None:
            attention = None
            for name, data in feed.items():
                if "attention" in name.lower():
                    attention = data
                    break
            if attention is None:
                attention = np.ones((1, token_embeddings.shape[1]), dtype=np.int64)
            mask = attention.astype(np.float32)[..., None]
            summed = (token_embeddings * mask).sum(axis=1)
            denom = np.maximum(mask.sum(axis=1), 1e-6)
            vector = summed / denom
            if vector.ndim == 2:
                vector = vector[0]
        elif sentence_embeddings is not None:
            vector = sentence_embeddings[0]

        if vector is None:
            return None

        vector = np.asarray(vector, dtype=np.float32).reshape(-1)
        norm = _safe_vector_norm(vector)
        if norm > 0.0:
            vector = vector / norm
        return vector

    def query_embedding(self, text: str) -> Optional["np.ndarray"]:
        return self.encode(text, is_query=True)

    def doc_embedding(self, text: str) -> Optional["np.ndarray"]:
        return self.encode(text, is_query=False)
