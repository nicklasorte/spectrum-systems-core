"""within_source is warn-only GLOBALLY (measurement instrument).

``extraction_within_source_required`` flags an extracted item that is
not a verbatim (normalized) substring of the transcript. That signal
is a MEASUREMENT INSTRUMENT that feeds the correction miner, NOT a
trust gate. It must therefore never hard-block promotion, for ANY
type: a single paraphrased item must not block a 138-chunk run and
starve the miner of the other grounded entries.

These tests prove, end to end:

* A within_source miss demotes to WARN for EVERY type (decisions,
  risks, action_items, commitments, …) — there is no hard-block lane.
* A WARN does not block promotion; its codes land on the promoted
  artifact's provenance and on the control_decision.
* The WARN row is present in eval_history.jsonl (correction-miner
  readable: a row whose ``status == "warn"`` and whose reason codes
  carry the ``within_source_warn`` prefix).
* ``WITHIN_SOURCE_HARD_BLOCK_TYPES`` is empty — no type hard-blocks.
* The OTHER gates are unchanged: ``regulatory_verb`` and
  ``llm_extraction_strict_schema`` still HARD-BLOCK; only within_source
  warns. ``route_within_source_eval`` only ever demotes the
  within_source eval, never another eval.
* Even a mixed / unparseable within_source fail still demotes (the
  instrument never blocks) and its codes are logged, never dropped.
"""
from __future__ import annotations

import json

import pytest

