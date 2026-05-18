"""Contract: the grounded path batches the chunk list and aggregates.

Root cause this defends against (the mission bug): the workflow used to
make ONE model call over the WHOLE transcript. At ~138-chunk scale that
single response exceeds the 16384 max_tokens budget;
``AnthropicJSONClient`` raises ``llm_output_truncated:max_tokens`` and
the producer returns a no-arrays base payload, so
``required_meeting_minutes_fields`` (`missing_field:*`) +
``regulatory_verb`` (`decisions_field_missing`) block the whole run. A
small run passed only because its output fit the budget.

Trust properties defended here (NOT ceremony):

* a transcript larger than one token-budget-safe call is processed as
  deterministic contiguous chunk batches, ONE model call per batch,
  and the parsed payloads are AGGREGATED into one payload the UNCHANGED
  evals judge once — the 138-chunk-equivalent run now promotes
  (constitution §4 Produce→Evaluate→Decide→Promote, golden workflow);
* a model that truncates on every call still blocks the WHOLE run with
  the cause visible — batching did NOT weaken the fail-closed gate
  (constitution §7: failed required evals block);
* one persistently-malformed batch fails the WHOLE run — a partial
  aggregation is never promoted as if it were the whole transcript;
* a transcript that already fits one batch takes the single-pass path
  and makes exactly ONE call — small runs / the existing suite are
  byte-unaffected by batching (the happy path did not move).

The artifact is produced by the REAL governed loop; only the transport
is a deterministic stub (no API key, no network).
"""
from __future__ import annotations

import json
import re

from spectrum_systems_core.workflows import run_meeting_minutes_llm_workflow
from spectrum_systems_core.workflows.llm_client import LLMClientError
from spectrum_systems_core.workflows.meeting_minutes_llm import (
    _CHUNKS_PER_BATCH,
)

_TURN_RE = re.compile(r"\[(t\d{4})\]")
_DEC_RE = re.compile(r"We adopted the interference threshold for band \d+\.")


def _name(i: int) -> str:
    # chunker label regex is ^[A-Z][A-Z\s\-\.]{1,40}:\s — letters only,
    # NO digits — so the speaker name is a base-26 letter code.
    return f"SPEAKER {chr(65 + i // 26)}{chr(65 + i % 26)}"


def _transcript(n_turns: int) -> str:
    return "\n".join(
        f"{_name(i)}: We adopted the interference threshold for band {i}."
        for i in range(n_turns)
    )


def _expected_batches(n_turns: int) -> int:
    return (n_turns + _CHUNKS_PER_BATCH - 1) // _CHUNKS_PER_BATCH


def _evals(result) -> dict:
    out = {}
    for e in result.eval_results:
        p = e.payload if isinstance(e.payload, dict) else {}
        out[p.get("eval_type")] = (p.get("status"), p.get("reason_codes"))
    return out


