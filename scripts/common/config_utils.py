from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import yaml


def load_yaml_file(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def resolve_optional_path(raw_path: Optional[str], *, base_dir: Optional[Path] = None) -> Optional[str]:
    if not raw_path:
        return None
    candidate = Path(str(raw_path)).expanduser()
    if base_dir is not None and not candidate.is_absolute():
        candidate = base_dir / candidate
    return str(candidate)
