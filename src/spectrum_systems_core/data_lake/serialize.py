"""Canonical JSON helpers used everywhere core writes to the lake.

Every byte that goes into `processed/` or `indexes/` passes through here so
the contract's determinism rule is enforced in one place.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from typing import Any

from ..artifacts import Artifact

_SLUG_SAFE = re.compile(r"[^a-z0-9]+")


def canonical_json(obj: Any) -> str:
    """Sorted-keys, compact JSON with a trailing newline. Bytes are stable."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def artifact_to_dict(artifact: Artifact) -> dict[str, Any]:
    """Envelope as a plain dict, in a fixed key order via canonical JSON."""
    if is_dataclass(artifact):
        return asdict(artifact)
    raise TypeError(f"expected Artifact dataclass, got {type(artifact)!r}")


def slugify(value: str, *, max_len: int = 64) -> str:
    """Deterministic, filesystem-safe slug. Empty input collapses to '_'."""
    lowered = (value or "").strip().lower()
    cleaned = _SLUG_SAFE.sub("-", lowered).strip("-")
    if not cleaned:
        cleaned = "_"
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip("-") or "_"
    return cleaned
