"""Phase 1 — promotion gate (`verify_grounding`) rejection tests.

Every rejection path the gate emits is paired with one or more fixtures
under ``tests/fixtures/grounding_rejections/<category>/``. This module
loads each fixture and asserts the gate rejects it with the expected
``reason_code``. The fixtures are the contract; the test is the
enforcer.

The gate is pure logic — no I/O beyond reading the fixture files
themselves. The "no LLM call" property is structural: this module
never imports an LLM client.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from spectrum_systems_core.grounding.normalize import normalize_transcript
from spectrum_systems_core.promotion.gate import (
    DEFAULT_MIN_QUOTE_CHARS_BY_TYPE,
    GROUNDING_RATE_FLOOR,
    MIN_QUOTE_CHARS_SHORT,
    MIN_QUOTE_CHARS_SUBSTANTIVE,
    TURN_AGGREGATE_TYPES,
    VERBATIM_TYPES,
    grounding_rejection_report_payload,
    verify_grounding,
)


FIXTURE_ROOT = (
    pathlib.Path(__file__).resolve().parent.parent
    / "fixtures"
    / "grounding_rejections"
)


def _load_fixture(name: str) -> tuple[dict, str, dict]:
    fixture_dir = FIXTURE_ROOT / name
    artifact = json.loads((fixture_dir / "artifact.json").read_text())
    transcript = (fixture_dir / "transcript.txt").read_text()
    expected = json.loads((fixture_dir / "expected.json").read_text())
    return artifact, transcript, expected


def test_missing_field_fixture_rejects_with_grounding_missing_field():
    artifact, transcript, expected = _load_fixture("missing_field")
    report = verify_grounding(artifact, transcript)
    assert len(report.rejected_items) == expected["rejected_count"]
    assert len(report.accepted_items) == expected["accepted_count"]
    assert report.rejected_items[0].reason_code == expected["reason_code"]
    # The rejection record must include enough detail to explain why.
    assert report.rejected_items[0].detail


def test_offset_mismatch_fixture_rejects_with_grounding_offset_mismatch():
    artifact, transcript, expected = _load_fixture("offset_mismatch")
    report = verify_grounding(artifact, transcript)
    assert report.rejected_items[0].reason_code == expected["reason_code"]
    # The rejection MUST surface both the expected quote and what was
    # actually at the declared offset — otherwise a reviewer cannot
    # explain it without reading code.
    rec = report.rejected_items[0]
    assert rec.expected_quote_normalized
    assert rec.offset_checked == 0


def test_exact_text_not_in_transcript_fixture_rejects_correctly():
    artifact, transcript, expected = _load_fixture(
        "exact_text_not_in_transcript"
    )
    report = verify_grounding(artifact, transcript)
    assert report.rejected_items[0].reason_code == expected["reason_code"]


def test_paraphrase_near_miss_rejects_as_exact_text_not_in_transcript():
    """The canonical hallucination signal: source_quote is a paraphrase
    of a real transcript span but does not appear byte-for-byte."""
    artifact, transcript, expected = _load_fixture("paraphrase_near_miss")
    report = verify_grounding(artifact, transcript)
    assert report.rejected_items[0].reason_code == expected["reason_code"]


def test_unknown_turn_id_fixture_rejects_with_unknown_turn_id():
    artifact, transcript, expected = _load_fixture("unknown_turn_id")
    report = verify_grounding(
        artifact,
        transcript,
        transcript_turn_ids=expected["transcript_turn_ids"],
    )
    assert report.rejected_items[0].reason_code == expected["reason_code"]


def test_grounding_rate_below_floor_blocks_whole_artifact():
    """Red-team Pass 1 #4 / Pass 2 #3: rate-below-floor MUST block
    the whole artifact, not just rejected items."""
    artifact, transcript, expected = _load_fixture(
        "grounding_rate_below_floor"
    )
    report = verify_grounding(artifact, transcript)
    assert len(report.accepted_items) == expected["accepted_count"]
    assert len(report.rejected_items) == expected["rejected_count"]
    assert report.artifact_blocked is True
    assert report.block_reason_code == expected["reason_code"]
    assert report.grounding_rate < GROUNDING_RATE_FLOOR


def test_transcript_unreadable_fails_closed_for_none():
    """Red-team Pass 1 #2: missing-input bypass. None transcript must
    fail closed, not pass-through."""
    report = verify_grounding({"payload": {}}, None)
    assert report.artifact_blocked is True
    assert report.block_reason_code == "transcript_unreadable"


def test_transcript_unreadable_fails_closed_for_empty():
    report = verify_grounding({"payload": {}}, "")
    assert report.artifact_blocked is True
    assert report.block_reason_code == "transcript_unreadable"


def test_vacuous_artifact_with_no_items_passes_with_rate_1():
    """An artifact with no items is a vacuous pass (rate=1.0); the
    gate must not block it on transcript_unreadable when a transcript
    is supplied, and must not block on rate-below-floor."""
    artifact = {
        "payload": {
            "decisions": [],
            "action_items": [],
            "open_questions": [],
        }
    }
    report = verify_grounding(artifact, "some transcript text")
    assert report.artifact_blocked is False
    assert report.grounding_rate == 1.0


def test_accepted_item_carries_normalized_match_hash_and_both_offsets():
    """Acceptance must record both the normalized AND original offsets
    plus the hash. These three fields are the audit trail the
    diagnostic report and the comparison engine consume."""
    transcript = (
        "CHAIR: Thanks for joining. We will be posting Nick's paper "
        "for review next week."
    )
    nt = normalize_transcript(transcript)
    quote = "We will be posting Nick's paper for review next week."
    from spectrum_systems_core.grounding.normalize import normalize_quote

    offset = nt.text.find(normalize_quote(quote))
    artifact = {
        "payload": {
            "decisions": [
                {
                    "text": quote,
                    "grounding_mode": "verbatim",
                    "source_quote": quote,
                    "quote_offset_normalized": offset,
                }
            ]
        }
    }
    report = verify_grounding(artifact, transcript)
    assert report.artifact_blocked is False
    assert len(report.rejected_items) == 0
    assert len(report.accepted_items) == 1
    acc = report.accepted_items[0]
    assert acc.quote_offset_normalized == offset
    assert acc.quote_offset_original is not None
    assert acc.normalized_match_hash is not None
    # The original offset must point to the literal "We" in the original.
    assert transcript[acc.quote_offset_original : acc.quote_offset_original + 2] == "We"


def test_legacy_string_item_in_object_branch_is_rejected_missing_field():
    """A legacy plain-string decision cannot carry source_quote, so the
    gate must reject it under 1.4.0 with grounding_missing_field. This
    proves the gate forces every 1.4 item to upgrade to the object
    branch."""
    transcript = "CHAIR: Some discussion. CHAIR: Decision made."
    artifact = {
        "payload": {
            "decisions": ["Decision made."]
        }
    }
    report = verify_grounding(artifact, transcript)
    assert len(report.rejected_items) == 1
    assert report.rejected_items[0].reason_code == "grounding_missing_field"


def test_grounding_rejection_report_payload_validates_against_schema():
    """The diagnostic payload builder must produce output that
    validates against the grounding_rejection_report schema."""
    from spectrum_systems_core.validation import validate_artifact

    artifact, transcript, _ = _load_fixture("paraphrase_near_miss")
    report = verify_grounding(artifact, transcript)
    payload = grounding_rejection_report_payload(
        report,
        artifact_id="abc",
        artifact_type="meeting_minutes",
        trace_id="trace-1",
        source_id="src-1",
        run_id="run-1",
    )
    # The validator expects the artifact_type / schema_version keys at
    # the top level of the payload.
    validate_artifact(payload, "grounding_rejection_report")


def test_verbatim_and_turn_aggregate_type_sets_are_disjoint():
    """No item type may be both verbatim AND turn_aggregate — the gate
    routes by type, so dual-classification would mean unreachable
    code."""
    overlap = VERBATIM_TYPES & TURN_AGGREGATE_TYPES
    assert not overlap, f"overlap: {overlap!r}"


def test_gate_is_pure_no_side_effects_on_input():
    """The gate must not mutate the artifact payload — promoters depend
    on the payload being unchanged so the only "trust" mutation is the
    accepted_payload_keys() projection."""
    transcript = "CHAIR: Hello world."
    payload = {
        "decisions": [
            {
                "text": "Hello.",
                "grounding_mode": "verbatim",
                "source_quote": "Hello.",
                "quote_offset_normalized": 0,
            }
        ]
    }
    snapshot = json.dumps(payload, sort_keys=True)
    verify_grounding({"payload": payload}, transcript)
    assert json.dumps(payload, sort_keys=True) == snapshot


def test_gate_accepts_bare_payload_dict():
    """Some callers pass the payload directly (no envelope wrapper).
    The gate must handle both shapes."""
    transcript = "CHAIR: Hello world."
    payload = {
        "decisions": [
            {
                "text": "Hello world.",
                "grounding_mode": "verbatim",
                "source_quote": "Hello world.",
                "quote_offset_normalized": 6,
            }
        ]
    }
    report = verify_grounding(payload, transcript)
    assert report.artifact_blocked is False
    assert len(report.accepted_items) == 1


@pytest.mark.parametrize(
    "category",
    [
        "missing_field",
        "offset_mismatch",
        "exact_text_not_in_transcript",
        "paraphrase_near_miss",
    ],
)
def test_every_required_fixture_category_has_at_least_one_fixture(category):
    """Pre-PR signal that fixture coverage hasn't silently dropped."""
    cat_dir = FIXTURE_ROOT / category
    assert cat_dir.is_dir(), f"missing category dir: {category}"
    assert (cat_dir / "artifact.json").is_file()
    assert (cat_dir / "transcript.txt").is_file()


