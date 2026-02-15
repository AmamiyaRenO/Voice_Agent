from __future__ import annotations

from pathlib import Path
from typing import Dict


def apply_env_file(path: Path, env: Dict[str, str]) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Environment file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                raise ValueError(f"Invalid line in env file {path}: {stripped}")
            key, value = stripped.split("=", 1)
            env[key.strip()] = value.strip()
