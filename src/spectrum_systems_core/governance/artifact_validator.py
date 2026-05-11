"""Deprecation validator for the artifact_kind -> artifact_type migration.

Phase Pre-N step 1 of 2. Emits a deprecation warning when an artifact
uses ``artifact_kind`` without ``artifact_type``. Never blocks the write;
warnings are appended to an audit log so a follow-up workflow can confirm
zero warnings before step 2 (removal of artifact_kind).

Audit log location:
- ``SDL_AUDIT_LOG`` env var if set
- otherwise ``<SDL_ROOT>/audit/artifact_deprecation.log`` (created on first
  write)
- otherwise the in-memory list returned by ``validate_artifact_fields`` is
  the only record (callers may discard).

Functions in this module never raise. Failures to append to the audit log
degrade silently (a missing audit log must not block a write).
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _audit_log_path() -> Optional[Path]:
    env = os.environ.get("SDL_AUDIT_LOG", "").strip()
    if env:
        return Path(env)
    sdl_root = os.environ.get("SDL_ROOT", "").strip()
    if sdl_root:
        return Path(sdl_root) / "audit" / "artifact_deprecation.log"
    return None


def _append_audit(line: str) -> None:
    path = _audit_log_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        return


def validate_artifact_fields(
    artifact: Dict[str, Any],
    schema_path: str = "",
) -> List[str]:
    """Return deprecation warnings for an artifact.

    Rules:
    - artifact has ``artifact_kind`` but no ``artifact_type``: warn.
    - artifact has neither: error-level message (does not block; logged).
    - artifact has ``artifact_type`` (with or without ``artifact_kind``):
      no warning.

    Mismatch between the two values (when both present) is also flagged
    so a producer cannot silently introduce a typed/kind drift.
    """
    warnings: List[str] = []
    if not isinstance(artifact, dict):
        return warnings

    has_kind = "artifact_kind" in artifact
    has_type = "artifact_type" in artifact
    artifact_id = artifact.get("artifact_id", "unknown")

    if has_kind and not has_type:
        warnings.append(
            "DEPRECATION: artifact uses artifact_kind without artifact_type. "
            f"artifact_id={artifact_id}. schema_path={schema_path or 'unknown'}. "
            "Migrate to artifact_type before next major release."
        )
    elif not has_kind and not has_type:
        warnings.append(
            "ERROR: artifact has neither artifact_kind nor artifact_type. "
            f"artifact_id={artifact_id}. schema_path={schema_path or 'unknown'}."
        )
    elif has_kind and has_type:
        if artifact["artifact_kind"] != artifact["artifact_type"]:
            warnings.append(
                "ERROR: artifact_kind and artifact_type disagree. "
                f"artifact_id={artifact_id}. "
                f"artifact_kind={artifact['artifact_kind']!r}, "
                f"artifact_type={artifact['artifact_type']!r}."
            )

    return warnings


def log_warnings(warnings: List[str]) -> None:
    """Append warnings to the audit log. Never raises."""
    if not warnings:
        return
    ts = _now_iso()
    for w in warnings:
        line = json.dumps({"ts": ts, "msg": w}, sort_keys=True)
        _append_audit(line)


def validate_and_log(
    artifact: Dict[str, Any],
    schema_path: str = "",
) -> List[str]:
    """Convenience: run the validator, log any warnings, return the list."""
    warnings = validate_artifact_fields(artifact, schema_path)
    if warnings:
        log_warnings(warnings)
    return warnings
