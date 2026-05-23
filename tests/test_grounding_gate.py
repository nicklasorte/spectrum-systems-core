"""Tests for the Phase 4.A G-GROUND-VERBATIM grounding gate.

Covers normalization, per-item validation, chunk-scoping, the
fall-back-to-transcript path, and the GroundingResult dataclass shape.
"""
from __future__ import annotations

from spectrum_systems_core.promotion.grounding_gate import (
    CLAIM_SHAPED_TYPES,
    GROUNDING_GATE_SCHEMA_VERSION,
    GroundingFailureReason,
    GroundingResult,
    ItemFailure,
    MAX_DETAIL_QUOTE_CHARS,
    MIN_QUOTE_LENGTH,
    check_grounding,
    grounding_gate_bypass_record,
    grounding_gate_result_payload,
    normalize_for_grounding,
    split_grounded_and_ungrounded,
)


# ---------------------------------------------------------------- normalize


def test_normalize_whitespace() -> None:
    """Runs of whitespace collapse to single space; leading/trailing stripped."""
    assert normalize_for_grounding("  hello   world  ") == "hello world"
    assert normalize_for_grounding("hello\n\nworld") == "hello world"
    assert normalize_for_grounding("\thello\tworld\t") == "hello world"


def test_normalize_smart_quotes() -> None:
    """Smart quotes map to their straight ASCII equivalents."""
    # Left double, right double
    assert normalize_for_grounding("“Hello”") == '"Hello"'
    # Left single, right single
    assert normalize_for_grounding("‘Hello’") == "'Hello'"
    # Primes
    assert normalize_for_grounding("′ ″") == "' \""


def test_normalize_nfkc() -> None:
    """Combining characters are composed via NFKC."""
    # "fi" ligature → "fi"
    assert normalize_for_grounding("ﬁnal") == "final"
    # E + combining acute → É
    assert normalize_for_grounding("É") == "É"


def test_normalize_idempotent() -> None:
    """Applying the normalizer twice yields the same result as once."""
    cases = [
        "  hello   world  ",
        "“Quoted” text",
        "Éclipse  with    spaces",
        "",
        "single",
    ]
    for c in cases:
        assert normalize_for_grounding(normalize_for_grounding(c)) == normalize_for_grounding(c)


def test_normalize_empty_string() -> None:
    """Empty input stays empty (no exception)."""
    assert normalize_for_grounding("") == ""


def test_normalize_preserves_case() -> None:
    """Case is intentionally preserved — gate treats it as signal."""
    assert normalize_for_grounding("DoD") == "DoD"
    assert normalize_for_grounding("FCC") == "FCC"


# ---------------------------------------------------------------- check_grounding (positive)


def test_check_grounding_passes_valid_substring() -> None:
    """A quote that is a substring of its chunk clears the gate."""
    items = {
        "decisions": [
            {"source_quote": "we will determine the band plan", "source_chunk_id": "c1"}
        ]
    }
    chunks = {"c1": "so we will determine the band plan today"}
    result = check_grounding(items, chunks, "")
    assert result.passed
    assert result.total_items == 1
    assert result.grounded_items == 1
    assert result.ungrounded_items == 0
    assert result.failures == ()


def test_check_grounding_multiple_items_all_pass() -> None:
    items = {
        "decisions": [
            {"source_quote": "we will determine the band plan", "source_chunk_id": "c1"},
            {"source_quote": "the seven gig hertz band is allocated", "source_chunk_id": "c2"},
        ],
        "action_items": [
            {"source_quote": "John will draft a memo by Friday", "source_chunk_id": "c2"},
        ],
    }
    chunks = {
        "c1": "so we will determine the band plan today",
        "c2": "the seven gig hertz band is allocated to fixed wireless. John will draft a memo by Friday at noon.",
    }
    result = check_grounding(items, chunks, "")
    assert result.passed
    assert result.total_items == 3
    assert result.grounded_items == 3


# ---------------------------------------------------------------- check_grounding (negative)


