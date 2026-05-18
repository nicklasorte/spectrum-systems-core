"""Phase Y.2 — alignment comparator reproduction + contract-version gate."""
from __future__ import annotations

import pytest

from spectrum_systems_core.artifacts import new_artifact
from spectrum_systems_core.data_lake.serialize import canonical_json
from spectrum_systems_core.evals.extraction_comparison import (
    AlignmentContractError,
    compare_extractions,
    contract_version,
)


def _ceiling(items):
    return new_artifact(
        artifact_type="opus_ceiling",
        payload={
            "artifact_type": "opus_ceiling",
            "schema_version": "1.0.0",
            "transcript_id": "m-y-cmp",
            "model_id": "claude-opus-4-7",
            "extracted_items": items,
            "per_type_counts": {},
            "transcript_keyword_hits": {},
        },
        trace_id="trace-cmp",
        status="draft",
    )


def _item(item_id, turns, text):
    return {
        "item_id": item_id,
        "schema_type": "decision",
        "source_turn_ids": turns,
        "source_text": text,
        "payload": {"t": text},
    }


def test_repro_recall_precision_fn_fp():
    aligned_text = (
        "the committee approved the seven gigahertz downlink power threshold"
    )
    ceiling = _ceiling(
        [
            _item("c-1", ["t1", "t2"], aligned_text),
            _item("c-2", ["t5"], "deferred the aggregate interference methodology"),
            _item("c-3", ["t9"], "directed staff to circulate revised ERP values"),
        ]
    )
    haiku = _ceiling(
        [
            _item("h-1", ["t1", "t2"], aligned_text),  # aligns to c-1
            _item("h-2", ["t99"], "unrelated lunch break logistics chatter"),
        ]
    )
    cmp = compare_extractions(
        ceiling_artifact=ceiling,
        haiku_artifact=haiku,
        alignment_contract_version=contract_version(),
    )
    m = cmp.payload["per_type_metrics"]["decision"]
    assert m["recall"] == 1 / 3
    assert m["precision"] == 1 / 2
    assert m["false_negatives"] == 2
    assert m["false_positives"] == 1
    assert m["true_positives"] == 1


def test_determinism_byte_identical():
    ceiling = _ceiling([_item("c-1", ["t1"], "approved the threshold")])
    haiku = _ceiling([_item("h-1", ["t1"], "approved the threshold")])
    a = compare_extractions(
        ceiling_artifact=ceiling,
        haiku_artifact=haiku,
        alignment_contract_version=contract_version(),
    )
    b = compare_extractions(
        ceiling_artifact=ceiling,
        haiku_artifact=haiku,
        alignment_contract_version=contract_version(),
    )
    assert canonical_json(a.payload) == canonical_json(b.payload)


def test_rejection_contract_version_mismatch_blocks():
    ceiling = _ceiling([_item("c-1", ["t1"], "approved the threshold")])
    haiku = _ceiling([_item("h-1", ["t1"], "approved the threshold")])
    with pytest.raises(AlignmentContractError) as exc:
        compare_extractions(
            ceiling_artifact=ceiling,
            haiku_artifact=haiku,
            alignment_contract_version="9.9.9",
        )
    assert exc.value.reason_code == "alignment_contract_version_mismatch"
