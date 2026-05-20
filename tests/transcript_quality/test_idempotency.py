"""Phase 2R — idempotency / determinism test.

Two ``validate()`` calls with identical inputs must produce reports
that are field-by-field identical except for ``generated_at``.
"""
from __future__ import annotations


import pytest

from spectrum_systems_core.transcript_quality import (
    QualityReport,
    report_to_dict,
    validate,
)

from . import fixtures as F


def _normalize(report: QualityReport) -> dict:
    d = report_to_dict(report)
    d["generated_at"] = "<excluded>"
    return d


FIXTURES = [
    ("valid", F.valid_transcript()),
    ("encoding_corrupted", F.encoding_corrupted_transcript()),
    ("single_speaker_long", F.single_speaker_long_transcript()),
    ("single_speaker_too_few", F.single_speaker_too_few_words()),
    ("no_format", F.no_format_transcript()),
    ("duplicate_turn_ids", F.duplicate_turn_ids_transcript()),
    ("speaker_dash_only", F.speaker_dash_only_transcript()),
    ("tied_format", F.tied_format_transcript()),
]


@pytest.mark.parametrize(
    "label, transcript", FIXTURES, ids=[name for name, _ in FIXTURES]
)
def test_idempotency_excluding_generated_at(label: str, transcript: str) -> None:
    a = validate(transcript)
    b = validate(transcript)
    assert _normalize(a) == _normalize(b), label


def test_none_input_is_idempotent() -> None:
    a = validate(None)
    b = validate(None)
    assert _normalize(a) == _normalize(b)


def test_generated_at_is_iso_8601_string() -> None:
    report = validate(F.valid_transcript())
    assert isinstance(report.generated_at, str)
    # ``YYYY-mm-ddTHH:MM:SS.ffffffZ`` shape.
    assert "T" in report.generated_at
    assert report.generated_at.endswith("Z")
