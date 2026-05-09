"""ObjectionEval: deterministic evals on objection_prediction artifacts.

EVAL-OBJ-001..005. No LLM. Block-on-failure for all five.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import jsonschema

from ._paths import agency_schema_path


class ObjectionEval:
    """Run schema + integrity checks on a list of objection_prediction dicts."""

    def run(self, predictions: List[Dict[str, Any]]) -> Dict[str, Any]:
        eval_results: List[Dict[str, Any]] = []
        reason_codes: List[str] = []

        # EVAL-OBJ-001: schema_conformance
        try:
            schema = json.loads(
                agency_schema_path("objection_prediction").read_text(encoding="utf-8")
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
        for pred in predictions:
            try:
                validator.validate(pred)
            except jsonschema.ValidationError as exc:
                schema_failures.append(
                    f"{pred.get('prediction_id', '?')}: {exc.message}"
                )
        if schema_failures:
            eval_results.append(
                {
                    "name": "EVAL-OBJ-001",
                    "status": "fail",
                    "reason": "; ".join(schema_failures),
                }
            )
            reason_codes.append("EVAL-OBJ-001:schema_conformance")
        else:
            eval_results.append(
                {"name": "EVAL-OBJ-001", "status": "pass", "reason": ""}
            )

        # EVAL-OBJ-002: high_confidence_requires_evidence (FINDING-E-001)
        offenders: List[str] = []
        for pred in predictions:
            if (
                pred.get("confidence") == "high"
                and not pred.get("evidence_basis")
            ):
                offenders.append(pred.get("prediction_id", "?"))
        if offenders:
            eval_results.append(
                {
                    "name": "EVAL-OBJ-002",
                    "status": "fail",
                    "reason": "high_confidence_without_evidence_basis: "
                    + ", ".join(offenders),
                }
            )
            reason_codes.append("EVAL-OBJ-002:high_confidence_without_evidence_basis")
        else:
            eval_results.append(
                {"name": "EVAL-OBJ-002", "status": "pass", "reason": ""}
            )

        # EVAL-OBJ-003: temperature_zero
        offenders = []
        for pred in predictions:
            if pred.get("extraction_temperature") != 0:
                offenders.append(pred.get("prediction_id", "?"))
        if offenders:
            eval_results.append(
                {
                    "name": "EVAL-OBJ-003",
                    "status": "fail",
                    "reason": "non_zero_temperature: " + ", ".join(offenders),
                }
            )
            reason_codes.append("EVAL-OBJ-003:temperature_zero")
        else:
            eval_results.append(
                {"name": "EVAL-OBJ-003", "status": "pass", "reason": ""}
            )

        # EVAL-OBJ-004: recency_cutoff_applied
        offenders = []
        for pred in predictions:
            if not pred.get("recency_cutoff_applied"):
                offenders.append(pred.get("prediction_id", "?"))
        if offenders:
            eval_results.append(
                {
                    "name": "EVAL-OBJ-004",
                    "status": "fail",
                    "reason": "recency_cutoff_not_applied: " + ", ".join(offenders),
                }
            )
            reason_codes.append("EVAL-OBJ-004:recency_cutoff_applied")
        else:
            eval_results.append(
                {"name": "EVAL-OBJ-004", "status": "pass", "reason": ""}
            )

        # EVAL-OBJ-005: no_evidence_basis_flag_consistent
        offenders = []
        for pred in predictions:
            empty = not pred.get("evidence_basis")
            flag = bool(pred.get("no_evidence_basis_flag"))
            if empty != flag:
                offenders.append(
                    f"{pred.get('prediction_id', '?')}: "
                    f"evidence_basis_empty={empty} flag={flag}"
                )
        if offenders:
            eval_results.append(
                {
                    "name": "EVAL-OBJ-005",
                    "status": "fail",
                    "reason": "no_evidence_basis_flag_inconsistent: "
                    + "; ".join(offenders),
                }
            )
            reason_codes.append("EVAL-OBJ-005:no_evidence_basis_flag_consistent")
        else:
            eval_results.append(
                {"name": "EVAL-OBJ-005", "status": "pass", "reason": ""}
            )

        decision = "block" if reason_codes else "allow"
        return {
            "decision": decision,
            "eval_results": eval_results,
            "reason_codes": reason_codes,
        }
