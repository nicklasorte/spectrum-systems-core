"""Phase T.1: domain taxonomy for spectrum policy extraction.

CANONICAL LOCATION. Every consumer of these lists MUST import them from
this module; never redefine them inline. Tests assert that downstream
modules (extraction prompt builder, binding validator) import the same
object (``id()`` equality) so a future engineer cannot silently drift
two copies.

The motivation is the "considered vs approved vs deferred" error class:
a meeting transcript where the FCC "considered" a band plan is not a
decision artifact, but the upstream LLM has no domain signal to make
that distinction without the taxonomy in its prompt. By keeping the
prompt's vocabulary and the post-hoc binding validator's vocabulary in
one place, drift is structurally impossible.
"""
from __future__ import annotations

# Whole-word verbs that license classifying a chunk as a *decision*.
# Order is preserved so the few-shot prompt section is stable across
# runs (the prompt builder iterates this list and renders it verbatim).
REGULATORY_VERBS: tuple[str, ...] = (
    "approved",
    "rejected",
    "deferred",
    "noted",
    "required",
    "recommended",
    "prohibited",
    "authorized",
    "designated",
    "adopted",
    "declined",
    "tabled",
    "withdrawn",
    "accepted",
    "denied",
    "postponed",
    "amended",
    "ratified",
    "revoked",
)


# Outcome classification for a decision. The extraction prompt instructs
# the model to attach exactly one of these values to every decision via
# the optional ``decision_outcome`` field. The binding validator can
# downstream consult this enum to detect outcome/verb mismatch.
DECISION_OUTCOME_TYPES: tuple[str, ...] = (
    "approval",
    "rejection",
    "deferral",
    "action_required",
    "noted",
    "question",
)


# Claim types — identical to the existing claim_extractor enum but
# hosted here so the prompt builder and the schema can both import from
# a single source.
CLAIM_TYPES: tuple[str, ...] = (
    "technical",
    "procedural",
    "regulatory",
    "opinion",
)


# Pre-built mapping from outcome class to the verbs that license it.
# Used by the prompt builder to render the "If the source contains X
# verb, emit Y outcome" few-shot block.
OUTCOME_TO_VERBS: dict = {
    "approval": ("approved", "authorized", "adopted", "ratified", "accepted"),
    "rejection": ("rejected", "denied", "declined", "withdrawn", "revoked"),
    "deferral": ("deferred", "tabled", "postponed"),
    "action_required": ("required", "recommended"),
    "noted": ("noted", "designated", "amended"),
}


# Phase V.6: scope over-broadening markers. The generalization-bias
# detector fires when source_text contains a specific band reference
# (e.g. "7 GHz", "6525 MHz") AND extracted_text contains one of these
# markers. Updating this list is a policy change and requires a PR --
# same governance as REGULATORY_VERBS.
OVERGENERALIZATION_MARKERS: tuple[str, ...] = (
    "all spectrum",
    "all bands",
    "all frequencies",
    "entire spectrum",
    "any frequency",
    "all allocations",
    "every band",
    "the spectrum as a whole",
)


# Phase Z.2: canonical decision-licensing verbs for the
# ``regulatory_verb`` eval. This set is narrower than ``REGULATORY_VERBS``
# above because the eval needs an authoritative "pass" classification:
# a transcript using one of these verbs on a decision item is a
# decision in the regulatory sense and no ambiguity finding is emitted.
# Kept as ``frozenset`` so a typo in a consumer fails at import-time
# instead of mutating the shared list at runtime.
DECISION_VERBS: frozenset = frozenset({
    "approved", "rejected", "deferred", "noted", "directed", "considered",
})