from spectrum_systems_core.artifacts import new_artifact
from spectrum_systems_core.control import decide_control
from spectrum_systems_core.evals import (
    EXTRACTION_NOT_IN_SOURCE,
    HIGH_STAKES_TYPES,
    REGULATORY_VERB_EVAL_TYPE,
    STANDARD_TYPES,
    STRICT_SCHEMA_EVAL_TYPE,
    WITHIN_SOURCE_EVAL_TYPE,
    WITHIN_SOURCE_HARD_BLOCK_TYPES,
    WITHIN_SOURCE_WARN_PREFIX,
    route_within_source_eval,
    route_within_source_result,
    run_llm_strict_schema_eval,
    run_llm_within_source_eval,
    run_regulatory_verb_eval,
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
# fails on this item.
_PARAPHRASE_ACTION = (
    "DoD agreed to send updated ERP figures ahead of the following meeting."
)

# A fabricated DECISION absent from the transcript. Its verb
# ("approved") IS a classified regulatory verb and the string is
# schema-valid, so the ONLY eval that flags it is within_source — which
# now warns. This is the exact case that used to hard-block a whole run
# on one item.
_FABRICATED_DECISION = "The committee approved a brand new unrelated budget line."


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
# Mission Step 3 — required tests
# ---------------------------------------------------------------------


def test_decisions_within_source_now_warns():
    """A decisions item whose text is NOT verbatim in the transcript
    used to hard-block. It now demotes to ``warn`` like every other
    type — within_source is a measurement instrument, not a trust
    gate."""
    art = _mk(
        _base_payload(
            decisions=["A decision that never appears verbatim anywhere."]
        )
    )
    res = run_llm_within_source_eval(art, "totally unrelated transcript body")
    assert res.payload["status"] == "fail"

    routed = route_within_source_result(res, "decisions")
    assert routed.payload["status"] == "warn"
    assert any(
        rc.startswith(WITHIN_SOURCE_WARN_PREFIX)
        for rc in routed.payload["reason_codes"]
    )
    # The block-causing prefix is gone — it is logged, not blocking.
    assert not any(
        rc.startswith(EXTRACTION_NOT_IN_SOURCE)
        for rc in routed.payload["reason_codes"]
    )
    # A NEW envelope — the original failed result is not mutated.
    assert res.payload["status"] == "fail"
    assert routed.artifact_id != res.artifact_id
    # The real pipeline path (lane derived internally) also warns.
    assert route_within_source_eval(res).payload["status"] == "warn"


@pytest.mark.parametrize(
    "key,item",
    [
        ("decisions", "A decision absent from the body entirely."),
        ("risks", {"risk_text": "A risk absent from the body entirely."}),
        ("action_items", "An action item absent from the body entirely."),
        (
            "commitments",
            {"commitment_text": "A commitment absent from the body."},
        ),
    ],
)
def test_all_types_within_source_warn(key, item):
    """For decisions, risks, action_items and commitments alike, a
    within_source miss demotes to ``warn`` — there is no hard-block
    lane left. Both the spec primitive (explicit item_type) and the
    real pipeline path agree."""
    art = _mk(_base_payload(**{key: [item]}))
    res = run_llm_within_source_eval(art, "totally unrelated transcript body")
    assert res.payload["status"] == "fail"
    # The miss is tagged with this array key so it is measured, not
    # silently dropped.
    assert any(
        rc.startswith(f"{EXTRACTION_NOT_IN_SOURCE}:{key}:")
        for rc in res.payload["reason_codes"]
    )

    routed = route_within_source_result(res, key)
    assert routed.payload["status"] == "warn"
    assert routed.payload["within_source_demoted"] is True
    assert any(
        rc.startswith(WITHIN_SOURCE_WARN_PREFIX)
        for rc in routed.payload["reason_codes"]
    )

    derived = route_within_source_eval(res)
    assert derived.payload["status"] == "warn"


def test_other_gates_unchanged():
    """within_source is the ONLY gate that warns. ``regulatory_verb``
    and ``llm_extraction_strict_schema`` still HARD-BLOCK, and
    ``route_within_source_eval`` refuses to demote any non-within_source
    eval (defence in depth)."""
    transcript = "totally unrelated transcript body"

    # 1. regulatory_verb still HARD-BLOCKS on a decision whose verb is
    #    not in the taxonomy. It is NOT demoted to warn.
    rv_art = _mk(
        _base_payload(
            decisions=[
                {"text": "The board frobnicated the rule.", "verb": "frobnicated"}
            ]
        )
    )
    rv = run_regulatory_verb_eval(rv_art)
    assert rv.payload["status"] == "fail"
    assert rv.payload["eval_type"] == REGULATORY_VERB_EVAL_TYPE
    # The within_source router must never touch another eval.
    assert route_within_source_eval(rv).payload["status"] == "fail"
    rv_decision = decide_control(rv_art, [rv])
    assert rv_decision.payload["decision"] == "block"
    assert (
        f"failed:{REGULATORY_VERB_EVAL_TYPE}"
        in rv_decision.payload["reason_codes"]
    )

    # 2. strict_schema still HARD-BLOCKS on a schema violation. It is
    #    NOT demoted to warn.
    ss_art = _mk(
        _base_payload(
            schema_version="1.2.0",
            meeting_phases=[{"phase_id": "p1", "phase_name": "lunch"}],
        )
    )
    ss = run_llm_strict_schema_eval(ss_art)
    assert ss.payload["status"] == "fail"
    assert ss.payload["eval_type"] == STRICT_SCHEMA_EVAL_TYPE
    assert route_within_source_eval(ss).payload["status"] == "fail"
    ss_decision = decide_control(ss_art, [ss])
    assert ss_decision.payload["decision"] == "block"
    assert (
        f"failed:{STRICT_SCHEMA_EVAL_TYPE}"
        in ss_decision.payload["reason_codes"]
    )

    # 3. within_source is the ONLY gate that warns — a decisions miss
    #    alone leads to ``allow``.
    ws_art = _mk(_base_payload(decisions=["A decision absent from the body."]))
    ws = route_within_source_eval(
        run_llm_within_source_eval(ws_art, transcript)
    )
    assert ws.payload["status"] == "warn"
    ws_decision = decide_control(ws_art, [ws])
    assert ws_decision.payload["decision"] == "allow"
    assert ws_decision.payload["reason_codes"] == []
    assert ws_decision.payload["within_source_warnings"]


# ---------------------------------------------------------------------
# Hard-block set is empty; lane sets unchanged
# ---------------------------------------------------------------------


def test_within_source_hard_block_set_is_empty():
    """No type hard-blocks within_source. The lane sets that drive
    WHICH evals run (regulatory_verb / nonempty are HIGH_STAKES-only)
    are deliberately UNCHANGED — only the within_source pass/fail
    semantics changed."""
    assert WITHIN_SOURCE_HARD_BLOCK_TYPES == frozenset()
    assert "decisions" not in WITHIN_SOURCE_HARD_BLOCK_TYPES
    assert "risks" not in WITHIN_SOURCE_HARD_BLOCK_TYPES
    # The routing lanes themselves are untouched (other gates depend on
    # them): decisions is still HIGH_STAKES, risks still STANDARD.
    assert "decisions" in HIGH_STAKES_TYPES
    assert "risks" in STANDARD_TYPES
    assert not (HIGH_STAKES_TYPES & STANDARD_TYPES)


# ---------------------------------------------------------------------
# Full workflow path — warn promotes, codes land on provenance
# ---------------------------------------------------------------------


def test_standard_within_source_demoted_to_warn():
    """An action_item paraphrase -> route demotes to ``warn`` and
    rewrites the codes to ``within_source_warn``; a NEW envelope is
    returned (the original failed result is not mutated)."""
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
    assert not any(
        rc.startswith(EXTRACTION_NOT_IN_SOURCE)
        for rc in routed.payload["reason_codes"]
    )
    assert res.payload["status"] == "fail"
    assert routed.artifact_id != res.artifact_id


def test_risks_demoted_to_warn():
    """A risk (STANDARD, object-form ``risk_text``) whose text is not
    verbatim routes to ``warn`` on both the spec-primitive and the
    real pipeline path."""
    art = _mk(
        _base_payload(
            risks=[
                {"risk_text": "A risk that never appears in the body at all."}
            ]
        )
    )
    res = run_llm_within_source_eval(art, "totally unrelated transcript body")
    assert res.payload["status"] == "fail"
    assert any(
        rc.startswith(f"{EXTRACTION_NOT_IN_SOURCE}:risks:")
        for rc in res.payload["reason_codes"]
    )

    routed = route_within_source_result(res, "risks")
    assert routed.payload["status"] == "warn"
    assert any(
        rc.startswith(WITHIN_SOURCE_WARN_PREFIX)
        for rc in routed.payload["reason_codes"]
    )
    assert route_within_source_eval(res).payload["status"] == "warn"


def test_warn_does_not_block_promotion():
    """Full eval path: a paraphrased action_item promotes, and the warn
    codes land on the promoted artifact's provenance and the
    control_decision."""
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
    assert (
        result.control_decision.payload["within_source_warnings"] == warnings
    )


def test_fabricated_decision_now_promotes_with_warn():
    """The exact case that used to hard-block a whole run: a fabricated
    DECISION absent from the transcript whose verb is a real regulatory
    verb. Only within_source flags it, so it now PROMOTES with the miss
    logged as a warn (the 138-chunk-run-unblocking outcome)."""
    assert _FABRICATED_DECISION not in DEC18
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            decisions=[_FABRICATED_DECISION],
            action_items=["DoD will submit revised ERP values."],
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
        and "decisions" in rc
        for rc in within["reason_codes"]
    )
    # The block-causing prefix is gone — logged, not blocking.
    assert not any(
        rc.startswith(EXTRACTION_NOT_IN_SOURCE)
        for rc in within["reason_codes"]
    )
    warnings = result.meeting_minutes.payload["provenance"][
        "within_source_warnings"
    ]
    assert warnings and all(
        w.startswith(WITHIN_SOURCE_WARN_PREFIX) for w in warnings
    )


