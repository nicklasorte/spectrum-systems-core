"""Locate processed/<family>/<source_id>/ regardless of layout."""
from __future__ import annotations

from pathlib import Path

from ..ingestion.source_loader import SOURCE_FAMILIES


def find_processed_dir(repo_root: Path, source_id: str) -> tuple[Path | None, str | None]:
    """Return (processed_dir, source_family) or (None, None) if not found."""
    for family in SOURCE_FAMILIES:
        candidate = repo_root / "processed" / family / source_id
        if candidate.is_dir():
            return candidate, family
    return None, None


def find_text_units_path(repo_root: Path, source_id: str) -> Path | None:
    processed_dir, _ = find_processed_dir(repo_root, source_id)
    if processed_dir is None:
        return None
    candidate = processed_dir / "text_units.jsonl"
    if not candidate.is_file():
        return None
    return candidate
