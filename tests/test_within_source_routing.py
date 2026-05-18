"""within_source BLOCK→WARN demotion routing.

The ``extraction_within_source_required`` eval blocks promotion when an
extracted item is not a verbatim (normalized) substring of the
transcript. For the accountability-bearing HIGH_STAKES lane that hard
block is correct and unchanged. For the high-volume STANDARD descriptive
lane a miss is usually slight model paraphrase / transcript-encoding
noise, not a trust failure — so a STANDARD-only miss is DEMOTED to a
logged warn (promote, but record it) while HIGH_STAKES still hard
blocks.

These tests prove, end to end:

* HIGH_STAKES within_source miss still BLOCKS (hard block preserved).
* STANDARD-only within_source miss demotes to WARN.
* A WARN does not block promotion; its codes land on the promoted
  artifact's provenance.
* A HIGH_STAKES fabrication still blocks promotion with the original
  (non-rewritten) ``extraction_not_in_source`` reason code.
* The WARN row is present in eval_history.jsonl (correction-miner
  readable: a row whose ``status == "warn"`` and whose reason codes
  carry the ``within_source_warn:`` prefix).
* The hard-block set can never drift from HIGH_STAKES_TYPES.
* Fail-closed: a mixed (HIGH_STAKES + STANDARD) or unparseable miss is
  NEVER demoted out of the block; a WARN status is never a blocking
  code.
"""
from __future__ import annotations

import json

from spectrum_systems_core.artifacts import new_artifact
from spectrum_systems_core.control import decide_control
from spectrum_systems_core.evals import (
    EXTRACTION_NOT_IN_SOURCE,
    HIGH_STAKES_TYPES,
    STANDARD_TYPES,
    WITHIN_SOURCE_EVAL_TYPE,
    WITHIN_SOURCE_HARD_BLOCK_TYPES,
    WITHIN_SOURCE_WARN_PREFIX,
    route_within_source_eval,
    route_within_source_result,
    run_llm_within_source_eval,
)
from spectrum_systems_core.workflows import run_meeting_minutes_llm_workflow
from spectrum_systems_core.workflows.llm_eval_history import (
    build_eval_records,
    write_eval_history,
)
from tests.llm_stub import (
    DEC18_DECISIONS,
    DEC18_OPEN_QUESTIONS,
    DEC18_TECHNICAL_PARAMETERS,
    json_stub,
    load_fixture,
)

DEC18 = load_fixture("dec18_transcript.txt")

# A genuine paraphrase of DEC18_ACTION_ITEMS[0] ("DoD will submit
# revised ERP values before the next session.") — semantically the same
# but not a normalized substring of the transcript, so within_source
# fails on this STANDARD-lane item.
_PARAPHRASE_ACTION = (
    "DoD agreed to send updated ERP figures ahead of the following meeting."
)


def _base_payload(**overrides) -> dict:
    payload = {
        "title": "T",
        "summary": "S",
        "decisions": [],
        "action_items": [],
        "open_questions": [],
        "schema_version": "1.0.0",
        "provenance": {"produced_by": "meeting_minutes_llm"},
    }
    payload.update(overrides)
    return payload


def _mk(payload: dict):
    return new_artifact(
        artifact_type="meeting_minutes",
        payload=payload,
        trace_id="trace-test",
        status="draft",
    )


def _within_eval(result):
    matches = [
        e
        for e in result.eval_results
        if e.payload.get("eval_type") == WITHIN_SOURCE_EVAL_TYPE
    ]
    assert len(matches) == 1, "expected exactly one within_source eval_result"
    return matches[0].payload


def _decision(result) -> str:
    return result.control_decision.payload["decision"]


# ---------------------------------------------------------------------
# Step 6 — required tests
# ---------------------------------------------------------------------


def test_high_stakes_within_source_still_blocks():
    """A decision (HIGH_STAKES) whose text is not in the transcript ->
    route returns it unchanged: status stays ``fail`` (hard block)."""
    art = _mk(
        _base_payload(
            decisions=["A decision that never appears anywhere in the body."]
        )
    )
    res = run_llm_within_source_eval(art, "totally unrelated transcript body")
    assert res.payload["status"] == "fail"

    routed = route_within_source_result(res, "decisions")
    assert routed.payload["status"] == "fail"
    # The original block-causing code is preserved verbatim (NOT
    # rewritten to within_source_warn).
    assert any(
        rc.startswith(EXTRACTION_NOT_IN_SOURCE)
        for rc in routed.payload["reason_codes"]
    )
    assert not any(
        rc.startswith(WITHIN_SOURCE_WARN_PREFIX)
        for rc in routed.payload["reason_codes"]
    )


