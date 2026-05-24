#!/usr/bin/env python3
"""Ingest an NTIA-authored meeting minutes .txt file as the gold standard.

Reads the .txt minutes for one transcript, runs the deterministic
parser in ``spectrum_systems_core.workflows.minutes_parser``, validates
the result against the ``human_minutes`` schema, and writes the
artifact to the data lake at::

    <data-lake>/store/processed/meetings/<source_id>/human_minutes__<source_id>.json

ZERO LLM calls. ``produced_by == "minutes_parser"``.

The script is intentionally minimal — it produces ONE artifact per
invocation and never reads any pipeline output. The artifact carries
``raw_source_hash`` so a downstream reader can prove which .txt file
the parser ran against.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

_REPO_ROOT = _SCRIPTS_DIR.parent
_SRC_DIR = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from _artifact_validator import (  # noqa: E402
    ArtifactValidationError,
    validate_artifact,
)
from spectrum_systems_core.workflows.minutes_parser import (  # noqa: E402
    ParsedMinutes,
    parse_minutes_txt,
)

HUMAN_MINUTES_ARTIFACT_TYPE = "human_minutes"
HUMAN_MINUTES_SCHEMA_VERSION = "1.0.0"
PRODUCED_BY = "minutes_parser"


def parsed_to_artifact(
    parsed: ParsedMinutes,
    *,
    source_id: str,
    raw_bytes: bytes,
) -> dict[str, Any]:
    """Wrap a ``ParsedMinutes`` value object as a schema-valid envelope."""
    raw_source_hash = hashlib.sha256(raw_bytes).hexdigest()

    artifact: dict[str, Any] = {
        "artifact_type": HUMAN_MINUTES_ARTIFACT_TYPE,
        "schema_version": HUMAN_MINUTES_SCHEMA_VERSION,
        "source_id": source_id,
        "meeting_name": parsed.meeting_name,
        "meeting_date": parsed.meeting_date,
        "prepared_by": parsed.prepared_by,
        "location": parsed.location,
        "overview": parsed.overview,
        "discussion_items": [
            {
                "item_number": item.item_number,
                "category": item.category,
                "question_topic": item.question_topic,
                "asked_by": item.asked_by,
                "response": item.response,
                "follow_up": item.follow_up,
            }
            for item in parsed.discussion_items
        ],
        "action_items": [
            {
                "text": item.text,
                "responsible_party": item.responsible_party,
                "due_date": item.due_date,
                "status": item.status,
            }
            for item in parsed.action_items
        ],
        "next_steps": list(parsed.next_steps),
        "produced_by": PRODUCED_BY,
        "raw_source_hash": f"sha256:{raw_source_hash}",
        "source_path": parsed.source_path,
    }
    return artifact


def write_artifact(
    artifact: dict[str, Any],
    *,
    data_lake: Path,
    source_id: str,
) -> Path:
    out_dir = data_lake / "store" / "processed" / "meetings" / source_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"human_minutes__{source_id}.json"
    out_path.write_text(
        json.dumps(artifact, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Parse an NTIA meeting minutes .txt file and write a "
            "human_minutes artifact to the data lake. ZERO LLM calls."
        )
    )
    parser.add_argument(
        "--minutes-file",
        required=True,
        help="Path to the .txt minutes file (relative to repo root or absolute).",
    )
    parser.add_argument(
        "--source-id",
        required=True,
        help="Transcript slug under data-lake/store/processed/meetings/.",
    )
    parser.add_argument(
        "--data-lake",
        required=True,
        help="Root path of the data-lake clone (the directory containing 'store/').",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and validate only; do not write the artifact to disk.",
    )
    args = parser.parse_args(argv)

    minutes_path = Path(args.minutes_file)
    if not minutes_path.is_absolute():
        minutes_path = (_REPO_ROOT / minutes_path).resolve()
    if not minutes_path.is_file():
        print(f"error: minutes file not found at {minutes_path}", file=sys.stderr)
        return 2

    data_lake = Path(args.data_lake)
    if not data_lake.is_absolute():
        data_lake = (_REPO_ROOT / data_lake).resolve()

    raw_bytes = minutes_path.read_bytes()
    parsed = parse_minutes_txt(minutes_path)
    artifact = parsed_to_artifact(
        parsed, source_id=args.source_id, raw_bytes=raw_bytes
    )

    try:
        validate_artifact(
            artifact,
            HUMAN_MINUTES_ARTIFACT_TYPE,
            str(minutes_path),
        )
    except ArtifactValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    summary = (
        f"discussion_items={len(parsed.discussion_items)} "
        f"action_items={len(parsed.action_items)} "
        f"next_steps={len(parsed.next_steps)}"
    )

    if args.dry_run:
        print(f"DRY RUN: parsed {minutes_path.name}; {summary}")
        print(f"would write to: {data_lake}/store/processed/meetings/"
              f"{args.source_id}/human_minutes__{args.source_id}.json")
        return 0

    out_path = write_artifact(artifact, data_lake=data_lake, source_id=args.source_id)
    print(f"wrote {out_path}; {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