def test_grounding_report_is_a_frozen_dataclass():
    """Defensive: the report must be immutable so a caller cannot
    mutate accepted_items / rejected_items after a verification.
    Mutating the report would let a buggy caller promote a rejected
    item by simply moving it into accepted_items."""
    report = verify_grounding({"payload": {}}, "hello")
    with pytest.raises(Exception):
        report.accepted_items = (1,)  # type: ignore[misc]


def test_accepted_payload_keys_groups_by_item_type():
    """The promoter writes `accepted_payload_keys()` into the promoted
    payload. The grouping must preserve item order within each type."""
    transcript = "CHAIR: Hello world. CHAIR: Goodbye now."
    payload = {
        "decisions": [
            {
                "text": "Hello world.",
                "grounding_mode": "verbatim",
                "source_quote": "Hello world.",
                "quote_offset_normalized": 6,
            },
            {
                "text": "Goodbye now.",
                "grounding_mode": "verbatim",
                "source_quote": "Goodbye now.",
                "quote_offset_normalized": 24,
            },
        ]
    }
    report = verify_grounding(payload, transcript)
    grouped = report.accepted_payload_keys()
    assert list(grouped.keys()) == ["decisions"]
    assert len(grouped["decisions"]) == 2


def test_gate_does_not_silently_pass_non_list_payload_field():
    """Red-team Pass 1 #1: silent-pass paths. A non-list value under a
    known item-type key must surface as a rejection, not pass-through."""
    transcript = "CHAIR: Hello."
    payload = {"decisions": {"not": "a list"}}
    report = verify_grounding(payload, transcript)
    assert len(report.rejected_items) == 1
    assert report.rejected_items[0].reason_code == "grounding_missing_field"


