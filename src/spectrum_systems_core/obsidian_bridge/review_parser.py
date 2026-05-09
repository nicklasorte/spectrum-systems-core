"""Module 2b: Obsidian review form parser.

Reads a completed review form note, validates structure and policy,
and emits a deterministic ``review_artifact`` envelope.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

import jsonschema
import yaml

from . import _frontmatter
from ._paths import schema_digest, schema_path


_SCHEMA_NAME = "review_artifact"
_COMPONENT_VERSION = "1.0.0"
_VALID_DECISIONS = {"approve", "revise", "block"}
_VALID_SEVERITIES = {"S0", "S1", "S2", "S3", "S4"}
_FINDING_HEADER_RE = re.compile(r"^###\s+Finding\b", re.MULTILINE)
_REVIEWER_NOTES_HEADER_RE = re.compile(
    r"^##\s+Reviewer Notes\s*$", re.MULTILINE
)
_NEXT_SECTION_RE = re.compile(r"^(?:#{1,3})\s", re.MULTILINE)


def _now_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _failure(reason: str) -> Dict[str, Any]:
    return {"status": "failure", "artifact": None, "reason": reason}


def _strip_html_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()


def _extract_field(block: str, label: str) -> str:
    """Pull the value after `**{label}**:` on its line, ignoring HTML comments."""
    pattern = re.compile(
        rf"\*\*{re.escape(label)}\*\*\s*:\s*(.*?)\s*$",
        re.MULTILINE,
    )
    match = pattern.search(block)
    if not match:
        return ""
    return _strip_html_comments(match.group(1))


def _split_finding_blocks(body: str) -> List[str]:
    """Return text blocks for each `### Finding` section."""
    headers = list(_FINDING_HEADER_RE.finditer(body))
    blocks: List[str] = []
    for idx, header in enumerate(headers):
        start = header.start()
        end = headers[idx + 1].start() if idx + 1 < len(headers) else len(body)
        blocks.append(body[start:end])
    return blocks


def _extract_reviewer_notes(body: str) -> str:
    match = _REVIEWER_NOTES_HEADER_RE.search(body)
    if not match:
        return ""
    start = match.end()
    next_section = _NEXT_SECTION_RE.search(body, pos=start)
    end = next_section.start() if next_section else len(body)
    section = body[start:end]
    cleaned_lines: List[str] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(">"):
            continue
        if stripped.startswith("---"):
            continue
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            continue
        cleaned_lines.append(line.rstrip())
    return "\n".join(cleaned_lines).strip()


def _parse_finding(block: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    severity = _extract_field(block, "severity")
    section = _extract_field(block, "section")
    description = _extract_field(block, "description")
    required_action = _extract_field(block, "required_action")

    if severity not in _VALID_SEVERITIES:
        return None, "invalid_severity"
    if severity in {"S2", "S3", "S4"}:
        if not section:
            return None, "missing_section"
        if not description:
            return None, "missing_description"
        if not required_action:
            return None, "missing_required_action"
    return (
        {
            "finding_id": str(uuid.uuid4()),
            "severity": severity,
            "section": section,
            "description": description,
            "required_action": required_action,
        },
        None,
    )


def _load_schema() -> Dict[str, Any]:
    return json.loads(schema_path(_SCHEMA_NAME).read_text(encoding="utf-8"))


class ObsidianReviewParser:

    def parse(self, review_note_path: str, vault_root: str) -> Dict[str, Any]:
        # Step 1: read + decode
        try:
            with open(review_note_path, "rb") as fh:
                raw_content = fh.read().decode("utf-8")
        except (FileNotFoundError, OSError):
            return _failure("unreadable_file")
        except UnicodeDecodeError:
            return _failure("encoding_error")

        # Step 2: parse frontmatter
        try:
            frontmatter, body = _frontmatter.split(raw_content)
        except (ValueError, yaml.YAMLError):
            return _failure("schema_violation")

        # Step 3: validate required frontmatter fields
        reviewer_id = frontmatter.get("reviewer_id")
        if not isinstance(reviewer_id, str) or not reviewer_id.strip():
            return _failure("blank_reviewer_id")

        decision = frontmatter.get("decision")
        if decision not in _VALID_DECISIONS:
            return _failure("invalid_decision")

        reviewed_at = frontmatter.get("reviewed_at")
        if not isinstance(reviewed_at, str) or not reviewed_at.strip():
            return _failure("invalid_reviewed_at")
        try:
            datetime.datetime.fromisoformat(
                reviewed_at.replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            return _failure("invalid_reviewed_at")

        review_for_artifact_id = frontmatter.get("review_for_artifact_id")
        if not isinstance(review_for_artifact_id, str):
            return _failure("invalid_review_for_artifact_id")
        try:
            uuid.UUID(review_for_artifact_id)
        except (ValueError, AttributeError, TypeError):
            return _failure("invalid_review_for_artifact_id")

        review_for_artifact_type = frontmatter.get("review_for_artifact_type")
        if (
            not isinstance(review_for_artifact_type, str)
            or not review_for_artifact_type.strip()
        ):
            return _failure("invalid_review_for_artifact_type")

        pipeline_run_id = frontmatter.get("pipeline_run_id")
        if not isinstance(pipeline_run_id, str) or not pipeline_run_id.strip():
            return _failure("invalid_pipeline_run_id")

        # Step 4: parse findings
        findings: List[Dict[str, Any]] = []
        for block in _split_finding_blocks(body):
            finding, err = _parse_finding(block)
            if err:
                return _failure(err)
            findings.append(finding)

        # Step 5: cross-validate
        has_critical = any(f["severity"] in {"S3", "S4"} for f in findings)
        if decision == "block" and not has_critical:
            return _failure("block_requires_critical_finding")
        if decision == "revise" and not findings:
            return _failure("revise_requires_findings")
        if has_critical and decision != "block":
            return _failure("critical_finding_requires_block")

        # Step 6: source content hash
        source_note_content_hash = "sha256:" + hashlib.sha256(
            raw_content.encode("utf-8")
        ).hexdigest()

        # Step 7: assemble review artifact
        try:
            digest = schema_digest(_SCHEMA_NAME)
        except (FileNotFoundError, OSError):
            return _failure("schema_violation")

        fingerprint_src = (
            review_note_path + source_note_content_hash + _COMPONENT_VERSION
        )
        execution_fingerprint_hash = "sha256:" + hashlib.sha256(
            fingerprint_src.encode("utf-8")
        ).hexdigest()

        reviewer_notes = _extract_reviewer_notes(body)

        artifact: Dict[str, Any] = {
            "artifact_kind": "review_artifact",
            "artifact_id": str(uuid.uuid4()),
            "created_at": _now_iso(),
            "schema_ref": {
                "name": _SCHEMA_NAME,
                "version": "1.0.0",
                "digest": digest,
            },
            "trace": {
                "trace_id": uuid.uuid4().hex,
                "span_id": uuid.uuid4().hex[:16],
                "parent_span_id": None,
            },
            "provenance": {
                "produced_by": {
                    "component": "obsidian_review_gateway",
                    "version": _COMPONENT_VERSION,
                },
                "input_artifact_ids": [review_for_artifact_id],
                "execution_fingerprint_hash": execution_fingerprint_hash,
            },
            "payload": {
                "review_for_artifact_id": review_for_artifact_id,
                "review_for_artifact_type": review_for_artifact_type,
                "pipeline_run_id": pipeline_run_id,
                "reviewer_id": reviewer_id,
                "decision": decision,
                "reviewed_at": reviewed_at,
                "findings": findings,
                "reviewer_notes": reviewer_notes,
                "source_note_path": os.path.relpath(
                    review_note_path, vault_root
                ),
                "source_note_content_hash": source_note_content_hash,
            },
        }

        # Step 8: schema validation
        try:
            schema = _load_schema()
            jsonschema.Draft202012Validator(schema).validate(artifact)
        except jsonschema.ValidationError:
            return _failure("schema_violation")
        except (FileNotFoundError, OSError):
            return _failure("schema_violation")

        # Step 9: success
        return {"status": "success", "artifact": artifact}
