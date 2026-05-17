"""Grounding helper: verify excerpts against text_units.jsonl.

Reads only — never writes. Case-sensitive substring match. No fuzzy logic.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .source_loader import SOURCE_FAMILIES


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _find_text_units_path(repo_root: Path, source_id: str) -> Path | None:
    for family in SOURCE_FAMILIES:
        candidate = (
            repo_root / "processed" / family / source_id / "text_units.jsonl"
        )
        if candidate.is_file():
            return candidate
    return None


def _read_text_units(path: Path) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    units.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return units


class GroundingHelper:
    """Verify that excerpts appear verbatim in a source's text units."""

    def verify_excerpt(
        self, excerpt: str, source_id: str, repo_root: str
    ) -> dict[str, Any]:
        normalized = (excerpt or "").strip()
        excerpt_hash = "sha256:" + _sha256_hex(normalized.encode("utf-8"))

        path = _find_text_units_path(Path(repo_root).resolve(), source_id)
        if path is None:
            return {
                "grounded": False,
                "matching_unit_ids": [],
                "excerpt_hash": excerpt_hash,
            }
        if not normalized:
            return {
                "grounded": False,
                "matching_unit_ids": [],
                "excerpt_hash": excerpt_hash,
            }

        matching: list[str] = []
        for unit in _read_text_units(path):
            text = unit.get("text", "")
            if isinstance(text, str) and normalized in text:
                unit_id = unit.get("unit_id")
                if isinstance(unit_id, str):
                    matching.append(unit_id)
        return {
            "grounded": bool(matching),
            "matching_unit_ids": matching,
            "excerpt_hash": excerpt_hash,
        }

    def find_units_by_text(
        self, query: str, source_id: str, repo_root: str
    ) -> list[dict[str, Any]]:
        if not query:
            return []
        path = _find_text_units_path(Path(repo_root).resolve(), source_id)
        if path is None:
            return []
        hits: list[dict[str, Any]] = []
        for unit in _read_text_units(path):
            text = unit.get("text", "")
            if isinstance(text, str) and query in text:
                hits.append(unit)
        return hits