def test_gate_normalized_match_hash_is_sha256_hex():
    """The match hash must be the sha256 of the normalized quote,
    written as lowercase hex. Downstream tools depend on this exact
    encoding for replay-stable comparisons."""
    import hashlib

    transcript = "CHAIR: Hello world."
    payload = {
        "decisions": [
            {
                "text": "Hello world.",
                "grounding_mode": "verbatim",
                "source_quote": "Hello world.",
                "quote_offset_normalized": 6,
            }
        ]
    }
    report = verify_grounding(payload, transcript)
    acc = report.accepted_items[0]
    expected = hashlib.sha256(b"hello world").hexdigest()
    assert acc.normalized_match_hash == expected


# ---------------------------------------------------------------------------
# Stage 2 — per-type minimum source_quote length threshold (opt-in).
# ---------------------------------------------------------------------------


def _decision_payload_with_quote(transcript: str, quote: str) -> dict:
    """Build a one-decision payload pinning the quote to a known offset."""
    from spectrum_systems_core.grounding.normalize import (
        normalize_quote,
        normalize_transcript,
    )

    nt = normalize_transcript(transcript)
    offset = nt.text.find(normalize_quote(quote))
    assert offset >= 0, "test transcript must contain the quote verbatim"
    return {
        "decisions": [
            {
                "text": quote,
                "grounding_mode": "verbatim",
                "source_quote": quote,
                "quote_offset_normalized": offset,
            }
        ]
    }


