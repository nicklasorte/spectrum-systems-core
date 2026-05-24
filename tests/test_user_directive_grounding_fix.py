"""Regression tests for the grounding_entries=0 architectural fix.

Root cause defended here: the live-LLM extraction request lived ENTIRELY
in the system prompt while the user turn carried only data (raw
transcript + turn block) with no directive. With the system prompt
saturated by "empty is safe / everything blocks", both Haiku and Sonnet
consistently returned the all-empty object — every content array AND
``grounding`` empty, model-independently.

These are unit + fail-closed-diagnostics tests:

* the user turn now carries an explicit extraction directive and the
  closing grounding imperative (the missing request);
* a well-formed model response still flows grounding through to a
  promoted artifact (grounding_entries > 0);
* the three opaque failure modes (transport, parse-miss, model-empty)
  are now explained in the per-chunk debug report rather than collapsing
  to an unexplained ``grounding_entries=0`` — the CLAUDE.md auto-debug
  rule ("a block a new engineer could not explain from the artifact
  alone").
"""
from __future__ import annotations

import json
import re

from spectrum_systems_core.data_lake.chunker import chunk_transcript
from spectrum_systems_core.workflows.llm_client import LLMClientError
from spectrum_systems_core.workflows.meeting_minutes_llm import (
    build_chunk_debug_report,
    run_meeting_minutes_llm_workflow,
)

# Content-present transcript (> MIN_CONTENT_CHARS=400, with speaker
# turns) — mirrors the real mission scenario where an all-empty model
# response MUST block (the nonempty eval), not promote. A sub-threshold
# transcript would legitimately promote an empty extraction (the
# constitution permits an empty, faithful extraction on a content-free
# transcript), which is not the case under test here.
_TRANSCRIPT = (
    "7 GHz Downlink TIG Meeting Kickoff - full working session\n\n"
    "Chair Smith: Good morning, this is the kickoff of the 7 GHz "
    "downlink technical interference group working session.\n"
    "NTIA Lead: NTIA has reviewed the prior comment cycle and the "
    "propagation modeling results in detail.\n"
    "Chair Smith: The group approved the 7 GHz downlink threshold of "
    "minus 47 dBm per megahertz.\n"
    "DoD Rep: DoD raised a concern about the aggregate interference "
    "methodology and asked for it to be revisited.\n"
    "Chair Smith: The group deferred the aggregate interference "
    "methodology pending further study.\n"
    "Chair Smith: DoD will submit revised ERP values before the next "
    "session.\n"
    "NTIA Lead: One open question remains for the coordination analysis "
    "to resolve.\n"
    "Chair Smith: What is the coordination distance for federal "
    "incumbents in the 7 GHz band?\n"
    "Chair Smith: Any other business? Hearing none, we adjourn.\n"
)

# The production turn-block marker (workflows.meeting_minutes_llm
# _TURN_BLOCK_HEADER). Distinct from the bare phrase the directive
# mentions, so an ordering assertion targets the REAL block.
_REAL_TURN_BLOCK = '=== TRANSCRIPT TURNS (cite these turn_ids in "grounding") ==='

_ALL_KEYS = (
    "decisions", "action_items", "open_questions", "commitments", "risks",
    "claims", "cross_references", "attendees", "topics",
    "regulatory_references", "technical_parameters", "named_artifacts",
    "scheduled_events", "sentiment_indicators", "meeting_phases",
    "issue_registry_entry", "position_statement", "dissent_or_objection",
    "agenda_item", "precedent_reference", "external_stakeholder_input",
    "glossary_definition", "procedural_ruling",
)


class _Recorder:
    def __init__(self, inner):
        self._inner = inner
        self.user = None
        self.system = None

    def __call__(self, *, system: str, user: str) -> str:
        if self.user is None:
            self.user, self.system = user, system
        return self._inner(system=system, user=user)


