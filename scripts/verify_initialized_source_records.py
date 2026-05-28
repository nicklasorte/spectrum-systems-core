#!/usr/bin/env python3
"""Verify ``source_record.json`` exists and validates for 32 backfilled slugs.

Companion verifier for ``initialize_new_source_records.py``. For each
slug in the hardcoded list, asserts that:

1. ``store/processed/meetings/<source_id>/source_record.json`` exists.
2. The file is valid UTF-8 JSON, a JSON object.
3. ``artifact_type == "source_record"``.
4. ``artifact_id`` is a valid UUID string.
5. ``payload.source_id`` equals the directory slug.
6. The record validates against
   ``contracts/schemas/source_record.schema.json`` (the canonical
   pipeline-internal schema).

Exits non-zero on any failure. Prints a JSON summary to stdout.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List

import jsonschema

from spectrum_systems_core.ingestion._paths import schema_path

# scripts/ is not a package — add it to sys.path so the sibling import
# below resolves whether the script is launched as ``python
# scripts/verify_initialized_source_records.py`` or imported in tests.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from initialize_new_source_records import SOURCE_IDS  # noqa: E402


def _load_schema() -> Dict[str, Any]:
    return json.loads(
        schema_path("source_record").read_text(encoding="utf-8")
    )


def verify_one(
    *,
    source_id: str,
    store_root: Path,
    validator: jsonschema.Draft202012Validator,
) -> Dict[str, Any]:
    sr_path = (
        store_root / "processed" / "meetings" / source_id
        / "source_record.json"
    )
    if not sr_path.is_file():
        return {
            "source_id": source_id,
            "status": "fail",
            "reason": f"missing file at {sr_path}",
        }
    try:
        record = json.loads(sr_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "source_id": source_id,
            "status": "fail",
            "reason": f"unreadable: {exc}",
        }
    except json.JSONDecodeError as exc:
        return {
            "source_id": source_id,
            "status": "fail",
            "reason": f"invalid JSON: {exc}",
        }
    if not isinstance(record, dict):
        return {
            "source_id": source_id,
            "status": "fail",
            "reason": f"not a JSON object (got {type(record).__name__})",
        }
    if record.get("artifact_type") != "source_record":
        return {
            "source_id": source_id,
            "status": "fail",
            "reason": (
                f"artifact_type is {record.get('artifact_type')!r}, "
                f"expected 'source_record'"
            ),
        }
    artifact_id = record.get("artifact_id")
    if not isinstance(artifact_id, str):
        return {
            "source_id": source_id,
            "status": "fail",
            "reason": (
                f"artifact_id is {type(artifact_id).__name__}, "
                f"expected string"
            ),
        }
    try:
        uuid.UUID(artifact_id)
    except ValueError as exc:
        return {
            "source_id": source_id,
            "status": "fail",
            "reason": f"artifact_id {artifact_id!r} is not a UUID: {exc}",
        }
    payload = record.get("payload") or {}
    if payload.get("source_id") != source_id:
        return {
            "source_id": source_id,
            "status": "fail",
            "reason": (
                f"payload.source_id is "
                f"{payload.get('source_id')!r}, expected {source_id!r}"
            ),
        }
    try:
        validator.validate(record)
    except jsonschema.ValidationError as exc:
        return {
            "source_id": source_id,
            "status": "fail",
            "reason": f"schema_violation: {exc.message}",
        }
    return {
        "source_id": source_id,
        "status": "pass",
        "reason": "",
        "artifact_id": artifact_id,
    }


def verify_all(
    *, data_lake: Path, source_ids: tuple[str, ...] = SOURCE_IDS
) -> Dict[str, Any]:
    store_root = data_lake / "store"
    schema = _load_schema()
    validator = jsonschema.Draft202012Validator(schema)
    per_source: List[Dict[str, Any]] = [
        verify_one(
            source_id=sid, store_root=store_root, validator=validator
        )
        for sid in source_ids
    ]
    counts: Dict[str, int] = {}
    for entry in per_source:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    passed = counts.get("pass", 0)
    return {
        "status": "success" if passed == len(source_ids) else "failure",
        "data_lake": str(data_lake),
        "total_sources": len(source_ids),
        "passed": passed,
        "counts": counts,
        "per_source": per_source,
    }


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-lake", required=True)
    args = parser.parse_args(argv)
    data_lake = Path(args.data_lake.strip())
    if not data_lake.is_dir():
        print(
            json.dumps(
                {
                    "status": "failure",
                    "reason": "data_lake_not_a_directory",
                    "detail": str(data_lake),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2

    summary = verify_all(data_lake=data_lake)
    print(json.dumps(summary, indent=2, sort_keys=True))
    for entry in summary["per_source"]:
        print(
            f"{entry['source_id']} | {entry['status']} "
            f"| {entry.get('reason') or ''}",
            file=sys.stderr,
        )
    return 0 if summary["status"] == "success" else 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
