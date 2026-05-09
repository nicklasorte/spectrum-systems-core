"""Cross-meeting JSONL index over promoted processed artifacts.

The index is a deterministic, byte-stable JSONL file that lists one
record per promoted artifact across all meetings in the lake. There is no
vector DB and no semantic search; this is plain string-based retrieval
backed by a sorted file.

Contract: docs/contracts/data_lake_contract.md sections 6 and 7.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .paths import artifact_index_path, is_run_metadata_filename
from .serialize import canonical_json

INDEX_FIELDS: tuple[str, ...] = (
    "meeting_id",
    "date",
    "artifact_id",
    "artifact_type",
    "title",
    "topic",
    "agency",
    "source_excerpt",
    "path",
)


class IndexError(ValueError):
    """Raised when index input is malformed."""


@dataclass(frozen=True)
class IndexRecord:
    meeting_id: str
    date: str
    artifact_id: str
    artifact_type: str
    title: str
    path: str
    topic: str | None = None
    agency: str | None = None
    source_excerpt: str | None = None

    def to_jsonable(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "meeting_id": self.meeting_id,
            "date": self.date,
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "title": self.title,
            "path": self.path,
        }
        if self.topic is not None:
            out["topic"] = self.topic
        if self.agency is not None:
            out["agency"] = self.agency
        if self.source_excerpt is not None:
            out["source_excerpt"] = self.source_excerpt
        return out


def _processed_meetings_root(lake_root: Path) -> Path:
    return lake_root / "processed" / "meetings"


def _read_artifact_file(path: Path) -> dict[str, Any] | None:
    """Return the artifact envelope dict, or None if it isn't one."""
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict):
        return None
    if body.get("artifact_type") in (None, "manifest", "debug_report"):
        return None
    if "status" not in body or "payload" not in body:
        return None
    return body


def _first_grounded_excerpt(payload: dict[str, Any]) -> str | None:
    grounding = payload.get("grounding") or []
    for entry in grounding:
        if isinstance(entry, dict):
            excerpt = entry.get("source_excerpt")
            if isinstance(excerpt, str) and excerpt:
                return excerpt
    return None


def _record_for(envelope: dict[str, Any], path: Path, lake_root: Path) -> IndexRecord | None:
    if envelope.get("status") != "promoted":
        return None
    payload = envelope.get("payload") or {}
    meeting_id = payload.get("meeting_id")
    if not isinstance(meeting_id, str) or not meeting_id:
        return None
    raw_meta = _load_raw_metadata(lake_root, meeting_id)

    def _from_payload_or_meta(key: str) -> str | None:
        v = payload.get(key)
        if isinstance(v, str) and v:
            return v
        v = raw_meta.get(key) if raw_meta else None
        return v if isinstance(v, str) and v else None

    return IndexRecord(
        meeting_id=meeting_id,
        date=str(_from_payload_or_meta("date") or ""),
        artifact_id=str(envelope.get("artifact_id", "")),
        artifact_type=str(envelope.get("artifact_type", "")),
        title=str(payload.get("title") or ""),
        path=str(path.relative_to(lake_root)),
        topic=_from_payload_or_meta("topic"),
        agency=_from_payload_or_meta("agency"),
        source_excerpt=_first_grounded_excerpt(payload),
    )


def _load_raw_metadata(lake_root: Path, meeting_id: str) -> dict[str, Any] | None:
    metadata_path = lake_root / "raw" / "meetings" / meeting_id / "metadata.json"
    if not metadata_path.is_file():
        return None
    try:
        meta = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return meta if isinstance(meta, dict) else None


def collect_index_records(lake_root: Path | str) -> list[IndexRecord]:
    """Walk processed/meetings/ and build a sorted list of records.

    Only files whose envelope has `status == "promoted"` produce records.
    Manifest and debug files are skipped because their filenames start
    with `manifest__` or `debug__` and they don't carry an artifact
    envelope shape (no payload + status pair on a real artifact).
    """
    lake_root = Path(lake_root)
    root = _processed_meetings_root(lake_root)
    if not root.is_dir():
        return []

    records: list[IndexRecord] = []
    for meeting_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for json_file in sorted(meeting_dir.glob("*.json")):
            if is_run_metadata_filename(json_file.name):
                continue
            envelope = _read_artifact_file(json_file)
            if envelope is None:
                continue
            record = _record_for(envelope, json_file, lake_root)
            if record is not None:
                records.append(record)

    records.sort(key=lambda r: (r.meeting_id, r.artifact_type, r.artifact_id))
    return records


def write_artifact_index(lake_root: Path | str) -> Path:
    """Build and write `indexes/meetings/artifact_index.jsonl`.

    Two writes over the same lake produce a byte-identical file.
    """
    lake_root = Path(lake_root)
    records = collect_index_records(lake_root)
    out_path = artifact_index_path(lake_root)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [canonical_json(r.to_jsonable()) for r in records]
    out_path.write_text("".join(lines), encoding="utf-8")
    return out_path


def read_artifact_index(lake_root: Path | str) -> list[dict[str, Any]]:
    lake_root = Path(lake_root)
    path = artifact_index_path(lake_root)
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if not isinstance(obj, dict):
            raise IndexError(f"non-object line in index: {line!r}")
        out.append(obj)
    return out