def test_standard_within_source_demoted_to_warn():
    """An action_item (STANDARD) paraphrase -> route demotes the result
    to ``warn`` and rewrites the codes to ``within_source_warn``."""
    art = _mk(
        _base_payload(
            action_items=["A paraphrased action item not present at all."]
        )
    )
    res = run_llm_within_source_eval(art, "totally unrelated transcript body")
    assert res.payload["status"] == "fail"

    routed = route_within_source_result(res, "action_items")
    assert routed.payload["status"] == "warn"
    assert any(
        rc.startswith(WITHIN_SOURCE_WARN_PREFIX)
        for rc in routed.payload["reason_codes"]
    )
    # The block-causing prefix is gone (it is logged, not blocking).
    assert not any(
        rc.startswith(EXTRACTION_NOT_IN_SOURCE)
        for rc in routed.payload["reason_codes"]
    )
    # A new envelope is returned — the original failed result is not
    # mutated in place.
    assert res.payload["status"] == "fail"
    assert routed.artifact_id != res.artifact_id


def test_warn_does_not_block_promotion():
    """Full eval path: a STANDARD-lane paraphrase promotes, and the warn
    codes land on the promoted artifact's provenance."""
    assert _PARAPHRASE_ACTION not in DEC18  # genuinely not verbatim
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            decisions=[DEC18_DECISIONS[0]],
            action_items=[_PARAPHRASE_ACTION],
            open_questions=DEC18_OPEN_QUESTIONS,
            technical_parameters=DEC18_TECHNICAL_PARAMETERS,
        ),
    )
    assert result.promoted is True
    assert _decision(result) == "allow"

    within = _within_eval(result)
    assert within["status"] == "warn"
    assert any(
        rc.startswith(WITHIN_SOURCE_WARN_PREFIX)
        for rc in within["reason_codes"]
    )

    prov = result.meeting_minutes.payload["provenance"]
    warnings = prov.get("within_source_warnings")
    assert warnings, "within_source_warnings must be populated on promote"
    assert all(
        w.startswith(WITHIN_SOURCE_WARN_PREFIX) for w in warnings
    )
    # The control_decision carries the same warn codes (their
    # authoritative source).
    assert (
        result.control_decision.payload["within_source_warnings"] == warnings
    )


def test_high_stakes_fabrication_blocks_promotion():
    """Full eval path: a fabricated decision (HIGH_STAKES) still blocks
    with the original ``extraction_not_in_source`` reason code, and no
    within_source_warnings are written (nothing was promoted)."""
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            decisions=[
                "The committee approved a brand new unrelated budget line."
            ],
        ),
    )
    assert result.promoted is False
    assert _decision(result) == "block"

    within = _within_eval(result)
    assert within["status"] == "fail"
    assert any(
        rc.startswith(EXTRACTION_NOT_IN_SOURCE)
        for rc in within["reason_codes"]
    )
    # The block is attributed to within_source in the decision.
    assert (
        f"failed:{WITHIN_SOURCE_EVAL_TYPE}"
        in result.control_decision.payload["reason_codes"]
    )
    # A blocked run never stamps within_source_warnings on provenance.
    assert (
        "within_source_warnings"
        not in result.meeting_minutes.payload.get("provenance", {})
    )


