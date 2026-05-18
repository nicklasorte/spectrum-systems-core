"""Phase Y.5 — correction miner's three distinguishable outcomes."""
from __future__ import annotations

from spectrum_systems_core.artifacts import new_artifact
from spectrum_systems_core.extraction.correction_miner import (
    CLUSTER_THRESHOLD,
    mine_corrections,
)


def _fn_set(false_negatives):
    return new_artifact(
        artifact_type="false_negative_set",
        payload={
            "artifact_type": "false_negative_set",
            "schema_version": "1.0.0",
            "transcript_id": "m-y-miner",
            "comparison_artifact_id": "cmp-1",
            "false_negatives": false_negatives,
        },
        trace_id="trace-miner",
        status="draft",
    )


def _fn(cid):
    # Same schema_type, no speaker, same first token, same length
    # bucket -> identical pattern_signature -> one cluster.
    return {
        "schema_type": "decision",
        "ceiling_item_id": cid,
        "source_turn_ids": ["t" + cid],
        "source_text": "Approved item " + cid,
        "ceiling_payload": {},
    }


def test_outcome_one_cluster_produces_candidate_no_finding():
    fns = [_fn(str(i)) for i in range(CLUSTER_THRESHOLD + 2)]
    result = mine_corrections(
        _fn_set(fns),
        opus_call=lambda texts, prompt: "Always extract Approved-prefixed "
        "decisions even when terse.",
    )
    assert result.blocked is False
    assert len(result.candidates) == 1
    cand = result.candidates[0].payload
    assert cand["candidate_source"] == "miner"
    assert cand["schema_version"] == "1.1.0"
    assert cand["cluster_size"] == CLUSTER_THRESHOLD + 2
    assert cand["proposed_prompt_addition"].startswith("Always extract")
    # No info/halt findings on the success path.
    assert result.findings == []


def test_outcome_below_threshold_emits_info_finding():
    fns = [_fn(str(i)) for i in range(CLUSTER_THRESHOLD - 1)]
    result = mine_corrections(
        _fn_set(fns), opus_call=lambda t, p: "unused"
    )
    assert result.candidates == []
    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.severity == "info"
    assert f.code == "miner_no_clusters_above_threshold"
    assert f.context["fn_count"] == CLUSTER_THRESHOLD - 1
    assert f.context["threshold"] == CLUSTER_THRESHOLD


def test_outcome_opus_crash_emits_halt_and_blocks():
    fns = [_fn(str(i)) for i in range(CLUSTER_THRESHOLD + 1)]

    def _boom(_texts, _prompt):
        raise RuntimeError("opus 529 overloaded")

    result = mine_corrections(_fn_set(fns), opus_call=_boom)
    assert result.blocked is True
    assert result.candidates == []
    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.severity == "halt"
    assert f.code == "miner_failed"
    assert f.context["exception_class"] == "RuntimeError"
    assert "opus 529 overloaded" in f.context["message"]