def _good(*, system: str, user: str) -> str:  # noqa: ARG001
    tids = re.findall(r"\[(t\d{4})\]", user)
    last = tids[-1]
    doc = {k: [] for k in _ALL_KEYS}
    doc["decisions"] = [
        "The group approved the 7 GHz downlink threshold of minus 47 "
        "dBm per megahertz.",
        "The group deferred the aggregate interference methodology "
        "pending further study.",
    ]
    doc["action_items"] = [
        {"action": "DoD will submit revised ERP values before the next session."}
    ]
    doc["open_questions"] = [
        "What is the coordination distance for federal incumbents in "
        "the 7 GHz band?"
    ]
    doc["technical_parameters"] = [
        {
            "param_id": "p1",
            "parameter_name": "7 GHz downlink threshold",
            "value": "minus 47 dBm per megahertz",
            "unit": "dBm/MHz",
            "context": "approved threshold",
            "speaker": "Chair Smith",
        }
    ]
    g = []
    for kind, key in (
        ("decision", "decisions"),
        ("action_item", "action_items"),
        ("open_question", "open_questions"),
    ):
        for it in doc[key]:
            g.append({"kind": kind, "text": it, "source_turns": [last]})
    g.append(
        {
            "kind": "technical_parameter",
            "text": "minus 47 dBm per megahertz",
            "source_turns": [last],
        }
    )
    doc["grounding"] = g
    return json.dumps(doc)


def test_user_turn_carries_the_extraction_directive() -> None:
    """The request is in the user turn (the root cause was its absence).
    The raw transcript still precedes the TURN block (the system
    prompt's source-attribution rule binds to that order) and the
    closing grounding imperative is present."""
    rec = _Recorder(_good)
    run_meeting_minutes_llm_workflow(
        _TRANSCRIPT, client=rec, meeting_id="m", source_id="m"
    )
    u = rec.user
    assert u is not None
    assert u.startswith("TASK: Extract the structured meeting minutes")
    # raw transcript appears before the REAL turn block
    assert u.index("7 GHz Downlink TIG Meeting Kickoff") < u.index(
        _REAL_TURN_BLOCK
    )
    # closing imperative re-asserts the grounding requirement
    assert '"grounding"' in u and "END OF INPUT" in u
    # the directive prefix (everything before the real turn block) must
    # not inject any fake [tNNNN] turn-id token
    assert not re.search(r"\[t\d{4}\]", u.split(_REAL_TURN_BLOCK, 1)[0])


def test_wellformed_response_promotes_with_nonzero_grounding() -> None:
    """The architecture promotes a faithfully grounded extraction:
    grounding_entries > 0 — the mission's success criterion."""
    res = run_meeting_minutes_llm_workflow(
        _TRANSCRIPT, client=_good, meeting_id="m", source_id="m"
    )
    assert res.promoted is True
    grounding = res.meeting_minutes.payload.get("grounding")
    assert isinstance(grounding, list) and len(grounding) > 0


def test_debug_report_explains_each_opaque_failure_mode() -> None:
    """grounding_entries=0 must never be unexplained: transport,
    parse-miss and model-empty each name their cause (CLAUDE.md
    auto-debug rule). None of these promote (gates unchanged)."""
    chunks = chunk_transcript(_TRANSCRIPT)

    def _trans(*, system, user):  # noqa: ARG001
        raise LLMClientError("llm_output_truncated:max_tokens (x)")

    rt = run_meeting_minutes_llm_workflow(
        _TRANSCRIPT, client=_trans, meeting_id="m", source_id="m"
    )
    rep = build_chunk_debug_report(
        payload=rt.meeting_minutes.payload,
        chunks=chunks,
        eval_results=rt.eval_results,
    )
    assert "LLM_TRANSPORT_FAILURE: llm_output_truncated:max_tokens" in rep
    assert rt.promoted is False

    def _empty(*, system, user):  # noqa: ARG001
        return json.dumps(
            {**{k: [] for k in _ALL_KEYS}, "grounding": []}
        )

    re_ = run_meeting_minutes_llm_workflow(
        _TRANSCRIPT, client=_empty, meeting_id="m", source_id="m"
    )
    rep = build_chunk_debug_report(
        payload=re_.meeting_minutes.payload,
        chunks=chunks,
        eval_results=re_.eval_results,
    )
    assert "LLM_RETURNED_EMPTY" in rep
    assert re_.promoted is False

    def _junk(*, system, user):  # noqa: ARG001
        return "I cannot help with that."

    rj = run_meeting_minutes_llm_workflow(
        _TRANSCRIPT, client=_junk, meeting_id="m", source_id="m"
    )
    rep = build_chunk_debug_report(
        payload=rj.meeting_minutes.payload,
        chunks=chunks,
        eval_results=rj.eval_results,
    )
    assert "LLM_RESPONSE_UNPARSED" in rep
    assert rj.promoted is False
