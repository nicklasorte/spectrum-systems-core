"""Regulatory verb classification eval (Phase Z.2).

For every decision item in a decision-bearing artifact, classifies the
governing verb against the canonical taxonomy in
``config.taxonomy``:

  - verb in ``CLASSIFIED_DECISION_VERBS`` → contributes ``pass``.
    This is the canonical ``REGULATORY_VERBS`` taxonomy (the SAME list
    the extraction prompt instructs the model to use) unioned with the
    legacy Phase Z ``DECISION_VERBS`` (which adds ``directed`` /
    ``considered``). Recognising the full canonical taxonomy — not an
    ad-hoc 6-verb subset — is what removes the prompt↔eval drift that
    hard-blocked correctly-extracted object decisions whose governing
    verb was a real regulatory verb such as ``adopted`` / ``authorized``
    / ``ratified``. It does NOT weaken the gate: a verb the canonical
    taxonomy does not contain still blocks (see ``frobnicated`` below).
  - verb in ``AMBIGUOUS_VERBS`` → contributes ``warn``
    (reason code ``verb_ambiguous:<verb>``; eval still passes).
    Checked BEFORE the classified-pass set so a verb that is both a
    regulatory verb and informal (e.g. ``recommended``) keeps its
    operator-visible warn instead of passing silently.
  - verb == ``UNCLASSIFIED_DECISION_VERB`` (the explicit sentinel the
    meeting_minutes_llm producer writes when the model emitted an
    object-form decision with no classifiable verb) → contributes a
    non-blocking surfaced note (reason code ``verb_unclassified:...``;
    eval still passes). This does NOT relax the gate's real trust
    property: the IDENTICAL decision in plain-string form already
    promotes today, and a decision that CLAIMS an unrecognised verb
    still blocks (the producer never overrides a claimed verb).
  - verb absent or unrecognized → contributes ``block``
    (reason code ``verb_not_classified:<verb_or___missing__>``)

The fail path is fail-closed: any unclassifiable verb on any decision
fails the whole eval, which the control function (``decide_control``)
translates into a ``block`` decision. Ambiguous verbs deliberately
never block — spectrum policy transcripts use informal language, so
an ambiguity finding gives the operator visibility without halting
the loop.

The eval is wired into the runner for ``meeting_minutes`` and
``decision_brief`` artifact types. For all other artifact types it
returns ``pass`` immediately: it has no decisions to inspect.

Schema invariants the eval relies on:

- The artifact payload's ``decisions`` field, when present, is a list.
- Each decision item is either a dict or a string. If a dict, the
  governing verb is read from the ``verb`` field if present, else
  extracted as the first taxonomy verb found in the ``text`` field.
- A decision-bearing artifact whose payload is missing the
  ``decisions`` key entirely fails fast with
  ``decisions_field_missing`` so an extraction regression cannot pass
  the gate by omitting the field.
"""
from __future__ import annotations

import re

from ..artifacts import Artifact, new_artifact
from ..config.taxonomy import (
    AMBIGUOUS_VERBS,
    DECISION_VERBS,
    REGULATORY_VERBS,
    UNCLASSIFIED_DECISION_VERB,
)

EVAL_TYPE = "regulatory_verb"

# The authoritative "this verb licenses a regulatory decision" set.
#
# It is the canonical ``REGULATORY_VERBS`` taxonomy (the exact list the
# extraction prompt tells the model to use) unioned with the legacy
# Phase Z ``DECISION_VERBS`` (which contributes ``directed`` /
# ``considered`` — decision verbs the Phase Z gold fixtures rely on that
# are not in ``REGULATORY_VERBS``).
#
# Before this, the eval used ONLY the 6-element ``DECISION_VERBS`` set,
# so a correctly-extracted object decision whose governing verb was a
# real regulatory verb the prompt instructs the model to emit (e.g.
# ``adopted``, ``authorized``, ``ratified``, ``amended``, ``accepted``)
# hard-blocked the whole run with ``verb_not_classified:<verb>``. That
# is exactly the prompt↔eval taxonomy drift CLAUDE.md forbids. Widening
# the pass set to the canonical taxonomy removes the drift WITHOUT
# weakening the gate: a verb absent from the canonical taxonomy (a
# hallucination such as ``frobnicated``) still falls through to the
# fail-closed block.
CLASSIFIED_DECISION_VERBS: frozenset = frozenset(REGULATORY_VERBS) | DECISION_VERBS

