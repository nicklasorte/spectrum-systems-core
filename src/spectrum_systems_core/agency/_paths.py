"""Locate agency schemas and per-agency directories."""
from __future__ import annotations

import hashlib
from pathlib import Path

from ..ingestion._paths import contracts_root


def agency_schema_path(schema_name: str) -> Path:
    return contracts_root() / "schemas" / "agency" / f"{schema_name}.schema.json"


def agency_schema_digest(schema_name: str) -> str:
    data = agency_schema_path(schema_name).read_bytes()
    return "sha256:" + hashlib.sha256(data).hexdigest()


def agency_dir(repo_root: Path, agency_slug: str, *, create: bool = False) -> Path:
    target = repo_root / "agency" / agency_slug
    if create:
        target.mkdir(parents=True, exist_ok=True)
    return target
