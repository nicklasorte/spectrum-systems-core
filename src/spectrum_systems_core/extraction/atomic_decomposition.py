"""Phase T.7: atomic factual decomposition for decisions.

Behind the ``ATOMIC_DECOMPOSITION_ENABLED`` feature flag (default OFF
so no model calls happen until an operator explicitly opts in).

When enabled, for each decision in a meeting_extraction artifact we
make a second Haiku call that takes the decision_text and returns a
JSON array of "atomic facts" -- the smallest independently verifiable
sub-claims of the decision. The list is stored on the decision under
``atomic_facts``. Items that produced zero atomic facts emit a single
``atomic_decomposition_failed`` warn finding; the decision itself is
preserved with ``atomic_facts: null`` so downstream consumers can
distinguish "decomposition not attempted" from "decomposition returned
nothing".

Cost: ~1 Haiku call per decision. Across the 13-transcript corpus
this can add up; the call count is recorded in ``orchestration_result``
so the operator sees the impact.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..health.finding import HealthFinding

_LOG = logging.getLogger(__name__)


ATOMIC_DECOMPOSITION_ENABLED_ENV: str = "ATOMIC_DECOMPOSITION_ENABLED"
_ENABLED_VALUES = frozenset({"true", "1", "yes", "on"})

_MODEL_ID = "claude-haiku-4-5-20251001"

_PROMPT = """Given this decision from a spectrum policy meeting:

{decision_text}

Break it into atomic factual statements -- the smallest independently
verifiable claims this decision makes.

Rules:
- Each atomic fact must be traceable to the decision text above.
- Do not add information not present in the decision text.
- If the decision is already atomic (one fact), return a list with one item.
- Return strict JSON: {{"atomic_facts": ["fact 1", "fact 2", ...]}}.
"""


def atomic_decomposition_enabled() -> bool:
    raw = os.environ.get(ATOMIC_DECOMPOSITION_ENABLED_ENV, "").strip().lower()
    return raw in _ENABLED_VALUES


def _default_api_caller(prompt: str) -> Dict[str, Any]:  # noqa: ARG001
    """Offline default: return an empty list of facts. Never raises."""
    return {"atomic_facts": []}


def decompose_one(
    decision_text: str,
    api_caller: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> List[str]:
    """Return the list of atomic facts for ``decision_text``.

    Never raises. On any failure (non-dict response, missing field,
    non-string entries) returns an empty list and lets the caller
    decide whether to emit a finding.
    """
    if not isinstance(decision_text, str) or not decision_text.strip():
        return []
    caller = api_caller or _default_api_caller
    prompt = _PROMPT.format(decision_text=decision_text.strip())
    try:
        resp = caller(prompt)
    except Exception as exc:  # noqa: BLE001 -- never escape
        _LOG.warning("atomic_decomposition_call_failed: %s", exc)
        return []
    if not isinstance(resp, dict):
        return []
    raw = resp.get("atomic_facts")
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def decompose_decisions(
    decisions: List[Dict[str, Any]],
    *,
    api_caller: Optional[Callable[[str], Dict[str, Any]]] = None,
    pipeline_run_id: Optional[str] = None,
    source_id: str = "",
) -> Tuple[List[Dict[str, Any]], List[HealthFinding], int]:
    """Decompose every decision in the list when the feature flag is on.

    Returns ``(annotated_decisions, findings, call_count)``. When the
    flag is off, the input is returned unchanged with ``atomic_facts:
    null`` attached to each decision, zero findings, zero calls.
    """
    if not isinstance(decisions, list):
        return [], [], 0
    if not atomic_decomposition_enabled():
        out = []
        for d in decisions or []:
            if isinstance(d, dict):
                clone = dict(d)
                clone["atomic_facts"] = None
                out.append(clone)
        return out, [], 0

    findings: List[HealthFinding] = []
    annotated: List[Dict[str, Any]] = []
    call_count = 0

    for d in decisions:
        if not isinstance(d, dict):
            continue
        decision_text = d.get("decision_text") or ""
        facts = decompose_one(decision_text, api_caller=api_caller)
        call_count += 1
        clone = dict(d)
        if facts:
            clone["atomic_facts"] = facts
            clone["atomic_decomposition_model"] = _MODEL_ID
        else:
            clone["atomic_facts"] = None
            findings.append(
                HealthFinding(
                    finding_code="atomic_decomposition_failed",
                    severity="warn",
                    pipeline_run_id=pipeline_run_id,
                    context={
                        "source_id": source_id or "",
                        "decision_text": str(decision_text)[:300],
                    },
                    remediation=(
                        "The atomic decomposition call returned no "
                        "facts for this decision. Either the decision "
                        "text is too short to decompose, or the model "
                        "rejected it. The decision is preserved with "
                        "atomic_facts: null."
                    ),
                )
            )
        annotated.append(clone)

    return annotated, findings, call_count


__all__ = [
    "ATOMIC_DECOMPOSITION_ENABLED_ENV",
    "atomic_decomposition_enabled",
    "decompose_decisions",
    "decompose_one",
]
