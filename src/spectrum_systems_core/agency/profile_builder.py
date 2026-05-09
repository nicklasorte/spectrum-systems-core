"""ProfileBuilder: ingest Phase D issue_records into agency profiles.

Reads paper/issues.jsonl issues with issue_type=="agency_comment".
Does NOT re-process raw comment text — Phase D's CommentProcessor already
did the structured extraction. ProfileBuilder lifts each agency_comment
issue into:
  - one position_entry (if a position can be extracted by Haiku)
  - one objection_history_entry (always)
  - profile counts incremented
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..extraction._paths import find_processed_dir
from .alias_normalizer import AliasNormalizer
from .profile_store import AgencyProfileStore

_COMPONENT_NAME = "agency_profile_builder"
_COMPONENT_VERSION = "1.0.0"
EXTRACTION_MODEL = "claude-haiku-4-5-20251001"
EXTRACTION_TEMPERATURE = 0
MAX_TOKENS = 300


POSITION_EXTRACTION_PROMPT = """You are analyzing an agency comment to extract
the agency's position on a technical matter.

Agency: {agency_name}
Issue description: {issue_description}
Issue type: {issue_type}
Severity: {severity}

Extract the agency's position. Return ONLY valid JSON. No preamble.

{{
  "topic": "what specific topic this position addresses (min 3 chars)",
  "position_statement": "clear statement of the agency's position (min 20 chars)",
  "position_type": "supports|opposes|conditionally_supports|requests_clarification|raises_concern",
  "confidence_basis": "why you are confident this is their position (one sentence)"
}}

