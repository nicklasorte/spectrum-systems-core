"""AgencyEval: deterministic evals on an agency profile + its position log.

EVAL-AGENCY-001..004. No LLM.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Dict, List

import jsonschema

from ..extraction._paths import find_processed_dir
from ._paths import agency_dir, agency_schema_path


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
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


def _scan_all_paper_issues(repo_root: Path) -> Dict[str, str]:
    """Map issue_id -> source_id across every processed/<family>/<source>/paper/issues.jsonl."""
    out: Dict[str, str] = {}
    processed_root = repo_root / "processed"
    if not processed_root.is_dir():
        return out
    for family_dir in processed_root.iterdir():
        if not family_dir.is_dir():
            continue
        for source_dir in family_dir.iterdir():
            issues_path = source_dir / "paper" / "issues.jsonl"
            if not issues_path.is_file():
                continue
            for issue in _read_jsonl(issues_path):
                iid = issue.get("issue_id")
                if isinstance(iid, str):
                    out[iid] = source_dir.name
    return out


class AgencyEval:
    """Deterministic evals against agency_profile + positions + history."""

    def run(self, agency_slug: str, repo_root: str) -> Dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        eval_results: List[Dict[str, Any]] = []
        reason_codes: List[str] = []

        agency_path = agency_dir(repo_root_path, agency_slug)
        profile_path = agency_path / "profile.json"
        positions_path = agency_path / "positions.jsonl"

        # EVAL-AGENCY-001: profile_schema_valid
        try:
            profile_text = profile_path.read_text(encoding="utf-8")
            profile = json.loads(profile_text)
            schema = json.loads(
                agency_schema_path("agency_profile").read_text(encoding="utf-8")
            )
            jsonschema.Draft202012Validator(schema).validate(profile)
            eval_results.append(
                {"name": "EVAL-AGENCY-001", "status": "pass", "reason": ""}
            )
        except (FileNotFoundError, OSError) as exc:
            eval_results.append(
                {
                    "name": "EVAL-AGENCY-001",
                    "status": "fail",
                    "reason": f"profile_unreadable: {exc}",
                }
            )
            reason_codes.append("EVAL-AGENCY-001:profile_schema_valid")
            profile = {}
        except json.JSONDecodeError as exc:
            eval_results.append(
                {
                    "name": "EVAL-AGENCY-001",
                    "status": "fail",
                    "reason": f"profile_json_invalid: {exc}",
                }
            )
            reason_codes.append("EVAL-AGENCY-001:profile_schema_valid")
            profile = {}
        except jsonschema.ValidationError as exc:
            eval_results.append(
                {
                    "name": "EVAL-AGENCY-001",
                    "status": "fail",
                    "reason": f"schema_violation: {exc.message}",
                }
            )
            reason_codes.append("EVAL-AGENCY-001:profile_schema_valid")
            profile = {}

        # EVAL-AGENCY-002: no_duplicate_profile (warn). Scan sibling profiles.
        canonical_name = str(profile.get("agency_name") or "").strip().lower()
        canonical_slug = str(profile.get("agency_slug") or agency_slug).strip().lower()
        canonical_aliases = {
            str(a).strip().lower()
            for a in (profile.get("aliases") or [])
            if isinstance(a, str)
        }
        duplicates: List[str] = []
        agency_root = repo_root_path / "agency"
        if agency_root.is_dir():
            for sibling in sorted(agency_root.iterdir()):
                if not sibling.is_dir() or sibling.name == agency_slug:
                    continue
                sib_profile_path = sibling / "profile.json"
                if not sib_profile_path.is_file():
                    continue
                try:
                    sib = json.loads(sib_profile_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                sib_name = str(sib.get("agency_name") or "").strip().lower()
                sib_aliases = {
                    str(a).strip().lower()
                    for a in (sib.get("aliases") or [])
                    if isinstance(a, str)
                }
                if sib_name and (
                    sib_name == canonical_name or sib_name in canonical_aliases
                ):
                    duplicates.append(sib.get("agency_slug") or sibling.name)
                    continue
                if canonical_aliases & sib_aliases:
                    duplicates.append(sib.get("agency_slug") or sibling.name)
                    continue
        if duplicates:
            eval_results.append(
                {
                    "name": "EVAL-AGENCY-002",
                    "status": "warn",
                    "reason": "duplicate_profile_candidates: " + ", ".join(duplicates),
                }
            )
        else:
            eval_results.append(
                {"name": "EVAL-AGENCY-002", "status": "pass", "reason": ""}
            )

        # EVAL-AGENCY-003: positions_have_source_issues
        positions = _read_jsonl(positions_path)
        all_issue_ids = set(_scan_all_paper_issues(repo_root_path).keys())
        orphans: List[str] = []
        for pos in positions:
            sids = pos.get("source_issue_ids") or []
            if not sids:
                orphans.append(
                    f"{pos.get('position_id', '?')}: empty_source_issue_ids"
                )
                continue
            if all_issue_ids and not any(s in all_issue_ids for s in sids):
                orphans.append(
                    f"{pos.get('position_id', '?')}: no_traceable_issue"
                )
        if orphans:
            eval_results.append(
                {
                    "name": "EVAL-AGENCY-003",
                    "status": "fail",
                    "reason": "untraceable_positions: " + "; ".join(orphans),
                }
            )
            reason_codes.append("EVAL-AGENCY-003:positions_have_source_issues")
        else:
            eval_results.append(
                {"name": "EVAL-AGENCY-003", "status": "pass", "reason": ""}
            )

        # EVAL-AGENCY-004: no_stale_position_as_primary (warn).
        stale_warnings: List[str] = []
        today = datetime.datetime.now(datetime.timezone.utc).date()
        # Group by topic; pick the most recent valid_from.
        by_topic: Dict[str, List[Dict[str, Any]]] = {}
        for pos in positions:
            topic = str(pos.get("topic") or "").strip().lower()
            if not topic:
                continue
            by_topic.setdefault(topic, []).append(pos)
        for topic, plist in by_topic.items():
            plist_sorted = sorted(
                plist, key=lambda p: str(p.get("valid_from") or ""), reverse=True
            )
            primary = plist_sorted[0]
            valid_until = primary.get("valid_until")
            if valid_until is None:
                continue
            try:
                vu_date = datetime.date.fromisoformat(str(valid_until))
            except ValueError:
                continue
            if vu_date < today:
                stale_warnings.append(
                    f"topic={topic} position_id={primary.get('position_id', '?')}"
                )
        if stale_warnings:
            eval_results.append(
                {
                    "name": "EVAL-AGENCY-004",
                    "status": "warn",
                    "reason": "stale_primary_position: " + "; ".join(stale_warnings),
                }
            )
        else:
            eval_results.append(
                {"name": "EVAL-AGENCY-004", "status": "pass", "reason": ""}
            )

        decision = "block" if reason_codes else "allow"
        return {
            "decision": decision,
            "eval_results": eval_results,
            "reason_codes": reason_codes,
        }
