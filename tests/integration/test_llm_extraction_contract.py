"""Step 2 gate (happy + rejection) and mutual-exclusion dispatch.

Integration-level: the artifact is produced by the REAL workflow /
dispatch (not a hand-rolled dict), written to a real temp lake, and
read back. This is the contract that would catch a writer/reader drift
between the LLM payload shape and the data-lake writer.
"""
from __future__ import annotations

import json

import pytest

from spectrum_systems_core.config import LLMConfigError
from spectrum_systems_core.data_lake import write_promoted_artifact
from spectrum_systems_core.workflows import (
    WorkflowDispatchError,
    run_meeting_minutes_dispatch,
    run_meeting_minutes_llm_workflow,
)
from tests.llm_stub import (
    DEC18_ACTION_ITEMS,
    DEC18_DECISIONS,
    DEC18_OPEN_QUESTIONS,
    DEC18_TECHNICAL_PARAMETERS,
    SpyStub,
    json_stub,
    load_fixture,
    text_stub,
)

DEC18 = load_fixture("dec18_transcript.txt")


def _real_stub():
    return json_stub(
        decisions=DEC18_DECISIONS,
        action_items=DEC18_ACTION_ITEMS,
        open_questions=DEC18_OPEN_QUESTIONS,
        technical_parameters=DEC18_TECHNICAL_PARAMETERS,
    )


# ---- Step 2 happy: promoted LLM artifact with correct provenance -------


def test_step2_happy_promoted_with_llm_provenance(tmp_path):
    result = run_meeting_minutes_llm_workflow(
        DEC18, client=_real_stub(), meeting_id="m-dec18-llm"
    )
    art = result.meeting_minutes
    assert result.promoted is True
    assert art.status == "promoted"
    assert art.artifact_type == "meeting_minutes"  # never artifact_kind
    assert art.payload["provenance"]["produced_by"] == "meeting_minutes_llm"
    assert art.payload["decisions"] == DEC18_DECISIONS
    assert result.control_decision.payload["decision"] == "allow"

    # Reachable on disk via the real writer (writer/reader contract).
    written = write_promoted_artifact(tmp_path, art)
    body = json.loads(written.read_text(encoding="utf-8"))
    assert body["payload"]["provenance"]["produced_by"] == "meeting_minutes_llm"


# ---- Step 2 rejection: malformed output fails closed -------------------


def test_step2_rejection_malformed_blocks_and_is_not_written(tmp_path):
    from spectrum_systems_core.data_lake.writer import WriterError

    result = run_meeting_minutes_llm_workflow(
        DEC18, client=text_stub("<<not json>>"), meeting_id="m-bad"
    )
    assert result.promoted is False
    assert result.meeting_minutes.status == "rejected"
    assert result.control_decision.payload["decision"] == "block"
    # The promotion gate is the only path to disk; a blocked artifact
    # cannot be written as a product.
    with pytest.raises(WriterError):
        write_promoted_artifact(tmp_path, result.meeting_minutes)


# ---- Mutual exclusion: exactly one extractor per transcript ------------


def test_dispatch_flag_off_runs_regex_arm_only():
    spy = SpyStub('{"decisions": [], "action_items": [], "open_questions": []}')
    result = run_meeting_minutes_dispatch(
        DEC18, llm_enabled=False, client=spy, meeting_id="m-regex"
    )
    # Regex arm ran: provenance says so AND the LLM client was untouched.
    assert result.meeting_minutes.payload["provenance"]["produced_by"] == (
        "meeting_minutes"
    )
    assert spy.calls == 0
    assert result.promoted is True


def test_dispatch_flag_on_runs_llm_arm_only():
    # SpyStub returns a FIXED string (it cannot see the turn block), so
    # it must carry its own grounding. t0000 is always a real chunk for
    # any non-empty transcript, so this attributes every item without
    # the stub needing to read the prompt — the Phase Y gates pass and
    # this stays a dispatch-wiring test, not a grounding test.
    spy = SpyStub(
        json.dumps(
            {
                "decisions": DEC18_DECISIONS,
                "action_items": DEC18_ACTION_ITEMS,
                "open_questions": DEC18_OPEN_QUESTIONS,
                "technical_parameters": DEC18_TECHNICAL_PARAMETERS,
                "grounding": [
                    {
                        "kind": "decision",
                        "text": DEC18_DECISIONS[0],
                        "source_turns": ["t0000"],
                    }
                ],
            }
        )
    )
    result = run_meeting_minutes_dispatch(
        DEC18,
        llm_enabled=True,
        client=spy,
        meeting_id="m-llm",
        env={"ANTHROPIC_API_KEY": "sk-test"},
    )
    assert result.meeting_minutes.payload["provenance"]["produced_by"] == (
        "meeting_minutes_llm"
    )
    assert spy.calls == 1
    assert result.promoted is True


def test_dispatch_flag_on_missing_key_halts_pre_run_no_artifact():
    spy = SpyStub("{}")
    with pytest.raises(LLMConfigError) as excinfo:
        run_meeting_minutes_dispatch(
            DEC18, llm_enabled=True, client=spy, env={}
        )
    assert excinfo.value.reason_code == "config_error"
    # Proven pre-run: the client was never reached, no artifact produced.
    assert spy.calls == 0


def test_dispatch_non_bool_flag_is_rejected_pre_run():
    with pytest.raises(WorkflowDispatchError):
        run_meeting_minutes_dispatch(
            DEC18, llm_enabled="true", client=json_stub()  # type: ignore[arg-type]
        )
