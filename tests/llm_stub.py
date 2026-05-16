"""Deterministic LLM client stubs for the meeting_minutes_llm tests.

A stub is the same injectable seam ``ai/adapter.py`` (``api_caller``)
and ``create_human_gt_pairs.py`` (``CREATE_HUMAN_GT_PAIRS_STUB_RESPONSE``)
use: the artifact is still produced by the REAL workflow / governed
loop, only the transport returns a fixed string. No API key, no
network.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "llm_extraction"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def text_stub(response_text: str) -> Callable[..., str]:
    def _client(*, system: str, user: str) -> str:  # noqa: ARG001
        return response_text

    return _client


_NEW_ARRAYS = (
    "commitments",
    "risks",
    "cross_references",
    "attendees",
    "topics",
    "regulatory_references",
    "technical_parameters",
    "named_artifacts",
    "scheduled_events",
)


def json_stub(
    *,
    decisions=(),
    action_items=(),
    open_questions=(),
    **new_arrays,
) -> Callable[..., str]:
    """Emit the full 12-key meeting_minutes content object.

    The three legacy arrays plus the nine PR #123 structured arrays.
    Any new array not passed defaults to ``[]`` — exactly what the
    parser must carry through and what the strict-schema eval expects.
    Unknown kwargs raise so a typo in a test fixture fails loudly
    instead of silently emitting an empty array.
    """
    unknown = set(new_arrays) - set(_NEW_ARRAYS)
    if unknown:
        raise TypeError(f"json_stub got unknown array kwargs: {sorted(unknown)}")
    doc = {
        "decisions": list(decisions),
        "action_items": list(action_items),
        "open_questions": list(open_questions),
    }
    for key in _NEW_ARRAYS:
        doc[key] = list(new_arrays.get(key, []))
    return text_stub(json.dumps(doc))


class SpyStub:
    """A stub that records whether it was called (mutual-exclusion proof)."""

    def __init__(self, response_text: str):
        self._response = response_text
        self.calls = 0

    def __call__(self, *, system: str, user: str) -> str:  # noqa: ARG002
        self.calls += 1
        return self._response


# Decision / action / question strings that are VERBATIM substrings of
# dec18_transcript.txt, so the within-source eval passes on the happy
# path. Kept here so every test uses the same grounded items.
DEC18_DECISIONS = [
    "The group approved the 7 GHz downlink threshold of minus 47 dBm per megahertz.",
    "The group deferred the aggregate interference methodology pending further study.",
]
DEC18_ACTION_ITEMS = [
    "DoD will submit revised ERP values before the next session.",
]
DEC18_OPEN_QUESTIONS = [
    "What is the coordination distance for federal incumbents in the 7 GHz band?",
]
# A technical_parameter whose ``value`` is a VERBATIM substring of
# dec18_transcript.txt, so the Step 4 structured within-source check
# and the Step 5 proxy-nonempty gate both pass on the happy path.
# Kept here so every happy-path test shares one grounded fact.
DEC18_TECHNICAL_PARAMETERS = [
    {
        "param_id": "param-1",
        "parameter_name": "7 GHz downlink threshold",
        "value": "minus 47 dBm per megahertz",
        "unit": "dBm/MHz",
        "context": "approved threshold for the 7 GHz downlink band",
        "speaker": "NTIA Lead",
    }
]
