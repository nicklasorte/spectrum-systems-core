"""Phase Y.4 — false_negative_set builder (pure derivation, stable)."""
from __future__ import annotations

import pytest

from spectrum_systems_core.artifacts import new_artifact
from spectrum_systems_core.data_lake.serialize import canonical_json
from spectrum_systems_core.extraction.false_negative_builder import (
    FalseNegativeBuilderError,
    build_false_negative_set,
)


def _comparison(false_negatives):
    return new_artifact(
        artifact_type="extraction_alignment_comparison",
        payload={
            "artifact_type": "extraction_alignment_comparison",
            "schema_version": "1.0.0",
            "transcript_id": "m-y-fn",
            "ceiling_artifact_id": "ceil-1",
            "haiku_artifact_id": "haiku-1",
            "alignment_contract_version": "1.0.0",
            "per_type_metrics": {},
            "total_metrics": {"recall": 0.0, "precision": 0.0, "f1": 0.0},
            "aligned_pairs": [],
            "false_negatives": false_negatives,
        },
        trace_id="trace-fn",
        status="draft",
    )


def _fn(stype, cid, turns, text):
    return {
        "schema_type": stype,
        "ceiling_item_id": cid,
        "source_turn_ids": turns,
        "source_text": text,
        "ceiling_payload": {"t": text},
    }


def test_repro_byte_identical_two_runs():
    cmp = _comparison(
        [
            _fn("decision", "c-3", ["t9"], "directed staff"),
            _fn("decision", "c-1", ["t1"], "approved threshold"),
            _fn("action_item", "c-2", ["t5"], "follow up with DoD"),
        ]
    )
    a = build_false_negative_set(cmp)
    b = build_false_negative_set(cmp)
    assert canonical_json(a.payload) == canonical_json(b.payload)
    # Sorted by (schema_type, ceiling_item_id).
    order = [
        (fn["schema_type"], fn["ceiling_item_id"])
        for fn in a.payload["false_negatives"]
    ]
    assert order == [
        ("action_item", "c-2"),
        ("decision", "c-1"),
        ("decision", "c-3"),
    ]
    assert a.payload["comparison_artifact_id"] == cmp.artifact_id


def test_rejection_wrong_input_type():
    bad = new_artifact(
        artifact_type="opus_ceiling",
        payload={"false_negatives": []},
        trace_id="t",
        status="draft",
    )
    with pytest.raises(FalseNegativeBuilderError):
        build_false_negative_set(bad)