def _technical_parameters_payload_with_quote(
    transcript: str, quote: str
) -> dict:
    from spectrum_systems_core.grounding.normalize import (
        normalize_quote,
        normalize_transcript,
    )

    nt = normalize_transcript(transcript)
    offset = nt.text.find(normalize_quote(quote))
    assert offset >= 0
    return {
        "technical_parameters": [
            {
                "param_id": "p1",
                "parameter_name": "frequency",
                "value": quote,
                "grounding_mode": "verbatim",
                "source_quote": quote,
                "quote_offset_normalized": offset,
            }
        ]
    }


def test_default_min_quote_chars_table_covers_all_verbatim_types():
    """Coverage invariant: every type in VERBATIM_TYPES must have an
    explicit entry in DEFAULT_MIN_QUOTE_CHARS_BY_TYPE.

    Without this, a Stage 2 caller could silently bypass the threshold
    for any verbatim type missing from the table — the gate would treat
    "no entry" as "no threshold" and pass the item.
    """
    missing = sorted(VERBATIM_TYPES - DEFAULT_MIN_QUOTE_CHARS_BY_TYPE.keys())
    assert not missing, (
        f"DEFAULT_MIN_QUOTE_CHARS_BY_TYPE is missing entries for: {missing!r}. "
        "Add them with the appropriate substantive/short tier — see the "
        "docstring on DEFAULT_MIN_QUOTE_CHARS_BY_TYPE."
    )


def test_default_min_quote_chars_table_only_uses_known_verbatim_types():
    """The table MUST NOT carry entries for turn_aggregate types or for
    unknown payload keys — a stray entry would advertise a threshold
    the gate never enforces (because turn_aggregate items have no
    source_quote)."""
    unknown = sorted(
        DEFAULT_MIN_QUOTE_CHARS_BY_TYPE.keys() - VERBATIM_TYPES
    )
    assert not unknown, (
        f"DEFAULT_MIN_QUOTE_CHARS_BY_TYPE carries non-verbatim entries: "
        f"{unknown!r}"
    )


def test_default_min_quote_chars_values_are_substantive_or_short_tier():
    """Every entry must be MIN_QUOTE_CHARS_SUBSTANTIVE or
    MIN_QUOTE_CHARS_SHORT. A free-floating numeric value would let a
    future edit lower the bar silently."""
    allowed = {MIN_QUOTE_CHARS_SUBSTANTIVE, MIN_QUOTE_CHARS_SHORT}
    bad = {
        k: v
        for k, v in DEFAULT_MIN_QUOTE_CHARS_BY_TYPE.items()
        if v not in allowed
    }
    assert not bad, (
        f"DEFAULT_MIN_QUOTE_CHARS_BY_TYPE carries non-tier values: {bad!r}. "
        f"Allowed tiers: {sorted(allowed)!r}"
    )