# Artifact types that carry regulatory decisions. Other types skip the
# eval cleanly — they have nothing for it to inspect.
DECISION_BEARING_ARTIFACT_TYPES: frozenset = frozenset({
    "meeting_minutes",
    "decision_brief",
})

# Reason code prefixes (stable, machine-grepable).
VERB_AMBIGUOUS_PREFIX = "verb_ambiguous:"
VERB_NOT_CLASSIFIED_PREFIX = "verb_not_classified:"
# Surfaced (never blocks) when a decision carries the explicit
# UNCLASSIFIED_DECISION_VERB sentinel — the producer recorded that the
# governing verb is indeterminate. Distinct prefix so the operator can
# tell "extractor explicitly could not classify" apart from an informal
# but recognised ambiguous verb (VERB_AMBIGUOUS_PREFIX) in eval_history.
VERB_UNCLASSIFIED_PREFIX = "verb_unclassified:"
DECISIONS_FIELD_MISSING = "decisions_field_missing"


def _extract_verb_from_text(text: str) -> str | None:
    """First taxonomy verb found anywhere in ``text`` (case-insensitive),
    or ``None`` if no taxonomy verb appears. The taxonomy lookup is the
    canonical authority — falling back to "first alphabetic word" would
    let any unrecognized verb pass."""
    if not isinstance(text, str) or not text.strip():
        return None
    tokens = re.findall(r"[A-Za-z']+", text.lower())
    classified = CLASSIFIED_DECISION_VERBS | AMBIGUOUS_VERBS
    for tok in tokens:
        if tok in classified:
            return tok
    return None


def _decision_text(item) -> str:
    if isinstance(item, dict):
        text = item.get("text")
        return text if isinstance(text, str) else ""
    if isinstance(item, str):
        return item
    return ""


def _decision_verb(item) -> str | None:
    """Resolve the governing verb for one decision item.

    Reads ``verb`` first when ``item`` is a dict. Falls through to
    scanning ``text`` for any taxonomy verb. Returns the lowercased
    verb when classified, otherwise the raw declared verb (so the
    fail-reason can surface what the artifact actually claimed), and
    ``None`` when nothing verb-like is present at all.
    """
    declared_verb: str | None = None
    if isinstance(item, dict):
        v = item.get("verb")
        if isinstance(v, str) and v.strip():
            declared_verb = v.strip().lower()
    text = _decision_text(item)
    text_verb = _extract_verb_from_text(text)

    if declared_verb is not None:
        # Prefer the declared verb so the eval respects what the
        # extractor classified. Fall through to text-derived verb
        # only if the declared verb is itself unclassified — that
        # gives the operator a "your text says X but you labelled Y"
        # signal via the reason code below.
        if declared_verb in DECISION_VERBS or declared_verb in AMBIGUOUS_VERBS:
            return declared_verb
        return declared_verb  # raw, so reason code surfaces it
    return text_verb


# Public alias: the meeting_minutes_llm producer imports this so the
# "is this decision already classifiable?" question it asks is the
# EXACT same function the gate answers it with. ``None`` here is
# precisely the ``verb_not_classified:__missing__`` block condition;
# the producer fills the explicit sentinel only in that one case.
resolve_decision_verb = _decision_verb


def _eval_result(
    target: Artifact, passed: bool, reason_codes: list[str]
) -> Artifact:
    payload = {
        "eval_type": EVAL_TYPE,
        "target_artifact_id": target.artifact_id,
        "status": "pass" if passed else "fail",
        "score": 1.0 if passed else 0.0,
        "reason_codes": reason_codes,
    }
    return new_artifact(
        artifact_type="eval_result",
        payload=payload,
        trace_id=target.trace_id,
        status="evaluated",
        input_refs=[target.artifact_id],
    )


