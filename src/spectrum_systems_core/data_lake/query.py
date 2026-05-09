"""Deterministic query over the JSONL index.

No vector search. No semantic search. No embeddings. Plain string and
field filters with predictable ordering. Keyword matching is
case-insensitive substring against string content only — never against
the JSON encoding's structure (field names, brackets, escapes).

The query function reads `indexes/meetings/artifact_index.jsonl` and the
linked artifact files when a keyword filter requires payload text. It
returns records (not artifact objects) so a caller can decide whether to
load the underlying file.

If the index file does not exist when `query` runs, the query rebuilds it
from the current state of `processed/`. Mutating `processed/` after the
query has executed does not invalidate already-returned results; rebuild
the index explicitly via `write_artifact_index` when in doubt.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .index import read_artifact_index, write_artifact_index
from .paths import artifact_index_path

SUPPORTED_FILTERS: frozenset[str] = frozenset(
    {"artifact_type", "meeting_id", "agency", "date_from", "date_to", "keyword"}
)

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class QueryResult:
    record: dict[str, Any]
    matched_fields: tuple[str, ...]


class QueryError(ValueError):
    """Raised when a query is malformed or uses an unsupported filter."""


def _validate_filters(**filters: Any) -> None:
    unsupported = [k for k in filters if k not in SUPPORTED_FILTERS]
    if unsupported:
        raise QueryError(
            f"unsupported filter(s): {sorted(unsupported)}; "
            f"supported: {sorted(SUPPORTED_FILTERS)}"
        )


def _validate_date_input(name: str, value: str | None) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not _DATE_PATTERN.match(value):
        raise QueryError(
            f"{name} must be in YYYY-MM-DD format, got {value!r}"
        )


def _collect_string_leaves(value: Any) -> list[str]:
    """Return every string leaf in a JSON-shaped value. Field names are excluded."""
    out: list[str] = []
    stack: list[Any] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return out


def _date_in_range(value: str, lo: str | None, hi: str | None) -> bool:
    if not value:
        return lo is None and hi is None
    if lo is not None and value < lo:
        return False
    if hi is not None and value > hi:
        return False
    return True


def _keyword_matches(record: dict[str, Any], lake_root: Path, keyword: str) -> tuple[bool, list[str]]:
    """Match keyword against title, source_excerpt, and payload string leaves.

    Matching is a case-insensitive substring check against string content
    only. The JSON encoding's structure (field names, brackets, escapes)
    is never matched. Loading the payload happens only if title and
    source_excerpt do not match, so the fast path stays fast.
    """
    needle = keyword.lower()
    matched: list[str] = []
    title = record.get("title") or ""
    if needle in title.lower():
        matched.append("title")
    excerpt = record.get("source_excerpt") or ""
    if needle in excerpt.lower():
        matched.append("source_excerpt")
    if matched:
        return True, matched

    rel_path = record.get("path")
    if not isinstance(rel_path, str):
        return False, []
    full_path = lake_root / rel_path
    if not full_path.is_file():
        return False, []
    try:
        body = json.loads(full_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False, []
    payload = body.get("payload") if isinstance(body, dict) else None
    if not isinstance(payload, dict):
        return False, []
    for leaf in _collect_string_leaves(payload):
        if needle in leaf.lower():
            return True, ["payload_text"]
    return False, []


def query(
    lake_root: Path | str,
    *,
    artifact_type: str | None = None,
    meeting_id: str | None = None,
    agency: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    keyword: str | None = None,
) -> list[QueryResult]:
    """Run a deterministic filter query over the JSONL index.

    Returns matching records sorted as the index file is sorted.
    """
    lake_root = Path(lake_root)
    _validate_filters(
        artifact_type=artifact_type,
        meeting_id=meeting_id,
        agency=agency,
        date_from=date_from,
        date_to=date_to,
        keyword=keyword,
    )
    _validate_date_input("date_from", date_from)
    _validate_date_input("date_to", date_to)

    if not artifact_index_path(lake_root).is_file():
        write_artifact_index(lake_root)
    records = read_artifact_index(lake_root)
    out: list[QueryResult] = []
    for r in records:
        if artifact_type is not None and r.get("artifact_type") != artifact_type:
            continue
        if meeting_id is not None and r.get("meeting_id") != meeting_id:
            continue
        if agency is not None and r.get("agency") != agency:
            continue
        if date_from is not None or date_to is not None:
            if not _date_in_range(r.get("date") or "", date_from, date_to):
                continue
        matched_fields: tuple[str, ...] = ()
        if keyword is not None:
            ok, fields = _keyword_matches(r, lake_root, keyword)
            if not ok:
                continue
            matched_fields = tuple(fields)
        out.append(QueryResult(record=r, matched_fields=matched_fields))
    return out