def test_warn_appears_in_eval_history(tmp_path):
    """After a warn promote, the eval_history projection carries a row
    the correction miner can read: ``status == "warn"`` and a reason
    code with the ``within_source_warn:`` prefix."""
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            decisions=[DEC18_DECISIONS[0]],
            action_items=[_PARAPHRASE_ACTION],
            open_questions=DEC18_OPEN_QUESTIONS,
            technical_parameters=DEC18_TECHNICAL_PARAMETERS,
        ),
    )
    assert result.promoted is True

    records = build_eval_records(
        result, meeting_id="m-warn", workflow_name="meeting_minutes_llm"
    )
    path = write_eval_history(tmp_path, source_id="m-warn", records=records)

    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    warn_rows = [
        r
        for r in rows
        if r.get("status") == "warn"
        and any(
            isinstance(rc, str) and rc.startswith(WITHIN_SOURCE_WARN_PREFIX)
            for rc in r.get("reason_codes", [])
        )
    ]
    assert warn_rows, "no within_source_warn row in eval_history.jsonl"
    # The within_source eval still ran for all types — the row is the
    # within_source eval_type, demoted (not a removed / renamed eval).
    assert warn_rows[0]["eval_type"] == WITHIN_SOURCE_EVAL_TYPE


def test_hard_block_types_equals_high_stakes_types():
    assert WITHIN_SOURCE_HARD_BLOCK_TYPES == HIGH_STAKES_TYPES


# ---------------------------------------------------------------------
# risks demoted HIGH_STAKES → STANDARD (analytical, not binding)
# ---------------------------------------------------------------------


def test_risks_demoted_to_warn():
    """A risk (now STANDARD) whose text is not verbatim in the
    transcript routes to ``warn``: a risk is an analytical observation,
    not a binding commitment, so a paraphrase is logged not blocked.

    ``risks`` is a Step-4 structured array (text field ``risk_text``),
    so the within_source eval picks it up only as an object item — the
    same shape the real meeting_minutes_llm extraction emits.
    """
    art = _mk(
        _base_payload(
            risks=[
                {"risk_text": "A risk that never appears in the body at all."}
            ]
        )
    )
    res = run_llm_within_source_eval(art, "totally unrelated transcript body")
    assert res.payload["status"] == "fail"
    # Sanity: the miss is tagged with the ``risks`` array key so lane
    # derivation can route it.
    assert any(
        rc.startswith(f"{EXTRACTION_NOT_IN_SOURCE}:risks:")
        for rc in res.payload["reason_codes"]
    )

    # Spec primitive with the explicit item_type.
    routed = route_within_source_result(res, "risks")
    assert routed.payload["status"] == "warn"
    assert any(
        rc.startswith(WITHIN_SOURCE_WARN_PREFIX)
        for rc in routed.payload["reason_codes"]
    )
    # The block-causing prefix is gone (logged, not blocking).
    assert not any(
        rc.startswith(EXTRACTION_NOT_IN_SOURCE)
        for rc in routed.payload["reason_codes"]
    )
    # A new envelope — the original failed result is not mutated.
    assert res.payload["status"] == "fail"
    assert routed.artifact_id != res.artifact_id

    # The real pipeline path (lane derived from the result's own codes,
    # not an explicit item_type) also demotes — this is what unblocks
    # the full 138-chunk run.
    derived = route_within_source_eval(res)
    assert derived.payload["status"] == "warn"


def test_decisions_still_hard_blocks():
    """A decision (still HIGH_STAKES) whose text is not verbatim in the
    transcript routes UNCHANGED: status stays ``fail``. The decisions
    hard block is preserved exactly — demoting risks must not weaken
    it."""
    art = _mk(
        _base_payload(
            decisions=["A decision that never appears verbatim anywhere."]
        )
    )
    res = run_llm_within_source_eval(art, "totally unrelated transcript body")
    assert res.payload["status"] == "fail"

    routed = route_within_source_result(res, "decisions")
    assert routed.payload["status"] == "fail"
    # Original block-causing code preserved verbatim (NOT rewritten).
    assert any(
        rc.startswith(EXTRACTION_NOT_IN_SOURCE)
        for rc in routed.payload["reason_codes"]
    )
    assert not any(
        rc.startswith(WITHIN_SOURCE_WARN_PREFIX)
        for rc in routed.payload["reason_codes"]
    )
    # The derived-lane path also keeps the block.
    derived = route_within_source_eval(res)
    assert derived.payload["status"] == "fail"