def test_min_quote_chars_default_off_preserves_baseline():
    """When the caller does NOT pass min_quote_chars_by_type, the
    threshold check is skipped and the gate behaviour is byte-identical
    to the pre-Stage-2 path. A 12-char decision quote that passes the
    byte-match must still be accepted."""
    transcript = "CHAIR: Hello world. The board approved the budget."
    payload = _decision_payload_with_quote(transcript, "Hello world.")
    report = verify_grounding(payload, transcript)
    assert report.artifact_blocked is False
    assert len(report.accepted_items) == 1
    assert len(report.rejected_items) == 0


def test_min_quote_chars_substantive_type_rejects_under_threshold():
    """A decision with a 29-char NORMALIZED source_quote must be rejected
    with ``grounding_source_quote_too_short`` when the default table is
    supplied.

    Threshold is enforced on the normalized length (the byte stream the
    gate actually compares against the transcript), not the raw length.
    """
    from spectrum_systems_core.grounding.normalize import normalize_quote

    quote = "the board approved the budget"  # 29 chars, no punctuation
    assert len(normalize_quote(quote)) == 29
    transcript = (
        "CHAIR: At the prior meeting the board approved the budget "
        "for next quarter."
    )
    payload = _decision_payload_with_quote(transcript, quote)
    report = verify_grounding(
        payload,
        transcript,
        min_quote_chars_by_type=DEFAULT_MIN_QUOTE_CHARS_BY_TYPE,
    )
    assert len(report.accepted_items) == 0
    assert len(report.rejected_items) == 1
    rej = report.rejected_items[0]
    assert rej.reason_code == "grounding_source_quote_too_short"
    # The reason must surface BOTH the actual length and the expected
    # minimum — otherwise a reviewer can't explain it without reading code.
    assert "29" in rej.detail
    assert str(MIN_QUOTE_CHARS_SUBSTANTIVE) in rej.detail
    assert "decisions" in rej.detail


def test_min_quote_chars_substantive_type_accepts_at_threshold():
    """A decision with a 30-char NORMALIZED source_quote (exactly at the
    substantive threshold) must be accepted."""
    from spectrum_systems_core.grounding.normalize import normalize_quote

    quote = "the boards approved the budget"  # 30 chars, no punctuation
    assert len(normalize_quote(quote)) == 30
    transcript = (
        "CHAIR: At the prior meeting the boards approved the budget "
        "and adjourned."
    )
    payload = _decision_payload_with_quote(transcript, quote)
    report = verify_grounding(
        payload,
        transcript,
        min_quote_chars_by_type=DEFAULT_MIN_QUOTE_CHARS_BY_TYPE,
    )
    assert len(report.accepted_items) == 1
    assert len(report.rejected_items) == 0


def test_min_quote_chars_short_type_rejects_under_threshold():
    """A technical_parameters item with a 9-char NORMALIZED source_quote
    must be rejected with ``grounding_source_quote_too_short`` — the
    SHORT tier is 10 chars."""
    from spectrum_systems_core.grounding.normalize import normalize_quote

    quote = "7125 mghz"  # 9 chars normalized, no punctuation
    assert len(normalize_quote(quote)) == 9
    transcript = (
        "ENGINEER: The proposed frequency is 7125 mghz with 100 mhz "
        "of bandwidth."
    )
    payload = _technical_parameters_payload_with_quote(transcript, quote)
    report = verify_grounding(
        payload,
        transcript,
        min_quote_chars_by_type=DEFAULT_MIN_QUOTE_CHARS_BY_TYPE,
    )
    assert len(report.accepted_items) == 0
    assert len(report.rejected_items) == 1
    rej = report.rejected_items[0]
    assert rej.reason_code == "grounding_source_quote_too_short"
    assert "9" in rej.detail
    assert str(MIN_QUOTE_CHARS_SHORT) in rej.detail
    assert "technical_parameters" in rej.detail