def test_check_grounding_rejects_missing_field() -> None:
    items = {"decisions": [{"text": "x"}]}
    result = check_grounding(items, {}, "irrelevant transcript")
    assert not result.passed
    assert result.failures[0].reason is GroundingFailureReason.MISSING_SOURCE_QUOTE


def test_check_grounding_rejects_empty_string() -> None:
    """Empty string MUST be rejected explicitly — '' in chunk returns True."""
    items = {"decisions": [{"source_quote": ""}]}
    result = check_grounding(items, {}, "irrelevant")
    assert not result.passed
    assert result.failures[0].reason is GroundingFailureReason.EMPTY_SOURCE_QUOTE


def test_check_grounding_rejects_whitespace_only_string() -> None:
    items = {"decisions": [{"source_quote": "   \t\n   "}]}
    result = check_grounding(items, {}, "irrelevant")
    assert not result.passed
    assert result.failures[0].reason is GroundingFailureReason.EMPTY_SOURCE_QUOTE


def test_check_grounding_rejects_too_short() -> None:
    """A quote shorter than MIN_QUOTE_LENGTH is rejected."""
    items = {"decisions": [{"source_quote": "yes"}]}
    result = check_grounding(items, {}, "yes")
    assert not result.passed
    assert result.failures[0].reason is GroundingFailureReason.TOO_SHORT


def test_check_grounding_rejects_non_substring() -> None:
    items = {
        "decisions": [
            {"source_quote": "this phrase does not appear at all", "source_chunk_id": "c1"}
        ]
    }
    chunks = {"c1": "an entirely different chunk of text here"}
    result = check_grounding(items, chunks, "")
    assert not result.passed
    assert result.failures[0].reason is GroundingFailureReason.NOT_SUBSTRING


# ---------------------------------------------------------------- chunk-id scoping


def test_check_grounding_uses_chunk_id_when_present() -> None:
    """Quote must be in the NAMED chunk, even if it exists in another chunk."""
    items = {
        "decisions": [
            {"source_quote": "this is in chunk two", "source_chunk_id": "c1"}
        ]
    }
    chunks = {
        "c1": "chunk one says something different",
        "c2": "this is in chunk two, which the model wrongly attributed",
    }
    result = check_grounding(items, chunks, "")
    assert not result.passed
    assert result.failures[0].reason is GroundingFailureReason.NOT_SUBSTRING


def test_check_grounding_falls_back_to_full_transcript_when_chunk_id_absent() -> None:
    """When source_chunk_id is omitted, gate checks against full transcript."""
    items = {"decisions": [{"source_quote": "we will determine the band plan"}]}
    full = "so we will determine the band plan today, said the chair"
    result = check_grounding(items, {}, full)
    assert result.passed
    assert any("fell back to full-transcript" in w for w in result.warnings)


def test_check_grounding_records_unknown_chunk_id_failure() -> None:
    items = {
        "decisions": [
            {"source_quote": "this would otherwise match", "source_chunk_id": "BAD"}
        ]
    }
    chunks = {"c1": "this would otherwise match somewhere"}
    result = check_grounding(items, chunks, "this would otherwise match somewhere")
    assert not result.passed
    assert result.failures[0].reason is GroundingFailureReason.UNKNOWN_CHUNK_ID
    # MUST not silently fall back when the model named a bad chunk id —
    # the wrong-chunk attribution is itself a signal we want to surface.


# ---------------------------------------------------------------- normalization integration


def test_grounding_normalizes_both_sides() -> None:
    """Both the quote and the chunk text are normalized before comparison."""
    items = {
        "decisions": [
            {
                "source_quote": "we   will  determine\nthe band plan",
                "source_chunk_id": "c1",
            }
        ]
    }
    chunks = {"c1": "so we will determine the band plan today"}
    result = check_grounding(items, chunks, "")
    assert result.passed, f"failures: {result.failures}"


def test_grounding_handles_smart_quotes_in_transcript() -> None:
    """A model emitting straight quotes still grounds against a smart-quote transcript."""
    items = {
        "decisions": [
            {
                "source_quote": 'we will adopt the "band plan" approach',
                "source_chunk_id": "c1",
            }
        ]
    }
    chunks = {"c1": "so we will adopt the “band plan” approach today"}
    result = check_grounding(items, chunks, "")
    assert result.passed, f"failures: {result.failures}"


