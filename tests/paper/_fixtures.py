"""Shared fixtures for Phase D tests."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List


def id_from_prompt(prompt: str, label: str) -> str:
    """Parse a UUID following '<label>:' in the prompt (Phase M.1 mocks)."""
    match = re.search(rf"{re.escape(label)}:\s*([0-9a-f-]{{36}})", prompt)
    if match is None:
        raise AssertionError(f"{label} not found in prompt")
    return match.group(1)


def write_text_units(
    repo_root: Path,
    *,
    family: str,
    source_id: str,
    texts: List[str],
) -> List[Dict[str, Any]]:
    """Write processed/<family>/<source_id>/text_units.jsonl. Returns the units."""
    target = repo_root / "processed" / family / source_id
    target.mkdir(parents=True, exist_ok=True)
    units: List[Dict[str, Any]] = []
    char_offset = 0
    for ordinal, text in enumerate(texts):
        units.append(
            {
                "unit_id": str(uuid.uuid4()),
                "source_id": source_id,
                "unit_type": "paragraph",
                "ordinal": ordinal,
                "text": text,
                "text_hash": "sha256:" + ("a" * 64),
                "locator": {
                    "line_start": ordinal,
                    "line_end": ordinal,
                    "char_start": char_offset,
                    "char_end": char_offset + len(text),
                },
            }
        )
        char_offset += len(text) + 2
    path = target / "text_units.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for unit in units:
            fh.write(json.dumps(unit, sort_keys=True) + "\n")
    return units


def write_source_record(
    repo_root: Path,
    *,
    family: str,
    source_id: str,
    raw_hash: str | None = None,
) -> str:
    """Write a minimal source_record.json with a raw_hash payload."""
    if raw_hash is None:
        raw_hash = "sha256:" + hashlib.sha256(source_id.encode()).hexdigest()
    target = repo_root / "processed" / family / source_id
    target.mkdir(parents=True, exist_ok=True)
    record = {
        "artifact_kind": "source_record",
        "artifact_id": str(uuid.uuid4()),
        "payload": {
            "source_id": source_id,
            "source_family": family,
            "raw_hash": raw_hash,
            "text_unit_count": 0,
        },
    }
    (target / "source_record.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return raw_hash


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not path.is_file():
        return out
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def make_claim(
    *,
    source_id: str,
    source_unit_id: str,
    claim_text: str,
    source_excerpt: str,
    materiality: str = "high",
    claim_type: str = "factual",
) -> Dict[str, Any]:
    fp = "sha256:" + hashlib.sha256(
        (source_unit_id + claim_text).encode()
    ).hexdigest()
    return {
        "schema_version": "1.1.0",
        "claim_id": str(uuid.uuid4()),
        "source_id": source_id,
        "source_unit_id": source_unit_id,
        "source_turn_ids": [source_unit_id],
        "source_turn_validation": "verified",
        "source_excerpt": source_excerpt,
        "claim_text": claim_text,
        "claim_type": claim_type,
        "materiality": materiality,
        "supported_by_evidence_ids": [],
        "contradicted_by_claim_ids": [],
        "extraction_model": "claude-haiku-4-5-20251001",
        "extraction_temperature": 0,
        "status": "candidate",
        "created_at": "2026-05-09T00:00:00+00:00",
        "provenance": {
            "produced_by": {"component": "claim_extractor", "version": "1.1.0"},
            "input_artifact_ids": [source_unit_id],
            "execution_fingerprint_hash": fp,
        },
    }


def make_evidence(
    *,
    claim_id: str,
    source_id: str,
    source_unit_id: str,
    source_excerpt: str,
    source_record_hash: str,
    evidence_type: str = "direct_support",
) -> Dict[str, Any]:
    fp = "sha256:" + hashlib.sha256(
        (claim_id + source_unit_id).encode()
    ).hexdigest()
    return {
        "evidence_id": str(uuid.uuid4()),
        "claim_id": claim_id,
        "source_id": source_id,
        "source_unit_id": source_unit_id,
        "source_excerpt": source_excerpt,
        "evidence_type": evidence_type,
        "source_record_hash": source_record_hash,
        "grounded": True,
        "grounded_unit_ids": [source_unit_id],
        "created_at": "2026-05-09T00:00:00+00:00",
        "provenance": {
            "produced_by": {"component": "evidence_builder", "version": "1.0.0"},
            "input_artifact_ids": [claim_id, source_unit_id],
            "execution_fingerprint_hash": fp,
        },
    }


def make_issue(
    *,
    source_id: str,
    description: str,
    issue_type: str = "unsupported_claim",
    source_unit_id: str | None = None,
    claim_id: str | None = None,
    severity: str = "major",
    status: str = "open",
) -> Dict[str, Any]:
    fp = "sha256:" + hashlib.sha256(description.encode()).hexdigest()
    return {
        "issue_id": str(uuid.uuid4()),
        "issue_type": issue_type,
        "source_id": source_id,
        "source_unit_id": source_unit_id,
        "claim_id": claim_id,
        "assumption_id": None,
        "description": description,
        "severity": severity,
        "similar_issue_ids": [],
        "status": status,
        "created_at": "2026-05-09T00:00:00+00:00",
        "provenance": {
            "produced_by": {"component": "comment_processor", "version": "1.0.0"},
            "input_artifact_ids": [source_id],
            "execution_fingerprint_hash": fp,
        },
    }


def make_revision_instruction(
    *,
    issue_id: str,
    target_section: str = "Section II",
    instruction_text: str = "Add a citation supporting this claim.",
    expected_outcome: str = "Claim supported.",
    instruction_type: str = "add_evidence",
    claim_id: str | None = None,
    priority: str = "high",
    status: str = "pending",
) -> Dict[str, Any]:
    fp = "sha256:" + hashlib.sha256(
        (issue_id + instruction_text).encode()
    ).hexdigest()
    return {
        "instruction_id": str(uuid.uuid4()),
        "issue_id": issue_id,
        "claim_id": claim_id,
        "target_section": target_section,
        "instruction_text": instruction_text,
        "expected_outcome": expected_outcome,
        "instruction_type": instruction_type,
        "priority": priority,
        "extraction_model": "claude-haiku-4-5-20251001",
        "extraction_temperature": 0,
        "status": status,
        "created_at": "2026-05-09T00:00:00+00:00",
        "provenance": {
            "produced_by": {"component": "revision_generator", "version": "1.0.0"},
            "input_artifact_ids": [issue_id],
            "execution_fingerprint_hash": fp,
        },
    }
