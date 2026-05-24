"""Phase 5 Variant C — tests for the meeting-type classifier + templates.

The classifier is pattern-matching on the source_id; the templates are
slices of ``prompts/meeting_type_templates.md`` keyed by markers. These
tests pin the boundary between "known type" and "unknown" so a future
edit cannot silently re-classify a corpus entry.
"""
from __future__ import annotations

import re

import pytest

from spectrum_systems_core.workflows.meeting_type_templates import (
    ALL_MEETING_TYPES,
    MEETING_TYPE_ADJUDICATION,
    MEETING_TYPE_DOWNLINK_TIG,
    MEETING_TYPE_DOWNLINK_TIG_WORKING,
    MEETING_TYPE_P2P_TIG,
    MEETING_TYPE_UNKNOWN,
    MEETING_TYPE_UPLINK_TIG,
    MEETING_TYPE_WORKING_GROUP,
    build_meeting_context_preamble,
    classify_meeting_type,
    load_template,
)


@pytest.mark.parametrize(
    "source_id,expected",
    [
        # Kickoffs — the regression cases from Step 7 of the Phase 5 spec.
        (
            "7-ghz-downlink-tig-meeting-kickoff---transcript-20251218",
            MEETING_TYPE_DOWNLINK_TIG,
        ),
        ("7-ghz-ul-kickoff-transcript-20251217", MEETING_TYPE_UPLINK_TIG),
        ("20251216---p2p-tig-meeting-16dec2025---transcript", MEETING_TYPE_P2P_TIG),
        (
            "7-ghz-study-working-group-meeting---transcript-20260115",
            MEETING_TYPE_WORKING_GROUP,
        ),
    ],
)
def test_classifier_known_source_ids(source_id: str, expected: str) -> None:
    """The classifier MUST return the documented type for known source_ids.

    These are the four cases the Phase 5 spec calls out; if any
    re-classifies, the A/B comparison artifact will mis-label the
    variant_c column and the F1-vs-Opus comparison becomes apples-to-
    oranges.
    """
    assert classify_meeting_type(source_id) == expected


def test_classifier_identifies_downlink_tig_working() -> None:
    """Working-session downlink TIG meetings classify as the _working variant.

    Kickoff and working sessions have different expected-volume
    profiles; the classifier must distinguish them.
    """
    assert (
        classify_meeting_type(
            "7-ghz-downlink-tig-working-session-20260201---transcript"
        )
        == MEETING_TYPE_DOWNLINK_TIG_WORKING
    )


def test_classifier_identifies_adjudication_before_tig() -> None:
    """Adjudication meetings beat any TIG match (a TIG can be adjudicated).

    The classifier walks adjudication first because the substring "tig"
    appears in many adjudication sessions where a TIG decision is being
    formally adjudicated. The adjudication template is the right
    expected-volume prior, not the TIG one.
    """
    assert (
        classify_meeting_type(
            "7-ghz-downlink-tig-adjudication-session---transcript-20260201"
        )
        == MEETING_TYPE_ADJUDICATION
    )


def test_classifier_returns_unknown_for_unmatched() -> None:
    """A source_id matching no pattern MUST return ``unknown``.

    The classifier is intentionally narrow; un-recognised ids degrade
    to no-op injection rather than producing a guess. Returning a
    guessed type here would silently corrupt the per-variant baseline.
    """
    assert classify_meeting_type("random-text-with-no-keywords") == MEETING_TYPE_UNKNOWN
    assert classify_meeting_type("") == MEETING_TYPE_UNKNOWN


def test_template_loadable_for_every_known_type_except_unknown() -> None:
    """Every classifier-output type MUST have a template body on disk.

    Otherwise ``build_meeting_context_preamble`` returns ``None`` and
    silently degrades. The only exception is ``unknown`` — that's the
    fall-back token by design.
    """
    for meeting_type in ALL_MEETING_TYPES:
        if meeting_type == MEETING_TYPE_UNKNOWN:
            assert load_template(meeting_type) is None
            continue
        body = load_template(meeting_type)
        assert body is not None, (
            f"No template body for {meeting_type} in meeting_type_templates.md"
        )
        assert len(body) > 100, (
            f"Template body for {meeting_type} is suspiciously short "
            f"({len(body)} chars)"
        )


