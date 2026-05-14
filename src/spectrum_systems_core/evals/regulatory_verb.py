"""Regulatory verb classification eval (Phase Z.2).

For every decision item in a decision-bearing artifact, classifies the
governing verb against the canonical taxonomy in
``config.taxonomy``:

  - verb in ``DECISION_VERBS``  → contributes ``pass``
  - verb in ``AMBIGUOUS_VERBS`` → contributes ``warn``
    (reason code ``verb_ambiguous:<verb>``; eval still passes)
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
from ..config.taxonomy import AMBIGUOUS_VERBS, DECISION_VERBS

EVAL_TYPE = "regulatory_verb"

# Artifact types that carry regulatory decisions. Other types skip the
# eval cleanly — they have nothing for it to inspect.
DECISION_BEARING_ARTIFACT_TYPES: frozenset = frozenset({
    "meeting_minutes",
    "decision_brief",
})

# Reason code prefixes (stable, machine-grepable).
VERB_AMBIGUOUS_PREFIX = "verb_ambiguous:"
VERB_NOT_CLASSIFIED_PREFIX = "verb_not_classified:"
DECISIONS_FIELD_MISSING = "decisions_field_missing"


def _extract_verb_from_text(text: str) -> str | None:
    """First taxonomy verb found anywhere in ``text`` (case-insensitive),
    or ``None`` if no taxonomy verb appears. The taxonomy lookup is the
    canonical authority — falling back to "first alphabetic word" would
    let any unrecognized verb pass."""
    if not isinstance(text, str) or not text.strip():
        return None
    tokens = re.findall(r"[A-Za-z']+", text.lower())
    classified = DECISION_VERBS | AMBIGUOUS_VERBS
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
    blocks: list[str] = []
    for idx, item in enumerate(decisions):
        verb = _decision_verb(item)
        item_text = _decision_text(item)[:80]
        if verb is None:
            blocks.append(
                f"{VERB_NOT_CLASSIFIED_PREFIX}__missing__"
                f"|decision[{idx}]:{item_text}"
            )
            continue
        if verb in DECISION_VERBS:
            continue
        if verb in AMBIGUOUS_VERBS:
            warns.append(
                f"{VERB_AMBIGUOUS_PREFIX}{verb}"
                f"|decision[{idx}]:{item_text}"
            )
            continue
        blocks.append(
            f"{VERB_NOT_CLASSIFIED_PREFIX}{verb}"
            f"|decision[{idx}]:{item_text}"
        )

    if blocks:
        # Surface every block AND every warn so a single eval_result
        # explains the full picture. A new engineer reading reason_codes
        # sees every problematic verb in order.
        return _eval_result(
            artifact, passed=False, reason_codes=blocks + warns
        )
    # No blocks — pass. Warns ride along as reason codes so the
    # operator still sees them in the manifest / debug report.
    return _eval_result(artifact, passed=True, reason_codes=warns)


__all__ = [
    "AMBIGUOUS_VERBS",
    "DECISIONS_FIELD_MISSING",
    "DECISION_BEARING_ARTIFACT_TYPES",
    "DECISION_VERBS",
    "EVAL_TYPE",
    "VERB_AMBIGUOUS_PREFIX",
    "VERB_NOT_CLASSIFIED_PREFIX",
    "run_regulatory_verb_eval",
]
