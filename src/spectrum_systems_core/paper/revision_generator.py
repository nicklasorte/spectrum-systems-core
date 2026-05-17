"""RevisionGenerator: open issues -> revision_instruction artifacts.

Model: claude-haiku-4-5-20251001 at temperature=0. Each instruction
includes target_section, instruction_text, expected_outcome (FINDING-D-006).
Orphan claim_id references are blocked at generation time (FINDING-D-004).
Status is always "pending" — humans must approve before application.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jsonschema

from ..extraction._paths import find_processed_dir
from ._paths import paper_schema_path

_COMPONENT_NAME = "revision_generator"
_COMPONENT_VERSION = "1.0.0"
EXTRACTION_MODEL = "claude-haiku-4-5-20251001"
EXTRACTION_TEMPERATURE = 0
MAX_TOKENS = 500

REVISION_INSTRUCTION_PROMPT = """You are a technical editor generating
revision instructions for a working paper.

Issue:
Type: {issue_type}
Description: {description}
Severity: {severity}
Related claim: {claim_text}

Generate ONE specific revision instruction.
Return ONLY valid JSON. No preamble. No markdown.

{{
  "target_section": "which section of the paper this applies to",
  "instruction_text": "specific action to take, minimum 20 chars",
  "expected_outcome": "what the revision should achieve, minimum 10 chars",
  "instruction_type": "add_evidence|revise_claim|remove_claim|add_caveat|restructure_section"
}}
"""

_VALID_INSTRUCTION_TYPES = {
    "add_evidence",
    "revise_claim",
    "remove_claim",
    "add_caveat",
    "restructure_section",
}


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _execution_fingerprint(issue_id: str, instruction_text: str) -> str:
    seed = (
        f"{issue_id}|{instruction_text}|{_COMPONENT_NAME}:{_COMPONENT_VERSION}"
    )
    return "sha256:" + _sha256_hex(seed.encode("utf-8"))


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


class RevisionGenerator:
    """Generate revision_instruction artifacts from open issues."""

    def __init__(self, api_caller: Callable[[str], str] | None = None):
        self._api_caller = api_caller

    def generate_for_issue(
        self,
        issue: dict[str, Any],
        claims: list[dict[str, Any]],
        source_id: str,
        repo_root: str,
    ) -> dict[str, Any]:
        related_claim = None
        if issue.get("claim_id"):
            for c in claims:
                if c.get("claim_id") == issue["claim_id"]:
                    related_claim = c
                    break

        if self._api_caller is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                return {
                    "status": "failure",
                    "instruction": None,
                    "reason": "api_key_missing",
                }
            try:
                self._api_caller = self._build_default_api_caller()
            except ImportError as exc:
                return {
                    "status": "failure",
                    "instruction": None,
                    "reason": f"anthropic_sdk_missing: {exc}",
                }

        prompt = REVISION_INSTRUCTION_PROMPT.format(
            issue_type=issue.get("issue_type", ""),
            description=issue.get("description", ""),
            severity=issue.get("severity", ""),
            claim_text=(
                related_claim.get("claim_text", "")
                if related_claim is not None
                else "none"
            ),
        )
        try:
            response_text = self._api_caller(prompt)
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "failure",
                "instruction": None,
                "reason": f"api_error: {type(exc).__name__}: {exc}",
            }

        try:
            parsed = json.loads(response_text)
        except (TypeError, json.JSONDecodeError) as exc:
            return {
                "status": "failure",
                "instruction": None,
                "reason": f"json_parse_error: {exc}",
            }
        if not isinstance(parsed, dict):
            return {
                "status": "failure",
                "instruction": None,
                "reason": "json_parse_error: not_object",
            }

        instruction_type = parsed.get("instruction_type")
        if instruction_type not in _VALID_INSTRUCTION_TYPES:
            return {
                "status": "failure",
                "instruction": None,
                "reason": f"invalid_instruction_type: {instruction_type}",
            }

        instruction_text = str(parsed.get("instruction_text") or "")
        instruction_id = str(uuid.uuid4())

        instruction = {
            "instruction_id": instruction_id,
            "issue_id": issue["issue_id"],
            "claim_id": issue.get("claim_id"),
            "target_section": str(parsed.get("target_section") or ""),
            "instruction_text": instruction_text,
            "expected_outcome": str(parsed.get("expected_outcome") or ""),
            "instruction_type": instruction_type,
            "priority": (
                "critical" if issue.get("severity") == "critical" else "high"
            ),
            "extraction_model": EXTRACTION_MODEL,
            "extraction_temperature": EXTRACTION_TEMPERATURE,
            "status": "pending",
            "created_at": _now_iso(),
            "provenance": {
                "produced_by": {
                    "component": _COMPONENT_NAME,
                    "version": _COMPONENT_VERSION,
                },
                "input_artifact_ids": [issue["issue_id"]],
                "execution_fingerprint_hash": _execution_fingerprint(
                    issue["issue_id"], instruction_text
                ),
            },
        }

        # FINDING-D-004: validate claim_id reference at generation time.
        if instruction["claim_id"] is not None:
            valid_ids = {c.get("claim_id") for c in claims}
            if instruction["claim_id"] not in valid_ids:
                return {
                    "status": "blocked",
                    "instruction": None,
                    "reason": f"orphan_claim_id: {instruction['claim_id']} "
                    "not in registry",
                }

        try:
            schema = json.loads(
                paper_schema_path("revision_instruction").read_text(
                    encoding="utf-8"
                )
            )
            jsonschema.Draft202012Validator(schema).validate(instruction)
        except (FileNotFoundError, OSError) as exc:
            return {
                "status": "failure",
                "instruction": None,
                "reason": f"schema_unreadable: {exc}",
            }
        except jsonschema.ValidationError as exc:
            return {
                "status": "failure",
                "instruction": None,
                "reason": f"schema_violation: {exc.message}",
            }

        return {"status": "success", "instruction": instruction, "reason": ""}

    def generate_for_source(
        self, working_paper_source_id: str, repo_root: str
    ) -> dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        processed_dir, _ = find_processed_dir(
            repo_root_path, working_paper_source_id
        )
        if processed_dir is None:
            return {
                "status": "failure",
                "instruction_count": 0,
                "blocked_count": 0,
                "reason": "source_not_found",
            }
        paper_dir = processed_dir / "paper"
        paper_dir.mkdir(parents=True, exist_ok=True)
        issues = _read_jsonl(paper_dir / "issues.jsonl")
        claims = _read_jsonl(paper_dir / "claims.jsonl")

        instructions: list[dict[str, Any]] = []
        blocked = 0
        for issue in issues:
            if issue.get("status") != "open":
                continue
            res = self.generate_for_issue(
                issue, claims, working_paper_source_id, repo_root
            )
            if res.get("status") == "success" and res.get("instruction") is not None:
                instructions.append(res["instruction"])
            elif res.get("status") == "blocked":
                blocked += 1

        out_path = paper_dir / "revision_instructions.jsonl"
        try:
            with out_path.open("w", encoding="utf-8") as fh:
                for inst in instructions:
                    fh.write(
                        json.dumps(inst, sort_keys=True, separators=(",", ":"))
                        + "\n"
                    )
        except OSError as exc:
            return {
                "status": "failure",
                "instruction_count": 0,
                "blocked_count": blocked,
                "reason": f"write_error: {exc}",
            }

        # Write the projection.
        from ..ingestion.obsidian_projection import ObsidianProjection
        ObsidianProjection().write_paper_revisions_projection(
            working_paper_source_id, instructions, [], str(repo_root_path)
        )

        return {
            "status": "success",
            "instruction_count": len(instructions),
            "blocked_count": blocked,
            "reason": "",
        }

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
            parts: list[str] = []
            for block in message.content:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    parts.append(text)
            return "\n".join(parts)

        return _call
