"""Central registry of reason codes emitted by Phase 2R.

This module is the single source of truth for the reason-code identifiers
that the transcript-quality validator and its CLI integrations emit. The
``tests/transcript_quality/test_reason_code_coverage.py`` test scans the
validator's ``CHECKS`` constant and the new CLI surface to assert every
emitted code appears here.

Reason codes in this module are scoped to Phase 2R surfaces. Pre-existing
reason codes used elsewhere in the codebase (e.g. ``transcript_unreadable``
inside the grounding gate) keep their original location to avoid churn;
this module names the ones the Phase 2R gate emits at its own surface.
"""
from __future__ import annotations

# CLI-layer failures (raised from `spectrum-core check-transcript` and the
# `--enable-pre-flight-check` hook).
TRANSCRIPT_NOT_FOUND = "transcript_not_found"
TRANSCRIPT_UNREADABLE = "transcript_unreadable"

# Validator-emitted reason codes (mirror the `reason_code_on_fail` values
# in `transcript_quality/checks.py::CHECKS`).
ENCODING_NOT_UTF8 = "encoding_not_utf8"
TRANSCRIPT_BELOW_MIN_LENGTH = "transcript_below_min_length"
TRANSCRIPT_ABOVE_ADVISORY_MAX_LENGTH = "transcript_above_advisory_max_length"
TRANSCRIPT_ABOVE_HARD_MAX_LENGTH = "transcript_above_hard_max_length"
INSUFFICIENT_TURN_COUNT = "insufficient_turn_count"
INSUFFICIENT_TOTAL_CONTENT = "insufficient_total_content"
NO_SPEAKER_FORMAT_DETECTED = "no_speaker_format_detected"
DUPLICATE_TURN_ID = "duplicate_turn_id"

# Extraction-CLI hook (set when `--enable-pre-flight-check` halts a run
# because the validator reported `has_errors: true`).
TRANSCRIPT_QUALITY_CHECK_FAILED = "transcript_quality_check_failed"


PHASE_2R_REASON_CODES: frozenset[str] = frozenset(
    {
        TRANSCRIPT_NOT_FOUND,
        TRANSCRIPT_UNREADABLE,
        ENCODING_NOT_UTF8,
        TRANSCRIPT_BELOW_MIN_LENGTH,
        TRANSCRIPT_ABOVE_ADVISORY_MAX_LENGTH,
        TRANSCRIPT_ABOVE_HARD_MAX_LENGTH,
        INSUFFICIENT_TURN_COUNT,
        INSUFFICIENT_TOTAL_CONTENT,
        NO_SPEAKER_FORMAT_DETECTED,
        DUPLICATE_TURN_ID,
        TRANSCRIPT_QUALITY_CHECK_FAILED,
    }
)
