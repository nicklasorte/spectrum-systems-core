"""MitigationEval: deterministic evals on mitigation_suggestion artifacts.

EVAL-MIT-001..004. No LLM. Block on failure.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import jsonschema

from ._paths import agency_schema_path


class MitigationEval:
    """Run schema + integrity + cross-reference checks on mitigations."""

    def run(
        self,
        mitigations: List[Dict[str, Any]],
        predictions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        eval_results: List[Dict[str, Any]] = []
        reason_codes: List[str] = []

        # EVAL-MIT-001: schema_conformance
        try:
            schema = json.loads(
                agency_schema_path("mitigation_suggestion").read_text(encoding="utf-8")
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
        validator = jsonschema.Draft202012Validator(schema)
        schema_failures: List[str] = []
        for mit in mitigations:
            try:
                validator.validate(mit)
            except jsonschema.ValidationError as exc:
                schema_failures.append(
                    f"{mit.get('mitigation_id', '?')}: {exc.message}"
                )
        if schema_failures:
            eval_results.append(
                {
                    "name": "EVAL-MIT-001",
                    "status": "fail",
                    "reason": "; ".join(schema_failures),
                }
            )
            reason_codes.append("EVAL-MIT-001:schema_conformance")
        else:
            eval_results.append(
                {"name": "EVAL-MIT-001", "status": "pass", "reason": ""}
            )

        # EVAL-MIT-002: add_evidence requires search terms (FINDING-E-006)
        offenders: List[str] = []
        for mit in mitigations:
            if mit.get("mitigation_type") == "add_evidence":
                terms = mit.get("evidence_search_terms") or []
                if not terms:
                    offenders.append(mit.get("mitigation_id", "?"))
        if offenders:
            eval_results.append(
                {
                    "name": "EVAL-MIT-002",
                    "status": "fail",
                    "reason": "add_evidence_missing_search_terms: "
                    + ", ".join(offenders),
                }
            )
            reason_codes.append("EVAL-MIT-002:add_evidence_missing_search_terms")
        else:
            eval_results.append(
                {"name": "EVAL-MIT-002", "status": "pass", "reason": ""}
            )

        # EVAL-MIT-003: prediction_id_exists
        valid_prediction_ids = {
            p.get("prediction_id")
            for p in predictions
            if isinstance(p.get("prediction_id"), str)
        }
        orphans: List[str] = []
        for mit in mitigations:
            pid = mit.get("prediction_id")
            if pid not in valid_prediction_ids:
                orphans.append(
                    f"{mit.get('mitigation_id', '?')}: prediction_id={pid}"
                )
        if orphans:
            eval_results.append(
                {
                    "name": "EVAL-MIT-003",
                    "status": "fail",
                    "reason": "orphan_prediction_id: " + "; ".join(orphans),
                }
            )
            reason_codes.append("EVAL-MIT-003:orphan_prediction_id")
        else:
            eval_results.append(
                {"name": "EVAL-MIT-003", "status": "pass", "reason": ""}
            )

        # EVAL-MIT-004: temperature_zero
        offenders = []
        for mit in mitigations:
            if mit.get("extraction_temperature") != 0:
                offenders.append(mit.get("mitigation_id", "?"))
        if offenders:
            eval_results.append(
                {
                    "name": "EVAL-MIT-004",
                    "status": "fail",
                    "reason": "non_zero_temperature: " + ", ".join(offenders),
                }
            )
            reason_codes.append("EVAL-MIT-004:temperature_zero")
        else:
            eval_results.append(
                {"name": "EVAL-MIT-004", "status": "pass", "reason": ""}
            )

        decision = "block" if reason_codes else "allow"
        return {
            "decision": decision,
            "eval_results": eval_results,
            "reason_codes": reason_codes,
        }
