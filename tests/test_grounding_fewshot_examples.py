"""Tests for the Phase 4.A few-shot ``source_quote`` refresh.

PR #237 added the verbatim-grounding prompt section but the 3.E
few-shot examples did not yet carry ``source_quote`` on their
claim-shaped items. These tests defend the trust property that the
prompt's own examples PASS the gate the prompt instructs the model to
clear: every claim-shaped item in every example must have a
``source_quote`` that is a normalized substring of that example's
transcript chunk.

A drift here would teach the model to omit ``source_quote`` even
though the operative rule above the examples says it is required —
the exact foot-gun where examples silently weaken a rule.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from spectrum_systems_core.promotion.grounding_gate import (
    CLAIM_SHAPED_TYPES,
    MIN_QUOTE_LENGTH,
    check_grounding,
    normalize_for_grounding,
)

PROMPTS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "spectrum_systems_core"
    / "workflows"
    / "prompts"
)
LLM_PROMPT = PROMPTS_DIR / "meeting_minutes_llm.md"
OPUS_PROMPT = PROMPTS_DIR / "meeting_minutes_opus.md"

BEGIN_MARKER = "<!-- FEW_SHOT_3E_BEGIN -->"
END_MARKER = "<!-- FEW_SHOT_3E_END -->"

# The three transcript chunks shipped with the 3.E examples. Kept
# inline (not auto-extracted from the markdown) so a future edit that
# accidentally rewrites a chunk and an example in lockstep — leaving
# the gate happy but the rule changed — is caught by the source-quote
# assertion failing against THIS frozen chunk.
EX1_CHUNK = (
    "OK so the group has agreed — we will use the propagation methodology "
    "from Chapter 5 of the NTIA Manual as the baseline for all protection "
    "zone calculations in this study. That's the decision."
)
EX2_CHUNK = (
    "You know, one option we could look at — and I'm just throwing this out "
    "there — is whether we could carve out some protected sites in the "
    "Pacific theater and still run the rest of the analysis CONUS-only. "
    "That would maybe simplify things. But we'd have to think about that."
)
EX3_CHUNK = (
    "So I think what we're hearing from Lenay is that the scope needs to "
    "cover US and Possessions, not just CONUS. We haven't gotten that in "
    "writing yet, but we're moving forward on that basis. Kerry, can you "
    "make sure the system list reflects that geographic scope?"
)
CHUNKS_IN_ORDER = (EX1_CHUNK, EX2_CHUNK, EX3_CHUNK)


def _extract_3e_block(prompt_text: str) -> str:
    """Return the FEW_SHOT_3E_BEGIN..END block from a prompt."""
    start = prompt_text.index(BEGIN_MARKER)
    end = prompt_text.index(END_MARKER, start) + len(END_MARKER)
    return prompt_text[start:end]


def _parse_examples(block: str) -> list[dict]:
    """Return each ```json``` JSON object inside the 3.E block."""
    blocks = re.findall(r"```json(.*?)```", block, re.DOTALL)
    return [json.loads(b) for b in blocks]


@pytest.fixture(scope="module")
def llm_examples() -> list[dict]:
    block = _extract_3e_block(LLM_PROMPT.read_text(encoding="utf-8"))
    examples = _parse_examples(block)
    assert len(examples) == 3, "expected exactly 3 examples in 3.E block"
    return examples


@pytest.fixture(scope="module")
def opus_examples() -> list[dict]:
    block = _extract_3e_block(OPUS_PROMPT.read_text(encoding="utf-8"))
    examples = _parse_examples(block)
    assert len(examples) == 3, "expected exactly 3 examples in 3.E block"
    return examples


# --------------------------------------------------------------------------
# source_quote presence + length per claim-shaped item
# --------------------------------------------------------------------------
def test_fewshot_3e_examples_have_source_quote_on_claim_shaped_items(
    llm_examples: list[dict],
) -> None:
    """Every non-empty claim-shaped item must carry a ``source_quote``.

    A claim-shaped item without ``source_quote`` is the exact failure
    the gate exists to catch — the example would teach the model to
    omit a field the operative rule says is required.
    """
    for i, example in enumerate(llm_examples, start=1):
        for type_name in CLAIM_SHAPED_TYPES:
            items = example.get(type_name, [])
            if not items:
                continue
            for j, item in enumerate(items):
                assert isinstance(item, dict), (
                    f"example {i} {type_name}[{j}] is not a dict — "
                    f"the schema requires structured items on claim-shaped "
                    f"types after Phase 4.A"
                )
                quote = item.get("source_quote")
                assert isinstance(quote, str) and len(quote) >= MIN_QUOTE_LENGTH, (
                    f"example {i} {type_name}[{j}] missing or too-short "
                    f"source_quote (got {quote!r})"
                )


# --------------------------------------------------------------------------
# source_quote substring-match the example's transcript chunk
# --------------------------------------------------------------------------
def test_fewshot_3e_source_quotes_pass_grounding_gate(
    llm_examples: list[dict],
) -> None:
    """Every example's items must clear ``check_grounding`` against
    that example's transcript chunk.

    The test feeds the example items into the SAME gate the workflow
    uses, with the example's chunk as the only haystack. A failure
    here means the example contradicts the prompt's rule — running
    the model against that example would teach it to emit an
    ungrounded source_quote.
    """
    for i, (example, chunk) in enumerate(
        zip(llm_examples, CHUNKS_IN_ORDER), start=1
    ):
        # Use a synthetic chunk_id so check_grounding scopes the search
        # to the example's chunk; the items themselves omit
        # source_chunk_id (Phase 4.A allows the fall-back-to-transcript
        # behaviour) so the gate uses the full_transcript haystack.
        items_for_gate = {
            t: list(example.get(t, []))
            for t in CLAIM_SHAPED_TYPES
        }
        result = check_grounding(
            items_for_gate, {"ex": chunk}, chunk
        )
        assert result.passed, (
            f"example {i} fails the grounding gate:\n"
            + "\n".join(
                f"  {f.extraction_type}[{f.item_index}] "
                f"{f.reason.value}: {f.detail}"
                for f in result.failures
            )
        )


# --------------------------------------------------------------------------
# Byte-identical 3.E block between the two prompts
# --------------------------------------------------------------------------
def test_fewshot_3e_source_quotes_byte_identical_between_prompts() -> None:
    """The 3.E block (including new ``source_quote`` values) must be
    byte-identical across Haiku and Opus prompts.

    Drift here would mean one prompt teaches a different set of
    grounding examples than the other — a silent fork that would
    surface as model-attributable F1 noise.
    """
    llm_block = _extract_3e_block(LLM_PROMPT.read_text(encoding="utf-8"))
    opus_block = _extract_3e_block(OPUS_PROMPT.read_text(encoding="utf-8"))
    assert llm_block == opus_block, (
        "Few-shot 3.E block drifted between prompts. First diff line:\n"
        + next(
            (
                f"  L: {l!r}\n  O: {o!r}"
                for l, o in zip(
                    llm_block.splitlines(), opus_block.splitlines()
                )
                if l != o
            ),
            "(length differs)",
        )
    )


# --------------------------------------------------------------------------
# Quotes are real verbatim substrings (normalize-then-substring check
# matches what the gate would do at production time)
# --------------------------------------------------------------------------
def test_fewshot_3e_quotes_are_normalized_substrings(
    llm_examples: list[dict],
) -> None:
    """Per-item: ``normalize_for_grounding(quote)`` must appear in
    ``normalize_for_grounding(chunk)``. This is the same predicate
    ``check_grounding`` runs, asserted directly so the failure
    message names the failing quote rather than the per-item failure
    record.
    """
    for i, (example, chunk) in enumerate(
        zip(llm_examples, CHUNKS_IN_ORDER), start=1
    ):
        norm_chunk = normalize_for_grounding(chunk)
        for type_name in CLAIM_SHAPED_TYPES:
            for j, item in enumerate(example.get(type_name, []) or []):
                quote = item.get("source_quote")
                if quote is None:
                    continue
                norm_quote = normalize_for_grounding(quote)
                assert norm_quote in norm_chunk, (
                    f"example {i} {type_name}[{j}] source_quote "
                    f"{quote!r} normalized to {norm_quote!r}, which is "
                    f"NOT a substring of the example chunk "
                    f"(normalized: {norm_chunk!r})"
                )
