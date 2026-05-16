"""Step 7 gate: feature-flag rollback exercised in CI, both states.

Same Dec 18 transcript run twice through the single dispatch point:

  1. llm_enabled=True  -> meeting_minutes artifact,
     provenance.produced_by == "meeting_minutes_llm"
  2. llm_enabled=False -> meeting_minutes artifact,
     provenance.produced_by == "meeting_minutes" (regex)

Both must be promoted AND reachable from the data_lake query module.
This test IS the rollback path's exercise: if the flag wiring breaks in
either state, this fails in CI before merge, not after.
"""
from __future__ import annotations

import json

from spectrum_systems_core.data_lake import (
    query,
    write_artifact_index,
    write_promoted_artifact,
)
from spectrum_systems_core.workflows import run_meeting_minutes_dispatch
from spectrum_systems_core.workflows.llm_eval_history import (
    build_eval_records,
    write_eval_history,
)
from tests.llm_stub import (
    DEC18_ACTION_ITEMS,
    DEC18_DECISIONS,
    DEC18_OPEN_QUESTIONS,
    json_stub,
    load_fixture,
)

DEC18 = load_fixture("dec18_transcript.txt")


def _provenance_via_query(lake_root, meeting_id):
    hits = query(lake_root, meeting_id=meeting_id, artifact_type="meeting_minutes")
    assert len(hits) == 1, f"expected one indexed artifact for {meeting_id}"
    written = lake_root / hits[0].record["path"]
    body = json.loads(written.read_text(encoding="utf-8"))
    return body["payload"]["provenance"]["produced_by"]


def test_rollback_both_flag_states_promote_and_are_queryable(tmp_path):
    lake_root = tmp_path / "lake"

    # Arm 1: flag ON -> live-LLM arm (deterministic stub, key present).
    on = run_meeting_minutes_dispatch(
        DEC18,
        llm_enabled=True,
        client=json_stub(
            decisions=DEC18_DECISIONS,
            action_items=DEC18_ACTION_ITEMS,
            open_questions=DEC18_OPEN_QUESTIONS,
        ),
        meeting_id="m-dec18-llm",
        env={"ANTHROPIC_API_KEY": "sk-test"},
    )
    assert on.promoted is True
    assert on.meeting_minutes.payload["provenance"]["produced_by"] == (
        "meeting_minutes_llm"
    )

    # Arm 2: flag OFF -> regex arm. Same transcript, rollback path.
    off = run_meeting_minutes_dispatch(
        DEC18, llm_enabled=False, meeting_id="m-dec18-regex"
    )
    assert off.promoted is True
    assert off.meeting_minutes.payload["provenance"]["produced_by"] == (
        "meeting_minutes"
    )

    # Both reachable from the data_lake query module.
    write_promoted_artifact(lake_root, on.meeting_minutes)
    write_promoted_artifact(lake_root, off.meeting_minutes)
    write_artifact_index(lake_root)

    assert _provenance_via_query(lake_root, "m-dec18-llm") == (
        "meeting_minutes_llm"
    )
    assert _provenance_via_query(lake_root, "m-dec18-regex") == (
        "meeting_minutes"
    )


def test_llm_arm_eval_history_records_threshold_for_audit(tmp_path):
    """Step 6 auditability: the GT-coverage threshold is written into
    eval_history.jsonl (via reason_codes projection), shape-identical to
    the pipeline's eval_history.jsonl."""
    source_id = "7-ghz-downlink-tig-meeting-kickoff---transcript-20251218"
    result = run_meeting_minutes_dispatch(
        DEC18,
        llm_enabled=True,
        client=json_stub(
            decisions=DEC18_DECISIONS,
            action_items=DEC18_ACTION_ITEMS,
            open_questions=DEC18_OPEN_QUESTIONS,
        ),
        meeting_id=source_id,
        source_id=source_id,
        lake_root=tmp_path,
        env={"ANTHROPIC_API_KEY": "sk-test"},
    )
    records = build_eval_records(
        result, meeting_id=source_id, workflow_name="meeting_minutes_llm"
    )
    out = write_eval_history(tmp_path, source_id=source_id, records=records)
    lines = [
        json.loads(ln)
        for ln in out.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    cov = [
        r
        for r in lines
        if r["eval_type"] == "extraction_vs_human_minutes_coverage"
    ]
    assert len(cov) == 1
    assert "coverage_threshold:0.0" in cov[0]["reason_codes"]
    # Determinism: re-projecting + re-writing is byte-identical.
    records2 = build_eval_records(
        result, meeting_id=source_id, workflow_name="meeting_minutes_llm"
    )
    out2 = write_eval_history(tmp_path, source_id=source_id, records=records2)
    assert out.read_text(encoding="utf-8") == out2.read_text(encoding="utf-8")
