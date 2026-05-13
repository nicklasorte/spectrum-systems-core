"""Unit tests for ``scripts/_artifact_validator.py``.

Covers the five cases called out by section C.3 of the integration
hardening task:

  1. Valid meeting_extraction artifact -> no error.
  2. Wrong artifact_type -> ArtifactValidationError with a clear message.
  3. Missing required field -> ArtifactValidationError naming the field.
  4. No schema registered -> warns but doesn't block.
  5. ``source_id`` field present but ``source_artifact_id`` absent ->
     the PR #78 bug class is caught at validation time.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _artifact_validator import (  # noqa: E402
    ArtifactValidationError,
    validate_artifact,
)

from tests.integration.fixtures import make_meeting_extraction_artifact


def _real_extraction() -> dict:
    return make_meeting_extraction_artifact(str(uuid.uuid4()))


def test_valid_meeting_extraction_passes() -> None:
    artifact = _real_extraction()
    # Must not raise.
    validate_artifact(artifact, "meeting_extraction", "/tmp/extraction.json")


def test_wrong_artifact_type_raises_with_clear_message() -> None:
    artifact = _real_extraction()
    artifact["artifact_type"] = "source_record"
    with pytest.raises(ArtifactValidationError) as info:
        validate_artifact(artifact, "meeting_extraction", "/tmp/extraction.json")
    msg = str(info.value)
    assert "meeting_extraction" in msg
    assert "source_record" in msg
    assert "/tmp/extraction.json" in msg


def test_missing_required_field_raises_naming_the_field() -> None:
    """The validator surface MUST tell the operator which field is
    missing, not just "validation failed". This is a usability
    requirement: a malformed artifact error that doesn't name the field
    is a 30-minute debug session."""
    artifact = _real_extraction()
    # ``provenance`` is required at the top level of meeting_extraction.
    del artifact["provenance"]
    with pytest.raises(ArtifactValidationError) as info:
        validate_artifact(artifact, "meeting_extraction")
    msg = str(info.value)
    assert "provenance" in msg, (
        f"error message must name the missing field; got: {msg}"
    )


def test_no_schema_registered_warns_but_does_not_block(capsys) -> None:
    """Unknown artifact types are warned about (so future drift is
    visible) but do not block the script. The package-level X-2 gate
    is the authoritative write-time validator."""
    artifact = {"artifact_type": "totally-made-up-type"}
    # Must not raise.
    validate_artifact(artifact, "totally-made-up-type", "/tmp/x.json")
    captured = capsys.readouterr()
    assert "warning" in captured.err.lower()
    assert "totally-made-up-type" in captured.err


def test_pr_78_bug_class_caught_at_validation_time() -> None:
    """Regression guard for PR #78: an artifact that carries
    ``source_id`` at the top level but is missing ``source_artifact_id``
    is exactly the bug class the validator must catch BEFORE the script
    reads any field off it.

    The meeting_extraction schema declares ``source_artifact_id`` as
    required and is closed via ``additionalProperties: false``, so
    either branch surfaces here.
    """
    artifact = _real_extraction()
    del artifact["source_artifact_id"]
    artifact["source_id"] = "test-slug"  # the legacy field name
    with pytest.raises(ArtifactValidationError) as info:
        validate_artifact(artifact, "meeting_extraction", "/tmp/extraction.json")
    msg = str(info.value)
    # Either the "additionalProperties" rejection of source_id OR the
    # "required" rejection of source_artifact_id is acceptable here --
    # both prove the validator caught the bug at the right layer.
    assert (
        "source_id" in msg or "source_artifact_id" in msg
    ), f"error message must name the drifted field; got: {msg}"


def test_validator_rejects_non_dict_input() -> None:
    """Defensive: a stray list or string in place of a dict must raise
    a clear error, not let the script crash with KeyError downstream."""
    with pytest.raises(ArtifactValidationError):
        validate_artifact(["not-a-dict"], "meeting_extraction")  # type: ignore[arg-type]


def test_validator_skips_artifact_type_check_when_disabled() -> None:
    """``require_artifact_type_field=False`` lets the validator inspect
    contracts/ artifacts that pre-date the artifact_type convention
    (``ground_truth_pair`` is the only live example today).
    """
    pair = {
        "pair_id": "11111111-1111-4111-1111-111111111111",
        "source_artifact_id": "src-001",
        "minutes_artifact_id": "min-001",
        "meeting_date": "2026-02-19",
        "meeting_name": "test",
        "match_confidence": "high",
        "status": "confirmed",
        "created_at": "2026-05-11T00:00:00+00:00",
        "confirmed_at": "2026-05-11T00:00:00+00:00",
        "confirmed_by": "fixture",
        "schema_version": "1.0.0",
        "provenance": {"produced_by": "GroundTruthLinker"},
    }
    # No artifact_type field -- must not raise when the flag is off.
    validate_artifact(
        pair,
        "ground_truth_pair",
        require_artifact_type_field=False,
    )
    # But schema violations still trip the validator:
    bad = dict(pair)
    bad["status"] = "garbage"
    with pytest.raises(ArtifactValidationError):
        validate_artifact(
            bad,
            "ground_truth_pair",
            require_artifact_type_field=False,
        )
