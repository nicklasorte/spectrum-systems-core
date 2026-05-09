"""Locate synthesis schemas and per-run directories."""
from __future__ import annotations

import hashlib
from pathlib import Path

from ..ingestion._paths import contracts_root


def synthesis_schema_path(schema_name: str) -> Path:
    return contracts_root() / "schemas" / "synthesis" / f"{schema_name}.schema.json"


def synthesis_schema_digest(schema_name: str) -> str:
    data = synthesis_schema_path(schema_name).read_bytes()
    return "sha256:" + hashlib.sha256(data).hexdigest()


def synthesis_run_dir(repo_root: Path, run_id: str, *, create: bool = False) -> Path:
    target = repo_root / "synthesis" / run_id
    if create:
        target.mkdir(parents=True, exist_ok=True)
        (target / "markdown").mkdir(parents=True, exist_ok=True)
    return target
