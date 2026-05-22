"""Contract: the grounded path batches the chunk list and aggregates.

Root cause this defends against (the mission bug): the workflow used to
make ONE model call over the WHOLE transcript. At ~138-chunk scale that
single response exceeds the 16384 max_tokens budget;
``AnthropicJSONClient`` raises ``llm_output_truncated:max_tokens`` and
the producer returns a no-arrays base payload, so
``required_meeting_minutes_fields`` (`missing_field:*`) +
``regulatory_verb`` (`decisions_field_missing`) block the whole run. A
small run passed only because its output fit the budget.

Follow-up (this PR): the diagnostic candidate ``_run_batch`` returns on
a failure path (transport error / persistent parse failure) now carries
the legacy content arrays as empty lists. The envelope is structurally
valid even on failure, so the fail-closed cause surfaces via the
``llm_extraction_nonempty_required`` gate
(``extraction_empty_with_content``) — the actual cause — instead of the
spurious ``required_meeting_minutes_fields`` (``missing_field:*``) that
masked it. The transport error / bad raw response remains visible on
``payload._llm_error`` / ``payload._llm_raw`` for diagnostics.

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
    with the cause visible — batching does not weaken the gate.

    The fail-closed gate is preserved via
    ``llm_extraction_nonempty_required`` (``extraction_empty_with_content``)
    rather than the spurious ``required_meeting_minutes_fields``
    (``missing_field:decisions``) that previously masked the real cause.
    The diagnostic candidate from ``_run_batch`` now carries the legacy
    arrays as empty lists, so the envelope is structurally valid and the
    failing eval surfaces the actual cause; the transport error is
    preserved verbatim on ``payload._llm_error`` for diagnostics.
    """

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
    # The spurious missing-field failure on the envelope is gone.
    assert ev["required_meeting_minutes_fields"][0] == "pass", ev
    # The actual cause now surfaces via the nonempty gate.
    assert ev["llm_extraction_nonempty_required"][0] == "fail", ev
    assert (
        "extraction_empty_with_content"
        in ev["llm_extraction_nonempty_required"][1]
    )
    # The transport error remains visible on the payload itself, so an
    # operator reading the diagnostic candidate sees the actual cause.
    assert "llm_output_truncated:max_tokens" in (
        result.meeting_minutes.payload.get("_llm_error") or ""
    )


def test_one_bad_batch_fails_the_whole_run():
    """A partial aggregation is never promoted. One persistently
    malformed batch blocks the whole run; the failure reason surfaces
    via the nonempty gate (with the bad model output preserved on
    ``payload._llm_raw``), and the multi-batch short-circuit prevents
    earlier successful batches from being promoted as if they were the
    whole transcript."""

    good = _BatchAwareStub()

    class _PoisonSecondBatch:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, *, system: str, user: str) -> str:
            self.calls += 1
            # Batch 1 valid; from batch 2 on, a parseable but
            # schema-invalid response (non-list `decisions`). The
            # parser rejects it (returns ``None``) so the candidate
            # falls back to the empty-arrays diagnostic envelope with
            # ``_llm_raw`` carrying the bad bytes.
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
    assert any(
        "llm_extraction_nonempty_required" in c for c in codes
    ), codes
    # The short-circuit returns the failing batch's diagnostic candidate
    # (empty arrays + ``_llm_raw``), not an aggregate of the earlier
    # successful batches — partial aggregation is never promoted.
    assert result.meeting_minutes.payload.get("decisions") == []
    assert "not-a-list" in (
        result.meeting_minutes.payload.get("_llm_raw") or ""
    )


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


class _NullAgencyAttendeeStub(_BatchAwareStub):
    """A faithful per-batch model that, in addition to the well-behaved
    decision / technical_parameter the base stub emits, extracts an
    attendee NAMED in the transcript whose AGENCY the transcript never
    states — so it correctly returns ``agency: null`` (the "never
    invent" rule) instead of fabricating an agency.

    This is the mission's exact failure shape: the 7 GHz Downlink TIG
    transcript names participants (DiFrancisco, LaSorte, Bhatt, Nolen)
    without stating their agencies. Before the schema fix, the strict
    -schema eval blocked the full 138-chunk run with
    ``None is not of type 'string' at ['attendees', N, 'agency']`` and
    ``tlc_routed_extraction`` (which re-runs strict_schema) failed too,
    while a single small chunk that triggered no roster extraction
    passed — the single-chunk-passes / full-run-blocks split.
    """

    def __call__(self, *, system: str, user: str) -> str:  # noqa: ARG002
        base = json.loads(super().__call__(system=system, user=user))
        turn_ids = list(dict.fromkeys(_TURN_RE.findall(user)))
        st = [turn_ids[0]] if turn_ids else []
        base["attendees"] = [
            {
                "name": "Working group member",
                "agency": None,
                "role": None,
                "present": True,
            }
        ]
        base["grounding"].append(
            {
                "kind": "attendees",
                "text": "Working group member",
                "source_turns": st,
            }
        )
        return json.dumps(base)


