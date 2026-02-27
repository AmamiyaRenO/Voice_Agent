#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

try:
    from .match_utils import (
        candidate_variants,
        clean_game_candidate,
        normalize_match_text,
        similarity_score,
    )
except Exception:
    from match_utils import (
        candidate_variants,
        clean_game_candidate,
        normalize_match_text,
        similarity_score,
    )


class ManifestAliasResolver:
    def __init__(self, manifest_path: Optional[str]) -> None:
        self.manifest_path = manifest_path
        self.alias_to_name: Dict[str, str] = {}
        self.normalized_alias_to_name: Dict[str, str] = {}
        self.game_to_aliases: Dict[str, List[str]] = {}
        self._load()

    def _load(self) -> None:
        path = self.manifest_path
        if not path:
            return
        try:
            if not os.path.exists(path):
                print(f"[intent] manifest not found: {path}")
                return
            with open(path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
            for game in data.get("games", []) or []:
                name = str(game.get("name") or game.get("id") or "").strip()
                if not name:
                    continue
                raw_aliases = [name]
                raw_id = str(game.get("id") or "").strip()
                if raw_id:
                    raw_aliases.append(raw_id)
                for alias in game.get("synonyms", []) or []:
                    text = str(alias).strip()
                    if text:
                        raw_aliases.append(text)

                for alias in raw_aliases:
                    key = alias.lower().strip()
                    if key:
                        self.alias_to_name[key] = name
                    normalized_key = normalize_match_text(alias)
                    if normalized_key:
                        self.normalized_alias_to_name[normalized_key] = name
                self.game_to_aliases[name] = sorted({a.strip() for a in raw_aliases if str(a).strip()})
            print(f"[intent] loaded {len(self.alias_to_name)} aliases from manifest")
        except Exception as exc:
            print(f"[intent] failed to load manifest aliases: {exc}")

    def prompt_catalog(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for game, aliases in self.game_to_aliases.items():
            rows.append({"name": game, "aliases": aliases})
        return rows

    def canonical_name(self, candidate: str) -> Optional[str]:
        cleaned = clean_game_candidate(candidate)
        if not cleaned:
            return None
        return self.alias_to_name.get(cleaned) or self.normalized_alias_to_name.get(cleaned)

    def resolve_best_name(self, candidate: str, fuzzy_threshold: int) -> Optional[str]:
        candidates = candidate_variants(candidate)
        if not candidates or not self.normalized_alias_to_name:
            return None

        # 1) Exact normalized match
        for cleaned in candidates:
            exact = self.normalized_alias_to_name.get(cleaned)
            if exact:
                return exact

        # 2) Similarity scoring (lexical + phonetic + token overlap)
        best_name = None
        best_score = 0.0
        for cleaned in candidates:
            for alias, game_name in self.normalized_alias_to_name.items():
                score = similarity_score(cleaned, alias)
                if score > best_score:
                    best_score = score
                    best_name = game_name

        threshold = max(50, min(100, int(fuzzy_threshold)))
        if best_name is not None and best_score >= float(threshold):
            return best_name

        return None
