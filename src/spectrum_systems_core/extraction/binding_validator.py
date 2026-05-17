"""Phase R.2: binding validation on multi-property tuples.

Tan & D'Souza (IRCDL 2026): LLMs degrade sharply when binding multiple
properties (actor + verb + object + band) to a single entity. For
spectrum-policy work the difference between "considered" / "approved" /
"deferred" is load-bearing, so a decision that fails to bind the
regulatory verb has to surface as a warning even when it passes the
required-field check.

Binding validation is fail-OPEN: it never halts a chunk. Failed bindings
emit a ``binding_warning`` artifact (forensic record) and add a
``binding_valid=false`` annotation to the decision so downstream
consumers can filter.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from ..config.taxonomy import REGULATORY_VERBS

_LOG = logging.getLogger(__name__)


# Phase T.1: When set to a truthy value, ``binding_no_regulatory_verb``
# is escalated from warn to halt. Defaults to OFF so existing pipelines
# keep their fail-OPEN behaviour. The rollback path is the env var
# itself: setting it to "false" restores warn-only semantics.
BINDING_VALIDATOR_HALT_ENABLED_ENV: str = "BINDING_VALIDATOR_HALT_ENABLED"
_ENABLED_VALUES: frozenset = frozenset({"true", "1", "yes", "on"})


def binding_validator_halt_enabled() -> bool:
    raw = os.environ.get(BINDING_VALIDATOR_HALT_ENABLED_ENV, "").strip().lower()
    return raw in _ENABLED_VALUES


REQUIRED_DECISION_FIELDS: tuple = (
    "decision_text",
    "decision_type",
    "stakeholders",
    "source_turns",
)


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _count_regulatory_verbs(text: str) -> int:
    """Count case-insensitive whole-word regulatory-verb occurrences.

    Whole-word match (bounded by non-letter chars) so "approved" does
    not also fire on "disapproved" or compound words. Counts distinct
    verb types, not occurrences -- two mentions of the same verb is
    one binding signal.
    """
    if not isinstance(text, str) or not text:
        return 0
    lowered = text.lower()
    found: set = set()
    for verb in REGULATORY_VERBS:
        # Build a manual word-boundary check that does not require regex.
        start = 0
        while True:
            idx = lowered.find(verb, start)
            if idx < 0:
                break
            before_ok = idx == 0 or not lowered[idx - 1].isalpha()
            after_idx = idx + len(verb)
            after_ok = (
                after_idx == len(lowered) or not lowered[after_idx].isalpha()
            )
            if before_ok and after_ok:
                found.add(verb)
                break
            start = idx + 1
    return len(found)


def validate_decision_binding(decision: dict[str, Any]) -> dict[str, Any]:
    """Validate the binding of a single decision item.

    Returns::

        {
          "binding_valid": bool,        # all required fields present + non-empty
          "regulatory_verb_count": int,
          "regulatory_verb_found": bool,
          "binding_weak": bool,         # zero regulatory verbs
          "binding_ambiguous": bool,    # >1 distinct regulatory verbs
          "missing_fields": [str, ...],
          "warnings": [str, ...],
        }

    The schema defers to the *outer* typed_extraction schema for
    decision field names: ``source_turn_ids`` in the schema maps to
    ``source_turns`` in this validator's REQUIRED list. We accept either
    spelling so the validator can run before or after the extraction
    merger normalises field names.
    """
    if not isinstance(decision, dict):
        return {
            "binding_valid": False,
            "regulatory_verb_count": 0,
            "regulatory_verb_found": False,
            "binding_weak": True,
            "binding_ambiguous": False,
            "missing_fields": list(REQUIRED_DECISION_FIELDS),
            "warnings": ["decision_not_a_dict"],
        }

    missing: list[str] = []
    for field in REQUIRED_DECISION_FIELDS:
        if field == "source_turns":
            # The extraction merger writes ``source_turn_ids``; accept
            # either spelling.
            value = decision.get("source_turns")
            if value is None:
                value = decision.get("source_turn_ids")
        else:
            value = decision.get(field)
        if value is None:
            missing.append(field)
            continue
        if isinstance(value, (list, tuple)) and len(value) == 0:
            missing.append(field)
            continue
        if isinstance(value, str) and not value.strip():
            missing.append(field)
            continue

    text = decision.get("decision_text") or ""
    verb_count = _count_regulatory_verbs(text)
    binding_weak = verb_count == 0
    binding_ambiguous = verb_count > 1
    regulatory_verb_found = verb_count == 1

    warnings: list[str] = []
    if missing:
        warnings.append(f"missing_required_fields:{','.join(missing)}")
    if binding_weak:
        warnings.append("binding_weak:zero_regulatory_verbs")
    if binding_ambiguous:
        warnings.append(f"binding_ambiguous:{verb_count}_regulatory_verbs")

    return {
        "binding_valid": not missing,
        "regulatory_verb_count": verb_count,
        "regulatory_verb_found": regulatory_verb_found,
        "binding_weak": binding_weak,
        "binding_ambiguous": binding_ambiguous,
        "missing_fields": missing,
        "warnings": warnings,
    }


def build_binding_warning(
    decision: dict[str, Any],
    result: dict[str, Any],
    *,
    source_id: str,
    extraction_run_id: str | None = None,
) -> dict[str, Any]:
    """Build a binding_warning artifact for a decision that flunked.

    The artifact is a *warning* — not a halt — so the orchestrator does
    NOT bump a blocked-chunk counter. It is emitted into the eval
    summary so the operator can spot extraction quality drift.
    """
    return {
        "artifact_type": "binding_warning",
        "schema_version": "1.0.0",
        "binding_warning_id": str(uuid.uuid4()),
        "source_id": source_id or "",
        "extraction_run_id": extraction_run_id or "",
        "decision_text": str(decision.get("decision_text") or "")[:1000],
        "decision_type": decision.get("decision_type") or "",
        "binding_valid": bool(result.get("binding_valid")),
        "regulatory_verb_count": int(result.get("regulatory_verb_count", 0)),
        "binding_weak": bool(result.get("binding_weak")),
        "binding_ambiguous": bool(result.get("binding_ambiguous")),
        "missing_fields": list(result.get("missing_fields") or []),
        "warnings": list(result.get("warnings") or []),
        "created_at": _now_iso(),
    }


def annotate_and_collect_warnings(
    decisions: list[dict[str, Any]],
    *,
    source_id: str,
    extraction_run_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run binding validation across every decision in the list.

    Each decision is annotated in-place with a ``binding_valid`` flag
    plus the supporting fields (``regulatory_verb_count`` etc.) and a
    list of ``binding_warning`` artifacts is built for every decision
    whose validation produced any warning string.

    Returns ``(annotated_decisions, binding_warnings)``. The caller
    decides where to persist the warnings (eval_summary, on-disk
    artifacts, etc.). The decision objects are NOT removed -- binding
    is a warning, not a halt.
    """
    annotated: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for d in decisions or []:
        result = validate_decision_binding(d)
        out = dict(d) if isinstance(d, dict) else {}
        out["binding_valid"] = bool(result["binding_valid"])
        out["regulatory_verb_count"] = int(result["regulatory_verb_count"])
        out["binding_weak"] = bool(result["binding_weak"])
        out["binding_ambiguous"] = bool(result["binding_ambiguous"])
        annotated.append(out)
        if result["warnings"]:
            warnings.append(
                build_binding_warning(
                    d if isinstance(d, dict) else {},
                    result,
                    source_id=source_id,
                    extraction_run_id=extraction_run_id,
                )
            )
    return annotated, warnings