def test_multi_batch_faithful_null_agency_attendee_promotes():
    """The mission's exact failure case, end-to-end through the REAL
    governed loop. A 138-turn (multi-batch) run where every batch
    faithfully extracts a NAMED attendee whose agency the transcript
    never states (``agency: null``) must now PROMOTE — a faithful
    roster extraction is no longer blocked by the strict-schema gate.

    Pre-fix this run blocked with ``failed:llm_extraction_strict_schema``
    + ``failed:tlc_routed_extraction`` (the exact mission reason codes).
    """
    n = 138  # the mission's scale; > _CHUNKS_PER_BATCH so batching fires
    stub = _NullAgencyAttendeeStub()
    result = run_meeting_minutes_llm_workflow(
        _transcript(n), client=stub, meeting_id="m138-null-agency"
    )
    n_batches = _expected_batches(n)
    assert stub.calls == n_batches, (stub.calls, n_batches)

    ev = _evals(result)
    # The two evals the mission reported as failing now PASS on the
    # aggregated payload that carries the null-agency attendees.
    assert ev["llm_extraction_strict_schema"][0] == "pass", ev
    assert ev["tlc_routed_extraction"][0] == "pass", ev

    payload = result.meeting_minutes.payload
    # Every batch's faithful null-agency attendee survived aggregation.
    assert len(payload["attendees"]) == n_batches, payload["attendees"]
    assert all(a["agency"] is None for a in payload["attendees"]), payload[
        "attendees"
    ]
    assert result.promoted is True
    assert _decision(result) == "allow"


def test_null_agency_attendee_single_batch_also_promotes():
    """The same faithful extraction on a single-batch (single-pass) run
    promotes too — proving the fix is in the schema gate, not the
    batching path, so the single-chunk and full-run behaviours converge
    instead of diverging."""
    n = _CHUNKS_PER_BATCH  # one batch -> single-pass path
    stub = _NullAgencyAttendeeStub()
    result = run_meeting_minutes_llm_workflow(
        _transcript(n), client=stub, meeting_id="msmall-single-null-agency"
    )
    assert stub.calls == 1, stub.calls
    ev = _evals(result)
    assert ev["llm_extraction_strict_schema"][0] == "pass", ev
    assert ev["tlc_routed_extraction"][0] == "pass", ev
    assert result.promoted is True
    assert _decision(result) == "allow"
    assert result.meeting_minutes.payload["attendees"][0]["agency"] is None


def test_multi_batch_required_fields_intact_when_one_batch_parse_fails():
    """The mission's exact failure shape (full 138-chunk Haiku
    extraction blocking on ``failed:required_meeting_minutes_fields``
    while ``SINGLE_CHUNK=true`` runs pass cleanly).

    Reproduces the asymmetry with a stub: most batches return clean
    JSON, but one batch persistently returns garbage that the parser
    rejects (``_parse_llm_payload`` returns ``None`` on every retry
    inside ``_run_batch``). Before the fix the multi-batch
    ``if not ok: return candidate`` short-circuited with a candidate
    that was the bare ``_base_payload`` — missing decisions,
    action_items, open_questions — so
    ``required_meeting_minutes_fields`` failed with three spurious
    ``missing_field:*`` reasons that masked the actual cause (the
    parse failure preserved in ``_llm_raw``).

    The fix makes ``_base_payload`` carry the legacy arrays as empty
    lists, so the envelope is structurally valid on the failure path
    too. ``required_meeting_minutes_fields`` now PASSES; the run still
    blocks fail-closed via ``llm_extraction_nonempty_required`` with
    the actually-informative ``extraction_empty_with_content`` reason;
    the bad bytes remain visible on ``payload._llm_raw``."""

    class _ParseFailOnThirdBatch:
        def __init__(self) -> None:
            self.calls = 0
            self.good = _BatchAwareStub()

        def __call__(self, *, system: str, user: str) -> str:
            self.calls += 1
            # Batches 1 and 2 succeed (1 call each), then batch 3
            # returns garbage on every retry — the parser keeps
            # rejecting it so ``_run_batch`` exhausts its retry
            # budget and returns ``(candidate, False)``.
            if self.calls in (1, 2):
                return self.good(system=system, user=user)
            return "this is not valid JSON"

    stub = _ParseFailOnThirdBatch()
    result = run_meeting_minutes_llm_workflow(
        _transcript(138), client=stub, meeting_id="m138-parse-fail"
    )
    assert result.promoted is False
    assert _decision(result) == "block"

    payload = result.meeting_minutes.payload
    # The envelope is structurally valid on the failure path: the
    # legacy required arrays are present (even though empty) so the
    # required-fields eval does not fire spuriously.
    assert payload["decisions"] == []
    assert payload["action_items"] == []
    assert payload["open_questions"] == []
    # The actual cause is still preserved on the diagnostic candidate.
    assert "this is not valid JSON" in (payload.get("_llm_raw") or "")

    ev = _evals(result)
    # The envelope-level eval no longer fires with spurious
    # ``missing_field:*`` reasons that masked the real cause.
    assert ev["required_meeting_minutes_fields"][0] == "pass", ev
    # The actual cause now surfaces via the nonempty gate.
    assert ev["llm_extraction_nonempty_required"][0] == "fail", ev
    assert (
        "extraction_empty_with_content"
        in ev["llm_extraction_nonempty_required"][1]
    )


def test_single_batch_run_payload_byte_identical_on_success_path():
    """Adding empty arrays to ``_base_payload`` must NOT change the
    success-path payload — on parse-success ``candidate.update(parsed)``
    overwrites the empty defaults with the model's parsed arrays, so a
    successful single-pass run is byte-identical to the pre-fix state
    (additivity / rollback property)."""
    n = _CHUNKS_PER_BATCH  # one batch -> single-pass path
    stub = _BatchAwareStub()
    result = run_meeting_minutes_llm_workflow(
        _transcript(n), client=stub, meeting_id="m-byte-id"
    )
    assert result.promoted is True
    payload = result.meeting_minutes.payload
    # Parsed arrays from the model overwrote the empty defaults; the
    # success path is unaffected.
    assert len(payload["decisions"]) == 1
    assert payload["decisions"][0]["verb"] == "adopted"
    assert len(payload["grounding"]) == 2
    # No diagnostic markers on the success-path candidate.
    assert "_llm_raw" not in payload
    assert "_llm_error" not in payload
