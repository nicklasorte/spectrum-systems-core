"""Locate contracts/ regardless of install layout."""
from __future__ import annotations

import hashlib
from pathlib import Path


def contracts_root() -> Path:
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        candidate = parent / "contracts"
        if candidate.is_dir() and (candidate / "schemas").is_dir():
            return candidate
    raise FileNotFoundError(
        "contracts/ directory not found above " + str(here)
    )


def schema_path(schema_name: str) -> Path:
    return contracts_root() / "schemas" / f"{schema_name}.schema.json"


def schema_digest(schema_name: str) -> str:
    data = schema_path(schema_name).read_bytes()
    return "sha256:" + hashlib.sha256(data).hexdigest()
