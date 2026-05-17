"""EvidenceEval: deterministic evals on evidence and contradictions.

EVAL-EVID-001..004, EVAL-CONTR-001. EVAL-EVID-003 emits a warn (not block)
on stale source_record_hash; EVAL-CONTR-001 also warns rather than blocks.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..extraction._paths import find_processed_dir


class EvidenceEval:
    """Run grounding, materiality coverage, freshness, and self-evidencing evals."""

    def run(
        self,
        claims: list[dict[str, Any]],
        evidence_records: list[dict[str, Any]],
        source_id: str,
        repo_root: str,
    ) -> dict[str, Any]:
        eval_results: list[dict[str, Any]] = []
        reason_codes: list[str] = []
        warnings: list[dict[str, Any]] = []

        # EVAL-EVID-001: evidence_grounded
        ungrounded = [
            r for r in evidence_records if not r.get("grounded")
        ]
        if ungrounded:
            eval_results.append(
                {
                    "name": "EVAL-EVID-001",
                    "status": "fail",
                    "reason": "; ".join(
                        f"{r.get('evidence_id', '?')}: not_grounded"
                        for r in ungrounded
                    ),
                }
            )
            reason_codes.append("EVAL-EVID-001:evidence_grounded")
        else:
            eval_results.append(
                {"name": "EVAL-EVID-001", "status": "pass", "reason": ""}
            )

        # EVAL-EVID-002: high_materiality_coverage
        evidence_by_claim: dict[str, list[dict[str, Any]]] = {}
        for r in evidence_records:
            evidence_by_claim.setdefault(r.get("claim_id", ""), []).append(r)

        high_failures: list[str] = []
        medium_warns: list[str] = []
        for claim in claims:
            cid = claim.get("claim_id", "?")
            mat = claim.get("materiality")
            if mat == "high" and not evidence_by_claim.get(cid):
                high_failures.append(cid)
            elif mat == "medium" and not evidence_by_claim.get(cid):
                medium_warns.append(cid)
        if high_failures:
            eval_results.append(
                {
                    "name": "EVAL-EVID-002",
                    "status": "fail",
                    "reason": "high_materiality_no_evidence: "
                    + ", ".join(high_failures),
                }
            )
            reason_codes.append("EVAL-EVID-002:high_materiality_coverage")
        else:
            status = "warn" if medium_warns else "pass"
            eval_results.append(
                {
                    "name": "EVAL-EVID-002",
                    "status": status,
                    "reason": (
                        "medium_materiality_no_evidence: " + ", ".join(medium_warns)
                        if medium_warns
                        else ""
                    ),
                }
            )

        # EVAL-EVID-003: source_record_hash_current (FINDING-D-005, warn only)
        repo_root_path = Path(repo_root).resolve()
        processed_dir, _ = find_processed_dir(repo_root_path, source_id)
        current_hash = ""
        if processed_dir is not None:
            sr_path = processed_dir / "source_record.json"
            if sr_path.is_file():
                try:
                    sr = json.loads(sr_path.read_text(encoding="utf-8"))
                    current_hash = sr.get("payload", {}).get("raw_hash", "")
                except (OSError, json.JSONDecodeError):
                    current_hash = ""
        stale: list[str] = []
        for r in evidence_records:
            stored = r.get("source_record_hash", "")
            if current_hash and stored and stored != current_hash:
                stale.append(r.get("evidence_id", "?"))
        if stale:
            eval_results.append(
                {
                    "name": "EVAL-EVID-003",
                    "status": "warn",
                    "reason": "stale_evidence_source: " + ", ".join(stale),
                }
            )
            warnings.append({"name": "EVAL-EVID-003", "stale_ids": stale})
        else:
            eval_results.append(
                {"name": "EVAL-EVID-003", "status": "pass", "reason": ""}
            )

        # EVAL-EVID-004: no_circular_evidence
        claim_unit_by_id = {
            c.get("claim_id", ""): c.get("source_unit_id", "") for c in claims
        }
        circular: list[str] = []
        for r in evidence_records:
            cid = r.get("claim_id", "")
            evidence_unit = r.get("source_unit_id", "")
            if (
                cid in claim_unit_by_id
                and evidence_unit
                and evidence_unit == claim_unit_by_id[cid]
            ):
                circular.append(r.get("evidence_id", "?"))
        if circular:
            eval_results.append(
                {
                    "name": "EVAL-EVID-004",
                    "status": "fail",
                    "reason": "self_evidencing: " + ", ".join(circular),
                }
            )
            reason_codes.append("EVAL-EVID-004:self_evidencing")
        else:
            eval_results.append(
                {"name": "EVAL-EVID-004", "status": "pass", "reason": ""}
            )

        # EVAL-CONTR-001: contradiction_review_required (warn only)
        contradicted_high = [
            c for c in claims
            if c.get("materiality") == "high"
            and (c.get("contradicted_by_claim_ids") or [])
        ]
        if contradicted_high:
            eval_results.append(
                {
                    "name": "EVAL-CONTR-001",
                    "status": "warn",
                    "reason": "high_materiality_contradiction_pending_review: "
                    + ", ".join(c.get("claim_id", "?") for c in contradicted_high),
                }
            )
            warnings.append(
                {
                    "name": "EVAL-CONTR-001",
                    "claim_ids": [c.get("claim_id") for c in contradicted_high],
                }
            )
        else:
            eval_results.append(
                {"name": "EVAL-CONTR-001", "status": "pass", "reason": ""}
            )

        decision = "block" if reason_codes else "allow"
        return {
            "decision": decision,
            "eval_results": eval_results,
            "reason_codes": reason_codes,
            "warnings": warnings,
        }
