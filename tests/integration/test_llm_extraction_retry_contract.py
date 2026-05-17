"""Contract: the bounded, deterministic re-prompt on a malformed first
Haiku response.

Trust properties defended here (NOT ceremony):

* a malformed FIRST response that is corrected on the one retry
  promotes — the workflow gave the model a single bounded chance to
  self-correct;
* a PERSISTENTLY malformed response still blocks with the precise
  strict-schema reason — the fail-closed gate is NOT weakened by the
  retry (constitution §7: failed required evals block);
* a valid FIRST response makes exactly one model call and is
  byte-identical (same content_hash) to the pre-retry behaviour — the
  happy path did not move;
* a transport error is not retried (unchanged) and still blocks;
* the retry is bounded — a never-valid model cannot loop.

The artifact is produced by the REAL governed loop; only the transport
is a deterministic stub (no API key, no network).
"""
from __future__ import annotations

import json

from spectrum_systems_core.workflows import run_meeting_minutes_llm_workflow
from spectrum_systems_core.workflows.llm_client import LLMClientError
from spectrum_systems_core.workflows.meeting_minutes_llm import (
    _MAX_LLM_ATTEMPTS,
)
from tests.llm_stub import (
    DEC18_ACTION_ITEMS,
    DEC18_DECISIONS,
    DEC18_OPEN_QUESTIONS,
    DEC18_TECHNICAL_PARAMETERS,
    load_fixture,
    text_stub,
)

DEC18 = load_fixture("dec18_transcript.txt")

# Every grounded item cites t0001 — a real chunk turn_id for DEC18 (the
# chunker yields t0000/t0001), so source_turn_validity + grounding
# coverage pass and the ONLY variable under test is the schema gate.
_T = ["t0001"]


def _g(kind: str, text: str) -> dict:
    return {"kind": kind, "text": text, "source_turns": _T}


def _grounding() -> list[dict]:
    return [
        _g("decision", DEC18_DECISIONS[0]),
        _g("decision", DEC18_DECISIONS[1]),
        _g("action_item", DEC18_ACTION_ITEMS[0]),
        _g("open_question", DEC18_OPEN_QUESTIONS[0]),
        _g("technical_parameter", DEC18_TECHNICAL_PARAMETERS[0]["value"]),
    ]


def _valid_response() -> dict:
    return {
        "decisions": [
            {"text": DEC18_DECISIONS[0], "verb": "approved"},
            {"text": DEC18_DECISIONS[1], "verb": "deferred"},
        ],
        "action_items": list(DEC18_ACTION_ITEMS),
        "open_questions": list(DEC18_OPEN_QUESTIONS),
        "technical_parameters": list(DEC18_TECHNICAL_PARAMETERS),
        "grounding": _grounding(),
    }


def _malformed_1_3_0_response() -> dict:
    """Schema-valid shape EXCEPT a bad 1.3.0 enum (issue_type) — the
    realistic 'imperfect first Haiku run on the larger 1.3.0 prompt'
    case. The strict-schema eval rejects it fail-closed."""
    doc = _valid_response()
    doc["issue_registry_entry"] = [
        {
            "issue_id": "i1",
            "title": "Aggregate interference methodology",
            "description": (
                "DoD has a concern about the aggregate interference "
                "methodology and would like it revisited"
            ),
            "issue_type": "blah",  # not in the schema enum
            "raised_by": "DoD Rep",
            "status": "open",
            "resolution_summary": None,
            "related_decisions": [],
            "source_turns": _T,
        }
    ]
    doc["grounding"] = _grounding() + [
        _g("issue_registry_entry", "Aggregate interference methodology")
    ]
    return doc


class _SeqStub:
    """Returns each queued response once, then repeats the last. Records
    the call count so the retry budget is asserted, not assumed."""

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self.calls = 0

    def __call__(self, *, system: str, user: str) -> str:  # noqa: ARG002
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx]


def _decision(result) -> str:
    return result.control_decision.payload["decision"]


def test_malformed_first_then_valid_self_corrects_and_promotes():
    stub = _SeqStub(
        json.dumps(_malformed_1_3_0_response()),
        json.dumps(_valid_response()),
    )
    result = run_meeting_minutes_llm_workflow(
        DEC18, client=stub, meeting_id="dec18", source_id="dec18"
    )
    assert stub.calls == 2, "expected exactly one corrective retry"
    assert result.promoted is True
    assert _decision(result) == "allow"
    assert (
        result.meeting_minutes.payload["provenance"]["produced_by"]
        == "meeting_minutes_llm"
    )


def test_persistently_malformed_still_blocks_fail_closed():
    stub = _SeqStub(json.dumps(_malformed_1_3_0_response()))
    result = run_meeting_minutes_llm_workflow(
        DEC18, client=stub, meeting_id="dec18", source_id="dec18"
    )
    assert stub.calls == _MAX_LLM_ATTEMPTS, "retry must be bounded"
    assert result.promoted is False
    assert result.meeting_minutes.status == "rejected"
    assert _decision(result) == "block"
    codes = result.control_decision.payload["reason_codes"]
    # The precise strict-schema reason is preserved — the retry does not
    # mask or weaken the gate.
    assert any("llm_extraction_strict_schema" in c for c in codes), codes


def test_parse_miss_first_then_valid_promotes_on_retry():
    stub = _SeqStub("<<not json at all>>", json.dumps(_valid_response()))
    result = run_meeting_minutes_llm_workflow(
        DEC18, client=stub, meeting_id="dec18", source_id="dec18"
    )
    assert stub.calls == 2
    assert result.promoted is True
    assert _decision(result) == "allow"


def test_valid_first_is_single_call_and_byte_identical():
    seq = _SeqStub(json.dumps(_valid_response()))
    r_seq = run_meeting_minutes_llm_workflow(
        DEC18, client=seq, meeting_id="dec18", source_id="dec18"
    )
    assert seq.calls == 1, "a valid first response must not retry"
    assert r_seq.promoted is True

    # The happy path did not move: identical to a single-shot client
    # that never exercises the retry branch at all.
    r_oneshot = run_meeting_minutes_llm_workflow(
        DEC18,
        client=text_stub(json.dumps(_valid_response())),
        meeting_id="dec18",
        source_id="dec18",
    )
    assert (
        r_seq.meeting_minutes.content_hash
        == r_oneshot.meeting_minutes.content_hash
    ), "content_hash drifted on the happy path"


def test_transport_error_is_not_retried_and_blocks():
    class _Boom:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, *, system: str, user: str) -> str:  # noqa: ARG002
            self.calls += 1
            raise LLMClientError("boom")

    boom = _Boom()
    result = run_meeting_minutes_llm_workflow(
        DEC18, client=boom, meeting_id="dec18", source_id="dec18"
    )
    assert boom.calls == 1, "transport error must not be retried"
    assert result.promoted is False
    assert _decision(result) == "block"