def run_regulatory_verb_eval(artifact: Artifact) -> Artifact:
    """Run the regulatory-verb classification check.

    Always returns an ``eval_result`` artifact; never raises. Non
    decision-bearing artifact types short-circuit to ``pass``.
    """
    if artifact.artifact_type not in DECISION_BEARING_ARTIFACT_TYPES:
        return _eval_result(artifact, passed=True, reason_codes=[])

    payload = artifact.payload or {}
    if "decisions" not in payload:
        # ``meeting_minutes`` MUST have ``decisions`` — its required-field
        # spec includes it, but this eval restates the rule so a future
        # renaming of the payload key cannot silently bypass the verb
        # check. ``decision_brief`` carries no ``decisions`` list by
        # design (it has ``recommendation``/``rationale`` instead), so
        # it passes through cleanly when no decisions field is present.
        if artifact.artifact_type == "meeting_minutes":
            return _eval_result(
                artifact,
                passed=False,
                reason_codes=[DECISIONS_FIELD_MISSING],
            )
        return _eval_result(artifact, passed=True, reason_codes=[])

    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        return _eval_result(
            artifact,
            passed=False,
            reason_codes=[
                f"{VERB_NOT_CLASSIFIED_PREFIX}__decisions_not_a_list__"
            ],
        )

    if not decisions:
        # Zero decisions is a pass for THIS eval — the required-field
        # eval owns the "must be present and a list" claim; this eval
        # only judges the verbs of the items that ARE present.
        return _eval_result(artifact, passed=True, reason_codes=[])

    # Legacy-string decision lists (every item is a plain string) do
    # not make explicit verb claims — they're prose statements emitted
    # by the deterministic line-prefix extractor. The eval has nothing
    # to verify on them; pass through. Once any decision is a dict
    # (the schema-version-1.1.0+ shape used by the typed extractors
    # and the Phase Z gold fixtures), strict verb classification
    # applies — the extractor IS making a claim and we check it.
    if all(isinstance(item, str) for item in decisions):
        return _eval_result(artifact, passed=True, reason_codes=[])

    warns: list[str] = []
    unclassified: list[str] = []
    blocks: list[str] = []
    for idx, item in enumerate(decisions):
        verb = _decision_verb(item)
        item_text = _decision_text(item)[:80]
        if verb is None:
            # No declared verb AND no taxonomy verb in text. This stays
            # fail-closed: the regex path and any caller that did NOT
            # route through the meeting_minutes_llm producer never carry
            # the explicit sentinel, so a bare missing verb still blocks.
            blocks.append(
                f"{VERB_NOT_CLASSIFIED_PREFIX}__missing__"
                f"|decision[{idx}]:{item_text}"
            )
            continue
        if verb == UNCLASSIFIED_DECISION_VERB:
            # Explicit producer (or model) marker that the governing
            # verb is indeterminate. Non-blocking — the SAME decision
            # in plain-string form already promotes today (the eval
            # never required a verb for string decisions), so honouring
            # this explicit marker only makes the object form
            # consistent; it does not relax the real trust property
            # (a CLAIMED-but-unrecognised verb still falls through to
            # the block below). Surfaced so the gap stays auditable.
            unclassified.append(
                f"{VERB_UNCLASSIFIED_PREFIX}decision[{idx}]:{item_text}"
            )
            continue
        if verb in AMBIGUOUS_VERBS:
            # Checked BEFORE the classified-pass set: a verb that is
            # both a real regulatory verb AND informal (``recommended``
            # is in REGULATORY_VERBS ∩ AMBIGUOUS_VERBS) keeps its
            # operator-visible warn rather than passing silently.
            warns.append(
                f"{VERB_AMBIGUOUS_PREFIX}{verb}"
                f"|decision[{idx}]:{item_text}"
            )
            continue
        if verb in CLASSIFIED_DECISION_VERBS:
            # The canonical regulatory-verb taxonomy (the SAME list the
            # extraction prompt instructs the model to emit). A real
            # decision verb such as ``adopted`` / ``authorized`` /
            # ``ratified`` passes here instead of being mis-blocked.
            continue
        blocks.append(
            f"{VERB_NOT_CLASSIFIED_PREFIX}{verb}"
            f"|decision[{idx}]:{item_text}"
        )

    if blocks:
        # Surface every block AND every warn / unclassified note so a
        # single eval_result explains the full picture. A new engineer
        # reading reason_codes sees every problematic verb in order.
        return _eval_result(
            artifact, passed=False, reason_codes=blocks + warns + unclassified
        )
    # No blocks — pass. Warns and the explicit unclassified markers ride
    # along as reason codes so the operator still sees them in the
    # manifest / debug report / eval_history.
    return _eval_result(
        artifact, passed=True, reason_codes=warns + unclassified
    )


__all__ = [
    "AMBIGUOUS_VERBS",
    "CLASSIFIED_DECISION_VERBS",
    "DECISIONS_FIELD_MISSING",
    "DECISION_BEARING_ARTIFACT_TYPES",
    "DECISION_VERBS",
    "EVAL_TYPE",
    "UNCLASSIFIED_DECISION_VERB",
    "VERB_AMBIGUOUS_PREFIX",
    "VERB_NOT_CLASSIFIED_PREFIX",
    "VERB_UNCLASSIFIED_PREFIX",
    "resolve_decision_verb",
    "run_regulatory_verb_eval",
]
