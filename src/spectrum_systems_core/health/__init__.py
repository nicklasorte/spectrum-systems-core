"""Automated silent failure detection.

Eight failure classes that previously surfaced as successes are now
caught by three components:

* ``preflight``      — runs before any pipeline stage.
* ``eval_integrity`` — runs around eval scoring (upstream gating,
  pair coverage audit, model-registry drift).
* ``index_verifier`` — runs after every artifact write.

Each component emits :class:`HealthFinding` artifacts (schema:
``health_finding.schema.json``). A finding that cannot be expressed
as an artifact is not a finding — it is a crash.

Rollback paths (per spec):

* ``PREFLIGHT_ENABLED=false``      — bypass preflight.
* ``EVAL_INTEGRITY_ENABLED=false`` — bypass eval integrity checks.
* ``INDEX_VERIFY_ENABLED=false``   — bypass post-write index verifier.

Each bypass logs a WARNING on use.
"""
from __future__ import annotations

from .finding import (
    ALL_FINDING_CODES,
    HALT_FINDING_CODES,
    HealthFinding,
    Severity,
    finding_to_artifact,
    write_finding,
)

__all__ = [
    "ALL_FINDING_CODES",
    "HALT_FINDING_CODES",
    "HealthFinding",
    "Severity",
    "finding_to_artifact",
    "write_finding",
]
