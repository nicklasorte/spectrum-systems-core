"""Phase 3P schema backward-compat tests.

The new optional ``reason`` field on decisions/action_items must not
break validation of pre-3P artifacts (which do not carry it). The new
``few_shot_reason_missing_rate`` field on the pipeline_invocation_log
schema must also be optional — pre-3P logs do not carry it.
"""
from __future__ import annotations


import pytest

from spectrum_systems_core.validation import (
    ArtifactValidationError,
    _load_schema,
    validate_artifact,
)


def _flush_schema_cache() -> None:
    _load_schema.cache_clear()


def test_pre_3p_meeting_minutes_validates_without_reason() -> None:
    _flush_schema_cache()
    art = {
        "artifact_type": "meeting_minutes",
        "schema_version": "1.3.0",
        "title": "ok",
        "summary": "ok",
        "decisions": [{"text": "the group decided to do X"}],
        "action_items": [{"action": "post the minutes"}],
        "open_questions": [],
    }
    validate_artifact(art, "meeting_minutes")


def test_3p_meeting_minutes_with_reason_validates() -> None:
    _flush_schema_cache()
    art = {
        "artifact_type": "meeting_minutes",
        "schema_version": "1.4.0",
        "title": "ok",
        "summary": "ok",
        "decisions": [
            {
                "text": "the group decided to do X",
                "reason": "Explicit decision: group affirmed.",
            }
        ],
        "action_items": [
            {
                "action": "post the minutes",
                "reason": "Procedural commitment: 'we will post'.",
            }
        ],
        "open_questions": [],
    }
    validate_artifact(art, "meeting_minutes")


def test_reason_empty_string_rejected() -> None:
    """minLength=1 on the reason field — empty string fails."""
    _flush_schema_cache()
    art = {
        "artifact_type": "meeting_minutes",
        "schema_version": "1.4.0",
        "title": "ok",
        "summary": "ok",
        "decisions": [{"text": "x", "reason": ""}],
        "action_items": [],
        "open_questions": [],
    }
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, "meeting_minutes")


def test_pre_3p_invocation_log_validates() -> None:
    _flush_schema_cache()
    log = {
        "artifact_type": "pipeline_invocation_log",
        "schema_version": "1.0.0",
        "source_id": "src",
        "invocation_id": "i" * 32,
        "started_at": "2026-05-19T10:00:00+00:00",
        "completed_at": "2026-05-19T10:01:00+00:00",
        "caller": "production_cli",
        "extraction_config_hash": "h" * 64,
        "prompt_content_hash": "",
        "transcript_hash": "",
        "comparison_artifact_path": "",
        "ttl_expires_at": "2026-06-18T10:00:00+00:00",
    }
    validate_artifact(log, "pipeline_invocation_log")


def test_3p_invocation_log_with_missing_rate_validates() -> None:
    _flush_schema_cache()
    log = {
        "artifact_type": "pipeline_invocation_log",
        "schema_version": "1.0.0",
        "source_id": "src",
        "invocation_id": "i" * 32,
        "started_at": "2026-05-19T10:00:00+00:00",
        "completed_at": "2026-05-19T10:01:00+00:00",
        "caller": "production_cli",
        "extraction_config_hash": "h" * 64,
        "prompt_content_hash": "",
        "transcript_hash": "",
        "comparison_artifact_path": "",
        "ttl_expires_at": "2026-06-18T10:00:00+00:00",
        "few_shot_reason_missing_rate": 0.30,
    }
    validate_artifact(log, "pipeline_invocation_log")


def test_invocation_log_missing_rate_out_of_range_rejected() -> None:
    _flush_schema_cache()
    log = {
        "artifact_type": "pipeline_invocation_log",
        "schema_version": "1.0.0",
        "source_id": "src",
        "invocation_id": "i" * 32,
        "started_at": "2026-05-19T10:00:00+00:00",
        "completed_at": "2026-05-19T10:01:00+00:00",
        "caller": "production_cli",
        "extraction_config_hash": "h" * 64,
        "prompt_content_hash": "",
        "transcript_hash": "",
        "comparison_artifact_path": "",
        "ttl_expires_at": "2026-06-18T10:00:00+00:00",
        "few_shot_reason_missing_rate": 1.50,
    }
    with pytest.raises(ArtifactValidationError):
        validate_artifact(log, "pipeline_invocation_log")
