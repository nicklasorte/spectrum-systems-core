"""Phase 2R — transcript ingestion quality gate.

A pure, read-only validator that classifies a transcript's well-formedness
before any extraction begins. The validator never calls an LLM, never reads
the file system inside :func:`validate`, and never mutates global state.

The diagnostic artifact written by the CLI (``transcript_quality_report``)
follows the same lifecycle as ``debug__<run_id>.json`` /
``grounding_rejection_report``: never promoted, never indexed.

See ``docs/architecture/rollback_contracts.md`` and the Phase 2R PR
description for the full design.
"""
from __future__ import annotations

from .checks import CHECKS
from .validate import (
    QualityCheckResult,
    QualityReport,
    report_to_dict,
    validate,
)

__all__ = [
    "CHECKS",
    "QualityCheckResult",
    "QualityReport",
    "report_to_dict",
    "validate",
]