def test_min_quote_chars_short_type_accepts_at_threshold():
    """A technical_parameters item with a 10-char NORMALIZED source_quote
    (exactly at the SHORT tier threshold) must be accepted."""
    from spectrum_systems_core.grounding.normalize import normalize_quote

    quote = "7250 mghzc"  # 10 chars normalized, no punctuation
    assert len(normalize_quote(quote)) == 10
    transcript = "ENGINEER: The frequency is 7250 mghzc per channel."
    payload = _technical_parameters_payload_with_quote(transcript, quote)
    report = verify_grounding(
        payload,
        transcript,
        min_quote_chars_by_type=DEFAULT_MIN_QUOTE_CHARS_BY_TYPE,
    )
    assert len(report.accepted_items) == 1
    assert len(report.rejected_items) == 0


def test_min_quote_chars_unknown_type_not_threshold_checked():
    """An item-type ABSENT from the threshold mapping must NOT be
    threshold-checked — a missing entry is read as "no threshold for
    this type". This pins the fail-safe behaviour: a partial mapping
    does not silently bypass the gate's other checks (byte-match still
    runs)."""
    transcript = "CHAIR: Hi there. The vote was unanimous."
    payload = _decision_payload_with_quote(transcript, "Hi there.")
    # Pass a mapping that does NOT include "decisions". The item is
    # below 30 chars but the mapping doesn't threshold this type, so
    # it must accept on the byte-match alone.
    partial_mapping = {"claims": MIN_QUOTE_CHARS_SUBSTANTIVE}
    report = verify_grounding(
        payload,
        transcript,
        min_quote_chars_by_type=partial_mapping,
    )
    assert len(report.accepted_items) == 1
    assert len(report.rejected_items) == 0


def test_min_quote_chars_too_short_reason_takes_precedence_over_byte_match():
    """Pinned ordering: a too-short quote that ALSO byte-matches must
    still reject with too_short — the reason code surfaces the
    precision problem rather than passing through to byte-match
    acceptance. Without this ordering, a 5-char "yes." anchor would
    silently pass."""
    transcript = "CHAIR: Yes. The motion passes unanimously."
    payload = _decision_payload_with_quote(transcript, "Yes.")
    report = verify_grounding(
        payload,
        transcript,
        min_quote_chars_by_type=DEFAULT_MIN_QUOTE_CHARS_BY_TYPE,
    )
    assert len(report.accepted_items) == 0
    assert len(report.rejected_items) == 1
    assert (
        report.rejected_items[0].reason_code
        == "grounding_source_quote_too_short"
    )


def test_min_quote_chars_blocks_artifact_via_rate_below_floor():
    """Whole-artifact block path: when every item fails the threshold,
    the grounding_rate drops to 0.0 (< GROUNDING_RATE_FLOOR) and the
    gate must mark artifact_blocked=True with
    grounding_rate_below_floor. The reason-code-on-block surfaces the
    aggregate problem; per-item too_short codes surface the cause."""
    transcript = "CHAIR: Yes. No. Maybe."
    payload = {
        "decisions": [
            {
                "text": "Yes.",
                "grounding_mode": "verbatim",
                "source_quote": "Yes.",
                "quote_offset_normalized": 7,
            }
        ]
    }
    report = verify_grounding(
        payload,
        transcript,
        min_quote_chars_by_type=DEFAULT_MIN_QUOTE_CHARS_BY_TYPE,
    )
    assert report.artifact_blocked is True
    assert report.block_reason_code == "grounding_rate_below_floor"
    assert report.grounding_rate < GROUNDING_RATE_FLOOR
    assert (
        report.rejected_items[0].reason_code
        == "grounding_source_quote_too_short"
    )