def test_warn_appears_in_eval_history(tmp_path):
    """After a warn promote, the eval_history projection carries a row
    the correction miner can read: ``status == "warn"`` and a reason
    code with the ``within_source_warn`` prefix. The within_source eval
    still RAN (it is the within_source eval_type, demoted — not a
    removed / renamed eval)."""
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
    assert warn_rows[0]["eval_type"] == WITHIN_SOURCE_EVAL_TYPE


# ---------------------------------------------------------------------
# The instrument NEVER blocks — mixed / unparseable still warn
# ---------------------------------------------------------------------


def test_mixed_high_and_standard_within_source_warns():
    """A result that fails on BOTH a decision (was HIGH_STAKES) and an
    action_item (STANDARD) now demotes to ``warn`` — there is no
    hard-block lane, so a mixed miss is logged, never blocked."""
    art = _mk(
        _base_payload(
            decisions=["A fabricated decision absent from the body."],
            action_items=["A fabricated action absent from the body."],
        )
    )
    res = run_llm_within_source_eval(art, "totally unrelated transcript body")
    assert res.payload["status"] == "fail"

    routed = route_within_source_eval(res)
    assert routed.payload["status"] == "warn"
    # Every miss is logged with the warn prefix; none is dropped.
    assert any(
        rc.startswith(WITHIN_SOURCE_WARN_PREFIX)
        for rc in routed.payload["reason_codes"]
    )
    assert not any(
        rc.startswith(EXTRACTION_NOT_IN_SOURCE)
        for rc in routed.payload["reason_codes"]
    )


def test_unparseable_within_source_codes_still_warn():
    """A within_source fail whose codes cannot be parsed back to a type
    still demotes to ``warn`` (the instrument never blocks) and the
    original code is preserved in the payload, not dropped — the
    correction miner must still see it."""
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
    assert routed.payload["status"] == "warn"
    # The code had no EXTRACTION_NOT_IN_SOURCE prefix to rewrite, so it
    # is preserved verbatim — logged, never silently dropped.
    assert "garbage_without_a_known_prefix" in routed.payload["reason_codes"]
    assert routed.payload["within_source_demoted"] is True


def test_non_within_source_eval_is_never_demoted():
    """``route_within_source_eval`` only ever demotes the within_source
    eval. A failing eval of any other type is returned untouched so a
    caller cannot accidentally soften another gate."""
    other = new_artifact(
        artifact_type="eval_result",
        payload={
            "eval_type": STRICT_SCHEMA_EVAL_TYPE,
            "status": "fail",
            "score": 0.0,
            "reason_codes": ["schema_violation:boom"],
        },
        trace_id="t",
        status="evaluated",
    )
    routed = route_within_source_eval(other)
    assert routed is other
    assert routed.payload["status"] == "fail"


# ---------------------------------------------------------------------
# decide_control treats warn as non-blocking (unchanged)
# ---------------------------------------------------------------------


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
            "reason_codes": [f"{WITHIN_SOURCE_WARN_PREFIX}:decisions:foo"],
        },
        trace_id="t",
        status="evaluated",
    )
    decision = decide_control(target, [passing, warn])
    assert decision.payload["decision"] == "allow"
    assert decision.payload["reason_codes"] == []
    assert decision.payload["within_source_warnings"] == [
        f"{WITHIN_SOURCE_WARN_PREFIX}:decisions:foo"
    ]


def test_fail_still_blocks_alongside_warn():
    """A real ``fail`` (strict_schema) blocks even when a within_source
    ``warn`` is also present — demotion never rescues an unrelated hard
    failure, and the warn is still recorded on the blocked run."""
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
    assert decision.payload["within_source_warnings"] == [
        f"{WITHIN_SOURCE_WARN_PREFIX}:topics:foo"
    ]