def test_grounding_preserves_transcription_errors() -> None:
    """A quote with a transcription error matches a chunk with the SAME error."""
    items = {
        "decisions": [
            {
                "source_quote": "we will use the seven gig hertz band",
                "source_chunk_id": "c1",
            }
        ]
    }
    chunks = {"c1": "so we will use the seven gig hertz band for this"}
    result = check_grounding(items, chunks, "")
    assert result.passed, f"failures: {result.failures}"


def test_grounding_rejects_corrected_quote() -> None:
    """A 'corrected' quote does NOT match a chunk that has the original error."""
    items = {
        "decisions": [
            {
                "source_quote": "we will use the 7 GHz band",  # "corrected"
                "source_chunk_id": "c1",
            }
        ]
    }
    chunks = {"c1": "so we will use the seven gig hertz band for this"}  # original error
    result = check_grounding(items, chunks, "")
    assert not result.passed
    assert result.failures[0].reason is GroundingFailureReason.NOT_SUBSTRING


# ---------------------------------------------------------------- dataclass shape & failure detail


def test_grounding_result_dataclass_shape() -> None:
    """GroundingResult exposes the documented attributes."""
    items: dict = {}
    result = check_grounding(items, {}, "")
    assert isinstance(result, GroundingResult)
    assert isinstance(result.passed, bool)
    assert isinstance(result.total_items, int)
    assert isinstance(result.grounded_items, int)
    assert isinstance(result.ungrounded_items, int)
    assert isinstance(result.failures, tuple)
    assert isinstance(result.warnings, tuple)


def test_grounding_failure_detail_truncates_long_quotes() -> None:
    long_quote = "x" * 5000  # well over MAX_DETAIL_QUOTE_CHARS
    items = {"decisions": [{"source_quote": long_quote, "source_chunk_id": "c1"}]}
    chunks = {"c1": "completely unrelated chunk text"}
    result = check_grounding(items, chunks, "")
    assert not result.passed
    failure = result.failures[0]
    # The retained source_quote on the failure is capped, and the detail
    # string never contains the full 5000-char quote.
    assert failure.source_quote is not None
    assert len(failure.source_quote) <= MAX_DETAIL_QUOTE_CHARS
    assert len(failure.detail) < 5000


def test_empty_artifact_passes_vacuously() -> None:
    """No items → passed=True (nothing to ground)."""
    result = check_grounding({}, {}, "hello")
    assert result.passed
    assert result.total_items == 0


def test_non_claim_shaped_types_are_ignored() -> None:
    """Types outside CLAIM_SHAPED_TYPES don't count toward total_items."""
    items = {
        "attendees": [{"name": "X", "agency": "Y"}],  # turn_aggregate
        "topics": [{"topic_id": "T1", "title": "X"}],  # turn_aggregate
    }
    result = check_grounding(items, {}, "")
    assert result.passed
    assert result.total_items == 0


def test_failure_iteration_order_is_deterministic() -> None:
    """Failures iterate by canonical type order, then item index."""
    items = {
        "claims": [{"source_quote": "missing chunk", "source_chunk_id": "BAD"}],
        "decisions": [{"source_quote": "another bad one", "source_chunk_id": "BAD"}],
        "risks": [{"source_quote": "third bad one", "source_chunk_id": "BAD"}],
    }
    result = check_grounding(items, {"good": "x"}, "")
    types_in_order = [f.extraction_type for f in result.failures]
    # decisions (#1 in canonical order) before claims (#5) before risks (#6).
    assert types_in_order == ["decisions", "claims", "risks"]


# ---------------------------------------------------------------- claim-shaped types coverage


def test_claim_shaped_types_has_14_entries() -> None:
    """The task specifies exactly 14 claim-shaped types."""
    assert len(CLAIM_SHAPED_TYPES) == 14


