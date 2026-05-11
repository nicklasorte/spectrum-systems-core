"""Phase P — tests that the verification-cycle recovery runbook is present
and covers every failure mode the Phase P safety nets reference.

These tests guard against a runbook silently going out-of-sync with the
CLI errors that point at it. The CLI emits
``See docs/runbooks/verification-cycle-recovery.md section <N>`` strings;
if those sections vanish, the cross-references break.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


RUNBOOK = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "runbooks"
    / "verification-cycle-recovery.md"
)


# Each entry is (section_number, identifying_section_header).
# Identifying header words come from Part D of the Phase P spec —
# we match by section number AND by a distinct keyword to avoid
# substring false-positives (red-team scenario 4 in Pass 2).
EXPECTED_SECTIONS = [
    (1, "Pre-flight check blocks because migration incomplete"),
    (2, "Migration workflow fails mid-run"),
    (3, "Force-run timeout"),
    (4, "Force-run partial completion with no timeout"),
    (5, "Eval workflow runs against partial pipeline output"),
    (6, "Sanity bound REVIEW from review-baseline-candidate"),
    (7, "Compound failure: timeout AND schema validation failure"),
]


def _read_runbook() -> str:
    assert RUNBOOK.is_file(), f"runbook missing: {RUNBOOK}"
    return RUNBOOK.read_text(encoding="utf-8")


def test_runbook_file_exists() -> None:
    assert RUNBOOK.is_file(), f"runbook missing: {RUNBOOK}"


def test_runbook_covers_all_failure_modes() -> None:
    """Each Part-D failure mode must have a numbered section header."""
    body = _read_runbook()
    for number, distinct_phrase in EXPECTED_SECTIONS:
        header_re = re.compile(
            rf"^##\s+Section\s+{number}\s+[\-—]\s+.+$", re.MULTILINE
        )
        assert header_re.search(body), (
            f"Section {number} header missing in runbook"
        )
        # And the distinct phrase must appear so the section actually
        # covers the failure mode it claims to cover.
        assert distinct_phrase in body, (
            f"Section {number} is missing key phrase {distinct_phrase!r}"
        )


def test_runbook_section_headers_are_distinct() -> None:
    """Substring matches in test_runbook_covers_all_failure_modes could
    cross-collide. Make sure each section header is unique."""
    body = _read_runbook()
    headers = re.findall(r"^##\s+Section\s+\d+.*$", body, flags=re.MULTILINE)
    assert len(headers) == len(EXPECTED_SECTIONS)
    assert len(set(headers)) == len(headers), (
        "Section headers must be unique: " + repr(headers)
    )


def test_runbook_compound_failure_composes_sections(
) -> None:
    """Section 7 is a compound failure mode. It must compose individual
    recoveries (refer back to other sections), not just list them as
    'see X' bullets. Red-team scenario 5 from Pass 1."""
    body = _read_runbook()
    # Find the actual H2 header for Section 7 (not the quick-triage row).
    header_match = re.search(
        r"^##\s+Section\s+7\b.*$", body, flags=re.MULTILINE
    )
    assert header_match is not None
    section_7_start = header_match.start()
    next_header = re.search(
        r"^##\s+", body[header_match.end():], flags=re.MULTILINE
    )
    if next_header is None:
        section_7_body = body[section_7_start:]
    else:
        section_7_body = body[
            section_7_start : header_match.end() + next_header.start()
        ]
    # Composition signals: section 7 must reference earlier sections by
    # number AND explain the ordering.
    assert "Section 1" in section_7_body or "section 1" in section_7_body
    assert "Section 3" in section_7_body or "section 3" in section_7_body
    # And it must say WHY the order matters (not just enumerate).
    composition_signals = (
        "first",
        "Then",
        "then",
        "order",
        "Finally",
    )
    assert any(s in section_7_body for s in composition_signals), (
        "Section 7 must compose recoveries (use 'first/then/finally' "
        "language), not just enumerate."
    )


def test_runbook_each_section_has_symptoms_and_commands() -> None:
    """Each failure-mode section must include Symptoms and Commands blocks
    (per Part D requirement)."""
    body = _read_runbook()
    for number, _ in EXPECTED_SECTIONS:
        # Slice out this section's body up to the next ## header.
        marker = re.search(
            rf"^##\s+Section\s+{number}\b.*$", body, flags=re.MULTILINE
        )
        assert marker is not None
        start = marker.end()
        next_marker = re.search(
            r"^##\s+", body[start:], flags=re.MULTILINE
        )
        end = start + (next_marker.start() if next_marker else len(body) - start)
        section_body = body[start:end]
        assert "Symptoms" in section_body, (
            f"Section {number} missing 'Symptoms' subsection"
        )
        assert "Commands" in section_body, (
            f"Section {number} missing 'Commands' subsection"
        )


def test_runbook_each_section_has_remediation_prompt_template() -> None:
    """Per Part D: each failure mode includes a remediation prompt template."""
    body = _read_runbook()
    for number, _ in EXPECTED_SECTIONS:
        marker = re.search(
            rf"^##\s+Section\s+{number}\b.*$", body, flags=re.MULTILINE
        )
        assert marker is not None
        start = marker.end()
        next_marker = re.search(
            r"^##\s+", body[start:], flags=re.MULTILINE
        )
        end = start + (next_marker.start() if next_marker else len(body) - start)
        section_body = body[start:end]
        assert "Remediation prompt template" in section_body, (
            f"Section {number} missing 'Remediation prompt template'"
        )
