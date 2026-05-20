"""Phase 6 cascade executor unit tests.

Covers the red-team pass criteria the spec calls out:
  Pass 1 #1  — every kept item exists in the source
  Pass 1 #2  — empty source produces an empty filtered artifact
  Pass 1 #3  — invalid filter response triggers conservative pass-through
  Pass 1 #4  — `reason` field stripped before being sent to the filter
  Pass 2 #2  — mutation test of decision application
  Pass 2 #5  — turn_aggregate truncation
  Pass 3 #7  — invalid filter response keeps items, not drops them
"""
from __future__ import annotations

import datetime
import json
from decimal import Decimal

import pytest

from spectrum_systems_core.cascade.executor import (
    FILTER_RESPONSE_INVALID_PASSTHROUGH,
    CascadeFilterResult,
    items_in_artifact_count,
    run_cascade_filter,
)

from ._helpers import (
    DeterministicFilterClient,
    always_drop_rule,
    always_keep_rule,
    drop_indexes_rule,
    make_action_item,
    make_chunk,
    make_decision,
    make_source_artifact,
    make_source_payload,
    make_topic,
)


# ---------------------------------------------------------------------------
# Pass 1 #1 — every kept item exists in the source.
# ---------------------------------------------------------------------------


def test_filtered_items_are_a_strict_subset_of_source() -> None:
    chunk_text = "the group decided to ship phase six and to test it"
    payload = make_source_payload(
        decisions=[
            make_decision("decided to ship phase six"),
            make_decision("to test it"),
        ],
    )
    source = make_source_artifact(payload)
    client = DeterministicFilterClient(decision_rule=drop_indexes_rule([0]))

    result = run_cascade_filter(
        source_artifact=source,
        chunks=[make_chunk(chunk_text)],
        api_client=client,
    )

    source_decisions = payload["decisions"]
    for kept in result.filtered_items["decisions"]:
        assert kept in source_decisions, "cascade invented a decision"
    # The cascade never produces an item that was not in the source's
    # array. Check across every key.
    for key, kept_list in result.filtered_items.items():
        source_list = payload.get(key) or []
        for item in kept_list:
            assert item in source_list, (
                f"cascade invented an item in {key!r}: {item!r}"
            )


# ---------------------------------------------------------------------------
# Pass 1 #2 — empty source.
# ---------------------------------------------------------------------------


def test_empty_source_produces_empty_filtered_artifact() -> None:
    payload = make_source_payload(decisions=[], action_items=[], topics=[])
    source = make_source_artifact(payload)
    client = DeterministicFilterClient(decision_rule=always_keep_rule)

    result = run_cascade_filter(
        source_artifact=source,
        chunks=[make_chunk("some unrelated transcript text")],
        api_client=client,
    )

    assert result.filter_metadata["items_kept_count"] == 0
    assert result.filter_metadata["items_dropped_count"] == 0
    # No items → no chunks evaluated → no API calls made.
    assert result.filter_metadata["chunks_evaluated"] == 0
    assert len(client.calls) == 0
    for v in result.filtered_items.values():
        assert v == []


# ---------------------------------------------------------------------------
# Pass 1 #3, Pass 3 #7 — invalid filter response triggers conservative
# pass-through (every item from the chunk is KEPT).
# ---------------------------------------------------------------------------


