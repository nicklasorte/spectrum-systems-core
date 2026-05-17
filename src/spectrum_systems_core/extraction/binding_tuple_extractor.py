"""Phase V.5: binding-tuple sub-object on decisions.

Decomposes a decision_text into ``(actor, action_verb, object_description,
band_or_spectrum_ref, constraint_or_condition)`` via a second Haiku call.

Gated by ``BINDING_TUPLE_ENABLED`` (default false). When disabled, the
``binding_tuple`` field on a decision is left null and zero model
calls are made -- the gate is read once per ``annotate_decisions`` call
and the entire pipeline short-circuits.

Findings:
- ``binding_tuple_parse_failed`` (warn) -- model returned unparseable
  JSON. The decision's ``binding_tuple`` is set to null and the
  surrounding extraction continues unaffected.
- ``binding_tuple_incomplete`` (warn) -- tuple parsed but ``actor`` is
  null on an ``approval``/``rejection`` decision. Other outcome
  classes (deferral, action_required, noted, question) tolerate a
  null actor by design.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..health.finding import HealthFinding

_BINDING_TUPLE_ENABLED_ENV: str = "BINDING_TUPLE_ENABLED"

BINDING_TUPLE_FIELDS: tuple[str, ...] = (
    "actor",
    "action_verb",
    "object_description",
    "band_or_spectrum_ref",
    "constraint_or_condition",
)

# Outcomes for which a null actor must trigger
# ``binding_tuple_incomplete``. Deferral / action_required / noted /
# question are allowed to lack an explicit actor.
ACTOR_REQUIRED_OUTCOMES: frozenset[str] = frozenset({"approval", "rejection"})


BINDING_TUPLE_PROMPT_TEMPLATE: str = (
    "Given this decision text from a spectrum policy meeting:\n\n"
    "{decision_text}\n\n"
    "Extract the binding tuple with these fields:\n"
    "- actor: the organization or body that made the decision "
    '(e.g. "NTIA", "FCC", "DoD")\n'
    "- action_verb: the regulatory verb "
    '(e.g. "approved", "directed", "deferred")\n'
    "- object_description: what was approved/directed/deferred "
    "(1 sentence max)\n"
    "- band_or_spectrum_ref: the specific spectrum band or frequency "
    "range, if mentioned\n"
    "- constraint_or_condition: any condition attached to the "
    "decision, if mentioned\n\n"
    "For any field where the information is not explicitly in the "
    "text, return null. Do not infer. Do not hallucinate.\n\n"
    "Return JSON only:\n"
    '{{"actor": ..., "action_verb": ..., "object_description": ...,'
    ' "band_or_spectrum_ref": ..., "constraint_or_condition": ...}}'
)


_LOG = logging.getLogger(__name__)


def binding_tuple_enabled() -> bool:
    """Read the feature flag once. False by default."""
    raw = os.environ.get(_BINDING_TUPLE_ENABLED_ENV, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def render_binding_tuple_prompt(decision_text: str) -> str:
    """Render the per-decision prompt. Public for testing."""
    return BINDING_TUPLE_PROMPT_TEMPLATE.format(decision_text=decision_text or "")


def _empty_tuple() -> dict[str, str | None]:
    return {field: None for field in BINDING_TUPLE_FIELDS}


def parse_binding_tuple_response(text: str) -> dict[str, str | None] | None:
    """Parse a model JSON response into the binding-tuple dict.

    Returns None on parse failure (caller emits
    ``binding_tuple_parse_failed``). Normalises every field present
    in the response into the canonical 5-key dict so consumers can
    rely on ``.get(field)`` semantics. Extra keys are ignored.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    payload = text.strip()
    # Strip a leading ```json fence if the model added one.
    if payload.startswith("```"):
        # Cheap fence strip; keep parser tolerant.
        payload = payload.strip("`")
        if payload.lower().startswith("json"):
            payload = payload[4:]
        payload = payload.strip()
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    result = _empty_tuple()
    for field in BINDING_TUPLE_FIELDS:
        if field in data:
            value = data[field]
            if value is None:
                result[field] = None
            elif isinstance(value, str):
                stripped = value.strip()
                result[field] = stripped if stripped else None
            else:
                # numeric / bool collapse to string repr; null otherwise.
                result[field] = None
    return result


def _missing_field_for_outcome(
    tuple_dict: dict[str, str | None], decision_outcome: str
) -> str | None:
    if decision_outcome in ACTOR_REQUIRED_OUTCOMES:
        if not tuple_dict.get("actor"):
            return "actor"
    return None


@dataclass
class BindingTupleResult:
    """Aggregate result of ``annotate_decisions``.

    ``call_count`` records the number of model calls actually made so
    the orchestrator can include it in ``orchestration_result``.
    """

    decisions: list[dict[str, Any]]
    findings: list[HealthFinding]
    call_count: int


