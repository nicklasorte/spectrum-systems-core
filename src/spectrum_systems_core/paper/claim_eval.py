"""ClaimEval: deterministic evals on claims and assumptions.

EVAL-CLAIM-001..004, EVAL-ASSUMP-001..002. No LLM. Block on any failed
required eval.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import jsonschema

from ..ingestion.grounding import GroundingHelper
from ._paths import paper_schema_path


class ClaimEval:
    """Run schema, grounding, unit-id, temperature, and assumption evals."""

    def __init__(self, grounding: GroundingHelper | None = None) -> None:
        self._grounding = grounding or GroundingHelper()

    def run(
        self,
        claims: List[Dict[str, Any]],
        assumptions: List[Dict[str, Any]],
        source_id: str,
        repo_root: str,
    ) -> Dict[str, Any]:
        eval_results: List[Dict[str, Any]] = []
        reason_codes: List[str] = []

        # Load schemas.
        try:
            claim_schema = json.loads(
                paper_schema_path("technical_claim").read_text(encoding="utf-8")
            )
            assumption_schema = json.loads(
                paper_schema_path("assumption_record").read_text(encoding="utf-8")
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
        claim_validator = jsonschema.Draft202012Validator(claim_schema)
        assumption_validator = jsonschema.Draft202012Validator(assumption_schema)

        # EVAL-CLAIM-001: schema_conformance
        claim_schema_failures: List[str] = []
        for claim in claims:
            try:
                claim_validator.validate(claim)
            except jsonschema.ValidationError as exc:
                claim_schema_failures.append(
                    f"{claim.get('claim_id', '?')}: {exc.message}"
                )
        if claim_schema_failures:
            eval_results.append(
                {
                    "name": "EVAL-CLAIM-001",
                    "status": "fail",
                    "reason": "; ".join(claim_schema_failures),
                }
            )
            reason_codes.append("EVAL-CLAIM-001:schema_conformance")
        else:
            eval_results.append(
                {"name": "EVAL-CLAIM-001", "status": "pass", "reason": ""}
            )

        # EVAL-CLAIM-002: source_grounding
        grounding_failures: List[str] = []
        for claim in claims:
            excerpt = claim.get("source_excerpt") or ""
            if not excerpt:
                grounding_failures.append(
                    f"{claim.get('claim_id', '?')}: empty_excerpt"
                )
                continue
            try:
                result = self._grounding.verify_excerpt(
                    excerpt, source_id, repo_root
                )
            except Exception as exc:  # noqa: BLE001
                grounding_failures.append(
                    f"{claim.get('claim_id', '?')}: grounding_error: {exc}"
                )
                continue
            if not result.get("grounded"):
                grounding_failures.append(
                    f"{claim.get('claim_id', '?')}: not_grounded"
                )
        if grounding_failures:
            eval_results.append(
                {
                    "name": "EVAL-CLAIM-002",
                    "status": "fail",
                    "reason": "; ".join(grounding_failures),
                }
            )
            reason_codes.append("EVAL-CLAIM-002:source_grounding")
        else:
            eval_results.append(
                {"name": "EVAL-CLAIM-002", "status": "pass", "reason": ""}
            )

        # EVAL-CLAIM-003: unit_id_present
        unit_id_failures: List[str] = []
        for claim in claims:
            if not claim.get("source_unit_id"):
                unit_id_failures.append(
                    f"{claim.get('claim_id', '?')}: missing_source_unit_id"
                )
        if unit_id_failures:
            eval_results.append(
                {
                    "name": "EVAL-CLAIM-003",
                    "status": "fail",
                    "reason": "; ".join(unit_id_failures),
                }
            )
            reason_codes.append("EVAL-CLAIM-003:unit_id_present")
        else:
            eval_results.append(
                {"name": "EVAL-CLAIM-003", "status": "pass", "reason": ""}
            )

        # EVAL-CLAIM-004: temperature_zero
        temp_failures: List[str] = []
        for claim in claims:
            if claim.get("extraction_temperature") != 0:
                temp_failures.append(
                    f"{claim.get('claim_id', '?')}: "
                    f"temperature={claim.get('extraction_temperature')}"
                )
        if temp_failures:
            eval_results.append(
                {
                    "name": "EVAL-CLAIM-004",
                    "status": "fail",
                    "reason": "; ".join(temp_failures),
                }
            )
            reason_codes.append("EVAL-CLAIM-004:temperature_zero")
        else:
            eval_results.append(
                {"name": "EVAL-CLAIM-004", "status": "pass", "reason": ""}
            )

        # EVAL-ASSUMP-001: schema_conformance
        assumption_schema_failures: List[str] = []
        for record in assumptions:
            try:
                assumption_validator.validate(record)
            except jsonschema.ValidationError as exc:
                assumption_schema_failures.append(
                    f"{record.get('assumption_id', '?')}: {exc.message}"
                )
        if assumption_schema_failures:
            eval_results.append(
                {
                    "name": "EVAL-ASSUMP-001",
                    "status": "fail",
                    "reason": "; ".join(assumption_schema_failures),
                }
            )
            reason_codes.append("EVAL-ASSUMP-001:schema_conformance")
        else:
            eval_results.append(
                {"name": "EVAL-ASSUMP-001", "status": "pass", "reason": ""}
            )

        # EVAL-ASSUMP-002: implicit assumption must have null source_excerpt.
        implicit_failures: List[str] = []
        for record in assumptions:
            explicit = record.get("explicit")
            excerpt = record.get("source_excerpt")
            if explicit is False and excerpt is not None:
                implicit_failures.append(
                    f"{record.get('assumption_id', '?')}: "
                    "implicit_with_excerpt"
                )
        if implicit_failures:
            eval_results.append(
                {
                    "name": "EVAL-ASSUMP-002",
                    "status": "fail",
                    "reason": "; ".join(implicit_failures),
                }
            )
            reason_codes.append("EVAL-ASSUMP-002:implicit_excerpt")
        else:
            eval_results.append(
                {"name": "EVAL-ASSUMP-002", "status": "pass", "reason": ""}
            )

        decision = "block" if reason_codes else "allow"
        return {
            "decision": decision,
            "eval_results": eval_results,
            "reason_codes": reason_codes,
        }
