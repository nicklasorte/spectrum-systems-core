"""Phase 2R — severity drift test.

Asserts the schema enum stays in sync with the severities declared in
``CHECKS``. If a new severity is added to CHECKS without bumping the
schema enum (or vice versa), this test fails before the schema can
ship.
"""
from __future__ import annotations

import json
from pathlib import Path

from spectrum_systems_core.transcript_quality.checks import (
    CHECKS,
    check_severities,
)

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "spectrum_systems_core"
    / "schemas"
    / "transcript_quality_report.schema.json"
)


def _schema_severity_enum() -> frozenset[str]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    enum = (
        schema["properties"]["checks"]["items"]["properties"]["severity"][
            "enum"
        ]
    )
    return frozenset(enum)


def test_every_check_declares_severity() -> None:
    for c in CHECKS:
        assert "severity" in c, c
        assert c["severity"] in {"error", "warning", "info"}, c


def test_code_severities_are_subset_of_schema_enum() -> None:
    schema_enum = _schema_severity_enum()
    code_severities = check_severities()
    assert code_severities.issubset(schema_enum), (
        code_severities - schema_enum
    )


def test_schema_enum_matches_distinct_code_severities() -> None:
    """The schema enum must equal the distinct severities the validator
    emits. The drift test fails if a severity is added to one side but
    not the other."""
    schema_enum = _schema_severity_enum()
    code_severities = check_severities()
    # The schema may legitimately include "info" (reserved) even if
    # CHECKS does not declare any info-severity checks yet. The drift
    # check we care about is: every code severity is in the schema enum,
    # and no orphan schema severity exists beyond the reserved "info".
    assert code_severities.issubset(schema_enum)
    assert schema_enum.issubset(code_severities | {"info"}), schema_enum
