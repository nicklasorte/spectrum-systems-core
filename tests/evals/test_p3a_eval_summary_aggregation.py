"""Phase P3-A: eval_summary aggregation of meeting_extraction passthrough."""
from __future__ import annotations

from spectrum_systems_core.evals.m4.runner import EvalRunner


def test_aggregate_no_sources_returns_none_fields() -> None:
    out = EvalRunner._aggregate_p3a_passthrough([])
    for key in (
        "extraction_mode",
        "glossary_version",
        "off_topic_rate",
        "extraction_path_breakdown",
        "source_turn_orphan_rate",
        "source_turn_diversity_rate",
        "stakeholders_populated_rate",
        "rationale_populated_rate",
        "claim_type_populated_rate",
        "population_rate_note",
    ):
        assert out[key] is None


def test_aggregate_single_source_copies_fields() -> None:
    out = EvalRunner._aggregate_p3a_passthrough([{
        "extraction_mode": "two_stage",
        "glossary_version": 1,
        "off_topic_rate": 0.125,
        "extraction_path_breakdown": {
            "decision": 3, "claim": 2, "action_item": 1, "off_topic": 1,
        },
        "source_turn_orphan_rate": 0.0,
        "source_turn_diversity_rate": 0.8,
        "stakeholders_populated_rate": 0.9,
        "rationale_populated_rate": 0.9,
        "claim_type_populated_rate": 1.0,
    }])
    assert out["extraction_mode"] == "two_stage"
    assert out["glossary_version"] == 1
    assert out["off_topic_rate"] == 0.125
    assert out["extraction_path_breakdown"]["decision"] == 3
    assert out["source_turn_orphan_rate"] == 0.0
    assert out["source_turn_diversity_rate"] == 0.8
    # Above threshold -> no population_rate_note.
    assert out["population_rate_note"] is None


def test_aggregate_means_floats_sums_breakdown() -> None:
    out = EvalRunner._aggregate_p3a_passthrough([
        {
            "extraction_mode": "two_stage",
            "off_topic_rate": 0.2,
            "extraction_path_breakdown": {
                "decision": 3, "claim": 2, "action_item": 1, "off_topic": 1,
            },
        },
        {
            "extraction_mode": "two_stage",
            "off_topic_rate": 0.4,
            "extraction_path_breakdown": {
                "decision": 1, "claim": 4, "action_item": 0, "off_topic": 3,
            },
        },
    ])
    # Mean of 0.2 and 0.4 = 0.3 (tolerate FP imprecision).
    assert abs(out["off_topic_rate"] - 0.3) < 1e-9
    # Sum across sources.
    assert out["extraction_path_breakdown"] == {
        "decision": 4, "claim": 6, "action_item": 1, "off_topic": 4,
    }


def test_aggregate_emits_population_rate_note_when_below_threshold() -> None:
    out = EvalRunner._aggregate_p3a_passthrough([{
        "extraction_mode": "two_stage",
        "stakeholders_populated_rate": 0.5,
        "rationale_populated_rate": 0.9,
        "claim_type_populated_rate": 0.4,
    }])
    note = out["population_rate_note"]
    assert isinstance(note, str)
    assert "stakeholders_populated_rate" in note
    assert "claim_type_populated_rate" in note
    # rationale is above threshold so it must NOT show up.
    assert "rationale_populated_rate" not in note


def test_aggregate_handles_missing_fields_per_source() -> None:
    # One source has every field; the other has none. The aggregate
    # uses the first non-null for scalars and means over reported
    # floats only -- so the missing source must not poison the rate.
    out = EvalRunner._aggregate_p3a_passthrough([
        {
            "extraction_mode": "two_stage",
            "glossary_version": 2,
            "off_topic_rate": 0.1,
        },
        {},
    ])
    assert out["extraction_mode"] == "two_stage"
    assert out["glossary_version"] == 2
    # Mean over the ONLY value reported (0.1), not averaged with 0.0.
    assert out["off_topic_rate"] == 0.1
