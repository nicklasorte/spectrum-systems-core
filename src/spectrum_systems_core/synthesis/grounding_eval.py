"""GroundingEval: verify all inline citations on a report_draft.

EVAL-GEN-001 schema, EVAL-GEN-002 citation_exists (FINDING-F-004),
EVAL-GEN-003 temperature_zero, EVAL-GEN-004 cost_within_budget
(FINDING-F-007). EVAL-GEN-002 blocks on a fabricated citation but only
warns when a section has factual content with no citations at all.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import jsonschema

from ._paths import synthesis_run_dir, synthesis_schema_path
from .cost_recorder import MAX_SYNTHESIS_COST_USD, total_cost_usd
from .data_lake_check import DataLakeChecker


MIN_CONTENT_FOR_CITATION = 50


class GroundingEval:
    """Verify inline citations against the data lake / local promoted set."""

    def __init__(self, checker: Optional[DataLakeChecker] = None):
        self._checker_override = checker

    def run(self, draft: Dict[str, Any], repo_root: str) -> Dict[str, Any]:
        eval_results: List[Dict[str, Any]] = []
        reason_codes: List[str] = []
        warn_codes: List[str] = []

        # EVAL-GEN-001: schema_conformance
        try:
            schema = json.loads(
                synthesis_schema_path("report_draft").read_text(encoding="utf-8")
            )
            jsonschema.Draft202012Validator(schema).validate(draft)
            eval_results.append(
                {"name": "EVAL-GEN-001", "status": "pass", "reason": ""}
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
                "total_cost_usd": 0.0,
            }
        except jsonschema.ValidationError as exc:
            return {
                "decision": "block",
                "eval_results": [
                    {
                        "name": "EVAL-GEN-001",
                        "status": "fail",
                        "reason": f"schema_violation: {exc.message}",
                    }
                ],
                "reason_codes": ["EVAL-GEN-001:schema_conformance"],
                "warn_codes": [],
                "total_cost_usd": 0.0,
            }

        # EVAL-GEN-002: citation_exists  (FINDING-F-004)
        checker = self._checker_override or DataLakeChecker(repo_root)
        fabricated_overall: List[str] = []
        missing_citation_sections: List[str] = []
        for section in draft.get("sections", []):
            citations = list(section.get("inline_citations") or [])
            unverified: List[str] = []
            for cid in citations:
                if not checker.exists(cid):
                    unverified.append(cid)
                    fabricated_overall.append(cid)
            section_grounded = True
            if unverified:
                section_grounded = False
            if not citations:
                # No citations at all — only warn if section has real content.
                if len(str(section.get("content") or "")) >= MIN_CONTENT_FOR_CITATION:
                    section_grounded = False
                    if "no_citations" not in unverified:
                        unverified.append("no_citations")
                        missing_citation_sections.append(
                            section.get("section_id", "?")
                        )
            section["grounded"] = section_grounded
            section["unverified_citations"] = unverified

        if fabricated_overall:
            eval_results.append(
                {
                    "name": "EVAL-GEN-002",
                    "status": "fail",
                    "reason": "fabricated_citations: "
                    + ", ".join(sorted(set(fabricated_overall))),
                }
            )
            reason_codes.append("EVAL-GEN-002:fabricated_citation")
        elif missing_citation_sections:
            eval_results.append(
                {
                    "name": "EVAL-GEN-002",
                    "status": "warn",
                    "reason": "sections_without_citations: "
                    + ", ".join(missing_citation_sections),
                }
            )
            warn_codes.append("EVAL-GEN-002:no_citations")
        else:
            eval_results.append(
                {"name": "EVAL-GEN-002", "status": "pass", "reason": ""}
            )

        # EVAL-GEN-003: temperature_zero
        if draft.get("generation_temperature") != 0:
            eval_results.append(
                {
                    "name": "EVAL-GEN-003",
                    "status": "fail",
                    "reason": "non_zero_temperature",
                }
            )
            reason_codes.append("EVAL-GEN-003:temperature_zero")
        else:
            eval_results.append(
                {"name": "EVAL-GEN-003", "status": "pass", "reason": ""}
            )

        # EVAL-GEN-004: cost_within_budget  (FINDING-F-007)
        cost_total = total_cost_usd(draft.get("run_id", ""), repo_root)
        if cost_total > MAX_SYNTHESIS_COST_USD:
            eval_results.append(
                {
                    "name": "EVAL-GEN-004",
                    "status": "warn",
                    "reason": (
                        f"cost_over_threshold: ${cost_total:.4f} > "
                        f"${MAX_SYNTHESIS_COST_USD:.2f}"
                    ),
                }
            )
            warn_codes.append("EVAL-GEN-004:cost_over_threshold")
        else:
            eval_results.append(
                {
                    "name": "EVAL-GEN-004",
                    "status": "pass",
                    "reason": f"total_cost_usd=${cost_total:.4f}",
                }
            )

        decision = "block" if reason_codes else ("warn" if warn_codes else "allow")

        # Persist updated grounding flags onto the draft on disk.
        all_grounded = all(
            bool(section.get("grounded")) for section in draft.get("sections", [])
        )
        if all_grounded and decision != "block":
            draft["status"] = "grounded"
        try:
            run_dir = synthesis_run_dir(
                Path(repo_root).resolve(), draft.get("run_id", ""), create=True
            )
            (run_dir / "report_draft.json").write_text(
                json.dumps(draft, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except (OSError, ValueError):
            pass

        return {
            "decision": decision,
            "eval_results": eval_results,
            "reason_codes": reason_codes,
            "warn_codes": warn_codes,
            "total_cost_usd": float(cost_total),
        }
