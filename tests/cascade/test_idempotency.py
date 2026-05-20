"""Phase 6 — Step 6.10 idempotency test.

With a deterministic mock filter (identical responses for identical
inputs), two cascade runs over the same Haiku artifact MUST produce
identical filtered subsets, identical log entries, and identical
metadata (modulo the timestamps the executor stamps from an injected
clock).
"""
from __future__ import annotations

import datetime
from typing import List

from spectrum_systems_core.cascade.executor import run_cascade_filter

from ._helpers import (
    DeterministicFilterClient,
    drop_indexes_rule,
    make_chunk,
    make_decision,
    make_source_artifact,
    make_source_payload,
)


def _frozen_clock() -> datetime.datetime:
    return datetime.datetime(2026, 5, 20, 12, 0, 0, tzinfo=datetime.timezone.utc)


def test_cascade_idempotent_with_deterministic_filter() -> None:
    chunk_text = " ".join(f"item-text-{i}" for i in range(8))
    payload = make_source_payload(
        decisions=[make_decision(f"item-text-{i}") for i in range(8)],
    )
    source = make_source_artifact(payload)
    drop_set = [1, 3, 5]

    def _run() -> dict:
        client = DeterministicFilterClient(
            decision_rule=drop_indexes_rule(drop_set)
        )
        result = run_cascade_filter(
            source_artifact=source,
            chunks=[make_chunk(chunk_text)],
            api_client=client,
            clock=_frozen_clock,
        )
        return {
            "filtered_items": result.filtered_items,
            "filter_metadata": result.filter_metadata,
            "log_entries": [
                (
                    e.chunk_index,
                    e.item_idx,
                    e.extraction_type,
                    e.decision,
                    e.reason,
                )
                for e in result.filter_log_entries
            ],
        }

    a = _run()
    b = _run()
    assert a["filtered_items"] == b["filtered_items"]
    assert a["filter_metadata"] == b["filter_metadata"]
    assert a["log_entries"] == b["log_entries"]