class _InvalidResponseClient:
    """Returns a response that fails JSON Schema (decision = 'maybe')."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *, system: str, user: str, **kw: object) -> str:
        self.calls += 1
        # Parse the count of items the filter was asked about.
        start = user.find("[")
        end = user.rfind("]")
        items = json.loads(user[start : end + 1])
        return json.dumps(
            [
                {
                    "item_idx": entry["item_idx"],
                    "decision": "maybe",
                    "reason": "invalid",
                }
                for entry in items
            ]
        )


def test_invalid_filter_response_keeps_all_items_in_chunk() -> None:
    chunk_text = "ship phase six. cover with tests."
    payload = make_source_payload(
        decisions=[
            make_decision("ship phase six"),
            make_decision("cover with tests"),
        ],
    )
    source = make_source_artifact(payload)

    result = run_cascade_filter(
        source_artifact=source,
        chunks=[make_chunk(chunk_text)],
        api_client=_InvalidResponseClient(),
    )

    # Conservative pass-through: every item KEPT (not dropped) and the
    # log records every entry with the invalid_response_passthrough
    # marker.
    assert result.filter_metadata["items_kept_count"] == 2
    assert result.filter_metadata["items_dropped_count"] == 0
    assert result.filter_metadata["chunks_with_invalid_filter_response"] == 1
    decisions = [
        e.decision for e in result.filter_log_entries
    ]
    assert all(d == FILTER_RESPONSE_INVALID_PASSTHROUGH for d in decisions)


def test_json_decode_error_keeps_all_items_in_chunk() -> None:
    """A non-JSON response is the same failure class as a schema
    violation — both must trigger conservative pass-through."""
    payload = make_source_payload(
        decisions=[make_decision("ship phase six")],
    )
    source = make_source_artifact(payload)

    def garbage_client(*, system: str, user: str, **kw: object) -> str:
        return "not valid json at all {{{"

    result = run_cascade_filter(
        source_artifact=source,
        chunks=[make_chunk("ship phase six")],
        api_client=garbage_client,
    )
    assert result.filter_metadata["items_kept_count"] == 1
    assert result.filter_metadata["chunks_with_invalid_filter_response"] == 1


# ---------------------------------------------------------------------------
# Pass 1 #4 — `reason` field stripped before being sent to the filter.
# ---------------------------------------------------------------------------


def test_reason_field_stripped_from_filter_input() -> None:
    quote = "make the next call short"
    chunk_text = quote
    distinctive_reason = "DISTINCTIVE_REASON_TOKEN_THAT_MUST_NOT_LEAK"
    payload = make_source_payload(
        decisions=[
            make_decision(quote, reason=distinctive_reason),
        ],
    )
    source = make_source_artifact(payload)
    client = DeterministicFilterClient(decision_rule=always_keep_rule)

    run_cascade_filter(
        source_artifact=source,
        chunks=[make_chunk(chunk_text)],
        api_client=client,
    )

    assert len(client.calls) == 1
    _system, user = client.calls[0]
    assert distinctive_reason not in user, (
        "reason field leaked into the filter prompt; it must be stripped"
    )


# ---------------------------------------------------------------------------
# Pass 2 #2 — mutation test of decision application.
# ---------------------------------------------------------------------------


def test_drop_set_application() -> None:
    """Construct 10 items in one chunk. Drop items 2, 5, 7. Assert the
    filtered artifact contains 0,1,3,4,6,8,9 (in source order)."""
    chunk_text = " ".join(f"item-text-{i}" for i in range(10))
    decisions = [make_decision(f"item-text-{i}") for i in range(10)]
    payload = make_source_payload(decisions=decisions)
    source = make_source_artifact(payload)

    client = DeterministicFilterClient(
        decision_rule=drop_indexes_rule([2, 5, 7]),
    )

    result = run_cascade_filter(
        source_artifact=source,
        chunks=[make_chunk(chunk_text)],
        api_client=client,
    )

    kept = result.filtered_items["decisions"]
    assert [d["text"] for d in kept] == [
        f"item-text-{i}" for i in [0, 1, 3, 4, 6, 8, 9]
    ]
    assert result.filter_metadata["items_kept_count"] == 7
    assert result.filter_metadata["items_dropped_count"] == 3

    # Log records the three drops with stable reasons.
    drop_entries = [
        e for e in result.filter_log_entries if e.decision == "drop"
    ]
    assert {e.item_idx for e in drop_entries} == {2, 5, 7}
    for e in drop_entries:
        assert e.reason.startswith("drop_")


# ---------------------------------------------------------------------------
# Pass 2 #5 — turn_aggregate truncation.
# ---------------------------------------------------------------------------


def test_turn_aggregate_truncation_to_10_turns() -> None:
    turn_ids = list(range(1, 16))  # 15 turns — over the 10 budget
    topic = make_topic("t1", "long topic", turn_ids)
    payload = make_source_payload(topics=[topic])
    source = make_source_artifact(payload)
    client = DeterministicFilterClient(decision_rule=always_keep_rule)

    turn_records = [
        {"turn_id": i, "text": f"turn-text-{i}"} for i in turn_ids
    ]

    result = run_cascade_filter(
        source_artifact=source,
        chunks=[make_chunk("some chunk text")],
        api_client=client,
        turn_records=turn_records,
    )

    assert result.filter_metadata["truncation_count"] == 1
    # The filter call should have received only 10 rendered turn
    # strings plus the truncation marker.
    _system, user = client.calls[0]
    # First 10 turn texts present, the 11th NOT present, marker present.
    assert "turn-text-1" in user
    assert "turn-text-10" in user
    assert "turn-text-11" not in user
    assert "5 more turns truncated" in user


def test_turn_aggregate_no_truncation_for_short_list() -> None:
    topic = make_topic("t1", "short topic", [1, 2, 3])
    payload = make_source_payload(topics=[topic])
    source = make_source_artifact(payload)
    client = DeterministicFilterClient(decision_rule=always_keep_rule)
    turn_records = [
        {"turn_id": i, "text": f"turn-text-{i}"} for i in [1, 2, 3]
    ]

    result = run_cascade_filter(
        source_artifact=source,
        chunks=[make_chunk("chunk text")],
        api_client=client,
        turn_records=turn_records,
    )

    assert result.filter_metadata["truncation_count"] == 0


# ---------------------------------------------------------------------------
# Drop-all and keep-all sanity.
# ---------------------------------------------------------------------------


def test_drop_all_produces_empty_filtered_arrays() -> None:
    payload = make_source_payload(
        decisions=[make_decision("a"), make_decision("b")],
    )
    source = make_source_artifact(payload)
    client = DeterministicFilterClient(decision_rule=always_drop_rule)

    result = run_cascade_filter(
        source_artifact=source,
        chunks=[make_chunk("a b")],
        api_client=client,
    )

    assert result.filter_metadata["items_kept_count"] == 0
    assert result.filter_metadata["items_dropped_count"] == 2
    assert result.filtered_items["decisions"] == []


def test_keep_all_returns_every_source_item() -> None:
    payload = make_source_payload(
        decisions=[make_decision("a"), make_decision("b")],
    )
    source = make_source_artifact(payload)
    client = DeterministicFilterClient(decision_rule=always_keep_rule)

    result = run_cascade_filter(
        source_artifact=source,
        chunks=[make_chunk("a b")],
        api_client=client,
    )

    assert result.filter_metadata["items_kept_count"] == 2
    assert result.filter_metadata["items_dropped_count"] == 0
    assert result.filtered_items["decisions"] == payload["decisions"]


# ---------------------------------------------------------------------------
# Bonus: items_in_artifact_count accepts both shapes.
# ---------------------------------------------------------------------------


def test_items_in_artifact_count_handles_envelope_and_payload() -> None:
    payload = make_source_payload(
        decisions=[make_decision("a")],
        action_items=[make_action_item("b")],
        topics=[make_topic("t", "x", [1])],
    )
    envelope = make_source_artifact(payload)
    assert items_in_artifact_count(payload) == 3
    assert items_in_artifact_count(envelope) == 3
