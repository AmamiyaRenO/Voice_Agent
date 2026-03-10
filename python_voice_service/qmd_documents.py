from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

try:
    from .game_grounding import normalize_manifest_payload
except Exception:
    from game_grounding import normalize_manifest_payload

QMD_BEGIN = "<!-- QMD-DATA-BEGIN -->"
QMD_END = "<!-- QMD-DATA-END -->"


def _slug(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in str(value or "").strip())
    while "--" in text:
        text = text.replace("--", "-")
    return text.strip("-") or "item"


def _front_matter(meta: Dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in meta.items():
        if isinstance(value, list):
            text = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, bool):
            text = "true" if value else "false"
        else:
            text = str(value)
        lines.append(f"{key}: {text}")
    lines.append("---")
    return "\n".join(lines)


def _extract_embedded_json(text: str) -> Dict[str, Any]:
    start = text.find(QMD_BEGIN)
    end = text.find(QMD_END)
    if start < 0 or end < 0 or end <= start:
        return {}
    raw = text[start + len(QMD_BEGIN) : end].strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _write_doc(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def export_memory_qmd(memory_root: Dict[str, Any], out_dir: Path) -> Dict[str, Any]:
    profiles = memory_root.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
    exported = 0
    users_dir = out_dir / "users"
    users_dir.mkdir(parents=True, exist_ok=True)
    for user_id, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        facts = [item for item in profile.get("facts", []) or [] if isinstance(item, dict)]
        episodes = [item for item in profile.get("episodes", []) or [] if isinstance(item, dict)]
        display_name = str(profile.get("display_name") or user_id).strip() or str(user_id)
        fact_lines = []
        for item in facts:
            if str(item.get("status") or "active").strip().lower() != "active":
                continue
            field = str(item.get("field") or "").strip()
            value = str(item.get("value") or "").strip()
            confidence = float(item.get("confidence") or 0.0)
            if field and value:
                fact_lines.append(f"- `{field}`: {value} (confidence {confidence:.2f})")
        if not fact_lines:
            fact_lines.append("- No active structured facts yet.")
        episode_lines = []
        for item in episodes[-8:]:
            role = str(item.get("role") or "user").strip()
            text = str(item.get("text") or "").strip()
            if text:
                episode_lines.append(f"- {role}: {text}")
        if not episode_lines:
            episode_lines.append("- No recent episodes yet.")
        content = "\n".join(
            [
                _front_matter(
                    {
                        "type": "user_profile",
                        "schema": "voice-agent-qmd-v1",
                        "user_id": str(user_id),
                        "display_name": display_name,
                        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                ),
                "",
                f"# User Profile: {display_name}",
                "",
                "## Structured Facts",
                *fact_lines,
                "",
                "## Recent Episodes",
                *episode_lines,
                "",
                QMD_BEGIN,
                json.dumps({"user_id": user_id, "profile": profile}, ensure_ascii=False, indent=2),
                QMD_END,
                "",
            ]
        )
        _write_doc(users_dir / f"{_slug(str(user_id))}.qmd", content)
        exported += 1
    return {"path": str(out_dir), "users_exported": exported}


def import_memory_qmd(memory_root: Dict[str, Any], in_dir: Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    profiles = memory_root.setdefault("profiles", {})
    imported = 0
    users_dir = in_dir / "users"
    if users_dir.exists():
        for path in sorted(users_dir.glob("*.qmd")):
            payload = _extract_embedded_json(path.read_text(encoding="utf-8"))
            user_id = str(payload.get("user_id") or "").strip()
            profile = payload.get("profile")
            if not user_id or not isinstance(profile, dict):
                continue
            profiles[user_id] = profile
            imported += 1
    return memory_root, {"path": str(in_dir), "users_imported": imported}


def export_game_qmd(manifest_root: Dict[str, Any], out_dir: Path) -> Dict[str, Any]:
    normalized = normalize_manifest_payload(manifest_root)
    games_dir = out_dir / "games"
    games_dir.mkdir(parents=True, exist_ok=True)
    exported = 0
    for item in normalized.get("games", []):
        if not isinstance(item, dict):
            continue
        game_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or game_id).strip() or game_id
        description = str(item.get("description") or "").strip()
        how_to_play = str(item.get("how_to_play") or "").strip()
        tags = [str(tag).strip() for tag in item.get("tags", []) or [] if str(tag).strip()]
        content = "\n".join(
            [
                _front_matter(
                    {
                        "type": "game_card",
                        "schema": "voice-agent-qmd-v1",
                        "game_id": game_id,
                        "name": name,
                        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                ),
                "",
                f"# Game Card: {name}",
                "",
                "## Description",
                description or "No description set.",
                "",
                "## How To Play",
                how_to_play or "No how-to-play text set.",
                "",
                "## Tags",
                ("- " + "\n- ".join(tags)) if tags else "- No tags set.",
                "",
                QMD_BEGIN,
                json.dumps({"game": item}, ensure_ascii=False, indent=2),
                QMD_END,
                "",
            ]
        )
        _write_doc(games_dir / f"{_slug(game_id or name)}.qmd", content)
        exported += 1
    return {"path": str(out_dir), "games_exported": exported}


def import_game_qmd(manifest_root: Dict[str, Any], in_dir: Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    games_dir = in_dir / "games"
    games: List[Dict[str, Any]] = []
    imported = 0
    if games_dir.exists():
        for path in sorted(games_dir.glob("*.qmd")):
            payload = _extract_embedded_json(path.read_text(encoding="utf-8"))
            game = payload.get("game")
            if not isinstance(game, dict):
                continue
            games.append(game)
            imported += 1
    if games:
        manifest_root = {"games": games}
    return normalize_manifest_payload(manifest_root), {"path": str(in_dir), "games_imported": imported}
