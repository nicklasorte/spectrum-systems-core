"""Phase P3-A T-1: chunk metadata contract gate tests."""
from __future__ import annotations

import os
from typing import Any, Dict

import pytest

from spectrum_systems_core.extraction.chunk_metadata_gate import (
    REQUIRED_CHUNK_FIELDS,
    STRICT_ENV_VAR,
    ChunkMetadataReport,
    format_report_for_log,
    validate_chunk_metadata,
)


def _well_formed_chunk(idx: int = 0) -> Dict[str, Any]:
    return {
        "chunk_id": f"chunk-{idx}",
        "speaker": "FCC.Smith",
        "agenda_item_id": "AI-001",
        "text": "Some content",
    }


def test_gate_well_formed_chunks_has_no_findings() -> None:
    chunks = [_well_formed_chunk(i) for i in range(3)]
    report = validate_chunk_metadata(chunks, strict=False)
    assert not report.has_violations()
    assert report.chunks_scanned == 3


def test_gate_turn_id_alias_is_accepted() -> None:
    # The task spec uses ``turn_id``; the codebase uses ``chunk_id``.
    # The gate must accept either name as satisfying the field.
    chunks = [
        {
            "turn_id": "t-0001",
            "speaker": "Smith",
            "agenda_item_id": "AI-001",
            "text": "Hello",
        }
    ]
    report = validate_chunk_metadata(chunks, strict=False)
    assert not report.has_violations()


def test_gate_chunk_id_absent_vs_null_are_reported_separately() -> None:
    chunks = [
        # absent
        {"speaker": "Smith", "agenda_item_id": "AI-001"},
        # null
        {"chunk_id": None, "speaker": "Smith", "agenda_item_id": "AI-001"},
    ]
    report = validate_chunk_metadata(chunks, strict=False)
    assert len(report.findings) == 2
    kinds = sorted(f.kind for f in report.findings)
    # absent and null must be reported separately so the operator
    # sees the writer-side cause vs the explicit-None cause.
    assert kinds == ["absent", "null"]
    rendered = "\n".join(report.as_strings())
    assert "(key missing, not null)" in rendered
    assert "is null" in rendered


def test_gate_speaker_absent_is_a_violation() -> None:
    chunks = [{"chunk_id": "c-1", "agenda_item_id": "AI-001"}]
    report = validate_chunk_metadata(chunks, strict=False)
    assert any(
        f.field_name == "speaker" and f.kind == "absent"
        for f in report.findings
    )


def test_gate_agenda_item_id_null_is_a_violation() -> None:
    chunks = [
        {"chunk_id": "c-1", "speaker": "Smith", "agenda_item_id": None},
    ]
    report = validate_chunk_metadata(chunks, strict=False)
    assert any(
        f.field_name == "agenda_item_id" and f.kind == "null"
        for f in report.findings
    )


def test_gate_agenda_item_id_empty_string_is_a_violation() -> None:
    chunks = [
        {"chunk_id": "c-1", "speaker": "Smith", "agenda_item_id": "  "},
    ]
    report = validate_chunk_metadata(chunks, strict=False)
    # Empty / whitespace-only agenda_item_id is treated as null
    # because the Phase X2 contract is "always a non-empty string".
    assert any(
        f.field_name == "agenda_item_id" and f.kind == "null"
        for f in report.findings
    )


def test_gate_strict_mode_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STRICT_ENV_VAR, "true")
    chunks = [_well_formed_chunk(0)]
    chunks[0].pop("speaker")
    report = validate_chunk_metadata(chunks)
    assert report.strict_mode is True


def test_gate_strict_mode_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(STRICT_ENV_VAR, "false")
    chunks = [_well_formed_chunk(0)]
    chunks[0].pop("speaker")
    report = validate_chunk_metadata(chunks, strict=True)
    # Explicit kwarg overrides env so callers in tests can force a mode.
    assert report.strict_mode is True


def test_gate_non_dict_chunk_is_a_violation() -> None:
    chunks = ["not a dict"]  # type: ignore[list-item]
    report = validate_chunk_metadata(chunks, strict=False)
    assert report.has_violations()
    assert report.chunks_scanned == 1


def test_format_report_truncates_after_five() -> None:
    chunks = [
        {"chunk_id": None} for _ in range(10)
    ]
    report = validate_chunk_metadata(chunks, strict=False)
    rendered = format_report_for_log(report)
    # 10 chunks * (3 missing fields each) = 30 findings; we render 5.
    assert "violations=30" in rendered
    assert "and 25 more" in rendered


def test_per_field_violation_counts() -> None:
    chunks = [
        {"chunk_id": None, "speaker": "x", "agenda_item_id": "AI-001"},
        {"chunk_id": None, "speaker": "y", "agenda_item_id": None},
    ]
    report = validate_chunk_metadata(chunks, strict=False)
    counts = report.per_field_violation_counts()
    assert counts["chunk_id"] == 2
    assert counts["agenda_item_id"] == 1
    assert "speaker" not in counts
