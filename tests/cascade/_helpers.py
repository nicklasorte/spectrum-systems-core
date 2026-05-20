"""Shared helpers for the Phase 6 cascade test suite.

These factories build the synthetic Haiku artifacts the cascade tests
feed through `run_cascade_filter`. Items are SHAPED LIKE real Phase 1
items (verbatim items carry source_quote / quote_offset_normalized;
turn_aggregate items carry source_turn_ids) so the executor's
grounding logic exercises its real branches.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple


def make_chunk(text: str) -> Dict[str, Any]:
    return {"text": text}


def make_decision(
    quote: str,
    *,
    text: Optional[str] = None,
    quote_offset_normalized: int = 0,
    reason: str = "test_reason_should_be_stripped",
) -> Dict[str, Any]:
    return {
        "text": text or quote,
        "grounding_mode": "verbatim",
        "source_quote": quote,
        "quote_offset_normalized": quote_offset_normalized,
        "quote_offset_original": quote_offset_normalized,
        "reason": reason,
    }


def make_action_item(
    quote: str,
    *,
    action: Optional[str] = None,
    quote_offset_normalized: int = 0,
    reason: str = "test_reason_should_be_stripped",
) -> Dict[str, Any]:
    return {
        "action": action or quote,
        "grounding_mode": "verbatim",
        "source_quote": quote,
        "quote_offset_normalized": quote_offset_normalized,
        "quote_offset_original": quote_offset_normalized,
        "reason": reason,
    }


def make_topic(
    topic_id: str,
    title: str,
    source_turn_ids: List[int],
) -> Dict[str, Any]:
    return {
        "topic_id": topic_id,
        "title": title,
        "grounding_mode": "turn_aggregate",
        "source_turn_ids": list(source_turn_ids),
    }


def make_source_payload(
    *,
    decisions: Optional[List[Dict[str, Any]]] = None,
    action_items: Optional[List[Dict[str, Any]]] = None,
    topics: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "title": "test meeting",
        "summary": "",
        "decisions": decisions or [],
        "action_items": action_items or [],
        "open_questions": [],
        "topics": topics or [],
    }


def make_source_artifact(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "artifact_id": "test_id",
        "artifact_type": "meeting_minutes",
        "schema_version": "1.4.0",
        "status": "promoted",
        "created_at": "1970-01-01T00:00:00+00:00",
        "trace_id": "test_trace",
        "input_refs": [],
        "content_hash": "test_hash",
        "payload": payload,
    }


@dataclass
class DeterministicFilterClient:
    """Test client that returns canned per-chunk responses.

    Captures the user message of each call into `calls` so tests can
    assert what was actually sent (e.g. confirm the `reason` field was
    stripped from items before they reached the filter).
    """

    decision_rule: Callable[[Dict[str, Any]], Tuple[str, str]]
    calls: List[Tuple[str, str]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    def __call__(
        self, *, system: str, user: str, **kwargs: Any
    ) -> str:
        self.calls.append((system, user))
        # Parse the items list out of the user message (the executor
        # rendered them as JSON between the prompt's
        # `<items_json_without_reason_field>` marker and the end-of-
        # prompt block). We just extract the JSON array.
        # The executor substitutes a `json.dumps(..., indent=2)` array.
        start = user.find("[")
        end = user.rfind("]")
        items = json.loads(user[start : end + 1])
        out = []
        for entry in items:
            decision, reason = self.decision_rule(entry)
            out.append(
                {
                    "item_idx": entry["item_idx"],
                    "decision": decision,
                    "reason": reason,
                }
            )
        return json.dumps(out)


def always_keep_rule(_entry: Dict[str, Any]) -> Tuple[str, str]:
    return ("keep", "keep_for_test")


def always_drop_rule(_entry: Dict[str, Any]) -> Tuple[str, str]:
    return ("drop", "drop_for_test")


def drop_indexes_rule(
    drop_set: List[int],
) -> Callable[[Dict[str, Any]], Tuple[str, str]]:
    def _rule(entry: Dict[str, Any]) -> Tuple[str, str]:
        if int(entry["item_idx"]) in set(drop_set):
            return ("drop", f"drop_{entry['item_idx']}")
        return ("keep", f"keep_{entry['item_idx']}")

    return _rule
