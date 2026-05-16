"""Steps 3-6 gates: hallucination defense, non-empty, within-source,
GT-coverage. Each gate has a happy path AND a rejection path that
proves it fails closed (eval fail -> reason code -> control block ->
unpromoted artifact).
"""
from __future__ import annotations

from spectrum_systems_core.evals import (
    EXTRACTION_EMPTY_WITH_CONTENT,
    EXTRACTION_NOT_IN_SOURCE,
    GT_COVERAGE_EVAL_TYPE,
    NONEMPTY_EVAL_TYPE,
    SCHEMA_VIOLATION,
    STRICT_SCHEMA_EVAL_TYPE,
    WITHIN_SOURCE_EVAL_TYPE,
)
from spectrum_systems_core.workflows import run_meeting_minutes_llm_workflow
from tests.llm_stub import (
    DEC18_ACTION_ITEMS,
    DEC18_DECISIONS,
    DEC18_OPEN_QUESTIONS,
    json_stub,
    load_fixture,
    text_stub,
)

DEC18 = load_fixture("dec18_transcript.txt")
PROCEDURAL = load_fixture("procedural_only.txt")


def _eval(result, eval_type):
    matches = [
        e for e in result.eval_results if e.payload.get("eval_type") == eval_type
    ]
    assert len(matches) == 1, f"expected exactly one {eval_type} eval_result"
    return matches[0].payload


def _decision(result) -> str:
    return result.control_decision.payload["decision"]


# ---- Step 3: hallucination defense (empty-content -> no invented items) --


def test_step3_procedural_only_yields_empty_decisions_and_promotes():
    result = run_meeting_minutes_llm_workflow(
        PROCEDURAL,
        client=json_stub(),  # model correctly returns all-empty
    )
    assert result.meeting_minutes.payload["decisions"] == []
    assert result.meeting_minutes.payload["action_items"] == []
    assert result.meeting_minutes.payload["open_questions"] == []
    # No content present -> empty is allowed -> promoted, nothing invented.
    assert result.promoted is True
    assert _decision(result) == "allow"


# ---- Step 4: non-empty extraction required when content present ---------


def test_step4_happy_content_with_items_passes():
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            decisions=DEC18_DECISIONS,
            action_items=DEC18_ACTION_ITEMS,
            open_questions=DEC18_OPEN_QUESTIONS,
        ),
    )
    nonempty = _eval(result, NONEMPTY_EVAL_TYPE)
    assert nonempty["status"] == "pass"
    assert result.promoted is True


def test_step4_rejection_forced_empty_on_content_blocks():
    # Dec 18 has known content; a forced-empty extraction must block.
    result = run_meeting_minutes_llm_workflow(DEC18, client=json_stub())
    nonempty = _eval(result, NONEMPTY_EVAL_TYPE)
    assert nonempty["status"] == "fail"
    assert EXTRACTION_EMPTY_WITH_CONTENT in nonempty["reason_codes"]
    assert _decision(result) == "block"
    assert result.promoted is False
    assert result.meeting_minutes.status == "rejected"


# ---- Step 5: within-source attribution ---------------------------------


def test_step5_happy_all_items_in_source():
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            decisions=DEC18_DECISIONS,
            action_items=DEC18_ACTION_ITEMS,
            open_questions=DEC18_OPEN_QUESTIONS,
        ),
    )
    within = _eval(result, WITHIN_SOURCE_EVAL_TYPE)
    assert within["status"] == "pass"
    assert within["items_in_source"] == 4
    assert within["items_not_in_source"] == 0


def test_step5_rejection_injected_non_source_decision_blocks():
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            decisions=[
                "The committee approved a brand new unrelated budget line."
            ],
        ),
    )
    within = _eval(result, WITHIN_SOURCE_EVAL_TYPE)
    assert within["status"] == "fail"
    assert within["items_not_in_source"] >= 1
    assert any(
        rc.startswith(EXTRACTION_NOT_IN_SOURCE)
        for rc in within["reason_codes"]
    )
    assert _decision(result) == "block"
    assert result.promoted is False