def test_claim_shaped_types_covers_the_documented_14() -> None:
    """Spelled out so a rename in either place breaks this test."""
    expected = {
        "decisions",
        "action_items",
        "open_questions",
        "commitments",
        "claims",
        "risks",
        "cross_references",
        "regulatory_references",
        "issue_registry_entry",
        "position_statement",
        "dissent_or_objection",
        "precedent_reference",
        "external_stakeholder_input",
        "procedural_ruling",
    }
    assert CLAIM_SHAPED_TYPES == expected


# ---------------------------------------------------------------- split helper


def test_split_grounded_and_ungrounded_separates_correctly() -> None:
    items = {
        "decisions": [
            {"source_quote": "we will determine the band plan", "source_chunk_id": "c1"},
            {"source_quote": "this is not in the chunk", "source_chunk_id": "c1"},
        ]
    }
    chunks = {"c1": "so we will determine the band plan today"}
    result = check_grounding(items, chunks, "")
    grounded, ungrounded = split_grounded_and_ungrounded(items, result)
    assert len(grounded["decisions"]) == 1
    assert len(ungrounded["decisions"]) == 1
    assert ungrounded["decisions"][0]["reason"] == "not_substring"


def test_split_passes_through_non_claim_shaped_types() -> None:
    items = {
        "attendees": [{"name": "X", "agency": "Y"}],
        "decisions": [
            {"source_quote": "we will determine the band plan", "source_chunk_id": "c1"}
        ],
    }
    chunks = {"c1": "so we will determine the band plan today"}
    result = check_grounding(items, chunks, "")
    grounded, ungrounded = split_grounded_and_ungrounded(items, result)
    assert grounded["attendees"] == items["attendees"]
    assert "attendees" not in ungrounded


# ---------------------------------------------------------------- artifact payloads


def test_grounding_gate_result_payload_artifact_type() -> None:
    """Artifact field is ``artifact_type``, never ``artifact_kind``."""
    items = {"decisions": [{"source_quote": "we will determine the band plan", "source_chunk_id": "c1"}]}
    chunks = {"c1": "so we will determine the band plan today"}
    result = check_grounding(items, chunks, "")
    payload = grounding_gate_result_payload(
        result, source_id="meeting-001", run_id="run-001", trace_id="trace-001"
    )
    assert payload["artifact_type"] == "grounding_gate_result"
    assert "artifact_kind" not in payload
    assert payload["source_id"] == "meeting-001"
    assert payload["passed"] is True
    assert payload["total_items"] == 1
    assert payload["grounded_count"] == 1
    assert payload["ungrounded_count"] == 0


def test_grounding_gate_result_payload_includes_gate_drop_rate() -> None:
    items = {
        "decisions": [
            {"source_quote": "we will determine the band plan", "source_chunk_id": "c1"},
            {"source_quote": "this is not in the chunk", "source_chunk_id": "c1"},
        ]
    }
    chunks = {"c1": "so we will determine the band plan today"}
    result = check_grounding(items, chunks, "")
    payload = grounding_gate_result_payload(
        result, source_id="m1", run_id="r1"
    )
    assert payload["gate_drop_rate"] == 0.5


def test_bypass_record_carries_audit_fields() -> None:
    record = grounding_gate_bypass_record(
        source_id="meeting-001",
        extraction_artifact_path="/path/to/extraction.json",
        operator="alice@example.com",
        timestamp="2026-05-23T12:00:00+00:00",
    )
    assert record["artifact_type"] == "grounding_gate_bypass_record"
    assert "artifact_kind" not in record
    assert record["source_id"] == "meeting-001"
    assert record["operator"] == "alice@example.com"
    assert record["timestamp"] == "2026-05-23T12:00:00+00:00"
    assert "operator override" in record["reason"]


def test_bypass_record_defaults_operator_to_unknown() -> None:
    record = grounding_gate_bypass_record(
        source_id="m1",
        extraction_artifact_path="/x",
        operator=None,
        timestamp="2026-05-23T00:00:00+00:00",
    )
    assert record["operator"] == "unknown"


# ---------------------------------------------------------------- constants


def test_gate_schema_version_is_1_5_0() -> None:
    assert GROUNDING_GATE_SCHEMA_VERSION == "1.5.0"


def test_min_quote_length_is_10() -> None:
    assert MIN_QUOTE_LENGTH == 10