# Decision-licensing verbs the meeting_minutes_llm extraction PROMPT
# itself sanctions but that are absent from ``REGULATORY_VERBS`` /
# ``DECISION_VERBS``. The prompt's own decision category definition
# enumerates "something the meeting decided, approved, rejected,
# deferred, adopted, or AGREED" and instructs the model to emit "the
# governing decision verb actually used in the transcript". A
# correctly-extracted object-form decision the model faithfully labels
# with the real decision verb used ("agreed" / "decided" / a direct
# decision synonym) was hard-blocked by ``regulatory_verb`` because the
# eval's classified set omitted these — the exact prompt↔eval taxonomy
# drift CLAUDE.md forbids (the IDENTICAL decision in plain-string form
# already promotes; the eval never verb-checks string decisions, so the
# block was an object-vs-string inconsistency, not a trust property).
#
# This is a CLOSED, curated set of unambiguous DECISION acts (the
# decision side of the constitution's "considered vs decided" line),
# NOT an open synonym pile: a hallucinated / garbage verb
# ("frobnicated") and a missing verb still fall through to the
# fail-closed block / PR #144 sentinel exactly as before. Widening the
# classified-pass set with verbs the prompt already sanctions removes
# the drift WITHOUT weakening the gate — the same reconciliation
# PR #146 applied for ``adopted`` / ``authorized`` / ``ratified``.
# Updating this list is a policy change and requires a PR — same
# governance as ``REGULATORY_VERBS``.
DECISION_SYNONYM_VERBS: frozenset = frozenset({
    "agreed",      # prompt decision definition enumerates it explicitly
    "decided",     # prompt: "something the meeting DECIDED"
    "endorsed",    # decision-side synonym of approved / adopted
    "concurred",   # formal agreement on the record = a decision
    "confirmed",   # decision-side confirmation of a position
    "finalized",   # decision-side: the group finalized X
    "resolved",    # formal "resolved that ..." decision verb
})


# Phase Z.2: verbs the regulatory_verb eval treats as "informal" — the
# decision item exists, but the verb does not by itself license a
# regulatory classification. Spectrum policy transcripts mix informal
# and formal language; the eval warns on these (does NOT block) so an
# operator can see the ambiguity without halting the loop.
AMBIGUOUS_VERBS: frozenset = frozenset({
    "discussed", "mentioned", "raised", "indicated", "suggested",
    "proposed", "recommended",
})


# Explicit sentinel the meeting_minutes_llm producer writes onto an
# object-form decision when the model emitted the OBJECT form (to
# attach stakeholders / confidence — which the extraction prompt
# encourages) but supplied no governing verb AND the decision text
# carries no taxonomy verb either. It records, ON the artifact, that
# the governing verb is indeterminate — an auditable field, not a
# silent extraction gap. The regulatory_verb eval recognises ONLY this
# exact string as the "extractor made no verb claim" marker and treats
# it the same non-blocking way it ALREADY treats a verb-free STRING
# decision (which has always promoted). A decision that CLAIMS a verb
# the taxonomy does not recognise still blocks — the producer never
# overrides a claimed verb, so the hallucination / mis-extraction
# defence is unchanged. Canonical here (CLAUDE.md taxonomy rule) so the
# producer and the eval import one object and cannot drift.
UNCLASSIFIED_DECISION_VERB: str = "unclassified"


# Phase AB.4: threshold for considering an extracted item to match a
# gold item in the extraction-gap metric. Pinned at 0.7 to match
# ``evals.extraction_precision.LCS_THRESHOLD`` (Phase Z) so the gap
# instrument and the precision eval use one paraphrase boundary.
# Lowering this without consultation is a measurement-trust regression;
# ``tests/evals/test_extraction_gap.py::test_lcs_threshold_pinned``
# pins the 0.7 value.
EXTRACTION_GAP_MIN_LCS = 0.7


# Phase AC.1: three-bucket extraction match thresholds. These two
# constants partition every extracted item into exactly one of three
# buckets relative to its best per-category gold match:
#
#   - matched  : LCS >= MATCH_LCS_THRESHOLD            (true positive)
#   - partial  : PARTIAL_LCS_THRESHOLD <= LCS < MATCH  (suspicious —
#                a possible hallucinated paraphrase; NOT a TP)
#   - spurious : LCS < PARTIAL_LCS_THRESHOLD            (false positive)
#
# MATCH_LCS_THRESHOLD intentionally equals EXTRACTION_GAP_MIN_LCS (and
# therefore the Phase Z ``extraction_precision`` threshold) so the
# per-entity instrument and the aggregate gap instrument share one
# paraphrase boundary. Partial matches count as FP for precision — the
# partial bucket exists for diagnostics, never to inflate scores.
# ``tests/evals/test_per_entity_metrics.py::test_match_thresholds_pinned``
# pins both values.
MATCH_LCS_THRESHOLD = 0.7
PARTIAL_LCS_THRESHOLD = 0.4


__all__ = [
    "AMBIGUOUS_VERBS",
    "CLAIM_TYPES",
    "DECISION_OUTCOME_TYPES",
    "DECISION_SYNONYM_VERBS",
    "DECISION_VERBS",
    "EXTRACTION_GAP_MIN_LCS",
    "MATCH_LCS_THRESHOLD",
    "OUTCOME_TO_VERBS",
    "OVERGENERALIZATION_MARKERS",
    "PARTIAL_LCS_THRESHOLD",
    "REGULATORY_VERBS",
    "UNCLASSIFIED_DECISION_VERB",
]