def annotate_decisions(
    decisions: list[dict[str, Any]],
    *,
    api_caller: Callable[[str], str] | None = None,
    pipeline_run_id: str | None = None,
) -> BindingTupleResult:
    """Annotate each decision with a ``binding_tuple`` sub-object.

    When the feature flag is OFF (default), every decision gets
    ``binding_tuple: None`` and zero model calls happen.

    When ON, ``api_caller(prompt) -> text`` is invoked once per
    decision; the parsed tuple is attached and any incompleteness is
    reported as a finding. Parse failures attach ``binding_tuple:
    None`` so downstream consumers always see the field.
    """
    annotated: list[dict[str, Any]] = []
    findings: list[HealthFinding] = []
    enabled = binding_tuple_enabled()

    if not enabled:
        for dec in decisions:
            new = dict(dec) if isinstance(dec, dict) else {}
            new["binding_tuple"] = None
            annotated.append(new)
        return BindingTupleResult(
            decisions=annotated, findings=findings, call_count=0
        )

    if api_caller is None:
        # The feature is on but no caller wired -- treat each
        # decision as a parse failure so the operator sees that the
        # pipeline didn't silently degrade.
        for dec in decisions:
            new = dict(dec) if isinstance(dec, dict) else {}
            new["binding_tuple"] = None
            annotated.append(new)
            findings.append(
                HealthFinding(
                    finding_code="binding_tuple_parse_failed",
                    severity="warn",
                    context={
                        "reason": "no_api_caller_configured",
                        "decision_id": str(new.get("decision_id") or ""),
                    },
                    remediation=(
                        "Wire api_caller into annotate_decisions or "
                        "disable BINDING_TUPLE_ENABLED."
                    ),
                    pipeline_run_id=pipeline_run_id,
                )
            )
        return BindingTupleResult(
            decisions=annotated, findings=findings, call_count=0
        )

    call_count = 0
    for dec in decisions:
        if not isinstance(dec, dict):
            annotated.append({"binding_tuple": None})
            continue
        new = dict(dec)
        text = str(new.get("decision_text") or "")
        prompt = render_binding_tuple_prompt(text)
        try:
            response = api_caller(prompt)
            call_count += 1
        except Exception as exc:  # pragma: no cover -- caller is mock
            _LOG.warning("binding_tuple_api_failed: %s", exc)
            new["binding_tuple"] = None
            findings.append(
                HealthFinding(
                    finding_code="binding_tuple_parse_failed",
                    severity="warn",
                    context={
                        "reason": "api_call_raised",
                        "exception_type": type(exc).__name__,
                        "decision_id": str(new.get("decision_id") or ""),
                    },
                    remediation="Inspect API caller error and retry.",
                    pipeline_run_id=pipeline_run_id,
                )
            )
            annotated.append(new)
            continue

        parsed = parse_binding_tuple_response(response or "")
        if parsed is None:
            new["binding_tuple"] = None
            findings.append(
                HealthFinding(
                    finding_code="binding_tuple_parse_failed",
                    severity="warn",
                    context={
                        "reason": "json_parse_failed",
                        "decision_id": str(new.get("decision_id") or ""),
                    },
                    remediation=(
                        "Inspect raw model response; expected JSON "
                        "object with the 5 tuple fields."
                    ),
                    pipeline_run_id=pipeline_run_id,
                )
            )
            annotated.append(new)
            continue

        new["binding_tuple"] = parsed
        outcome = str(new.get("decision_outcome") or "")
        missing = _missing_field_for_outcome(parsed, outcome)
        if missing:
            findings.append(
                HealthFinding(
                    finding_code="binding_tuple_incomplete",
                    severity="warn",
                    context={
                        "missing_field": missing,
                        "decision_outcome": outcome,
                        "decision_id": str(new.get("decision_id") or ""),
                    },
                    remediation=(
                        "Inspect the source chunk for an explicit "
                        "actor (NTIA/FCC/DoD/etc.); add to the "
                        "decision_text or correct the outcome class."
                    ),
                    pipeline_run_id=pipeline_run_id,
                )
            )
        annotated.append(new)

    return BindingTupleResult(
        decisions=annotated, findings=findings, call_count=call_count
    )


__all__ = [
    "ACTOR_REQUIRED_OUTCOMES",
    "BINDING_TUPLE_FIELDS",
    "BINDING_TUPLE_PROMPT_TEMPLATE",
    "BindingTupleResult",
    "annotate_decisions",
    "binding_tuple_enabled",
    "parse_binding_tuple_response",
    "render_binding_tuple_prompt",
]
