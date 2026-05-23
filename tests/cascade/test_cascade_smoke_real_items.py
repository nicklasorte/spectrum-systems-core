"""Phase 2.C smoke test — cascade keep/drop dispatch on real-shaped items.

Pins the cascade filter's keep/drop dispatch against a static 3-item
fixture derived from the Dec 18 7 GHz Downlink TIG kickoff Haiku
artifact (`d019c5f793c4`). The fixture lives at
`tests/cascade/fixtures/phase_2c_smoke_items.json`; this test loads it,
constructs a `meeting_minutes` envelope from its three items, and
drives the production `run_cascade_filter` (from
`spectrum_systems_core.cascade.executor`) against a deterministic
api_client stub.

Why a stub: the cascade architecture requires an `api_client` callable
parameter so the LLM call can be swapped between real Sonnet (CLI
dispatch) and deterministic test fakes. The stub is NOT a mock of the
cascade filter — the production `run_cascade_filter` is exercised
end-to-end, including chunk assignment, per-chunk payload building,
the `reason`-stripping step, filter response validation, and the
splice-back-into-filtered_items step. Only the LLM call boundary is
stubbed.

CI runs without data-lake access. The fixture is fully static; this
test makes NO data-lake reads.

Pins three behaviours:

1. **Item A** (procedural_ruling, well-grounded verbatim quote) →
   `keep`. The cascade applies the filter's keep decision.
2. **Item B** (action_items, vague verbatim quote `"Thank you."`) →
   `drop`. The cascade applies the filter's drop decision and the
   item disappears from `filtered_items["action_items"]`.
3. **Item C** (action_items, `source_quote: null`) → kept by the
   cascade's graceful pass-through path. NO exception. NO special
   reason code (no `failed:missing_source_quote`). The cascade
   forwards the item to the filter unchanged and applies whatever
   decision the filter returns.

The decision rule used by the stub is content-aware (it inspects
`source_quote` to identify Item B) rather than index-based, so a
future reorder of the fixture does not silently swap which item is
dropped.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Tuple

from spectrum_systems_core.cascade.executor import (
    FILTER_RESPONSE_INVALID_PASSTHROUGH,
    run_cascade_filter,
)

from ._helpers import DeterministicFilterClient

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "phase_2c_smoke_items.json"
)

ITEM_B_QUOTE = "Thank you."


def _load_fixture() -> Dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _build_source_artifact(fixture: Dict[str, Any]) -> Dict[str, Any]:
    """Build a `meeting_minutes` envelope from the fixture's three items.

    Item A lives under `procedural_ruling`; Items B and C live under
    `action_items` in that order. The cascade's
    `_assign_items_to_chunks` iterates `_EXTRACTION_ARRAY_KEYS` in the
    fixed module order (`action_items` before `procedural_ruling`),
    so the chunk-0 filter call receives the items in this order:
    [B (action_items[0]), C (action_items[1]), A (procedural_ruling[0])].
    """
    payload: Dict[str, Any] = {
        "title": "phase_2c_smoke_fixture",
        "summary": "",
        "decisions": [],
        "action_items": [
            fixture["item_B"]["item"],
            fixture["item_C"]["item"],
        ],
        "open_questions": [],
        "procedural_ruling": [
            fixture["item_A"]["item"],
        ],
    }
    return {
        "artifact_id": "phase_2c_smoke_fixture",
        "artifact_type": "meeting_minutes",
        "schema_version": "1.4.0",
        "status": "promoted",
        "created_at": "1970-01-01T00:00:00+00:00",
        "trace_id": "phase_2c_smoke_trace",
        "input_refs": [],
        "content_hash": "phase_2c_smoke_hash",
        "payload": payload,
    }


def _smoke_decision_rule(entry: Dict[str, Any]) -> Tuple[str, str]:
    """Drop Item B (identified by its vague `source_quote`); keep all else.

    Content-aware so a future fixture reorder cannot silently swap
    which item the rule drops.
    """
    item = entry.get("item") or {}
    quote = item.get("source_quote")
    if quote == ITEM_B_QUOTE:
        return ("drop", "smoke_drop_vague_action_item_B")
    return ("keep", "smoke_keep_well_grounded_or_null_quote")


def test_smoke_cascade_keep_drop_dispatch_on_real_shaped_items() -> None:
    fixture = _load_fixture()
    source = _build_source_artifact(fixture)
    chunk_text = fixture["chunk_text"]

    client = DeterministicFilterClient(decision_rule=_smoke_decision_rule)

    result = run_cascade_filter(
        source_artifact=source,
        chunks=[{"text": chunk_text}],
        api_client=client,
    )

    # ---- Item A: kept ----------------------------------------------------
    kept_rulings = result.filtered_items["procedural_ruling"]
    assert len(kept_rulings) == 1, (
        f"Item A (procedural_ruling) should be kept; got "
        f"{len(kept_rulings)} rulings: {kept_rulings!r}"
    )
    assert kept_rulings[0]["ruling_id"] == "PR-001"
    assert (
        kept_rulings[0]["source_quote"]
        == "The group approved the 7 GHz downlink threshold of minus 47 dBm per megahertz."
    )

    # ---- Item B: dropped; Item C: kept (no special reason code) ---------
    kept_actions = result.filtered_items["action_items"]
    assert len(kept_actions) == 1, (
        f"Item B (action_items, vague) should be dropped and Item C "
        f"(action_items, null source_quote) should be kept; got "
        f"{len(kept_actions)} action_items: {kept_actions!r}"
    )
    kept_action = kept_actions[0]
    assert kept_action["source_quote"] is None, (
        "Item C should be the kept action_item (source_quote: null); "
        f"got: {kept_action!r}"
    )
    assert kept_action["action"] == "Follow up with stakeholders on outstanding items"

    # ---- Counts: 2 kept (A, C); 1 dropped (B) ---------------------------
    assert result.filter_metadata["items_kept_count"] == 2
    assert result.filter_metadata["items_dropped_count"] == 1

    # ---- Item C pass-through: no special reason code ---------------------
    # The cascade does NOT emit `failed:missing_source_quote` or
    # `FILTER_RESPONSE_INVALID_PASSTHROUGH` for the null-quote item.
    # The log entry for Item C carries the stub's normal `keep` reason.
    reasons_emitted = {(e.extraction_type, e.decision, e.reason) for e in result.filter_log_entries}
    for _etype, _decision, reason in reasons_emitted:
        assert "failed:missing_source_quote" not in reason, (
            f"cascade should NOT emit a 'failed:missing_source_quote' "
            f"reason code for null source_quote items; got: {reason!r}"
        )
        assert reason != FILTER_RESPONSE_INVALID_PASSTHROUGH, (
            f"cascade should NOT enter the conservative invalid-response "
            f"pass-through path for a well-formed stub response; got: "
            f"{reason!r}"
        )

    # ---- Item C pass-through: no `_chunk_context` injected ---------------
    # When source_quote is null, _build_chunk_payload_for_filter must
    # not splice a _chunk_context window — the field is reserved for
    # verbatim items whose quote was located in the chunk. Inspect the
    # actual `user` message sent to the stub.
    assert len(client.calls) == 1, (
        f"expected exactly one cascade chunk call; got {len(client.calls)}"
    )
    _system_text, user_text = client.calls[0]
    # Parse the items array out of the rendered prompt.
    items_start = user_text.find("[")
    items_end = user_text.rfind("]")
    sent_items = json.loads(user_text[items_start : items_end + 1])
    by_idx = {entry["item_idx"]: entry for entry in sent_items}

    # The fixture iteration order puts B at idx 0, C at idx 1, A at idx 2.
    item_b_sent = by_idx[0]["item"]
    item_c_sent = by_idx[1]["item"]
    item_a_sent = by_idx[2]["item"]

    assert item_a_sent["source_quote"].startswith(
        "The group approved the 7 GHz downlink threshold"
    )
    assert item_a_sent.get("_chunk_context"), (
        "Item A's quote is in the chunk; cascade should splice _chunk_context"
    )
    assert item_b_sent["source_quote"] == ITEM_B_QUOTE
    assert item_b_sent.get("_chunk_context"), (
        "Item B's quote ('Thank you.') is in the chunk; cascade should "
        "splice _chunk_context even though the quote is short"
    )
    assert item_c_sent["source_quote"] is None
    assert "_chunk_context" not in item_c_sent, (
        "Item C (source_quote: null) must NOT have _chunk_context spliced; "
        "the cascade's verbatim branch requires a non-empty string quote"
    )

    # ---- `reason` field stripped from items sent to the filter ----------
    # Independent confirmation of an existing cascade invariant: the
    # filter must judge each item independently of Haiku's reasoning.
    for entry in sent_items:
        assert "reason" not in entry["item"], (
            f"cascade leaked the Haiku reason field to the filter for "
            f"item_idx={entry['item_idx']}: {entry!r}"
        )


def test_smoke_cascade_response_schema_pins_keep_drop_enum() -> None:
    """Tripwire: the cascade response schema's `decision` enum MUST be
    exactly `{"keep", "drop"}`. The cascade NEVER invents or mutates
    items (see `cascade/executor.py` module docstring), so widening the
    enum is a contract break. If a future PR widens the enum, this
    fixture / test bundle would silently accept the new value — lock
    the enum here so the widening is caught at test time.
    """
    schema_path = (
        Path(__file__).resolve().parents[1].parents[0]
        / "src"
        / "spectrum_systems_core"
        / "cascade"
        / "cascade_filter_response.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    decision_enum = schema["items"]["properties"]["decision"]["enum"]
    assert set(decision_enum) == {"keep", "drop"}, (
        f"cascade response schema enum must be exactly "
        f"{{'keep', 'drop'}}; got {set(decision_enum)!r}. Widening the "
        f"enum breaks the cascade contract per "
        f"`cascade/executor.py` module docstring."
    )
