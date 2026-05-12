"""Tests for the HealthFinding artifact model + schema."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from spectrum_systems_core.health.finding import (
    ALL_FINDING_CODES,
    HealthFinding,
    finding_to_artifact,
    write_finding,
)
from spectrum_systems_core.validation import (
    ArtifactValidationError,
    _load_schema,
    validate_artifact,
)


def _schema_enum() -> set[str]:
    schema = _load_schema("health_finding")
    return set(schema["properties"]["finding_code"]["enum"])


def test_all_finding_codes_match_schema_enum() -> None:
    """Codes declared in Python must equal the schema enum."""
    assert ALL_FINDING_CODES == _schema_enum()


def test_undeclared_code_rejected() -> None:
    with pytest.raises(ValueError, match="undeclared finding_code"):
        HealthFinding(
            finding_code="not_a_real_code",
            severity="halt",
            remediation="x",
        )


def test_invalid_severity_rejected() -> None:
    with pytest.raises(ValueError, match="invalid severity"):
        HealthFinding(
            finding_code="feature_flag_missing",
            severity="critical",
            remediation="x",
        )


def test_artifact_envelope_matches_schema(tmp_path: Path) -> None:
    finding = HealthFinding(
        finding_code="feature_flag_missing",
        severity="halt",
        context={"flag_name": "x"},
        remediation="seed it",
        pipeline_run_id="run-1",
    )
    artifact = finding_to_artifact(finding)
    # Must validate against the on-disk schema.
    validate_artifact(artifact, "health_finding")


def test_artifact_envelope_validates_with_null_run_id() -> None:
    finding = HealthFinding(
        finding_code="feature_flag_missing",
        severity="halt",
        remediation="seed it",
    )
    artifact = finding_to_artifact(finding)
    assert artifact["pipeline_run_id"] is None
    validate_artifact(artifact, "health_finding")


def test_extra_field_in_artifact_rejected() -> None:
    finding = HealthFinding(
        finding_code="feature_flag_missing",
        severity="halt",
        remediation="seed",
    )
    artifact = finding_to_artifact(finding)
    artifact["extra"] = "boom"
    with pytest.raises(ArtifactValidationError):
        validate_artifact(artifact, "health_finding")


def test_write_finding_creates_file(tmp_path: Path) -> None:
    finding = HealthFinding(
        finding_code="feature_flag_missing",
        severity="halt",
        remediation="seed",
        context={"flag_name": "x"},
    )
    path = write_finding(finding, data_lake_path=tmp_path)
    assert path.is_file()
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["finding_code"] == "feature_flag_missing"
    assert body["severity"] == "halt"