def test_template_injected_into_preamble_for_known_type() -> None:
    """The preamble MUST embed the type's expected profile.

    Asserting on the exact preamble structure means a later refactor
    that drops the "Meeting type:" line or the template body fails
    loudly here rather than producing a silent no-op preamble.
    """
    preamble = build_meeting_context_preamble(MEETING_TYPE_DOWNLINK_TIG)
    assert preamble is not None
    assert "## Meeting Context" in preamble
    assert f"Meeting type: {MEETING_TYPE_DOWNLINK_TIG}" in preamble
    assert "decisions: 1-3" in preamble  # body content from the template
    assert "expected range for any type" in preamble  # closing imperative


def test_template_not_injected_for_unknown_type() -> None:
    """Unknown types MUST return ``None`` so the caller skips injection.

    The graceful-fallback contract: a transcript whose meeting type
    can't be inferred runs at the baseline prompt with no variant-C
    perturbation.
    """
    assert build_meeting_context_preamble(MEETING_TYPE_UNKNOWN) is None
    # Also for a literal string that isn't in ALL_MEETING_TYPES.
    assert build_meeting_context_preamble("nonsense_type") is None


# Opus baselines for the 7 GHz downlink kickoff (Phase 4.B comparison
# artifact, taken as the per-type ceiling reference). These are
# conservative high-water marks from the existing comparison runs; the
# Variant C templates' max expected counts must stay BELOW 2x these
# numbers for every type so a runaway extraction has a numerical
# ceiling to justify against.
OPUS_BASELINE_MAX_BY_TYPE: dict[str, int] = {
    "decisions": 8,
    "action_items": 12,
    "open_questions": 7,
    "procedural_ruling": 8,
    "attendees": 40,
    "topics": 10,
    "technical_parameters": 8,
    "regulatory_references": 8,
    "named_artifacts": 10,
    "scheduled_events": 6,
    "claims": 8,
    "risks": 5,
    "cross_references": 15,
    "external_stakeholder_input": 15,
    "position_statement": 4,
    "issue_registry_entry": 4,
}


_EXPECTED_PROFILE_RE = re.compile(
    r"-\s+([a-z_]+):\s+\d+-(\d+)\b",
)


def _parse_max_counts(template_body: str) -> dict[str, int]:
    """Extract ``{type: max_count}`` from an "X: N-M" template line."""
    out: dict[str, int] = {}
    for match in _EXPECTED_PROFILE_RE.finditer(template_body):
        type_name = match.group(1)
        max_count = int(match.group(2))
        out[type_name] = max_count
    return out


def test_template_expected_counts_are_realistic() -> None:
    """Each template's max-count MUST be < 2x the Opus baseline.

    Variant C exists to give Haiku a ceiling. If a template's max
    expected count drifted above 2x the Opus number, the ceiling would
    be useless — Haiku could keep over-extracting and still be "under
    expectations". This test pins the ceiling contract.
    """
    failures: list[str] = []
    for meeting_type in ALL_MEETING_TYPES - {MEETING_TYPE_UNKNOWN}:
        body = load_template(meeting_type)
        assert body is not None
        counts = _parse_max_counts(body)
        for type_name, max_count in counts.items():
            baseline = OPUS_BASELINE_MAX_BY_TYPE.get(type_name)
            if baseline is None:
                # Type not in the baseline table — skip rather than
                # speculate on a ceiling.
                continue
            ceiling = 2 * baseline
            if max_count > ceiling:
                failures.append(
                    f"{meeting_type}.{type_name}: max={max_count} "
                    f"> 2x Opus ({ceiling})"
                )
    assert not failures, (
        "Variant C templates have unrealistic max counts:\n" + "\n".join(failures)
    )


def test_classifier_output_in_all_meeting_types() -> None:
    """Every classifier output token MUST be in ALL_MEETING_TYPES.

    Catches the case where a future edit introduces a new return value
    but forgets to add it to the canonical set, which would break
    template loading silently.
    """
    sample_inputs = [
        "7-ghz-downlink-tig-meeting-kickoff---transcript-20251218",
        "7-ghz-ul-kickoff-transcript-20251217",
        "20251216---p2p-tig-meeting-16dec2025---transcript",
        "7-ghz-study-working-group-meeting---transcript-20260115",
        "random-unmatched-id",
        "7-ghz-downlink-tig-working-session---transcript-20260201",
        "7-ghz-uplink-tig-working-session---transcript-20260201",
        "comment-adjudication-session---transcript-20260301",
    ]
    for source_id in sample_inputs:
        result = classify_meeting_type(source_id)
        assert result in ALL_MEETING_TYPES, (
            f"{source_id!r} → {result!r} not in ALL_MEETING_TYPES"
        )