# ---- Step 6: coverage vs human GT pairs (observe-only) ------------------


def _seed_gt_pairs(lake_root, source_id, source_artifact_id, texts_types):
    from tests.integration.fixtures import make_human_minutes_gt_pair

    out = (
        lake_root
        / "store"
        / "processed"
        / "meetings"
        / source_id
        / "ground_truth"
        / "human_minutes_gt_pairs.jsonl"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    import json as _json

    lines = []
    for text, etype in texts_types:
        pair = make_human_minutes_gt_pair(
            source_id=source_id,
            source_artifact_id=source_artifact_id,
            ground_truth_text=text,
            extraction_type=etype,
        )
        lines.append(_json.dumps(pair, sort_keys=True, separators=(",", ":")))
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_step6_coverage_emits_numeric_float_and_threshold(tmp_path):
    source_id = "7-ghz-downlink-tig-meeting-kickoff---transcript-20251218"
    _seed_gt_pairs(
        tmp_path,
        source_id,
        "11111111-1111-4111-8111-111111111111",
        [
            (DEC18_DECISIONS[0], "decision"),
            ("an unrelated claim never extracted", "claim"),
        ],
    )
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            decisions=DEC18_DECISIONS,
            action_items=DEC18_ACTION_ITEMS,
            open_questions=DEC18_OPEN_QUESTIONS,
        ),
        source_id=source_id,
        lake_root=tmp_path,
    )
    cov = _eval(result, GT_COVERAGE_EVAL_TYPE)
    assert isinstance(cov["coverage_percent"], float)
    assert isinstance(cov["threshold"], float)
    assert cov["threshold"] == 0.0
    # one decision pair covered, one claim pair unmatched -> 0.5
    assert cov["coverage_percent"] == 0.5
    assert cov["status"] == "pass"  # observe-only never blocks
    # Threshold echoed into reason_codes for eval_history auditability.
    assert "coverage_threshold:0.0" in cov["reason_codes"]
    # Observe-only: the overall run is still allowed.
    assert _decision(result) == "allow"
    assert result.promoted is True


def test_step6_no_gt_file_still_numeric_and_passes():
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            decisions=DEC18_DECISIONS,
            action_items=DEC18_ACTION_ITEMS,
            open_questions=DEC18_OPEN_QUESTIONS,
        ),
        source_id="missing",
        lake_root=None,
    )
    cov = _eval(result, GT_COVERAGE_EVAL_TYPE)
    assert isinstance(cov["coverage_percent"], float)
    assert cov["coverage_percent"] == 0.0
    assert cov["status"] == "pass"


# ---- Step 2 strict schema (rejection here; happy path in contract test) -


def test_step2_rejection_malformed_raw_string_blocks():
    result = run_meeting_minutes_llm_workflow(
        DEC18, client=text_stub("this is not json at all")
    )
    strict = _eval(result, STRICT_SCHEMA_EVAL_TYPE)
    assert strict["status"] == "fail"
    assert any(
        rc.startswith(SCHEMA_VIOLATION) for rc in strict["reason_codes"]
    )
    assert _decision(result) == "block"
    assert result.promoted is False
    assert result.meeting_minutes.status == "rejected"


def test_step2_rejection_json_string_not_object_blocks():
    result = run_meeting_minutes_llm_workflow(
        DEC18, client=text_stub('"a bare json string"')
    )
    strict = _eval(result, STRICT_SCHEMA_EVAL_TYPE)
    assert strict["status"] == "fail"
    assert _decision(result) == "block"


def test_step2_rejection_missing_one_array_blocks():
    # Valid object but missing open_questions -> schema_violation.
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=text_stub('{"decisions": [], "action_items": []}'),
    )
    strict = _eval(result, STRICT_SCHEMA_EVAL_TYPE)
    assert strict["status"] == "fail"
    assert any(
        "missing_array" in rc for rc in strict["reason_codes"]
    )
    assert _decision(result) == "block"
    assert result.promoted is False
