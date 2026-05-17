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
3. REGULATORY_TAXONOMY_BLOCK (decision extractor only)
4. Glossary (terminology) block
5. Few-shot examples block
6. Chunk content
7. Output schema description
8. CONFIDENCE_SCORING_BLOCK

Phase T.1: the regulatory-verb taxonomy is now sourced from
``spectrum_systems_core.config.taxonomy``. The block is built lazily so
the same Python object backs both the prompt text and the binding
validator -- drift between the two is structurally impossible.
"""
from __future__ import annotations

import hashlib

from ..config.taxonomy import (
    DECISION_OUTCOME_TYPES,
    OUTCOME_TO_VERBS,
)

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
    "For each extracted decision or claim, add a \"confidence\" field "
    "(0.0 to 1.0):\n"
    "  1.0     -- explicitly stated, unambiguous, speaker clearly committed\n"
    "  0.7-0.9 -- clearly implied, strong evidence in source_turns\n"
    "  0.4-0.6 -- inferred, indirect evidence, requires interpretation\n"
    "  0.0-0.3 -- weak evidence, speculative\n"
    "\n"
    "Briefly state your reasoning before assigning the score.\n"
    "If you would score an item below 0.5: OMIT it instead of including it\n"
    "with a low confidence score. Low confidence is a reason to omit.\n"
    "If not in transcript, omit; do not infer."
)


# ----------------------------------------------------------------------
# Phase T.1: regulatory taxonomy injected into the decision prompt.
# The block is built once at import time from the canonical lists so the
# decision extractor cannot drift from the binding validator. We do NOT
# rebuild on every prompt because the source lists are tuples (immutable
# at the language level).
# ----------------------------------------------------------------------

def _build_regulatory_taxonomy_block() -> str:
    parts: list = [
        "===============================================================",
        "DECISION CLASSIFICATION RULES (regulatory taxonomy):",
        "",
        "A decision MUST contain at least one of the following whole-word",
        "outcome verbs. If none are present, extract as a claim instead.",
        "",
    ]
    for outcome, verbs in OUTCOME_TO_VERBS.items():
        parts.append(f"  {outcome}: {', '.join(verbs)}")
    parts.append("")
    parts.append(
        "Attach a \"decision_outcome\" field to every extracted decision."
    )
    parts.append(
        f"  decision_outcome must be one of: {', '.join(DECISION_OUTCOME_TYPES)}"
    )
    parts.append("")
    parts.append(
        "If the source text contains none of the listed verbs, do not"
    )
    parts.append(
        "emit a decision. Re-classify the chunk as a procedural claim."
    )
    parts.append(
        "==============================================================="
    )
    return "\n".join(parts)


REGULATORY_TAXONOMY_BLOCK: str = _build_regulatory_taxonomy_block()


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


# ----------------------------------------------------------------------
# Phase P2-B: prompt_version derivation.
#
# Stamped on every meeting_extraction artifact so a regression after a
# prompt edit can be traced to the change. The version is derived from
# the concatenated stable prompt fragments + the OUTPUT SCHEMA literal
# from decision_extractor._build_prompt -- the same bytes the model
# actually sees. Anything else (chunk content, glossary, few-shot,
# attention block) varies per-run and would defeat the rollback signal.
# ----------------------------------------------------------------------

_DECISION_OUTPUT_SCHEMA_LITERAL: str = (
    "OUTPUT SCHEMA:\n"
    "Return JSON {\"items\": [{\"decision_text\": <str>, "
    "\"decision_type\": <one of approved/rejected/deferred/noted/"
    "considered/action_required/open_question/to_be_determined>, "
    "\"decision_outcome\": <one of approval/rejection/deferral/"
    "action_required/noted/question>, "
    "\"stakeholders\": [<str>...], \"rationale\": <str or null>, "
    "\"source_turn_ids\": [<chunk_id>...], "
    "\"confidence\": <number 0.0-1.0>}]}. "
    "Every item MUST cite at least one source_turn_id. Use the "
    "controlled vocabulary exactly."
)


def _canonical_prompt_template() -> str:
    """Return the byte-stable concatenation of the run-invariant prompt parts.

    Excludes chunk text, glossary, few-shot content, and attention
    blocks because those vary per chunk / per run. Includes the
    PROMPT_SCHEMA_VERSION literal so a few-shot schema bump also bumps
    prompt_version.
    """
    return "\n\n".join((
        f"PROMPT_SCHEMA_VERSION={PROMPT_SCHEMA_VERSION}",
        OMIT_INSTRUCTION_BLOCK,
        REGULATORY_TAXONOMY_BLOCK,
        CONFIDENCE_SCORING_BLOCK,
        _DECISION_OUTPUT_SCHEMA_LITERAL,
    ))


def compute_prompt_version(prompt_template: str) -> str:
    """Deterministic version string for an extraction prompt template.

    Same input bytes => same version; one-byte change => different
    version. Format: ``sha256:<first 12 chars of hex digest>``. The
    12-char prefix is enough to keep a unique value across the small
    universe of prompt versions this repo ships while keeping the
    artifact field human-readable in step summaries.
    """
    digest = hashlib.sha256(prompt_template.encode("utf-8")).hexdigest()
    return "sha256:" + digest[:12]


PROMPT_VERSION: str = compute_prompt_version(_canonical_prompt_template())
