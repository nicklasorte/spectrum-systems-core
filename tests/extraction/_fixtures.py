"""Shared fixture helpers for extraction tests."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, List


def write_text_units(
    repo_root: Path,
    *,
    family: str,
    source_id: str,
    texts: List[str],
    page_numbers: List[int] | None = None,
) -> Path:
    """Write processed/<family>/<source_id>/text_units.jsonl."""
    target = repo_root / "processed" / family / source_id
    target.mkdir(parents=True, exist_ok=True)
    units: List[Dict[str, Any]] = []
    char_offset = 0
    for ordinal, text in enumerate(texts):
        locator: Dict[str, Any] = {
            "line_start": ordinal,
            "line_end": ordinal,
            "char_start": char_offset,
            "char_end": char_offset + len(text),
        }
        if page_numbers is not None:
            locator["page_number"] = page_numbers[ordinal]
        units.append(
            {
                "unit_id": str(uuid.uuid4()),
                "source_id": source_id,
                "unit_type": "paragraph",
                "ordinal": ordinal,
                "text": text,
                "text_hash": "sha256:" + ("a" * 64),
                "locator": locator,
            }
        )
        char_offset += len(text) + 2

    units_path = target / "text_units.jsonl"
    with units_path.open("w", encoding="utf-8") as fh:
        for unit in units:
            fh.write(json.dumps(unit, sort_keys=True) + "\n")
    return units_path


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out
