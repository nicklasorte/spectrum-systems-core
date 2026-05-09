"""BundleEval: deterministic evals on a context_bundle.

EVAL-CTX-001..005. All five are required (block on failure). The token
budget enforcement (EVAL-CTX-003) implements FINDING-F-001. The
promoted-only enforcement (EVAL-CTX-002) implements FINDING-F-002. The
audience enum check (EVAL-CTX-005) implements FINDING-F-003.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import jsonschema

from ._paths import synthesis_schema_path
from .bundle_assembler import (
    MAX_BUNDLE_TOKENS,
    PROMOTED_STATUSES,
    VALID_AUDIENCES,
)


MIN_BUNDLE_ITEMS = 3


class BundleEval:
    """Run schema + integrity + budget checks on a context_bundle dict."""

    def run(self, bundle: Dict[str, Any]) -> Dict[str, Any]:
        eval_results: List[Dict[str, Any]] = []
        reason_codes: List[str] = []

        # EVAL-CTX-001: schema_conformance
        try:
            schema = json.loads(
                synthesis_schema_path("context_bundle").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError) as exc:
            return {
                "decision": "block",
                "eval_results": [
                    {
                        "name": "schema_load",
                        "status": "fail",
                        "reason": f"schema_unreadable: {exc}",
                    }
                ],
                "reason_codes": ["schema_unreadable"],
            }
        try:
            jsonschema.Draft202012Validator(schema).validate(bundle)
            eval_results.append(
                {"name": "EVAL-CTX-001", "status": "pass", "reason": ""}
            )
        except jsonschema.ValidationError as exc:
            eval_results.append(
                {
                    "name": "EVAL-CTX-001",
                    "status": "fail",
                    "reason": f"schema_violation: {exc.message}",
                }
            )
            reason_codes.append("EVAL-CTX-001:schema_conformance")

        items = bundle.get("items", []) or []

        # EVAL-CTX-002: promoted_only_enforced (FINDING-F-002)
        offenders = [
            it.get("artifact_id", "?")
            for it in items
            if it.get("promoted_status") not in PROMOTED_STATUSES
        ]
        if offenders:
            eval_results.append(
                {
                    "name": "EVAL-CTX-002",
                    "status": "fail",
                    "reason": "non_promoted_items: " + ", ".join(offenders),
                }
            )
            reason_codes.append("EVAL-CTX-002:promoted_only_enforced")
        else:
            eval_results.append(
                {"name": "EVAL-CTX-002", "status": "pass", "reason": ""}
            )

        # EVAL-CTX-003: token_budget_enforced (FINDING-F-001)
        total = int(bundle.get("total_token_estimate", 0))
        if total > MAX_BUNDLE_TOKENS:
            eval_results.append(
                {
                    "name": "EVAL-CTX-003",
                    "status": "fail",
                    "reason": (
                        f"token_budget_exceeded: {total} > {MAX_BUNDLE_TOKENS}"
                    ),
                }
            )
            reason_codes.append("EVAL-CTX-003:token_budget_exceeded")
        else:
            eval_results.append(
                {"name": "EVAL-CTX-003", "status": "pass", "reason": ""}
            )

        # EVAL-CTX-004: minimum_items
        if len(items) < MIN_BUNDLE_ITEMS:
            eval_results.append(
                {
                    "name": "EVAL-CTX-004",
                    "status": "fail",
                    "reason": (
                        f"insufficient_items: {len(items)} < {MIN_BUNDLE_ITEMS}"
                    ),
                }
            )
            reason_codes.append("EVAL-CTX-004:minimum_items")
        else:
            eval_results.append(
                {"name": "EVAL-CTX-004", "status": "pass", "reason": ""}
            )

        # EVAL-CTX-005: audience_valid (FINDING-F-003)
        if bundle.get("audience") not in VALID_AUDIENCES:
            eval_results.append(
                {
                    "name": "EVAL-CTX-005",
                    "status": "fail",
                    "reason": f"invalid_audience: {bundle.get('audience')}",
                }
            )
            reason_codes.append("EVAL-CTX-005:audience_valid")
        else:
            eval_results.append(
                {"name": "EVAL-CTX-005", "status": "pass", "reason": ""}
            )

        decision = "block" if reason_codes else "allow"
        return {
            "decision": decision,
            "eval_results": eval_results,
            "reason_codes": reason_codes,
        }