def write_binding_warnings(
    warnings: list[dict[str, Any]],
    sdl_root: Path | None,
) -> list[Path]:
    """Persist binding_warning artifacts under ``<sdl_root>/binding/``.

    Failure to write is logged but never raised. The warnings list
    itself is the durable signal -- callers carry it into the eval
    summary regardless of whether disk write succeeded.
    """
    if not warnings or sdl_root is None:
        return []
    target_dir = Path(sdl_root) / "binding"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _LOG.warning("binding_warning_dir_create_failed: %s", exc)
        return []
    out: list[Path] = []
    for w in warnings:
        path = target_dir / f"{w['binding_warning_id']}.json"
        try:
            path.write_text(
                json.dumps(w, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            out.append(path)
        except OSError as exc:
            _LOG.warning(
                "binding_warning_write_failed: id=%s err=%s",
                w["binding_warning_id"], exc,
            )
    return out


def build_taxonomy_finding(
    decision: dict[str, Any],
    result: dict[str, Any],
    *,
    pipeline_run_id: str | None = None,
):
    """Build a ``taxonomy_regulatory_verb_missing`` HealthFinding.

    Returns the constructed :class:`HealthFinding` or ``None`` when the
    decision had at least one regulatory verb. Severity is ``halt`` when
    ``BINDING_VALIDATOR_HALT_ENABLED=true``, otherwise ``warn``. Callers
    decide whether to write or simply collect the finding.
    """
    if not isinstance(result, dict):
        return None
    if not result.get("binding_weak"):
        return None
    # Local import keeps this module free of a circular at the
    # validator <-> health layer.
    from ..health.finding import HealthFinding

    severity = "halt" if binding_validator_halt_enabled() else "warn"
    decision_text = str(decision.get("decision_text") or "")[:400]
    return HealthFinding(
        finding_code="taxonomy_regulatory_verb_missing",
        severity=severity,
        pipeline_run_id=pipeline_run_id,
        context={
            "decision_text": decision_text,
            "searched_verbs": list(REGULATORY_VERBS),
            "regulatory_verb_count": int(
                result.get("regulatory_verb_count", 0)
            ),
            "halt_enabled": binding_validator_halt_enabled(),
        },
        remediation=(
            "Decision text contains no regulatory verb. Either the model "
            "misclassified a claim as a decision, or the chunk extraction "
            "lost the verb. Re-classify as a procedural claim, or correct "
            "the source_turn citation."
        ),
    )


__all__ = [
    "BINDING_VALIDATOR_HALT_ENABLED_ENV",
    "REGULATORY_VERBS",
    "REQUIRED_DECISION_FIELDS",
    "annotate_and_collect_warnings",
    "binding_validator_halt_enabled",
    "build_binding_warning",
    "build_taxonomy_finding",
    "validate_decision_binding",
    "write_binding_warnings",
]
