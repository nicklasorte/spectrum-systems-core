"""Chunker: text_units.jsonl -> overlapping chunks.jsonl.

Standard library only. No LLM calls. Deterministic. Fail-closed.

CHUNK_SIZE = 8 text units per chunk.
OVERLAP = 1: the last unit of chunk N is the first unit of chunk N+1.
This prevents a story being cut mid-sentence at chunk boundaries
(FINDING-C-006 fix).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List

import jsonschema

from ..ingestion._paths import schema_path
from ._paths import find_processed_dir


CHUNK_SIZE = 8
OVERLAP = 1


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _failure(reason: str) -> Dict[str, Any]:
    return {"status": "failure", "chunks": [], "reason": reason}


class Chunker:
    """Split text_units.jsonl into overlapping chunks for story extraction."""

    def chunk(self, source_id: str, repo_root: str) -> Dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        processed_dir, source_family = find_processed_dir(
            repo_root_path, source_id
        )
        if processed_dir is None or source_family is None:
            return _failure("text_units_not_found")
        units_path = processed_dir / "text_units.jsonl"
        if not units_path.is_file():
            return _failure("text_units_not_found")

        units: List[Dict[str, Any]] = []
        try:
            with units_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        units.append(json.loads(line))
                    except json.JSONDecodeError as exc:
                        return _failure(
                            f"text_unit_malformed: invalid json: {exc}"
                        )
        except OSError as exc:
            return _failure(f"text_units_not_found: {exc}")

        if not units:
            return _failure("text_units_empty")

        for u in units:
            if not isinstance(u, dict):
                return _failure("text_unit_malformed: non-object line")
            for key in ("unit_id", "text", "unit_type", "ordinal", "locator"):
                if key not in u:
                    return _failure(f"text_unit_malformed: missing {key}")

        units = sorted(units, key=lambda x: int(x["ordinal"]))

        try:
            chunk_schema = json.loads(
                schema_path("chunk").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError) as exc:
            return _failure(f"chunk_schema_violation: schema unreadable: {exc}")
        validator = jsonschema.Draft202012Validator(chunk_schema)

        chunks: List[Dict[str, Any]] = []
        chunk_index = 0
        i = 0
        n = len(units)
        while i < n:
            chunk_units = units[i : i + CHUNK_SIZE]
            # FINDING-C-006: chunks share their boundary unit. The first unit
            # of chunk N is the last unit of chunk N-1. overlap_unit_id is
            # that shared unit (None for the first chunk).
            overlap_unit_id = chunk_units[0]["unit_id"] if i > 0 else None
            chunk_text = "\n".join(u["text"] for u in chunk_units)
            page_numbers: List[int] = []
            seen_pages: set[int] = set()
            for u in chunk_units:
                locator = u.get("locator", {}) or {}
                page = locator.get("page_number")
                if isinstance(page, int) and page not in seen_pages:
                    seen_pages.add(page)
                    page_numbers.append(page)
            page_numbers.sort()
            chunk = {
                "chunk_id": str(uuid.uuid4()),
                "source_id": source_id,
                "source_family": source_family,
                "chunk_index": chunk_index,
                "unit_ids": [u["unit_id"] for u in chunk_units],
                "text": chunk_text,
                "text_hash": "sha256:" + _sha256_hex(chunk_text.encode("utf-8")),
                "unit_count": len(chunk_units),
                "overlap_unit_id": overlap_unit_id,
                "page_numbers": page_numbers,
                "char_count": len(chunk_text),
            }
            try:
                validator.validate(chunk)
            except jsonschema.ValidationError as exc:
                return _failure(f"chunk_schema_violation: {exc.message}")
            chunks.append(chunk)
            step = CHUNK_SIZE - OVERLAP
            if step <= 0:
                return _failure("chunk_step_invalid")
            i += step
            chunk_index += 1
            if len(chunk_units) < CHUNK_SIZE:
                break

        out_dir = processed_dir / "stories"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / "chunks.jsonl"
            with out_path.open("w", encoding="utf-8") as fh:
                for chunk in chunks:
                    fh.write(
                        json.dumps(chunk, sort_keys=True, separators=(",", ":"))
                        + "\n"
                    )
        except OSError as exc:
            return _failure(f"write_error: {exc}")

        return {"status": "success", "chunks": chunks, "reason": ""}
