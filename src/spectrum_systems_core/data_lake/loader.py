"""Transcript + metadata loader. Fail-closed at the lake boundary.

Contract: docs/contracts/data_lake_contract.md sections 4 and 5.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import (
    raw_meeting_dir,
    validate_meeting_id,
)

REQUIRED_METADATA_FIELDS: tuple[str, ...] = (
    "meeting_id",
    "title",
    "date",
    "source_type",
)

ALLOWED_SOURCE_TYPES: frozenset[str] = frozenset({"transcript", "notes", "summary"})


class LoaderError(ValueError):
    """Raised when raw inputs violate the data lake contract."""


@dataclass(frozen=True)
class TranscriptInput:
    meeting_id: str
    title: str
    date: str
    source_type: str
    transcript_text: str
    transcript_lines: tuple[str, ...]
    metadata: dict[str, Any]
    transcript_hash: str
    metadata_hash: str
    transcript_path: str
    metadata_path: str

    def line(self, n: int) -> str:
        if n < 1 or n > len(self.transcript_lines):
            raise IndexError(f"line {n} out of range 1..{len(self.transcript_lines)}")
        return self.transcript_lines[n - 1]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_date(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 10:
        raise LoaderError(f"metadata.date must be YYYY-MM-DD, got {value!r}")
    y, m, d = value[0:4], value[5:7], value[8:10]
    if value[4] != "-" or value[7] != "-":
        raise LoaderError(f"metadata.date must be YYYY-MM-DD, got {value!r}")
    if not (y.isdigit() and m.isdigit() and d.isdigit()):
        raise LoaderError(f"metadata.date must be YYYY-MM-DD, got {value!r}")
    return value


def _validate_required_metadata(meta: dict[str, Any], meeting_id: str) -> None:
    for field_name in REQUIRED_METADATA_FIELDS:
        if field_name not in meta:
            raise LoaderError(f"metadata missing required field: {field_name}")
        value = meta[field_name]
        if not isinstance(value, str) or not value.strip():
            raise LoaderError(
                f"metadata.{field_name} must be a non-empty string, got {value!r}"
            )

    if meta["meeting_id"] != meeting_id:
        raise LoaderError(
            f"metadata.meeting_id ({meta['meeting_id']!r}) does not match "
            f"directory name ({meeting_id!r})"
        )

    _validate_date(meta["date"])

    source_type = meta["source_type"]
    if source_type not in ALLOWED_SOURCE_TYPES:
        raise LoaderError(
            f"metadata.source_type must be one of "
            f"{sorted(ALLOWED_SOURCE_TYPES)}, got {source_type!r}"
        )


def load_meeting_from_dir(meeting_dir: Path | str) -> TranscriptInput:
    """Load a single meeting from an explicit directory."""
    meeting_dir = Path(meeting_dir)
    meeting_id = meeting_dir.name
    validate_meeting_id(meeting_id)

    transcript_path = meeting_dir / "transcript.txt"
    metadata_path = meeting_dir / "metadata.json"

    if not transcript_path.is_file():
        raise LoaderError(f"missing transcript at {transcript_path}")
    if not metadata_path.is_file():
        raise LoaderError(f"missing metadata at {metadata_path}")

    transcript_bytes = transcript_path.read_bytes()
    if not transcript_bytes.strip():
        raise LoaderError(f"transcript is empty at {transcript_path}")

    metadata_bytes = metadata_path.read_bytes()
    try:
        metadata = json.loads(metadata_bytes.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise LoaderError(f"invalid JSON in {metadata_path}: {exc}") from exc

    if not isinstance(metadata, dict):
        raise LoaderError(f"metadata at {metadata_path} must be a JSON object")

    _validate_required_metadata(metadata, meeting_id)

    transcript_text = transcript_bytes.decode("utf-8")
    transcript_lines = tuple(transcript_text.splitlines())

    canonical_meta = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")

    return TranscriptInput(
        meeting_id=meeting_id,
        title=metadata["title"],
        date=metadata["date"],
        source_type=metadata["source_type"],
        transcript_text=transcript_text,
        transcript_lines=transcript_lines,
        metadata=dict(metadata),
        transcript_hash=_sha256_bytes(transcript_bytes),
        metadata_hash=_sha256_bytes(canonical_meta),
        transcript_path=str(transcript_path),
        metadata_path=str(metadata_path),
    )


def load_meeting(lake_root: Path | str, meeting_id: str) -> TranscriptInput:
    """Load a meeting using the data lake layout."""
    return load_meeting_from_dir(raw_meeting_dir(lake_root, meeting_id))
