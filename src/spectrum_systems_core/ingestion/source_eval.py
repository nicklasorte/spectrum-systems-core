"""SourceEval: deterministic eval cases for source_record + text_units.

Five eval cases (EVAL-SRC-001..005). All pass → allow. Any fail → block.
No LLM. Stdlib + jsonschema only.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

import jsonschema

from ._paths import schema_path

EVAL_CASES = (
    ("EVAL-SRC-001", "schema_conformance"),
    ("EVAL-SRC-002", "evidence_coverage"),
    ("EVAL-SRC-003", "replay_consistency"),
    ("EVAL-SRC-004", "evidence_coverage"),
    ("EVAL-SRC-005", "replay_consistency"),
)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _result(name: str, eval_type: str, passed: bool, reason: str) -> Dict[str, Any]:
    return {
        "eval_name": name,
        "eval_type": eval_type,
        "status": "pass" if passed else "fail",
        "reason": "" if passed else reason,
    }


def _resolve_path(payload_path: str, repo_root: Path) -> Path:
    p = Path(payload_path)
    if p.is_absolute():
        return p
    return repo_root / payload_path


class SourceEval:
    """Run five deterministic eval cases against a source_record."""

    def run(
        self,
        source_record: Dict[str, Any],
        text_units: List[Dict[str, Any]],
        repo_root: str | Path | None = None,
    ) -> Dict[str, Any]:
        repo_root_path = (
            Path(repo_root).resolve()
            if repo_root is not None
            else Path.cwd().resolve()
        )
        results: List[Dict[str, Any]] = []
        reason_codes: List[str] = []

        # EVAL-SRC-001: schema_conformance
        try:
            schema = json.loads(
                schema_path("source_record").read_text(encoding="utf-8")
            )
            jsonschema.Draft202012Validator(schema).validate(source_record)
            results.append(
                _result("EVAL-SRC-001", "schema_conformance", True, "")
            )
        except (FileNotFoundError, OSError, jsonschema.ValidationError) as exc:
            results.append(
                _result(
                    "EVAL-SRC-001",
                    "schema_conformance",
                    False,
                    f"source_record_schema_invalid: {exc}",
                )
            )
            reason_codes.append("source_record_schema_invalid")

        payload = (
            source_record.get("payload", {}) if isinstance(source_record, dict) else {}
        )

        # EVAL-SRC-002: source_not_empty
        text_unit_count = payload.get("text_unit_count", 0)
        if isinstance(text_unit_count, int) and text_unit_count > 0:
            results.append(
                _result("EVAL-SRC-002", "evidence_coverage", True, "")
            )
        else:
            results.append(
                _result(
                    "EVAL-SRC-002",
                    "evidence_coverage",
                    False,
                    "source_has_no_text_units",
                )
            )
            reason_codes.append("source_has_no_text_units")

        # EVAL-SRC-003: raw_hash_stable
        raw_path_value = payload.get("raw_path")
        stored_raw_hash = payload.get("raw_hash")
        if not isinstance(raw_path_value, str) or not isinstance(
            stored_raw_hash, str
        ):
            results.append(
                _result(
                    "EVAL-SRC-003",
                    "replay_consistency",
                    False,
                    "raw_hash_mismatch",
                )
            )
            reason_codes.append("raw_hash_mismatch")
        else:
            try:
                raw_bytes = _resolve_path(raw_path_value, repo_root_path).read_bytes()
                recomputed = "sha256:" + _sha256_hex(raw_bytes)
                if recomputed == stored_raw_hash:
                    results.append(
                        _result("EVAL-SRC-003", "replay_consistency", True, "")
                    )
                else:
                    results.append(
                        _result(
                            "EVAL-SRC-003",
                            "replay_consistency",
                            False,
                            "raw_hash_mismatch",
                        )
                    )
                    reason_codes.append("raw_hash_mismatch")
            except OSError:
                results.append(
                    _result(
                        "EVAL-SRC-003",
                        "replay_consistency",
                        False,
                        "raw_hash_mismatch",
                    )
                )
                reason_codes.append("raw_hash_mismatch")

        # EVAL-SRC-004: text_units_readable
        # Pass requires (1) at least one readable line and (2) the readable
        # count matches source_record.payload.text_unit_count, so truncated /
        # appended JSONL files cannot slip past with a stale stored count.
        processed_path_value = payload.get("processed_path", "")
        readable_units: List[Dict[str, Any]] = []
        if isinstance(processed_path_value, str) and processed_path_value:
            jsonl_path = _resolve_path(processed_path_value, repo_root_path) / "text_units.jsonl"
            try:
                with jsonl_path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            readable_units.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            except OSError:
                readable_units = []

        readable_count = len(readable_units)
        stored_count = (
            text_unit_count if isinstance(text_unit_count, int) else None
        )
        if readable_units and stored_count is not None and readable_count == stored_count:
            results.append(
                _result("EVAL-SRC-004", "evidence_coverage", True, "")
            )
        else:
            results.append(
                _result(
                    "EVAL-SRC-004",
                    "evidence_coverage",
                    False,
                    "text_units_unreadable",
                )
            )
            reason_codes.append("text_units_unreadable")

        # EVAL-SRC-005: text_unit_hashes_stable
        units_for_hash_check = readable_units or text_units or []
        all_match = True
        if not units_for_hash_check:
            all_match = False
        for unit in units_for_hash_check:
            text = unit.get("text", "")
            stored = unit.get("text_hash", "")
            recomputed = "sha256:" + _sha256_hex(text.encode("utf-8"))
            if recomputed != stored:
                all_match = False
                break
        if all_match:
            results.append(
                _result("EVAL-SRC-005", "replay_consistency", True, "")
            )
        else:
            results.append(
                _result(
                    "EVAL-SRC-005",
                    "replay_consistency",
                    False,
                    "text_unit_hash_mismatch",
                )
            )
            reason_codes.append("text_unit_hash_mismatch")

        decision = "block" if reason_codes else "allow"
        return {
            "decision": decision,
            "eval_results": results,
            "reason_codes": reason_codes,
        }
