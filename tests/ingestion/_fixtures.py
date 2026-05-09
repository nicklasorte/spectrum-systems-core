"""Shared fixture helpers for ingestion tests."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


DEFAULT_METADATA: Dict[str, Any] = {
    "description": "",
    "author": "",
    "tags": [],
    "raw_format": "txt",
    "private_use_only": False,
}


def write_source(
    repo_root: Path,
    *,
    family: str,
    source_id: str,
    content: str,
    metadata_overrides: Dict[str, Any] | None = None,
    filename: str = "source.txt",
) -> Path:
    target = repo_root / "raw" / family / source_id
    target.mkdir(parents=True, exist_ok=True)
    (target / filename).write_text(content, encoding="utf-8")

    metadata: Dict[str, Any] = dict(DEFAULT_METADATA)
    metadata.update(
        {
            "source_id": source_id,
            "source_family": family,
            "source_type": "transcript" if family == "meetings" else "block",
            "title": f"Title for {source_id}",
            "date": "2026-05-09",
        }
    )
    if metadata_overrides:
        metadata.update(metadata_overrides)
    (target / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


MEETING_TRANSCRIPT = (
    "ALICE: Welcome to the planning sync.\n"
    "We have several items today.\n"
    "BOB: Thanks. I have an update on Q3.\n"
    "CAROL: I will present the agency comments next.\n"
)


BOOK_PARAGRAPHS = (
    "Chapter one opens with a quiet observation.\n"
    "It carries through the room without interruption.\n"
    "\n"
    "The second paragraph picks up the thread.\n"
    "\n"
    "Finally, a closing thought.\n"
)
