"""MitigationOutcomeTracker: dual-signal outcome recording.

FINDING-E-004: human_marked_outcome alone cannot mark a mitigation effective.
If a secondary source is provided AND a matching objection_type recurs from
the same agency in that source, the outcome is auto-downgraded to
"ineffective" regardless of the human mark. Both signals are recorded.
"""
from __future__ import annotations

import datetime
import json
import uuid
from pathlib import Path
from typing import Any

import jsonschema

from ..extraction._paths import find_processed_dir
from ._paths import agency_dir, agency_schema_path


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


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


_VALID_OUTCOMES = {"effective", "ineffective", "partial", "unknown"}


class MitigationOutcomeTracker:
    """Record outcomes for applied mitigations with deterministic auto-downgrade."""

    def record_outcome(
        self,
        mitigation_id: str,
        agency_slug: str,
        paper_source_id: str,
        human_marked_outcome: str,
        secondary_check_source_id: str | None = None,
        repo_root: str | None = None,
        outcome_note: str = "",
    ) -> dict[str, Any]:
        if repo_root is None:
            return {"status": "failure", "reason": "repo_root_required"}
        repo_root_path = Path(repo_root).resolve()

        if human_marked_outcome not in _VALID_OUTCOMES:
            return {
                "status": "failure",
                "reason": f"invalid_human_marked_outcome:{human_marked_outcome}",
            }

        # Step 1: locate the mitigation by mitigation_id.
        mitigation = self._find_mitigation(
            repo_root_path, paper_source_id, mitigation_id
        )
        if mitigation is None:
            return {"status": "failure", "reason": "mitigation_not_found"}

        # Locate originating prediction's objection_type for the secondary check.
        prediction_id = mitigation.get("prediction_id")
        prediction = self._find_prediction(
            repo_root_path, paper_source_id, prediction_id
        ) if prediction_id else None
        original_objection_type = (
            prediction.get("objection_type") if prediction else None
        )

        # Step 2: secondary check.
        secondary_recurred: bool | None = None
        if secondary_check_source_id:
            secondary_recurred = self._check_objection_recurrence(
                repo_root_path,
                secondary_source_id=secondary_check_source_id,
                agency_slug=agency_slug,
                original_objection_type=original_objection_type,
            )

        # Step 3: compute final_outcome.
        if secondary_recurred is True:
            final_outcome = "ineffective"  # FINDING-E-004 auto-downgrade
            auto_downgraded = True
        else:
            final_outcome = human_marked_outcome
            auto_downgraded = False

        # Step 4: assemble + validate outcome_record.
        outcome = {
            "outcome_id": str(uuid.uuid4()),
            "mitigation_id": mitigation_id,
            "agency_slug": agency_slug,
            "paper_source_id": paper_source_id,
            "human_marked_outcome": human_marked_outcome,
            "secondary_check_source_id": secondary_check_source_id,
            "secondary_check_objection_recurred": secondary_recurred,
            "final_outcome": final_outcome,
            "outcome_note": outcome_note,
            "recorded_at": _now_iso(),
        }
        try:
            schema = json.loads(
                agency_schema_path("outcome_record").read_text(encoding="utf-8")
            )
            jsonschema.Draft202012Validator(schema).validate(outcome)
        except (FileNotFoundError, OSError) as exc:
            return {"status": "failure", "reason": f"schema_unreadable: {exc}"}
        except jsonschema.ValidationError as exc:
            return {
                "status": "failure",
                "reason": f"schema_violation: {exc.message}",
            }

        # Step 5: append to mitigation_outcomes.jsonl.
        target_dir = agency_dir(repo_root_path, agency_slug, create=True)
        outcomes_path = target_dir / "mitigation_outcomes.jsonl"
        try:
            with outcomes_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(outcome, sort_keys=True, separators=(",", ":")) + "\n"
                )
        except OSError as exc:
            return {"status": "failure", "reason": f"write_error: {exc}"}

        return {
            "status": "success",
            "final_outcome": final_outcome,
            "auto_downgraded": auto_downgraded,
            "outcome": outcome,
            "reason": "",
        }

    def _find_mitigation(
        self,
        repo_root: Path,
        paper_source_id: str,
        mitigation_id: str,
    ) -> dict[str, Any] | None:
        processed_dir, _ = find_processed_dir(repo_root, paper_source_id)
        if processed_dir is None:
            return None
        path = processed_dir / "paper" / "objections" / "mitigations.jsonl"
        for mit in _read_jsonl(path):
            if mit.get("mitigation_id") == mitigation_id:
                return mit
        return None

    def _find_prediction(
        self,
        repo_root: Path,
        paper_source_id: str,
        prediction_id: str | None,
    ) -> dict[str, Any] | None:
        if not prediction_id:
            return None
        processed_dir, _ = find_processed_dir(repo_root, paper_source_id)
        if processed_dir is None:
            return None
        path = processed_dir / "paper" / "objections" / "predictions.jsonl"
        for pred in _read_jsonl(path):
            if pred.get("prediction_id") == prediction_id:
                return pred
        return None

    def _check_objection_recurrence(
        self,
        repo_root: Path,
        *,
        secondary_source_id: str,
        agency_slug: str,
        original_objection_type: str | None,
    ) -> bool:
        """True iff the secondary source raised the same objection_type from this agency.

        We map each issue in the secondary source's paper/issues.jsonl that
        is an agency_comment back to an agency profile (via the existing
        objection_history.jsonl entries). If any matching entry has the
        same objection_type, recurrence=True.

        If we cannot determine recurrence (e.g. secondary source not found),
        return False — the human's mark stands.
        """
        if not original_objection_type:
            return False

        # Direct read from objection_history.jsonl: any entry whose
        # paper_source_id == secondary_source_id and objection_type matches.
        history_path = (
            repo_root / "agency" / agency_slug / "objection_history.jsonl"
        )
        for entry in _read_jsonl(history_path):
            if (
                entry.get("paper_source_id") == secondary_source_id
                and entry.get("objection_type") == original_objection_type
            ):
                return True

        # Fall back to scanning the secondary source's issues.jsonl directly.
        secondary_processed, _ = find_processed_dir(repo_root, secondary_source_id)
        if secondary_processed is None:
            return False
        issues_path = secondary_processed / "paper" / "issues.jsonl"
        for issue in _read_jsonl(issues_path):
            if issue.get("issue_type") != "agency_comment":
                continue
            # We recurrence-match by severity bucketed objection_type — same
            # mapping ProfileBuilder uses.
            severity = issue.get("severity") or "minor"
            severity_to_type = {
                "critical": "technical_dispute",
                "major": "methodology_concern",
                "minor": "procedural",
            }
            if severity_to_type.get(severity) == original_objection_type:
                return True
        return False
