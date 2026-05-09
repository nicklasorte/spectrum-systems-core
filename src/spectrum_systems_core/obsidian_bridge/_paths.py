"""Locate the contracts/ directory regardless of install layout."""
from __future__ import annotations

import os
from pathlib import Path


def find_contracts_root() -> Path:
    """Walk upward from this file to find the contracts/ directory."""
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        candidate = parent / "contracts"
        if candidate.is_dir() and (candidate / "schemas").is_dir():
            return candidate
    raise FileNotFoundError("contracts/ directory not found above " + str(here))


def schema_path(schema_name: str) -> Path:
    """Return absolute path to a schema file under contracts/schemas/."""
    return find_contracts_root() / "schemas" / f"{schema_name}.schema.json"


def schema_digest(schema_name: str) -> str:
    """Return sha256-prefixed digest of a schema file's bytes."""
    import hashlib
    data = schema_path(schema_name).read_bytes()
    return "sha256:" + hashlib.sha256(data).hexdigest()
