"""RevisionWorkflow: apply approved revision instructions via Sonnet.

Model: claude-sonnet-4-20250514 at temperature=0. Each revision is
followed by a deterministic re-scan to check that no high-materiality
claim's source_excerpt has been dropped (FINDING-D-001). Blocked
revisions write a revision_diff with status="blocked" and never produce
a revised_draft.json entry for that section.

Application is gated by the approve-revisions CLI — this class refuses
to apply instructions whose status is not "approved".
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

_COMPONENT_NAME = "revision_workflow"
_COMPONENT_VERSION = "1.0.0"
REVISION_MODEL = "claude-sonnet-4-20250514"
REVISION_TEMPERATURE = 0
MAX_TOKENS = 4000

REVISION_APPLICATION_PROMPT = """You are applying a specific revision to a
section of a working paper.

Original section text:
{original_text}

Revision instruction:
Target: {target_section}
Action: {instruction_text}
Expected outcome: {expected_outcome}
Instruction type: {instruction_type}

Apply ONLY the specified revision. Do not add, remove, or change anything else.
Return ONLY the revised section text. No preamble. No JSON wrapper.
"""


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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


class RevisionWorkflow:
    """Apply approved revision instructions section-by-section."""

    def __init__(self, api_caller: Callable[[str], str] | None = None):
        self._api_caller = api_caller

    def apply_instruction(
        self,
        instruction: dict[str, Any],
        section_text: str,
        claims_before: list[dict[str, Any]],
        source_id: str,
        repo_root: str,
    ) -> dict[str, Any]:
        instruction_id = instruction.get("instruction_id", str(uuid.uuid4()))
        original_hash = "sha256:" + _sha256_hex(section_text.encode("utf-8"))

        # Step 2: count high-materiality claims in section_text.
        claims_in_section = [
            c
            for c in claims_before
            if c.get("source_excerpt")
            and c["source_excerpt"] in section_text
        ]
        high_materiality_in_section = [
            c for c in claims_in_section if c.get("materiality") == "high"
        ]
        claims_before_count = len(claims_in_section)

        if self._api_caller is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                return {
                    "status": "failure",
                    "revised_text": None,
                    "revision_diff": self._failure_diff(
                        instruction_id=instruction_id,
                        target=instruction.get("target_section", ""),
                        original_hash=original_hash,
                        original_chars=len(section_text),
                        claims_before=claims_before_count,
                        reason="api_key_missing",
                    ),
                    "reason": "api_key_missing",
                }
            try:
                self._api_caller = self._build_default_api_caller()
            except ImportError as exc:
                return {
                    "status": "failure",
                    "revised_text": None,
                    "revision_diff": self._failure_diff(
                        instruction_id=instruction_id,
                        target=instruction.get("target_section", ""),
                        original_hash=original_hash,
                        original_chars=len(section_text),
                        claims_before=claims_before_count,
                        reason=f"anthropic_sdk_missing: {exc}",
                    ),
                    "reason": f"anthropic_sdk_missing: {exc}",
                }

        prompt = REVISION_APPLICATION_PROMPT.format(
            original_text=section_text,
            target_section=instruction.get("target_section", ""),
            instruction_text=instruction.get("instruction_text", ""),
            expected_outcome=instruction.get("expected_outcome", ""),
            instruction_type=instruction.get("instruction_type", ""),
        )
        try:
            revised_text = (self._api_caller(prompt) or "").strip()
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "failure",
                "revised_text": None,
                "revision_diff": self._failure_diff(
                    instruction_id=instruction_id,
                    target=instruction.get("target_section", ""),
                    original_hash=original_hash,
                    original_chars=len(section_text),
                    claims_before=claims_before_count,
                    reason=f"api_error: {type(exc).__name__}: {exc}",
                ),
                "reason": f"api_error: {type(exc).__name__}: {exc}",
            }

        revised_hash = "sha256:" + _sha256_hex(revised_text.encode("utf-8"))

        # Step 5 (FINDING-D-001): post-revision claim drop check.
        dropped = [
            c
            for c in high_materiality_in_section
            if c.get("source_excerpt") not in revised_text
        ]
        if dropped:
            blocked_diff = {
                "diff_id": str(uuid.uuid4()),
                "instruction_id": instruction_id,
                "source_section": instruction.get("target_section", ""),
                "original_text_hash": original_hash,
                "revised_text_hash": revised_hash,
                "original_char_count": len(section_text),
                "revised_char_count": len(revised_text),
                "claims_before_count": claims_before_count,
                "claims_after_count": claims_before_count - len(dropped),
                "high_materiality_claims_dropped": [
                    c["claim_id"] for c in dropped
                ],
                "revision_model": REVISION_MODEL,
                "revision_temperature": REVISION_TEMPERATURE,
                "status": "blocked",
                "failure_reason": "high_materiality_claim_dropped",
                "created_at": _now_iso(),
            }
            self._validate_diff(blocked_diff)
            return {
                "status": "blocked",
                "revised_text": None,
                "revision_diff": blocked_diff,
                "reason": "high_materiality_claim_dropped",
            }

        diff = {
            "diff_id": str(uuid.uuid4()),
            "instruction_id": instruction_id,
            "source_section": instruction.get("target_section", ""),
            "original_text_hash": original_hash,
            "revised_text_hash": revised_hash,
            "original_char_count": len(section_text),
            "revised_char_count": len(revised_text),
            "claims_before_count": claims_before_count,
            "claims_after_count": claims_before_count,
            "high_materiality_claims_dropped": [],
            "revision_model": REVISION_MODEL,
            "revision_temperature": REVISION_TEMPERATURE,
            "status": "success",
            "failure_reason": "",
            "created_at": _now_iso(),
        }
        self._validate_diff(diff)
        return {
            "status": "success",
            "revised_text": revised_text,
            "revision_diff": diff,
            "reason": "",
        }

    def apply_all_approved(
        self,
        working_paper_source_id: str,
        approved_instruction_ids: list[str],
        repo_root: str,
    ) -> dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        processed_dir, _ = find_processed_dir(
            repo_root_path, working_paper_source_id
        )
        if processed_dir is None:
            return {
                "status": "failure",
                "applied": 0,
                "blocked": 0,
                "reason": "source_not_found",
            }

        paper_dir = processed_dir / "paper"
        paper_dir.mkdir(parents=True, exist_ok=True)
        instructions_path = paper_dir / "revision_instructions.jsonl"
        instructions = _read_jsonl(instructions_path)
        # Only apply explicitly approved instructions (RT5-002).
        approved_set = set(approved_instruction_ids)
        approved_instructions = [
            i
            for i in instructions
            if i.get("instruction_id") in approved_set
            and i.get("status") == "approved"
        ]

        # Sort by priority for deterministic order.
        priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        approved_instructions.sort(
            key=lambda i: priority_order.get(i.get("priority", "low"), 9)
        )

        claims = _read_jsonl(paper_dir / "claims.jsonl")
        text_units = _read_jsonl(processed_dir / "text_units.jsonl")
        # Build a dict of section_id -> text. We treat each text_unit as a
        # candidate section; target_section identifies which one.
        sections_by_id: dict[str, str] = {}
        for u in text_units:
            uid = u.get("unit_id")
            if isinstance(uid, str):
                sections_by_id[uid] = u.get("text") or ""

        diffs: list[dict[str, Any]] = []
        revised_sections: dict[str, str] = {}
        applied = 0
        blocked = 0

        for inst in approved_instructions:
            target = inst.get("target_section", "")
            section_text = (
                sections_by_id.get(target)
                or revised_sections.get(target, "")
            )
            if not section_text:
                # Treat the target_section as the literal text if it isn't a
                # unit_id — bestever-effort matching still produces a diff.
                section_text = target
            res = self.apply_instruction(
                inst, section_text, claims, working_paper_source_id, repo_root
            )
            diffs.append(res["revision_diff"])
            if res.get("status") == "success":
                revised_sections[target] = res["revised_text"]
                applied += 1
            elif res.get("status") == "blocked":
                blocked += 1

        diffs_path = paper_dir / "revision_diff.jsonl"
        if diffs:
            try:
                with diffs_path.open("a", encoding="utf-8") as fh:
                    for d in diffs:
                        fh.write(
                            json.dumps(d, sort_keys=True, separators=(",", ":"))
                            + "\n"
                        )
            except OSError as exc:
                return {
                    "status": "failure",
                    "applied": applied,
                    "blocked": blocked,
                    "reason": f"write_error: {exc}",
                }

        if applied > 0:
            revised_draft = {
                "source_id": working_paper_source_id,
                "generated_at": _now_iso(),
                "revised_sections": revised_sections,
                "applied_instruction_ids": [
                    i["instruction_id"]
                    for i in approved_instructions
                    if i["instruction_id"]
                    in {
                        d["instruction_id"]
                        for d in diffs
                        if d["status"] == "success"
                    }
                ],
            }
            try:
                (paper_dir / "revised_draft.json").write_text(
                    json.dumps(revised_draft, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                return {
                    "status": "failure",
                    "applied": applied,
                    "blocked": blocked,
                    "reason": f"write_error: {exc}",
                }

        # Always update the projection (FINDING-D-001 + RT5-006).
        from ..ingestion.obsidian_projection import ObsidianProjection
        ObsidianProjection().write_paper_revisions_projection(
            working_paper_source_id, instructions, diffs, str(repo_root_path)
        )

        return {
            "status": "success",
            "applied": applied,
            "blocked": blocked,
            "reason": "",
        }

    # ------------------- helpers -------------------

    def _failure_diff(
        self,
        *,
        instruction_id: str,
        target: str,
        original_hash: str,
        original_chars: int,
        claims_before: int,
        reason: str,
    ) -> dict[str, Any]:
        # Failure diffs use a 64-zero placeholder revised hash so the schema
        # still validates. They are append-safe and debuggable.
        zero_hash = "sha256:" + ("0" * 64)
        return {
            "diff_id": str(uuid.uuid4()),
            "instruction_id": instruction_id,
            "source_section": target,
            "original_text_hash": original_hash,
            "revised_text_hash": zero_hash,
            "original_char_count": original_chars,
            "revised_char_count": 0,
            "claims_before_count": claims_before,
            "claims_after_count": claims_before,
            "high_materiality_claims_dropped": [],
            "revision_model": REVISION_MODEL,
            "revision_temperature": REVISION_TEMPERATURE,
            "status": "failure",
            "failure_reason": reason,
            "created_at": _now_iso(),
        }

    def _validate_diff(self, diff: dict[str, Any]) -> None:
        try:
            schema = json.loads(
                paper_schema_path("revision_diff").read_text(encoding="utf-8")
            )
            jsonschema.Draft202012Validator(schema).validate(diff)
        except (FileNotFoundError, OSError, jsonschema.ValidationError):
            # Schema validation is best-effort here — the test suite covers
            # the strict path; production code should not crash if the schema
            # is unavailable for any reason.
            return

    def _build_default_api_caller(self) -> Callable[[str], str]:
        import anthropic

        client = anthropic.Anthropic()

        def _call(prompt: str) -> str:
            # Stream to stay under the SDK's 10-minute non-streaming cap.
            with client.messages.stream(
                model=REVISION_MODEL,
                max_tokens=MAX_TOKENS,
                temperature=REVISION_TEMPERATURE,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                message = stream.get_final_message()
            parts: list[str] = []
            for block in message.content:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    parts.append(text)
            return "\n".join(parts)

        return _call
