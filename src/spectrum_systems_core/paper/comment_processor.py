"""CommentProcessor: agency comment text -> issue_record or warning artifact.

FINDING-D-003: comments without identifiable structure produce an
unstructured_comment_warning rather than silently materializing as
issue records. The admission check is a deterministic keyword scan,
not an LLM judgement.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import jsonschema

from ..extraction._paths import find_processed_dir
from ._paths import paper_schema_path

_COMPONENT_NAME = "comment_processor"
_COMPONENT_VERSION = "1.0.0"
EXTRACTION_MODEL = "claude-haiku-4-5-20251001"
EXTRACTION_TEMPERATURE = 0
MAX_TOKENS = 500

STRUCTURED_COMMENT_INDICATORS = [
    "recommends", "objects to", "disagrees", "requests", "proposes",
    "concerns", "notes that", "argues", "contends", "states that",
    "the agency", "the commission", "the bureau", "comment",
    "in response to", "we find", "we conclude",
]

COMMENT_EXTRACTION_PROMPT = """Extract the agency position from this comment.
Return JSON only: {{"agency_name": "string", "position_statement": "string >= 20 chars",
"references_claim_text": "string or null",
"severity": "critical|major|minor"}}

Comment:
{comment_text}
"""


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _execution_fingerprint(
    component_seed: str, comment_hash: str
) -> str:
    seed = f"{component_seed}|{comment_hash}|{_COMPONENT_NAME}:{_COMPONENT_VERSION}"
    return "sha256:" + _sha256_hex(seed.encode("utf-8"))


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


class CommentProcessor:
    """Process agency comments into issue_record artifacts (or warnings)."""

    def __init__(self, api_caller: Optional[Callable[[str], str]] = None):
        self._api_caller = api_caller

    def process(
        self,
        comment_text: str,
        source_id: str,
        comment_source_id: str,
        repo_root: str,
    ) -> Dict[str, Any]:
        text = comment_text or ""
        normalized = text.lower()
        raw_hash = "sha256:" + _sha256_hex(text.encode("utf-8"))

        # Step 1: admission check (FINDING-D-003).
        if not any(ind in normalized for ind in STRUCTURED_COMMENT_INDICATORS):
            return self._emit_unstructured_warning(
                comment_text=text,
                comment_source_id=comment_source_id,
                paper_source_id=source_id,
                raw_hash=raw_hash,
                repo_root=repo_root,
                reason="no_structured_comment_indicator",
            )

        # Step 2: structured -> Haiku extraction.
        if self._api_caller is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                return {
                    "status": "failure",
                    "artifact": None,
                    "reason": "api_key_missing",
                }
            try:
                self._api_caller = self._build_default_api_caller()
            except ImportError as exc:
                return {
                    "status": "failure",
                    "artifact": None,
                    "reason": f"anthropic_sdk_missing: {exc}",
                }

        try:
            response_text = self._api_caller(
                COMMENT_EXTRACTION_PROMPT.format(comment_text=text)
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "failure",
                "artifact": None,
                "reason": f"api_error: {type(exc).__name__}: {exc}",
            }

        try:
            parsed = json.loads(response_text)
        except (TypeError, json.JSONDecodeError) as exc:
            return {
                "status": "failure",
                "artifact": None,
                "reason": f"json_parse_error: {exc}",
            }
        if not isinstance(parsed, dict):
            return {
                "status": "failure",
                "artifact": None,
                "reason": "json_parse_error: not_object",
            }

        position = str(parsed.get("position_statement") or "").strip()
        severity = str(parsed.get("severity") or "minor").strip()
        if severity not in {"critical", "major", "minor"}:
            severity = "minor"

        issue_record = {
            "issue_id": str(uuid.uuid4()),
            "issue_type": "agency_comment",
            "source_id": source_id,
            "source_unit_id": None,
            "claim_id": None,
            "assumption_id": None,
            "description": position,
            "severity": severity,
            "similar_issue_ids": [],
            "status": "open",
            "created_at": _now_iso(),
            "provenance": {
                "produced_by": {
                    "component": _COMPONENT_NAME,
                    "version": _COMPONENT_VERSION,
                },
                "input_artifact_ids": [comment_source_id],
                "execution_fingerprint_hash": _execution_fingerprint(
                    comment_source_id, raw_hash
                ),
            },
        }

        try:
            schema = json.loads(
                paper_schema_path("issue_record").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError) as exc:
            return {
                "status": "failure",
                "artifact": None,
                "reason": f"schema_unreadable: {exc}",
            }
        try:
            jsonschema.Draft202012Validator(schema).validate(issue_record)
        except jsonschema.ValidationError as exc:
            return {
                "status": "failure",
                "artifact": None,
                "reason": f"schema_violation: {exc.message}",
            }

        return {"status": "issue_created", "artifact": issue_record, "reason": ""}

    def process_source(
        self,
        comment_source_id: str,
        working_paper_source_id: str,
        repo_root: str,
    ) -> Dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        comment_processed, _ = find_processed_dir(repo_root_path, comment_source_id)
        if comment_processed is None:
            return {
                "status": "failure",
                "issues_created": 0,
                "warnings": 0,
                "reason": "comment_source_not_found",
            }
        text_units = _read_jsonl(comment_processed / "text_units.jsonl")
        if not text_units:
            return {
                "status": "failure",
                "issues_created": 0,
                "warnings": 0,
                "reason": "comment_text_units_empty",
            }

        # Late import to avoid circular deps.
        from .issue_registry import IssueRegistry

        registry = IssueRegistry()
        issues_created = 0
        warnings = 0

        for unit in text_units:
            comment_text = unit.get("text", "") or ""
            if not comment_text:
                continue
            result = self.process(
                comment_text=comment_text,
                source_id=working_paper_source_id,
                comment_source_id=comment_source_id,
                repo_root=repo_root,
            )
            status = result.get("status")
            if status == "issue_created":
                registry.add_issue(
                    result["artifact"],
                    repo_root=repo_root,
                    working_paper_source_id=working_paper_source_id,
                )
                issues_created += 1
            elif status == "warning_emitted":
                warnings += 1

        return {
            "status": "success",
            "issues_created": issues_created,
            "warnings": warnings,
            "reason": "",
        }

    def _emit_unstructured_warning(
        self,
        *,
        comment_text: str,
        comment_source_id: str,
        paper_source_id: str,
        raw_hash: str,
        repo_root: str,
        reason: str,
    ) -> Dict[str, Any]:
        warning = {
            "warning_id": str(uuid.uuid4()),
            "source_id": paper_source_id,
            "raw_comment_text": comment_text,
            "raw_comment_hash": raw_hash,
            "reason": reason,
            "requires_human_tagging": True,
            "created_at": _now_iso(),
        }
        try:
            schema = json.loads(
                paper_schema_path("unstructured_comment_warning").read_text(
                    encoding="utf-8"
                )
            )
            jsonschema.Draft202012Validator(schema).validate(warning)
        except (FileNotFoundError, OSError, jsonschema.ValidationError) as exc:
            return {
                "status": "failure",
                "artifact": None,
                "reason": f"warning_invalid: {exc}",
            }

        repo_root_path = Path(repo_root).resolve()
        processed_dir, _ = find_processed_dir(repo_root_path, paper_source_id)
        if processed_dir is not None:
            paper_dir = processed_dir / "paper"
            paper_dir.mkdir(parents=True, exist_ok=True)
            warnings_path = paper_dir / "unstructured_warnings.jsonl"
            try:
                with warnings_path.open("a", encoding="utf-8") as fh:
                    fh.write(
                        json.dumps(warning, sort_keys=True, separators=(",", ":"))
                        + "\n"
                    )
            except OSError as exc:
                return {
                    "status": "failure",
                    "artifact": None,
                    "reason": f"write_error: {exc}",
                }
        return {"status": "warning_emitted", "artifact": warning, "reason": ""}

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