class _BatchAwareStub:
    """A well-behaved per-batch model. Each call sees one batch's slice
    + turn block; it returns a schema-valid payload whose decision /
    technical_parameter text is a VERBATIM substring of that slice and
    whose grounding cites a REAL turn_id from that batch, so every
    grounded eval passes per batch and the aggregate promotes. Records
    the call count so the batch fan-out is asserted, not assumed."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *, system: str, user: str) -> str:  # noqa: ARG002
        self.calls += 1
        turn_ids = list(dict.fromkeys(_TURN_RE.findall(user)))
        m = _DEC_RE.search(user)
        text = m.group(0) if m else "We adopted the threshold."
        st = [turn_ids[0]] if turn_ids else []
        return json.dumps(
            {
                "decisions": [{"text": text, "verb": "adopted"}],
                "action_items": [],
                "open_questions": [],
                "technical_parameters": [
                    {
                        "param_id": f"p-{st[0] if st else 'x'}",
                        "parameter_name": "interference threshold",
                        "value": text,
                    }
                ],
                "grounding": [
                    {"kind": "decision", "text": text, "source_turns": st},
                    {
                        "kind": "technical_parameters",
                        "text": text,
                        "source_turns": st,
                    },
                ],
            }
        )


def _decision(result) -> str:
    return result.control_decision.payload["decision"]


def test_multi_batch_run_aggregates_and_promotes():
    n = 138  # the mission's scale; > _CHUNKS_PER_BATCH so batching fires
    stub = _BatchAwareStub()
    result = run_meeting_minutes_llm_workflow(
        _transcript(n), client=stub, meeting_id="m138"
    )
    n_batches = _expected_batches(n)
    assert stub.calls == n_batches, (stub.calls, n_batches)

    ev = _evals(result)
    # The exact two evals the mission reported as failing now PASS on the
    # AGGREGATED payload — they were never per-chunk.
    assert ev["required_meeting_minutes_fields"][0] == "pass", ev
    assert ev["regulatory_verb"][0] == "pass", ev

    payload = result.meeting_minutes.payload
    # One decision + two grounding entries per batch -> proves every
    # batch's parsed payload was aggregated, not just the last.
    assert len(payload["decisions"]) == n_batches
    assert len(payload["grounding"]) == 2 * n_batches
    assert result.promoted is True
    assert _decision(result) == "allow"


def test_every_call_truncates_still_blocks_fail_closed():
    """The mission's exact failure shape. A model that always truncates
    (the over-budget single-call symptom) must STILL block the whole run
    with the cause visible — batching does not weaken the gate."""

    def truncate_always(*, system: str, user: str) -> str:  # noqa: ARG001
        raise LLMClientError(
            "llm_output_truncated:max_tokens "
            "(model=claude-sonnet-4-6, max_tokens=16384); "
            "raise max_tokens for this extraction"
        )

    result = run_meeting_minutes_llm_workflow(
        _transcript(138), client=truncate_always, meeting_id="m138"
    )
    assert result.promoted is False
    assert _decision(result) == "block"
    ev = _evals(result)
    assert ev["required_meeting_minutes_fields"][0] == "fail"
    assert "missing_field:decisions" in ev["required_meeting_minutes_fields"][1]
    assert ev["regulatory_verb"][0] == "fail"
    assert "decisions_field_missing" in ev["regulatory_verb"][1]
    assert "llm_output_truncated:max_tokens" in (
        result.meeting_minutes.payload.get("_llm_error") or ""
    )


def test_one_bad_batch_fails_the_whole_run():
    """A partial aggregation is never promoted. One persistently
    schema-invalid batch blocks the whole run with the strict-schema
    reason preserved (no gate weakened by aggregation)."""

    good = _BatchAwareStub()

    class _PoisonSecondBatch:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, *, system: str, user: str) -> str:
            self.calls += 1
            # Batch 1 valid; from batch 2 on, a parseable but
            # schema-invalid response (non-list `decisions`).
            if self.calls == 1:
                return good(system=system, user=user)
            return json.dumps(
                {
                    "decisions": "not-a-list",
                    "action_items": [],
                    "open_questions": [],
                    "grounding": [],
                }
            )

    stub = _PoisonSecondBatch()
    result = run_meeting_minutes_llm_workflow(
        _transcript(60), client=stub, meeting_id="m60"
    )
    assert result.promoted is False
    assert _decision(result) == "block"
    codes = result.control_decision.payload["reason_codes"]
    assert any("llm_extraction_strict_schema" in c for c in codes), codes


def test_single_batch_transcript_is_one_call_single_pass():
    """A transcript that already fits one batch takes the single-pass
    path and makes exactly ONE model call — batching is inert below the
    threshold, so small runs are byte-unaffected."""
    n = _CHUNKS_PER_BATCH  # exactly one batch worth -> single pass
    stub = _BatchAwareStub()
    result = run_meeting_minutes_llm_workflow(
        _transcript(n), client=stub, meeting_id="msmall"
    )
    assert stub.calls == 1, stub.calls
    assert result.promoted is True
    assert _decision(result) == "allow"
    assert len(result.meeting_minutes.payload["decisions"]) == 1
