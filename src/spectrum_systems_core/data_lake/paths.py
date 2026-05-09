"""Path helpers for the data lake layout.

Authoritative layout: docs/contracts/data_lake_contract.md.
"""
from __future__ import annotations

import re
from pathlib import Path

MEETING_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")


def validate_meeting_id(meeting_id: str) -> None:
    if not isinstance(meeting_id, str) or not MEETING_ID_PATTERN.match(meeting_id):
        raise ValueError(
            f"invalid meeting_id {meeting_id!r}; must match "
            f"{MEETING_ID_PATTERN.pattern}"
        )


def raw_meeting_dir(lake_root: Path | str, meeting_id: str) -> Path:
    validate_meeting_id(meeting_id)
    return Path(lake_root) / "raw" / "meetings" / meeting_id


def raw_transcript_path(lake_root: Path | str, meeting_id: str) -> Path:
    return raw_meeting_dir(lake_root, meeting_id) / "transcript.txt"


def raw_metadata_path(lake_root: Path | str, meeting_id: str) -> Path:
    return raw_meeting_dir(lake_root, meeting_id) / "metadata.json"


def processed_meeting_dir(lake_root: Path | str, meeting_id: str) -> Path:
    validate_meeting_id(meeting_id)
    return Path(lake_root) / "processed" / "meetings" / meeting_id


def artifact_index_path(lake_root: Path | str) -> Path:
    return Path(lake_root) / "indexes" / "meetings" / "artifact_index.jsonl"


MANIFEST_PREFIX = "manifest__"
DEBUG_PREFIX = "debug__"


def manifest_filename(run_id: str) -> str:
    return f"{MANIFEST_PREFIX}{run_id}.json"


def debug_filename(run_id: str) -> str:
    return f"{DEBUG_PREFIX}{run_id}.json"


def is_run_metadata_filename(name: str) -> bool:
    """True for manifest/debug records that are not product artifacts."""
    return name.startswith(MANIFEST_PREFIX) or name.startswith(DEBUG_PREFIX)
