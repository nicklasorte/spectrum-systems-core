"""Contract: the producer-side ``source_turn_validity`` corrective
retry.

Trust properties defended here (NOT ceremony):

* a FIRST response whose grounding cites a fabricated turn_id is
  re-asked once with the precise invalid ids + the valid set fed back,
  and the corrected response promotes — extending the existing bounded
  retry pattern from schema rejection to source_turn_validity covers
  the intermittent turn-id hallucination observed on 138-chunk
  full-transcript runs;
* the retry budget is the SAME ``_MAX_LLM_ATTEMPTS`` already used for
  schema rejections — schema and source-turn issues share one bounded
  budget, so a persistently-bad model still cannot loop;
* a PERSISTENTLY invalid response still blocks fail-closed with the
  authoritative ``source_turn_validity`` reason codes preserved — the
  in-loop gate is NOT weakened by the producer-side pre-check;
* the retry prompt actually carries the valid turn-id list and the
  precise invalid turn-id(s) — the model gets concrete corrective
  information, not a generic "try again".

The artifact is produced by the REAL governed loop; only the transport
is a deterministic stub (no API key, no network).
"""
from __future__ import annotations

import json

from spectrum_systems_core.workflows import run_meeting_minutes_llm_workflow
from spectrum_systems_core.workflows.meeting_minutes_llm import (
    _MAX_LLM_ATTEMPTS,
)
from tests.llm_stub import (
    DEC18_ACTION_ITEMS,
    DEC18_DECISIONS,
    DEC18_OPEN_QUESTIONS,
    DEC18_TECHNICAL_PARAMETERS,
    load_fixture,
)

DEC18 = load_fixture("dec18_transcript.txt")

# DEC18's chunker yields exactly two turn_ids: t0000 (the title line)
# and t0001 (the meeting body). t0001 carries every decision / action
# item / open question, so a well-behaved model cites it.
_VALID_TURN = "t0001"
_FABRICATED_TURN = "t9999"


def _g(kind: str, text: str, *, turn_id: str) -> dict:
    return {"kind": kind, "text": text, "source_turns": [turn_id]}


def _grounding(turn_id: str) -> list[dict]:
    return [
        _g("decision", DEC18_DECISIONS[0], turn_id=turn_id),
        _g("decision", DEC18_DECISIONS[1], turn_id=turn_id),
        _g("action_item", DEC18_ACTION_ITEMS[0], turn_id=turn_id),
        _g("open_question", DEC18_OPEN_QUESTIONS[0], turn_id=turn_id),
        _g(
            "technical_parameter",
            DEC18_TECHNICAL_PARAMETERS[0]["value"],
            turn_id=turn_id,
        ),
    ]


def _response(turn_id: str) -> dict:
    """Full meeting_minutes content payload citing ``turn_id`` for every
    grounded item. With ``turn_id == _VALID_TURN`` the artifact passes
    every grounded eval; with ``turn_id == _FABRICATED_TURN`` the only
    failing eval is ``source_turn_validity``."""
    return {
        "decisions": [
            {"text": DEC18_DECISIONS[0], "verb": "approved"},
            {"text": DEC18_DECISIONS[1], "verb": "deferred"},
        ],
        "action_items": list(DEC18_ACTION_ITEMS),
        "open_questions": list(DEC18_OPEN_QUESTIONS),
        "technical_parameters": list(DEC18_TECHNICAL_PARAMETERS),
        "grounding": _grounding(turn_id),
    }


class _RecordingSeqStub:
    """Returns each queued response once, then repeats the last. Records
    every (system, user) call so the retry prompt contents can be
    asserted, not assumed."""

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, *, system: str, user: str) -> str:
        idx = min(len(self.calls), len(self._responses) - 1)
        self.calls.append((system, user))
        return self._responses[idx]


def _decision(result) -> str:
    return result.control_decision.payload["decision"]


def _evals(result) -> dict:
    out = {}
    for e in result.eval_results:
        p = e.payload if isinstance(e.payload, dict) else {}
        out[p.get("eval_type")] = (p.get("status"), p.get("reason_codes"))
    return out