If the issue does not clearly represent a position (e.g. it is purely procedural),
return: {{"no_position": true}}
"""


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _today_date() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


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


_VALID_POSITION_TYPES = {
    "supports",
    "opposes",
    "conditionally_supports",
    "requests_clarification",
    "raises_concern",
}


def _severity_to_objection_type(severity: str) -> str:
    severity_map = {
        "critical": "technical_dispute",
        "major": "methodology_concern",
        "minor": "procedural",
    }
    return severity_map.get(severity, "procedural")


class ProfileBuilder:
    """Lift Phase D agency_comment issues into agency profile state."""

    def __init__(self, api_caller: Optional[Callable[[str], str]] = None):
        self._api_caller = api_caller

    def ingest_issues_into_profile(
        self,
        paper_source_id: str,
        agency_name: str,
        repo_root: str,
    ) -> Dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        processed_dir, _ = find_processed_dir(repo_root_path, paper_source_id)
        if processed_dir is None:
            return {
                "status": "failure",
                "positions_added": 0,
                "history_added": 0,
                "warnings": 0,
                "reason": "paper_source_not_found",
            }
        issues_path = processed_dir / "paper" / "issues.jsonl"
        all_issues = _read_jsonl(issues_path)
        agency_comment_issues = [
            issue for issue in all_issues if issue.get("issue_type") == "agency_comment"
        ]
        if not agency_comment_issues:
            return {
                "status": "success",
                "positions_added": 0,
                "history_added": 0,
                "warnings": 0,
                "reason": "no_agency_comment_issues",
                "agency_slug": "",
            }

        normalizer = AliasNormalizer()
        agency_slug = normalizer.normalize(agency_name, str(repo_root_path))

        store = AgencyProfileStore()
        store.get_or_create(agency_name, str(repo_root_path))

        if self._api_caller is None:
            if os.environ.get("ANTHROPIC_API_KEY"):
                try:
                    self._api_caller = self._build_default_api_caller()
                except ImportError:
                    self._api_caller = None
            else:
                self._api_caller = None

        positions_added = 0
        history_added = 0
        warnings = 0

        for issue in agency_comment_issues:
            description = issue.get("description") or ""
            severity = issue.get("severity") or "minor"
            issue_id = issue.get("issue_id") or str(uuid.uuid4())

            # Always record an objection_history entry, even when no position.
            entry = {
                "entry_id": _stable_id("history", agency_slug, issue_id),
                "agency_slug": agency_slug,
                "objection_text": description if len(description) >= 20
                else (description + " " * 20)[:60],
                "objection_type": _severity_to_objection_type(severity),
                "source_issue_id": issue_id,
                "paper_source_id": paper_source_id,
                "raised_at": (issue.get("created_at") or _now_iso())[:10],
                "resolved": issue.get("status") in {"addressed", "wont_fix"},
                "resolution_description": "",
                "created_at": _now_iso(),
            }
            history_result = store.add_objection_history(
                agency_slug, entry, str(repo_root_path)
            )
            if history_result["status"] == "success":
                history_added += 1
            elif history_result["status"] == "failure":
                warnings += 1

            position = self._extract_position(
                agency_name=agency_name,
                description=description,
                issue_type=str(issue.get("issue_type") or ""),
                severity=severity,
            )
            if position is None:
                warnings += 1
                continue
            if position == {"no_position": True}:
                continue

            position_entry = {
                "position_id": str(uuid.uuid4()),
                "agency_slug": agency_slug,
                "topic": str(position.get("topic") or "").strip()[:200] or "general",
                "position_statement": str(position.get("position_statement") or "").strip(),
                "position_type": (
                    str(position.get("position_type") or "").strip()
                    if str(position.get("position_type") or "").strip() in _VALID_POSITION_TYPES
                    else "raises_concern"
                ),
                "source_issue_ids": [issue_id],
                "source_comment_ids": [
                    iaid
                    for iaid in (issue.get("provenance") or {}).get(
                        "input_artifact_ids", []
                    )
                    if isinstance(iaid, str)
                ] or [issue_id],
                "valid_from": (issue.get("created_at") or _now_iso())[:10],
                "valid_until": None,
                "superseded_by": None,
                "confidence_basis": str(position.get("confidence_basis") or "").strip()
                or "extracted from issue_record description",
                "created_at": _now_iso(),
            }

            add_result = store.add_position(
                agency_slug, position_entry, str(repo_root_path)
            )
            if add_result["status"] == "success":
                positions_added += 1
            else:
                warnings += 1

        store.update_counts(
            agency_slug,
            comment_count_delta=len(agency_comment_issues),
            objection_count_delta=history_added,
            repo_root=str(repo_root_path),
        )

        try:
            store.write_agency_projection(agency_slug, str(repo_root_path))
        except (FileNotFoundError, OSError):
            warnings += 1

        return {
            "status": "success",
            "positions_added": positions_added,
            "history_added": history_added,
            "warnings": warnings,
            "agency_slug": agency_slug,
            "reason": "",
        }

    def _extract_position(
        self,
        *,
        agency_name: str,
        description: str,
        issue_type: str,
        severity: str,
    ) -> Optional[Dict[str, Any]]:
        if self._api_caller is None:
            return None
        prompt = POSITION_EXTRACTION_PROMPT.format(
            agency_name=agency_name,
            issue_description=description,
            issue_type=issue_type,
            severity=severity,
        )
        try:
            response = self._api_caller(prompt)
        except Exception:  # noqa: BLE001
            return None
        if not response:
            return None
        try:
            parsed = json.loads(response)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        if parsed.get("no_position") is True:
            return {"no_position": True}
        statement = str(parsed.get("position_statement") or "")
        if len(statement.strip()) < 20:
            return None
        topic = str(parsed.get("topic") or "")
        if len(topic.strip()) < 3:
            return None
        return parsed

    def _build_default_api_caller(self) -> Callable[[str], str]:
        import anthropic

        client = anthropic.Anthropic()

        def _call(prompt: str) -> str:
            message = client.messages.create(
                model=EXTRACTION_MODEL,
                max_tokens=MAX_TOKENS,
                temperature=EXTRACTION_TEMPERATURE,
                messages=[{"role": "user", "content": prompt}],
            )
            parts: List[str] = []
            for block in message.content:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    parts.append(text)
            return "\n".join(parts)

        return _call


def _stable_id(*parts: str) -> str:
    """Deterministic uuid5 from the given parts.

    Used for objection_history entry_id so re-running ingestion does not
    duplicate entries (CHECK-RT2-005).
    """
    namespace = uuid.UUID("11111111-2222-3333-4444-555555555555")
    return str(uuid.uuid5(namespace, "|".join(parts)))
