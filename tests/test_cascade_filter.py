"""Phase 4.C cascade filter unit tests.

Each test pins one failure mode: keep/drop/modify happy paths, the
modify re-grounding guard, the malformed-response fail-closed drop,
the max-batches cost cap, the bypass path, and the constant/prompt
loading discipline.

Tests inject a deterministic ``api_client`` so the module is exercised
end-to-end without network. The cascade module is pure except for the
api_client, so a stub api_client gives byte-stable test output.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from spectrum_systems_core.promotion.cascade_filter import (
    CASCADE_BATCH_SIZE,
    CASCADE_FILTER_MODEL,
    CASCADE_FILTER_SCHEMA_VERSION,
    CASCADE_MAX_BATCHES_DEFAULT,
    CascadeDecision,
    CascadeDropReason,
    filter_items,
    load_cascade_prompt_template,
    load_meeting_minutes_prompt,
    load_type_definitions,
    parse_type_disqualifiers,
    render_prompt,
)
from spectrum_systems_core.promotion.grounding_gate import CLAIM_SHAPED_TYPES


# --------------------------------------------------------------------------
# Stubs / fixtures
# --------------------------------------------------------------------------


def _make_grounded_item(
    quote: str, chunk_id: str = "c1", **extra
) -> dict:
    item = {
        "text": quote,
        "source_quote": quote,
        "source_chunk_id": chunk_id,
        "grounding_mode": "verbatim",
        "reason": "Test fixture.",
    }
    item.update(extra)
    return item


class _StubApiClient:
    """Deterministic api_client. Configurable per-call by predicate.

    Predicate is invoked per (extraction_type, source_quote, item_index)
    and returns the decision string for that item. The stub assembles
    the per-batch JSON array response itself.
    """

    def __init__(self, decide_fn, modified_text_for=None):
        self.decide_fn = decide_fn
        self.modified_text_for = modified_text_for or {}
        self.call_count = 0
        self.calls: list[dict] = []

    def __call__(self, prompt: str, model: str = CASCADE_FILTER_MODEL) -> str:
        self.call_count += 1
        self.calls.append({"prompt": prompt, "model": model})
        # Parse the items_json out of the prompt — the stub needs to
        # know the batch contents to compose its response.
        # The renderer inlines the items as a `[...]` JSON block after
        # the `## Items to adjudicate` heading, fenced with ```json.
        items_block = _extract_items_block(prompt)
        response = []
        for entry in items_block:
            idx = entry["item_index"]
            typ = entry["extraction_type"]
            quote = entry["source_quote"]
            decision = self.decide_fn(typ, quote, idx, entry)
            row = {
                "item_index": idx,
                "decision": decision,
                "reason": f"stub: {decision} for {typ}",
            }
            if decision == "modify":
                row["modified_text"] = self.modified_text_for.get(
                    (typ, quote), quote
                )
            response.append(row)
        return json.dumps(response)


def _extract_items_block(prompt: str) -> list[dict]:
    """Pull the JSON list embedded between the items heading + fence."""
    marker = "## Items to adjudicate"
    pos = prompt.find(marker)
    assert pos >= 0, "prompt template lost the items heading"
    fence_open = prompt.find("```json", pos)
    fence_close = prompt.find("```", fence_open + len("```json"))
    block = prompt[fence_open + len("```json") : fence_close].strip()
    return json.loads(block)


def _make_inputs(items_by_type):
    """Build the filter_items inputs from a {type: [item, ...]} dict."""
    type_defs = load_type_definitions()
    type_disq = parse_type_disqualifiers(load_meeting_minutes_prompt())
    chunks = {"c1": "Long enough chunk text that contains every test quote: "
              "we will adopt the new propagation method as the baseline. "
              "we should think about an alternative methodology too. "
              "we will use the propagation methodology from Chapter 5 of the "
              "NTIA Manual. ALPHA BETA GAMMA."}
    return {
        "grounded_items_by_type": items_by_type,
        "chunks_by_id": chunks,
        "type_definitions": type_defs,
        "type_disqualifiers": type_disq,
    }


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


def test_filter_items_keeps_correct_item():
    inputs = _make_inputs(
        {"decisions": [_make_grounded_item(
            "we will adopt the new propagation method as the baseline"
        )]}
    )
    stub = _StubApiClient(lambda typ, q, i, e: "keep")
    result = filter_items(**inputs, api_client=stub)
    assert result.total_items == 1
    assert result.kept_count == 1
    assert result.dropped_count == 0
    assert result.modified_count == 0
    assert result.item_results[0].decision == CascadeDecision.KEEP


def test_filter_items_drops_over_extracted_item():
    inputs = _make_inputs(
        {"decisions": [_make_grounded_item(
            "we should think about an alternative methodology too"
        )]}
    )
    stub = _StubApiClient(lambda typ, q, i, e: "drop")
    result = filter_items(**inputs, api_client=stub)
    assert result.dropped_count == 1
    assert result.kept_count == 0
    drop_reason = result.item_results[0].reason
    assert drop_reason.startswith(CascadeDropReason.SONNET_DROP.value)


def test_filter_items_modifies_acceptable_item():
    # Original quote is verbose; modify tightens to a verbatim substring.
    long_quote = (
        "we will use the propagation methodology from Chapter 5 of the "
        "NTIA Manual"
    )
    tightened = "we will use the propagation methodology from Chapter 5"
    inputs = _make_inputs(
        {"decisions": [_make_grounded_item(long_quote)]}
    )
    stub = _StubApiClient(
        lambda typ, q, i, e: "modify",
        modified_text_for={("decisions", long_quote): tightened},
    )
    result = filter_items(**inputs, api_client=stub)
    assert result.modified_count == 1
    assert result.dropped_count == 0
    final = result.item_results[0].final_item
    assert final is not None
    assert final["source_quote"] == tightened
    assert final["text"] == tightened  # text mirrored source_quote


def test_modify_breaking_grounding_is_dropped():
    long_quote = (
        "we will use the propagation methodology from Chapter 5 of the "
        "NTIA Manual"
    )
    inputs = _make_inputs(
        {"decisions": [_make_grounded_item(long_quote)]}
    )
    # Stub provides a "modified_text" that is NOT a substring of the
    # chunk — the cascade must drop with MODIFY_BROKE_GROUNDING.
    bad_paraphrase = "the group chose the propagation approach from Chapter 5"
    stub = _StubApiClient(
        lambda typ, q, i, e: "modify",
        modified_text_for={("decisions", long_quote): bad_paraphrase},
    )
    result = filter_items(**inputs, api_client=stub)
    assert result.modified_count == 0
    assert result.dropped_count == 1
    assert result.item_results[0].reason.startswith(
        CascadeDropReason.MODIFY_BROKE_GROUNDING.value
    )


def test_malformed_sonnet_response_drops_item():
    inputs = _make_inputs(
        {"decisions": [_make_grounded_item(
            "we will adopt the new propagation method as the baseline"
        )]}
    )

    def bad_client(prompt: str, model: str = CASCADE_FILTER_MODEL) -> str:
        return "not json at all"

    result = filter_items(**inputs, api_client=bad_client)
    # Fail-closed: drop, not keep.
    assert result.dropped_count == 1
    assert result.kept_count == 0
    assert result.item_results[0].reason.startswith(
        CascadeDropReason.SONNET_RESPONSE_INVALID.value
    )


def test_batch_results_match_per_item_results():
    """Single batch of 5 vs five batches of 1 produce same decisions."""
    quotes = [
        "we will adopt the new propagation method as the baseline",
        "we should think about an alternative methodology too",
        "we will use the propagation methodology from Chapter 5",
        "we will adopt the new propagation method as the baseline",
        "we should think about an alternative methodology too",
    ]
    items = [_make_grounded_item(q) for q in quotes]
    inputs_batch = _make_inputs({"decisions": items})

    def decide(typ, q, i, e):
        if "think about" in q:
            return "drop"
        return "keep"

    stub_batch = _StubApiClient(decide)
    batch_result = filter_items(
        **inputs_batch, api_client=stub_batch, batch_size=5
    )

    # Per-item: batch_size=1 forces one Sonnet call per item.
    stub_per_item = _StubApiClient(decide)
    per_item_result = filter_items(
        **inputs_batch, api_client=stub_per_item, batch_size=1
    )

    batch_decisions = [r.decision for r in batch_result.item_results]
    per_item_decisions = [r.decision for r in per_item_result.item_results]
    assert batch_decisions == per_item_decisions
    assert batch_result.kept_count == per_item_result.kept_count
    assert batch_result.dropped_count == per_item_result.dropped_count
    assert stub_batch.call_count == 1
    assert stub_per_item.call_count == 5


def test_max_batches_drops_remaining_items_fail_closed():
    """When max_batches caps mid-run, remaining items DROP — not pass."""
    items = [
        _make_grounded_item(
            f"we will adopt the new propagation method as the baseline {i}"
        )
        for i in range(20)
    ]
    inputs = _make_inputs({"decisions": items})
    stub = _StubApiClient(lambda typ, q, i, e: "keep")
    # batch_size 5, max_batches 2 → 10 items processed, 10 dropped as
    # MAX_BATCHES_EXCEEDED.
    result = filter_items(
        **inputs, api_client=stub, batch_size=5, max_batches=2
    )
    assert result.batches_used == 2
    assert result.kept_count == 10
    assert result.dropped_count == 10
    # The capped items carry the MAX_BATCHES_EXCEEDED reason — never
    # silently kept.
    capped = [
        r
        for r in result.item_results
        if r.reason.startswith(CascadeDropReason.MAX_BATCHES_EXCEEDED.value)
    ]
    assert len(capped) == 10


def test_disable_cascade_bypasses_filter_and_keeps_all():
    items = [
        _make_grounded_item(
            "we should think about an alternative methodology too"
        )
        for _ in range(7)
    ]
    inputs = _make_inputs({"decisions": items})
    result = filter_items(
        **inputs, api_client=None, disable_cascade=True
    )
    assert result.bypassed is True
    assert result.total_items == 7
    assert result.kept_count == 7
    assert result.dropped_count == 0
    assert result.batches_used == 0
    for r in result.item_results:
        assert r.decision == CascadeDecision.KEEP
        assert r.reason == "cascade_bypassed"


def test_disable_cascade_does_not_call_api_client():
    """A bypass run must not even attempt an api_client call."""
    items = [_make_grounded_item(
        "we will adopt the new propagation method as the baseline"
    )]
    inputs = _make_inputs({"decisions": items})

    def must_not_be_called(prompt, model=None):
        raise AssertionError("api_client was called on a bypass run")

    result = filter_items(
        **inputs, api_client=must_not_be_called, disable_cascade=True
    )
    assert result.bypassed is True


def test_cascade_filter_model_constant_is_canonical():
    """The literal `claude-sonnet-` must only appear in CASCADE_FILTER_MODEL
    in `src/spectrum_systems_core/promotion/cascade_filter.py`."""
    cascade_path = pathlib.Path(
        "src/spectrum_systems_core/promotion/cascade_filter.py"
    )
    text = cascade_path.read_text()
    # Only one occurrence is allowed: the CASCADE_FILTER_MODEL constant
    # definition. Anything else is a hardcoded string slipped in.
    hits = [
        ln
        for ln in text.splitlines()
        if "claude-sonnet-" in ln and "CASCADE_FILTER_MODEL" not in ln
        and not ln.lstrip().startswith("#")
    ]
    assert hits == [], (
        f"Found hardcoded sonnet model string(s) in cascade_filter.py "
        f"outside CASCADE_FILTER_MODEL: {hits}"
    )


def test_cascade_filter_model_value_is_sonnet_4_6():
    assert CASCADE_FILTER_MODEL == "claude-sonnet-4-6"


def test_cascade_filter_schema_version_is_initial():
    assert CASCADE_FILTER_SCHEMA_VERSION == "1.0.0"


def test_cascade_batch_and_max_constants():
    assert CASCADE_BATCH_SIZE == 10
    assert CASCADE_MAX_BATCHES_DEFAULT == 30


def test_type_disqualifiers_parsed_for_every_claim_shaped_type():
    """Every claim-shaped type must have non-empty disqualifier text."""
    disq = parse_type_disqualifiers(load_meeting_minutes_prompt())
    for typ in CLAIM_SHAPED_TYPES:
        assert typ in disq, f"missing disqualifier for {typ}"
        assert disq[typ].strip(), f"disqualifier for {typ} is empty"


def test_type_definitions_loaded_from_schema():
    """Every claim-shaped type has a non-empty definition string."""
    defs = load_type_definitions()
    for typ in CLAIM_SHAPED_TYPES:
        assert typ in defs
        assert defs[typ].strip()


def test_keep_decision_preserves_item_by_reference():
    item = _make_grounded_item(
        "we will adopt the new propagation method as the baseline"
    )
    inputs = _make_inputs({"decisions": [item]})
    stub = _StubApiClient(lambda typ, q, i, e: "keep")
    result = filter_items(**inputs, api_client=stub)
    final = result.item_results[0].final_item
    assert final is not None
    # Same content (modulo dict-copy semantics) — the cascade preserves
    # everything when keeping.
    assert final == item


def test_render_prompt_substitutes_all_three_placeholders():
    template = (
        "DEFS:\n{type_definitions}\nDISQ:\n{type_disqualifiers}\nITEMS:\n{items_json}"
    )
    out = render_prompt(
        template,
        type_definitions={"decisions": "A decision."},
        type_disqualifiers={"decisions": "Don't extract brainstorming."},
        items=[{"item_index": 0, "extraction_type": "decisions"}],
    )
    assert "A decision." in out
    assert "Don't extract brainstorming." in out
    assert '"item_index": 0' in out
    # All placeholders consumed.
    assert "{type_definitions}" not in out
    assert "{type_disqualifiers}" not in out
    assert "{items_json}" not in out


def test_cascade_prompt_template_has_required_placeholders():
    template = load_cascade_prompt_template()
    assert "{type_definitions}" in template
    assert "{type_disqualifiers}" in template
    assert "{items_json}" in template
    # The response-format section must specify the keep/drop/modify enum
    # so a future template edit cannot silently lose it.
    assert "## Response format" in template
    for keyword in ("keep", "drop", "modify"):
        assert keyword in template


def test_modify_rejects_paraphrase_substitution_attack():
    """A paraphrase that LOOKS reasonable but isn't a substring is dropped.

    This is the hallucination-vector test: Sonnet returns a modify with
    a paraphrase of the source. The cascade must drop, not silently
    accept, because passing the modify through would smuggle invented
    text into a 'promoted' artifact.
    """
    long_quote = (
        "we will use the propagation methodology from Chapter 5 of the "
        "NTIA Manual"
    )
    inputs = _make_inputs(
        {"decisions": [_make_grounded_item(long_quote)]}
    )
    # The paraphrase preserves meaning but every word is different — a
    # silent acceptance would be a hallucination promoted to product.
    paraphrase = "the team adopted the methodology defined by NTIA chapter five"
    stub = _StubApiClient(
        lambda typ, q, i, e: "modify",
        modified_text_for={("decisions", long_quote): paraphrase},
    )
    result = filter_items(**inputs, api_client=stub)
    assert result.modified_count == 0
    assert result.dropped_count == 1
    assert result.item_results[0].decision == CascadeDecision.DROP


def test_only_claim_shaped_types_pass_to_sonnet():
    """Items in non-claim-shaped types (e.g. topics) are not adjudicated."""
    inputs = _make_inputs(
        {
            "decisions": [_make_grounded_item(
                "we will adopt the new propagation method as the baseline"
            )],
            "topics": [{"text": "out of scope"}],  # non-claim-shaped
        }
    )
    stub = _StubApiClient(lambda typ, q, i, e: "keep")
    result = filter_items(**inputs, api_client=stub)
    # topics never entered the cascade.
    for r in result.item_results:
        assert r.extraction_type in CLAIM_SHAPED_TYPES
    assert result.total_items == 1


def test_cascade_handles_phase_4b_object_form_action_items():
    """Phase 4.B (PR #247) bumped meeting_minutes to 1.6.0 and tightened
    action_items to object-only. The cascade must adjudicate the new
    shape without choking on the missing legacy string branch."""
    inputs = _make_inputs(
        {
            "action_items": [
                {
                    "action": "we will adopt the new propagation method as the baseline",
                    "source_quote": "we will adopt the new propagation method as the baseline",
                    "source_chunk_id": "c1",
                    "grounding_mode": "verbatim",
                    "reason": "explicit commitment",
                }
            ]
        }
    )
    stub = _StubApiClient(lambda typ, q, i, e: "keep")
    result = filter_items(**inputs, api_client=stub)
    assert result.total_items == 1
    assert result.kept_count == 1
    # The object-form action_item passes through with all its fields
    # intact.
    final = result.item_results[0].final_item
    assert final is not None
    assert final["action"] == (
        "we will adopt the new propagation method as the baseline"
    )


def test_cascade_disqualifiers_pick_up_4b_decisions_guidance():
    """The on-disk Phase 4.B prompt's decisions disqualifier
    ('probably', 'finishing before 1:00', etc.) flows through to the
    rendered cascade prompt."""
    disq = parse_type_disqualifiers(load_meeting_minutes_prompt())
    # Phase 4.B authored the decisions entry from haiku_only patterns;
    # the 'probably' tentative-language signal must survive parsing.
    assert "probably" in disq["decisions"].lower(), (
        f"4.B 'probably' signal missing from decisions disqualifier: "
        f"{disq['decisions']!r}"
    )


def test_response_with_wrong_count_drops_batch():
    """Sonnet returns fewer entries than expected — fail-closed drop."""
    items = [_make_grounded_item(
        "we will adopt the new propagation method as the baseline " + str(i)
    ) for i in range(3)]
    inputs = _make_inputs({"decisions": items})

    def short_response(prompt, model=None):
        # Only return one entry instead of three.
        return json.dumps([{
            "item_index": 0, "decision": "keep", "reason": "ok"
        }])

    result = filter_items(**inputs, api_client=short_response)
    assert result.dropped_count == 3
    assert result.kept_count == 0
