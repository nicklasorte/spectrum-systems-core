"""Shared prompt fragments and policy constants for typed extractors.

Defined in one place so the three extractors (decision, claim, action_item)
inject byte-identical OMIT and CONFIDENCE blocks. Tests assert presence of
exact substrings from these constants in the rendered prompts.

Block positioning per ``transcript_extraction_research_2026.pdf`` and the
Wan et al. positional-bias finding: critical instructions must precede the
chunk content, not trail it.

Prompt order enforced by every extractor's ``_build_prompt``:

1. Role / context
2. OMIT_INSTRUCTION_BLOCK
3. Glossary (terminology) block
4. Few-shot examples block
5. Chunk content
6. Output schema description
7. CONFIDENCE_SCORING_BLOCK
"""
from __future__ import annotations


# Prompt-schema version the typed extractors expect from FewShotLoader.
# Bumping this disables few-shot injection until the seed is re-authored
# against the new schema.
PROMPT_SCHEMA_VERSION: str = "1.0.0"


# Items with model-reported confidence strictly below this go to the HITL
# review queue (items_requiring_review=True, review_reason="low_confidence").
# They are NOT dropped: humans should see what the extractor was unsure
# about. 0.5 is the midpoint of the scoring rubric in CONFIDENCE_SCORING_BLOCK
# (0.5 == "reasonably inferred from context; some ambiguity").
CONFIDENCE_THRESHOLD: float = 0.5


OMIT_INSTRUCTION_BLOCK: str = (
    "===============================================================\n"
    "CRITICAL CONSTRAINT -- OMIT IF NOT IN TRANSCRIPT:\n"
    "\n"
    "If a claim, decision, or action item is NOT explicitly stated in the\n"
    "provided transcript chunks, DO NOT include it.\n"
    "\n"
    "Rules:\n"
    "- Do not infer. Do not extrapolate. Do not complete partial thoughts.\n"
    "- Do not include things that \"probably\" happened or \"would have\" been said.\n"
    "- If you cannot find direct evidence in the source_turn_ids: OMIT the item.\n"
    "- Uncertainty is not a reason to include. It is a reason to omit.\n"
    "- A short accurate extraction is better than a long inaccurate one.\n"
    "==============================================================="
)


CONFIDENCE_SCORING_BLOCK: str = (
    "CONFIDENCE SCORING:\n"
    "For each item you extract, assign a confidence score (0.0-1.0):\n"
    "  1.0 -- Explicitly stated in the transcript with clear attribution\n"
    "  0.8 -- Clearly implied by direct statements, unambiguous\n"
    "  0.5 -- Reasonably inferred from context; some ambiguity\n"
    "  0.3 -- Weak grounding; possible but not certain from this text\n"
    "  0.0 -- Should not be included (use this as a signal to omit instead)\n"
    "\n"
    "If you would score an item below 0.5: OMIT it instead of including it\n"
    "with a low confidence score. Low confidence is a reason to omit."
)


def normalize_confidence(value: object) -> float:
    """Clamp a model-supplied confidence value to ``[0.0, 1.0]``.

    Missing or non-numeric values become ``0.0`` -- that pushes the item
    into the review queue via the threshold rather than silently dropping
    it. We treat "model failed to score" the same as "model said zero".
    """
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        try:
            v = float(value)
        except (TypeError, ValueError):
            return 0.0
        if v < 0.0:
            return 0.0
        if v > 1.0:
            return 1.0
        return v
    return 0.0


def apply_confidence_threshold(
    items: list, threshold: float = CONFIDENCE_THRESHOLD
) -> int:
    """Mutate ``items`` in place: tag low-confidence items, return their count.

    Items with ``confidence < threshold`` get
    ``items_requiring_review = True`` and ``review_reason = "low_confidence"``.
    Items at or above the threshold are left untouched (preserving any
    existing ``items_requiring_review`` set by other code paths).
    """
    count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        conf = item.get("confidence", 0.0)
        if isinstance(conf, (int, float)) and float(conf) < threshold:
            item["items_requiring_review"] = True
            item["review_reason"] = "low_confidence"
            count += 1
    return count
