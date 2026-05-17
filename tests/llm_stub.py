"""Deterministic LLM client stubs for the meeting_minutes_llm tests.

A stub is the same injectable seam ``ai/adapter.py`` (``api_caller``)
and ``create_human_gt_pairs.py`` (``CREATE_HUMAN_GT_PAIRS_STUB_RESPONSE``)
use: the artifact is still produced by the REAL workflow / governed
loop, only the transport returns a fixed string. No API key, no
network.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "llm_extraction"

# turn_id token as the grounded workflow renders it into the user
# message: ``[t0000]``. The stub mirrors a well-behaved model — it
# cites the REAL turn_ids it was shown rather than inventing them.
_TURN_RE = re.compile(r"\[(t\d{4})\]")

# Distinguishes "caller did not pass grounding" (auto-derive) from
# "caller passed grounding=[...]" including an empty / fabricated list
# (use verbatim — this is how a hallucination test injects a bogus
# turn_id).
_NO_GROUNDING_OVERRIDE = object()


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


_ITEM_TEXT_FIELDS = (
    "text",
    "action",
    "question_text",
    "commitment_text",
    "risk_text",
    "ref_text",
    "reference_text",
    "parameter_name",
    "name",
    "title",
)


def _item_text(item) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for field in _ITEM_TEXT_FIELDS:
            value = item.get(field)
            if isinstance(value, str) and value:
                return value
    return ""


def _auto_grounding(doc: dict, user: str) -> list[dict]:
    """Mirror a well-behaved model: one grounding entry per content
    item, each citing every real turn_id the workflow showed in the
    user message. Deterministic given the same prompt. When there is no
    content, grounding is ``[]`` (the coverage floor allows that)."""
    turn_ids = list(dict.fromkeys(_TURN_RE.findall(user)))
    entries: list[dict] = []
    for kind, key in (
        ("decision", "decisions"),
        ("action_item", "action_items"),
        ("open_question", "open_questions"),
    ):
        for item in doc[key]:
            entries.append(
                {
                    "kind": kind,
                    "text": _item_text(item),
                    "source_turns": turn_ids,
                }
            )
    for key in _NEW_ARRAYS:
        for item in doc[key]:
            entries.append(
                {
                    "kind": key,
                    "text": _item_text(item),
                    "source_turns": turn_ids,
                }
            )
    return entries


def json_stub(
    *,
    decisions=(),
    action_items=(),
    open_questions=(),
    grounding=_NO_GROUNDING_OVERRIDE,
    **new_arrays,
) -> Callable[..., str]:
    """Emit the full meeting_minutes content object.

    The three legacy arrays plus the nine PR #123 structured arrays.
    Any new array not passed defaults to ``[]`` — exactly what the
    parser must carry through and what the strict-schema eval expects.
    Unknown kwargs raise so a typo in a test fixture fails loudly
    instead of silently emitting an empty array.

    ``grounding`` (Phase Y): when NOT passed, the stub auto-derives one
    grounding entry per content item citing the real turn_ids present
    in the user message — so a well-behaved-model happy path needs no
    per-test wiring. Pass ``grounding=[...]`` to inject a specific
    (possibly fabricated) attribution: that is exactly how the
    synthetic-hallucination test cites a turn_id that does not exist in
    chunks.jsonl.
    """
    unknown = set(new_arrays) - set(_NEW_ARRAYS)
    if unknown:
        raise TypeError(f"json_stub got unknown array kwargs: {sorted(unknown)}")
    base = {
        "decisions": list(decisions),
        "action_items": list(action_items),
        "open_questions": list(open_questions),
    }
    for key in _NEW_ARRAYS:
        base[key] = list(new_arrays.get(key, []))
    override = (
        _NO_GROUNDING_OVERRIDE
        if grounding is _NO_GROUNDING_OVERRIDE
        else list(grounding)
    )

    def _client(*, system: str, user: str) -> str:  # noqa: ARG001
        doc = {k: list(v) for k, v in base.items()}
        if override is _NO_GROUNDING_OVERRIDE:
            doc["grounding"] = _auto_grounding(doc, user)
        else:
            doc["grounding"] = override
        return json.dumps(doc)

    return _client


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
