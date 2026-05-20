"""Phase 2R — declarative quality-check catalog.

The CHECKS constant is the single source of truth for which checks the
validator runs, their severity, and the reason code emitted on failure.
A drift test (``tests/transcript_quality/test_severity_drift.py``)
asserts the schema's severity enum matches the distinct severities here,
so a new check cannot ship without updating the schema.
"""
from __future__ import annotations

from typing import Final

CHECKS: Final[tuple[dict[str, str], ...]] = (
    {
        "name": "encoding_utf8",
        "severity": "error",
        "reason_code_on_fail": "encoding_not_utf8",
        "description": "Transcript must be UTF-8 (no replacement characters).",
    },
    {
        "name": "length_above_min",
        "severity": "warning",
        "reason_code_on_fail": "transcript_below_min_length",
        "description": "Transcript should be at least 500 bytes (configurable).",
    },
    {
        "name": "length_below_advisory_max",
        "severity": "warning",
        "reason_code_on_fail": "transcript_above_advisory_max_length",
        "description": "Transcript should be below 1M bytes (configurable).",
    },
    {
        "name": "length_below_hard_max",
        "severity": "error",
        "reason_code_on_fail": "transcript_above_hard_max_length",
        "description": "Transcript must be below 10M bytes (NOT user-tunable above 10M).",
    },
    {
        "name": "turn_count_above_min",
        "severity": "warning",
        "reason_code_on_fail": "insufficient_turn_count",
        "description": "Transcript should have at least 2 speaker turns (monologue allowed but warned).",
    },
    {
        "name": "sufficient_total_content",
        "severity": "error",
        "reason_code_on_fail": "insufficient_total_content",
        "description": "Transcript must have at least 100 words OR at least 2 distinct speakers.",
    },
    {
        "name": "format_detected",
        "severity": "error",
        "reason_code_on_fail": "no_speaker_format_detected",
        "description": "Auto-detection must identify a speaker format (speaker_colon or speaker_dash).",
    },
    {
        "name": "unique_turn_ids",
        "severity": "error",
        "reason_code_on_fail": "duplicate_turn_id",
        "description": "If turn IDs are present, they must be unique.",
    },
)


def check_severities() -> frozenset[str]:
    """Return the distinct severities declared in :data:`CHECKS`."""
    return frozenset(c["severity"] for c in CHECKS)


def reason_codes_emitted() -> tuple[str, ...]:
    """Return the reason codes the validator may emit via :data:`CHECKS`."""
    return tuple(c["reason_code_on_fail"] for c in CHECKS)
