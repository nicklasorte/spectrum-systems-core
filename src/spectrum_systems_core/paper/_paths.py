"""Locate paper schemas and processed/<family>/<source_id>/paper/ directory."""
from __future__ import annotations

import hashlib
from pathlib import Path

from ..ingestion._paths import contracts_root
from ..ingestion.source_loader import SOURCE_FAMILIES


def paper_schema_path(schema_name: str) -> Path:
    return contracts_root() / "schemas" / "paper" / f"{schema_name}.schema.json"


def paper_schema_digest(schema_name: str) -> str:
    data = paper_schema_path(schema_name).read_bytes()
    return "sha256:" + hashlib.sha256(data).hexdigest()


def find_paper_dir(
    repo_root: Path, source_id: str, *, create: bool = False
) -> tuple[Path | None, str | None]:
    """Locate processed/<family>/<source_id>/paper/. Returns (path, family)."""
    for family in SOURCE_FAMILIES:
        candidate = repo_root / "processed" / family / source_id
        if candidate.is_dir():
            paper_dir = candidate / "paper"
            if create:
                paper_dir.mkdir(parents=True, exist_ok=True)
            return paper_dir, family
    return None, None
