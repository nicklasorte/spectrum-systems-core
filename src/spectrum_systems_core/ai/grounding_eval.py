"""AIGroundingEval: deterministic governance evals for ai_output artifacts.

Six evals total. EVAL-AI-001 schema; EVAL-AI-002 advisory flag and human
review required (FINDING-H-005); EVAL-AI-003 every citation verified by
DataLake.exists() (FINDING-H-003 / FINDING-H-004); EVAL-AI-004 all citations
match UUID v4 (FINDING-H-003); EVAL-AI-005 cost within budget (warn, not
block); EVAL-AI-006 temperature == 0 (determinism).

A high-confidence answer with no citations is also blocked (RT3-001) because
that pattern is a hallucination signal. Low-confidence answers without
citations warn but do not block.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import jsonschema

from ._paths import ai_costs_dir, load_schema


MAX_QUERY_TOKENS = 4000
MAX_QUERY_COST_USD = 0.10
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _read_query_cost(repo_root: str, query_id: str) -> Optional[float]:
    cost_path = ai_costs_dir(repo_root) / f"{query_id}.json"
    if not cost_path.is_file():
        return None
    try:
        record = json.loads(cost_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    val = record.get("estimated_cost_usd")
    if isinstance(val, (int, float)):
        return float(val)
    return None


class AIGroundingEval:
    """Run grounding evals on an ai_output artifact."""

    def run(
        self,
        ai_output: Dict[str, Any],
        query_id: str,
        repo_root: str,
    ) -> Dict[str, Any]:
        eval_results: List[Dict[str, Any]] = []
        reason_codes: List[str] = []
        warn_codes: List[str] = []
        failure_types: List[str] = []

        # EVAL-AI-001: schema_conformance
        try:
            schema = load_schema("ai_output")
            jsonschema.Draft202012Validator(schema).validate(ai_output)
            eval_results.append(
                {"name": "EVAL-AI-001", "status": "pass", "reason": ""}
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
                "warn_codes": [],
                "failure_types": ["schema_violation"],
            }
        except jsonschema.ValidationError as exc:
            return {
                "decision": "block",
                "eval_results": [
                    {
                        "name": "EVAL-AI-001",
                        "status": "fail",
                        "reason": f"schema_violation: {exc.message}",
                    }
                ],
                "reason_codes": ["EVAL-AI-001:schema_conformance"],
                "warn_codes": [],
                "failure_types": ["schema_violation"],
            }

        # EVAL-AI-002: advisory_flag_present
        if (
            ai_output.get("ai_advisory") is not True
            or ai_output.get("requires_human_review") is not True
        ):
            eval_results.append(
                {
                    "name": "EVAL-AI-002",
                    "status": "fail",
                    "reason": "missing_advisory_or_review_flag",
                }
            )
            reason_codes.append("EVAL-AI-002:advisory_flag_present")
            failure_types.append("missing_advisory_flag")
        else:
            eval_results.append(
                {"name": "EVAL-AI-002", "status": "pass", "reason": ""}
            )

        citations = list(ai_output.get("citations") or [])
        unverified = list(ai_output.get("unverified_citations") or [])

        # EVAL-AI-004: citations_are_uuids — run before EVAL-AI-003 because
        # an unverified-uuid set is meaningless if a citation is malformed.
        bad_citations = [c for c in citations if not UUID_PATTERN.match(c)]
        if bad_citations:
            eval_results.append(
                {
                    "name": "EVAL-AI-004",
                    "status": "fail",
                    "reason": "non_uuid_citations: " + ", ".join(bad_citations),
                }
            )
            reason_codes.append("EVAL-AI-004:citations_are_uuids")
            failure_types.append("non_uuid_citation")
        else:
            eval_results.append(
                {"name": "EVAL-AI-004", "status": "pass", "reason": ""}
            )

        # EVAL-AI-003: no_fabricated_citations
        if unverified:
            eval_results.append(
                {
                    "name": "EVAL-AI-003",
                    "status": "fail",
                    "reason": "fabricated_citations: " + ", ".join(unverified),
                }
            )
            reason_codes.append("EVAL-AI-003:no_fabricated_citations")
            failure_types.append("fabricated_citation")
        else:
            eval_results.append(
                {"name": "EVAL-AI-003", "status": "pass", "reason": ""}
            )

        # No citations + high confidence is a hallucination signal (RT3-001).
        if not citations:
            confidence = ai_output.get("confidence")
            if confidence == "high":
                eval_results.append(
                    {
                        "name": "EVAL-AI-003",
                        "status": "fail",
                        "reason": "no_citations_in_high_confidence_output",
                    }
                )
                reason_codes.append("EVAL-AI-003:no_citations_in_output")
                failure_types.append("no_citations_in_output")
            else:
                eval_results.append(
                    {
                        "name": "EVAL-AI-003",
                        "status": "warn",
                        "reason": "no_citations_low_or_medium_confidence",
                    }
                )
                warn_codes.append("EVAL-AI-003:no_citations_low_confidence")

        # EVAL-AI-005: cost_within_budget — warn only.
        cost = _read_query_cost(repo_root, query_id)
        if cost is None:
            eval_results.append(
                {
                    "name": "EVAL-AI-005",
                    "status": "pass",
                    "reason": "no_cost_record",
                }
            )
        elif cost > MAX_QUERY_COST_USD:
            eval_results.append(
                {
                    "name": "EVAL-AI-005",
                    "status": "warn",
                    "reason": (
                        f"cost_over_threshold: ${cost:.4f} > "
                        f"${MAX_QUERY_COST_USD:.2f}"
                    ),
                }
            )
            warn_codes.append("EVAL-AI-005:cost_over_threshold")
        else:
            eval_results.append(
                {
                    "name": "EVAL-AI-005",
                    "status": "pass",
                    "reason": f"cost_usd=${cost:.4f}",
                }
            )

        # EVAL-AI-006: temperature_zero
        provenance = ai_output.get("provenance") or {}
        if provenance.get("temperature") != 0:
            eval_results.append(
                {
                    "name": "EVAL-AI-006",
                    "status": "fail",
                    "reason": "non_zero_temperature",
                }
            )
            reason_codes.append("EVAL-AI-006:temperature_zero")
            failure_types.append("schema_violation")
        else:
            eval_results.append(
                {"name": "EVAL-AI-006", "status": "pass", "reason": ""}
            )

        decision = "block" if reason_codes else "allow"
        return {
            "decision": decision,
            "eval_results": eval_results,
            "reason_codes": reason_codes,
            "warn_codes": warn_codes,
            "failure_types": failure_types,
        }
