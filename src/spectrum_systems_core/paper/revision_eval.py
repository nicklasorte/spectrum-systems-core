"""RevisionEval: deterministic evals on revision instructions.

EVAL-REV-001..004. All required. Any failure blocks. Auto-application is
explicitly forbidden — instructions must start as "pending" until a human
approves them via the approve-revisions CLI command.
"""
from __future__ import annotations

from typing import Any

_REQUIRED_FIELDS = ("target_section", "instruction_text", "expected_outcome")


class RevisionEval:
    """Run required-fields, claim-id, temperature, and pending-status evals."""

    def run(
        self,
        instructions: list[dict[str, Any]],
        claims: list[dict[str, Any]],
    ) -> dict[str, Any]:
        eval_results: list[dict[str, Any]] = []
        reason_codes: list[str] = []

        # EVAL-REV-001: required fields present + non-empty.
        missing_fields: list[str] = []
        for inst in instructions:
            for f in _REQUIRED_FIELDS:
                value = inst.get(f)
                if not isinstance(value, str) or not value.strip():
                    missing_fields.append(
                        f"{inst.get('instruction_id', '?')}: empty_{f}"
                    )
        if missing_fields:
            eval_results.append(
                {
                    "name": "EVAL-REV-001",
                    "status": "fail",
                    "reason": "; ".join(missing_fields),
                }
            )
            reason_codes.append("EVAL-REV-001:required_fields_present")
        else:
            eval_results.append(
                {"name": "EVAL-REV-001", "status": "pass", "reason": ""}
            )

        # EVAL-REV-002: claim_id_exists (orphan check)
        valid_claim_ids = {c.get("claim_id") for c in claims if c.get("claim_id")}
        orphans: list[str] = []
        for inst in instructions:
            cid = inst.get("claim_id")
            if cid is None:
                continue
            if cid not in valid_claim_ids:
                orphans.append(
                    f"{inst.get('instruction_id', '?')}: orphan_claim_id={cid}"
                )
        if orphans:
            eval_results.append(
                {
                    "name": "EVAL-REV-002",
                    "status": "fail",
                    "reason": "; ".join(orphans),
                }
            )
            reason_codes.append("EVAL-REV-002:claim_id_exists")
        else:
            eval_results.append(
                {"name": "EVAL-REV-002", "status": "pass", "reason": ""}
            )

        # EVAL-REV-003: temperature_zero
        non_zero: list[str] = []
        for inst in instructions:
            if inst.get("extraction_temperature") != 0:
                non_zero.append(
                    f"{inst.get('instruction_id', '?')}: "
                    f"temperature={inst.get('extraction_temperature')}"
                )
        if non_zero:
            eval_results.append(
                {
                    "name": "EVAL-REV-003",
                    "status": "fail",
                    "reason": "; ".join(non_zero),
                }
            )
            reason_codes.append("EVAL-REV-003:temperature_zero")
        else:
            eval_results.append(
                {"name": "EVAL-REV-003", "status": "pass", "reason": ""}
            )

        # EVAL-REV-004: pending_status
        not_pending: list[str] = []
        for inst in instructions:
            if inst.get("status") != "pending":
                not_pending.append(
                    f"{inst.get('instruction_id', '?')}: "
                    f"status={inst.get('status')}"
                )
        if not_pending:
            eval_results.append(
                {
                    "name": "EVAL-REV-004",
                    "status": "fail",
                    "reason": "auto_application_forbidden: "
                    + "; ".join(not_pending),
                }
            )
            reason_codes.append("EVAL-REV-004:pending_status")
        else:
            eval_results.append(
                {"name": "EVAL-REV-004", "status": "pass", "reason": ""}
            )

        decision = "block" if reason_codes else "allow"
        return {
            "decision": decision,
            "eval_results": eval_results,
            "reason_codes": reason_codes,
        }
