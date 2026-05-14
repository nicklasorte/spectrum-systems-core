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

from typing import Tuple


# Whole-word verbs that license classifying a chunk as a *decision*.
# Order is preserved so the few-shot prompt section is stable across
# runs (the prompt builder iterates this list and renders it verbatim).
REGULATORY_VERBS: Tuple[str, ...] = (
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
DECISION_OUTCOME_TYPES: Tuple[str, ...] = (
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
CLAIM_TYPES: Tuple[str, ...] = (
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
OVERGENERALIZATION_MARKERS: Tuple[str, ...] = (
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


# Phase Z.2: verbs the regulatory_verb eval treats as "informal" — the
# decision item exists, but the verb does not by itself license a
# regulatory classification. Spectrum policy transcripts mix informal
# and formal language; the eval warns on these (does NOT block) so an
# operator can see the ambiguity without halting the loop.
AMBIGUOUS_VERBS: frozenset = frozenset({
    "discussed", "mentioned", "raised", "indicated", "suggested",
    "proposed", "recommended",
})


__all__ = [
    "AMBIGUOUS_VERBS",
    "CLAIM_TYPES",
    "DECISION_OUTCOME_TYPES",
    "DECISION_VERBS",
    "OUTCOME_TO_VERBS",
    "OVERGENERALIZATION_MARKERS",
    "REGULATORY_VERBS",
]