def test_fabricated_turn_id_first_then_valid_self_corrects_and_promotes():
    """The mission's exact failure shape on a single batch. First
    response cites ``t9999`` (not in DEC18's chunks); the corrective
    retry returns the SAME items citing the real ``t0001``. The
    governed loop sees a clean aggregate and promotes."""
    stub = _RecordingSeqStub(
        json.dumps(_response(_FABRICATED_TURN)),
        json.dumps(_response(_VALID_TURN)),
    )
    result = run_meeting_minutes_llm_workflow(
        DEC18, client=stub, meeting_id="dec18", source_id="dec18"
    )
    assert len(stub.calls) == 2, (
        "expected exactly one corrective retry, "
        f"got {len(stub.calls)} calls"
    )
    assert result.promoted is True, _evals(result)
    assert _decision(result) == "allow"

    ev = _evals(result)
    # The eval that was failing on attempt 1 now PASSES on the
    # aggregated payload — the retry's purpose.
    assert ev["source_turn_validity"][0] == "pass", ev


def test_retry_prompt_lists_valid_and_invalid_turn_ids():
    """The retry prompt must carry CONCRETE corrective information: the
    fabricated turn_id(s) AND the valid turn-id set for this batch.
    Asserts on the exact user message the stub received on the second
    call — a generic 'try again' would not help the model."""
    stub = _RecordingSeqStub(
        json.dumps(_response(_FABRICATED_TURN)),
        json.dumps(_response(_VALID_TURN)),
    )
    run_meeting_minutes_llm_workflow(
        DEC18, client=stub, meeting_id="dec18", source_id="dec18"
    )
    assert len(stub.calls) == 2
    _, retry_user = stub.calls[1]

    # The fabricated id is explicitly named.
    assert _FABRICATED_TURN in retry_user, retry_user

    # The valid turn-id set for this batch is named so the model has
    # something concrete to cite on the retry. DEC18 chunks: t0000, t0001.
    assert "t0000" in retry_user, retry_user
    assert _VALID_TURN in retry_user, retry_user

    # The retry header is the source-turn-specific one, not the schema
    # one — the model is told what specifically to fix.
    assert "source_turn_ids" in retry_user, retry_user
    assert "valid turn IDs for this batch" in retry_user, retry_user


def test_only_one_retry_attempt_no_infinite_loop():
    """The retry budget is shared with the schema-reject retry and is
    bounded by ``_MAX_LLM_ATTEMPTS``. A model that ALWAYS cites a
    fabricated turn_id makes exactly that many calls — never more."""
    stub = _RecordingSeqStub(json.dumps(_response(_FABRICATED_TURN)))
    result = run_meeting_minutes_llm_workflow(
        DEC18, client=stub, meeting_id="dec18", source_id="dec18"
    )
    assert len(stub.calls) == _MAX_LLM_ATTEMPTS, (
        f"retry must be bounded by _MAX_LLM_ATTEMPTS={_MAX_LLM_ATTEMPTS}, "
        f"got {len(stub.calls)} calls"
    )
    # Persistent invalid turn_id still blocks fail-closed — the in-loop
    # source_turn_validity eval is the authoritative gate.
    assert result.promoted is False
    assert _decision(result) == "block"


def test_persistently_invalid_turn_id_blocks_with_authoritative_reason():
    """A model that NEVER returns valid turn_ids still blocks the run
    with the precise ``source_turn_validity`` reason codes — the
    producer-side pre-check never masks or weakens the in-loop gate."""
    stub = _RecordingSeqStub(json.dumps(_response(_FABRICATED_TURN)))
    result = run_meeting_minutes_llm_workflow(
        DEC18, client=stub, meeting_id="dec18", source_id="dec18"
    )
    assert result.promoted is False
    assert result.meeting_minutes.status == "rejected"
    assert _decision(result) == "block"

    ev = _evals(result)
    assert ev["source_turn_validity"][0] == "fail", ev
    # The fabricated turn_id appears verbatim in the eval's reason codes
    # — the cause is visible to the operator.
    codes = ev["source_turn_validity"][1]
    assert any(
        _FABRICATED_TURN in c for c in codes
    ), f"expected {_FABRICATED_TURN} in reason codes, got {codes}"

    # The control decision also reflects the source_turn_validity fail.
    control_codes = result.control_decision.payload["reason_codes"]
    assert any(
        "source_turn_validity" in c for c in control_codes
    ), control_codes


def test_valid_first_response_is_single_call_unchanged():
    """A model that returns a valid response on the first call must make
    exactly ONE call — the source_turn pre-check fires only on a real
    failure, so the happy path is byte-unaffected by this change."""
    stub = _RecordingSeqStub(json.dumps(_response(_VALID_TURN)))
    result = run_meeting_minutes_llm_workflow(
        DEC18, client=stub, meeting_id="dec18", source_id="dec18"
    )
    assert len(stub.calls) == 1, (
        "a valid first response must not retry; "
        f"got {len(stub.calls)} calls"
    )
    assert result.promoted is True
    assert _decision(result) == "allow"