def test_high_stakes_set_no_longer_contains_risks():
    """The type classification moved: risks left HIGH_STAKES for
    STANDARD; decisions stayed HIGH_STAKES; and the hard-block set
    (derived from HIGH_STAKES) therefore no longer contains risks."""
    assert "risks" not in HIGH_STAKES_TYPES
    assert "risks" in STANDARD_TYPES
    assert "decisions" in HIGH_STAKES_TYPES
    # The hard-block set is HIGH_STAKES verbatim, so it dropped risks
    # too while keeping decisions.
    assert "risks" not in WITHIN_SOURCE_HARD_BLOCK_TYPES
    assert "decisions" in WITHIN_SOURCE_HARD_BLOCK_TYPES


# ---------------------------------------------------------------------
# Embedded red-team hardening
# ---------------------------------------------------------------------


def test_mixed_high_and_standard_within_source_blocks():
    """A result that fails on BOTH a decision (HIGH_STAKES) and an
    action_item (STANDARD) must NOT demote — HIGH_STAKES wins, the
    combined result still hard-blocks."""
    art = _mk(
        _base_payload(
            decisions=["A fabricated decision absent from the body."],
            action_items=["A fabricated action absent from the body."],
        )
    )
    res = run_llm_within_source_eval(art, "totally unrelated transcript body")
    assert res.payload["status"] == "fail"

    routed = route_within_source_eval(res)
    assert routed.payload["status"] == "fail"
    assert any(
        rc.startswith(EXTRACTION_NOT_IN_SOURCE)
        for rc in routed.payload["reason_codes"]
    )


def test_unparseable_within_source_codes_fail_closed():
    """A within_source fail whose codes cannot be parsed back to a type
    must NOT be demoted (fail-closed)."""
    bogus = new_artifact(
        artifact_type="eval_result",
        payload={
            "eval_type": WITHIN_SOURCE_EVAL_TYPE,
            "status": "fail",
            "score": 0.0,
            "reason_codes": ["garbage_without_a_known_prefix"],
        },
        trace_id="t",
        status="evaluated",
    )
    routed = route_within_source_eval(bogus)
    assert routed.payload["status"] == "fail"


def test_warn_status_never_in_blocking_codes():
    """decide_control treats ``status == "warn"`` as non-blocking and
    surfaces the warn codes on the decision (never as a ``failed:``
    blocking code)."""
    target = _mk(_base_payload())
    passing = new_artifact(
        artifact_type="eval_result",
        payload={
            "eval_type": "some_pass",
            "status": "pass",
            "score": 1.0,
            "reason_codes": [],
        },
        trace_id="t",
        status="evaluated",
    )
    warn = new_artifact(
        artifact_type="eval_result",
        payload={
            "eval_type": WITHIN_SOURCE_EVAL_TYPE,
            "status": "warn",
            "score": 0.0,
            "reason_codes": [f"{WITHIN_SOURCE_WARN_PREFIX}:action_items:foo"],
        },
        trace_id="t",
        status="evaluated",
    )
    decision = decide_control(target, [passing, warn])
    assert decision.payload["decision"] == "allow"
    assert decision.payload["reason_codes"] == []
    assert decision.payload["within_source_warnings"] == [
        f"{WITHIN_SOURCE_WARN_PREFIX}:action_items:foo"
    ]


def test_fail_still_blocks_alongside_warn():
    """A real ``fail`` blocks even when a ``warn`` is also present —
    the demotion never rescues an unrelated hard failure."""
    target = _mk(_base_payload())
    hard_fail = new_artifact(
        artifact_type="eval_result",
        payload={
            "eval_type": "llm_extraction_strict_schema",
            "status": "fail",
            "score": 0.0,
            "reason_codes": ["schema_violation:boom"],
        },
        trace_id="t",
        status="evaluated",
    )
    warn = new_artifact(
        artifact_type="eval_result",
        payload={
            "eval_type": WITHIN_SOURCE_EVAL_TYPE,
            "status": "warn",
            "score": 0.0,
            "reason_codes": [f"{WITHIN_SOURCE_WARN_PREFIX}:topics:foo"],
        },
        trace_id="t",
        status="evaluated",
    )
    decision = decide_control(target, [hard_fail, warn])
    assert decision.payload["decision"] == "block"
    assert "failed:llm_extraction_strict_schema" in (
        decision.payload["reason_codes"]
    )
    # The warn is still recorded even on a blocked run (audit).
    assert decision.payload["within_source_warnings"] == [
        f"{WITHIN_SOURCE_WARN_PREFIX}:topics:foo"
    ]
