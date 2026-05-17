"""IssueEval: deterministic evals on the issue registry.

EVAL-ISSUE-001..004. Validates schema, source traceability, and orphan
claim_id references. Critical-issue review is a warn (humans decide).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

from ..extraction._paths import find_processed_dir
from ._paths import paper_schema_path


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.is_file():
        return out
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


class IssueEval:
    """Run schema + traceability + orphan checks on issue records."""

    def run(
        self,
        issues: list[dict[str, Any]],
        *,
        working_paper_source_id: str | None = None,
        repo_root: str | None = None,
    ) -> dict[str, Any]:
        eval_results: list[dict[str, Any]] = []
        reason_codes: list[str] = []

        # EVAL-ISSUE-001: schema_conformance
        try:
            schema = json.loads(
                paper_schema_path("issue_record").read_text(encoding="utf-8")
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
        schema_failures: list[str] = []
        for issue in issues:
            try:
                validator.validate(issue)
            except jsonschema.ValidationError as exc:
                schema_failures.append(
                    f"{issue.get('issue_id', '?')}: {exc.message}"
                )
        if schema_failures:
            eval_results.append(
                {
                    "name": "EVAL-ISSUE-001",
                    "status": "fail",
                    "reason": "; ".join(schema_failures),
                }
            )
            reason_codes.append("EVAL-ISSUE-001:schema_conformance")
        else:
            eval_results.append(
                {"name": "EVAL-ISSUE-001", "status": "pass", "reason": ""}
            )

        # EVAL-ISSUE-002: critical issues addressed (warn).
        unaddressed_critical = [
            i.get("issue_id", "?")
            for i in issues
            if i.get("severity") == "critical" and i.get("status") == "open"
        ]
        if unaddressed_critical:
            eval_results.append(
                {
                    "name": "EVAL-ISSUE-002",
                    "status": "warn",
                    "reason": "open_critical_issues: "
                    + ", ".join(unaddressed_critical),
                }
            )
        else:
            eval_results.append(
                {"name": "EVAL-ISSUE-002", "status": "pass", "reason": ""}
            )

        # EVAL-ISSUE-003: no orphan claim_ids.
        valid_claim_ids: set = set()
        if working_paper_source_id and repo_root:
            repo_root_path = Path(repo_root).resolve()
            processed_dir, _ = find_processed_dir(
                repo_root_path, working_paper_source_id
            )
            if processed_dir is not None:
                claims = _read_jsonl(processed_dir / "paper" / "claims.jsonl")
                valid_claim_ids = {
                    c.get("claim_id") for c in claims if c.get("claim_id")
                }

        orphans: list[str] = []
        for issue in issues:
            cid = issue.get("claim_id")
            if cid is None:
                continue
            if valid_claim_ids and cid not in valid_claim_ids:
                orphans.append(
                    f"{issue.get('issue_id', '?')}: claim_id={cid}"
                )
            elif not valid_claim_ids:
                # If we have no claim registry context, flag any non-null claim_id
                # as orphan to fail closed.
                orphans.append(
                    f"{issue.get('issue_id', '?')}: claim_id={cid} "
                    "(no_claim_registry_context)"
                )
        if orphans:
            eval_results.append(
                {
                    "name": "EVAL-ISSUE-003",
                    "status": "fail",
                    "reason": "orphan_claim_ids: " + "; ".join(orphans),
                }
            )
            reason_codes.append("EVAL-ISSUE-003:orphan_claim_ids")
        else:
            eval_results.append(
                {"name": "EVAL-ISSUE-003", "status": "pass", "reason": ""}
            )

        # EVAL-ISSUE-004: source traceability.
        traceability_failures: list[str] = []
        for issue in issues:
            if issue.get("issue_type") == "agency_comment":
                continue
            if not issue.get("source_unit_id"):
                traceability_failures.append(
                    f"{issue.get('issue_id', '?')}: missing_source_unit_id"
                )
        if traceability_failures:
            eval_results.append(
                {
                    "name": "EVAL-ISSUE-004",
                    "status": "fail",
                    "reason": "; ".join(traceability_failures),
                }
            )
            reason_codes.append("EVAL-ISSUE-004:source_traceability")
        else:
            eval_results.append(
                {"name": "EVAL-ISSUE-004", "status": "pass", "reason": ""}
            )

        decision = "block" if reason_codes else "allow"
        return {
            "decision": decision,
            "eval_results": eval_results,
            "reason_codes": reason_codes,
        }
