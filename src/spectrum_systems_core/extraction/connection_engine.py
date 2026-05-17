"""ConnectionEngine: structured field matching across promoted stories.

NO vector search. NO embeddings. Pure structured field comparison.

A connection is recorded only when:
- The two stories come from DIFFERENT source_ids.
- At least 2 named fields match exactly between them (FINDING-C-002).
- Each matching field value is at least 10 characters
  (FINDING-C-002: trivial words like "risk" must not connect sources).

Strength:
- 3+ matching fields → strong
- 2 matching fields → moderate
- 1 matching field → weak (counted but NOT written to connections.jsonl)
"""
from __future__ import annotations

import datetime
import json
import uuid
from pathlib import Path
from typing import Any

import jsonschema

from ..ingestion._paths import schema_path
from ..ingestion.source_loader import SOURCE_FAMILIES

MIN_MATCHING_FIELDS = 2
MIN_FIELD_VALUE_LENGTH = 10


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _read_promoted_stories(processed_dir: Path) -> list[dict[str, Any]]:
    promoted_dir = processed_dir / "stories" / "promoted"
    if not promoted_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(promoted_dir.glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _all_sources(repo_root: Path) -> list[tuple[str, str, Path]]:
    """Return (source_id, source_family, processed_dir) tuples."""
    out: list[tuple[str, str, Path]] = []
    for family in SOURCE_FAMILIES:
        family_dir = repo_root / "processed" / family
        if not family_dir.is_dir():
            continue
        for source_dir in sorted(family_dir.iterdir()):
            if source_dir.is_dir():
                out.append((source_dir.name, family, source_dir))
    return out


_FIELDS_TO_COMPARE = (
    "possible_theme",
    "story_summary",
    "why_it_might_work",
)


class ConnectionEngine:
    """Find moderate / strong cross-source connections among promoted stories."""

    def find_connections(self, repo_root: str) -> dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        sources = _all_sources(repo_root_path)
        source_stories: dict[str, list[dict[str, Any]]] = {}
        source_dirs: dict[str, Path] = {}
        for sid, _family, processed_dir in sources:
            stories = _read_promoted_stories(processed_dir)
            if stories:
                source_stories[sid] = stories
                source_dirs[sid] = processed_dir

        try:
            schema = json.loads(
                schema_path("connection_record").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError):
            schema = None
        validator = jsonschema.Draft202012Validator(schema) if schema else None

        connections_by_source: dict[str, list[dict[str, Any]]] = {}
        weak_count = 0
        strong_count = 0
        moderate_count = 0

        ids = sorted(source_stories.keys())
        for i, sid_a in enumerate(ids):
            for sid_b in ids[i + 1 :]:
                if sid_a == sid_b:
                    continue  # RT4-003: never connect a source to itself.
                for story_a in source_stories[sid_a]:
                    for story_b in source_stories[sid_b]:
                        matching = self._matching_fields(story_a, story_b)
                        if not matching:
                            continue
                        if len(matching) == 1:
                            weak_count += 1
                            continue  # weak: not written to disk.
                        if len(matching) >= 3:
                            strength = "strong"
                            strong_count += 1
                        else:
                            strength = "moderate"
                            moderate_count += 1
                        connection = {
                            "connection_id": str(uuid.uuid4()),
                            "source_id_a": sid_a,
                            "source_id_b": sid_b,
                            "matching_fields": matching,
                            "connection_type": "theme_overlap",
                            "strength": strength,
                            "status": "candidate",
                            "created_at": _now_iso(),
                        }
                        if validator is not None:
                            try:
                                validator.validate(connection)
                            except jsonschema.ValidationError:
                                continue
                        connections_by_source.setdefault(sid_a, []).append(
                            connection
                        )

        for sid_a, conns in connections_by_source.items():
            out_path = (
                source_dirs[sid_a] / "knowledge" / "connections.jsonl"
            )
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", encoding="utf-8") as fh:
                for conn in conns:
                    fh.write(
                        json.dumps(conn, sort_keys=True, separators=(",", ":"))
                        + "\n"
                    )

        return {
            "status": "success",
            "connection_count": strong_count + moderate_count,
            "strong_count": strong_count,
            "moderate_count": moderate_count,
            "weak_count": weak_count,
            "reason": "",
        }

    def _matching_fields(
        self, story_a: dict[str, Any], story_b: dict[str, Any]
    ) -> list[dict[str, Any]]:
        matching: list[dict[str, Any]] = []
        for field_name in _FIELDS_TO_COMPARE:
            value_a = (story_a.get(field_name) or "").strip()
            value_b = (story_b.get(field_name) or "").strip()
            if not value_a or not value_b:
                continue
            if len(value_a) < MIN_FIELD_VALUE_LENGTH:
                continue
            if len(value_b) < MIN_FIELD_VALUE_LENGTH:
                continue
            if value_a != value_b:
                continue
            matching.append(
                {
                    "field_name": field_name,
                    "value_a": value_a,
                    "value_b": value_b,
                }
            )
        return matching
