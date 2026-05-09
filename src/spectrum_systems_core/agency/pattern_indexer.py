"""PatternIndexer: deterministic Jaccard-only recurring-objection index.

FINDING-E-005: vector/embedding/semantic similarity is out of scope.
Patterns are found via the shared Jaccard word-similarity helper at
threshold 0.6. If no patterns are found, an empty patterns.jsonl is
written — that is a successful run, not a failure.
"""
from __future__ import annotations

import datetime
import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List

import jsonschema

from ..utils.text_similarity import jaccard
from ._paths import agency_schema_path

JACCARD_THRESHOLD = 0.6
MIN_WORD_LENGTH = 4

_TOPIC_TOKEN = re.compile(r"[a-z0-9]+")


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not path.is_file():
        return out
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _topic_keywords(text_a: str, text_b: str) -> List[str]:
    """The shared word set (length >= MIN_WORD_LENGTH) — used as topic_keywords."""
    a = {w for w in _TOPIC_TOKEN.findall((text_a or "").lower()) if len(w) >= MIN_WORD_LENGTH}
    b = {w for w in _TOPIC_TOKEN.findall((text_b or "").lower()) if len(w) >= MIN_WORD_LENGTH}
    shared = sorted(a & b)
    return shared[:10] or ["objection"]


class PatternIndexer:
    """Find recurring objections across agencies via Jaccard word similarity."""

    def _jaccard(self, text_a: str, text_b: str) -> float:
        # Shared deterministic Jaccard. Vector/semantic similarity is out of
        # scope per SSC-VISION-001 until structured retrieval proves
        # insufficient.
        return jaccard(text_a, text_b, min_word_length=MIN_WORD_LENGTH)

    def build_patterns(self, repo_root: str) -> Dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        agency_root = repo_root_path / "agency"
        all_entries: List[Dict[str, Any]] = []
        if agency_root.is_dir():
            for sub in sorted(agency_root.iterdir()):
                if not sub.is_dir():
                    continue
                history_path = sub / "objection_history.jsonl"
                for entry in _read_jsonl(history_path):
                    if isinstance(entry, dict):
                        all_entries.append(entry)

        # Validate output schema once (so we never write an invalid record).
        try:
            schema = json.loads(
                agency_schema_path("recurring_pattern").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError) as exc:
            return {
                "status": "failure",
                "pattern_count": 0,
                "reason": f"schema_unreadable: {exc}",
            }
        validator = jsonschema.Draft202012Validator(schema)

        patterns: List[Dict[str, Any]] = []
        seen_pairs: set = set()
        for i in range(len(all_entries)):
            for j in range(i + 1, len(all_entries)):
                a = all_entries[i]
                b = all_entries[j]
                text_a = str(a.get("objection_text") or "")
                text_b = str(b.get("objection_text") or "")
                score = self._jaccard(text_a, text_b)
                if score < JACCARD_THRESHOLD:
                    continue
                pair_key = tuple(
                    sorted([
                        str(a.get("entry_id") or ""),
                        str(b.get("entry_id") or ""),
                    ])
                )
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                slug_a = str(a.get("agency_slug") or "")
                slug_b = str(b.get("agency_slug") or "")
                slugs = sorted({slug_a, slug_b} - {""})
                description = (
                    f"Similar objections across agencies ({', '.join(slugs)}): "
                    f"{text_a[:80]} | {text_b[:80]}"
                )
                if len(description) < 20:
                    description = (description + " " * 20)[:60]
                pattern = {
                    "pattern_id": str(uuid.uuid4()),
                    "pattern_type": "recurring_objection",
                    "description": description,
                    "agency_slugs": slugs or [slug_a or "unknown"],
                    "source_objection_entry_ids": [
                        str(a.get("entry_id") or ""),
                        str(b.get("entry_id") or ""),
                    ],
                    "jaccard_similarity": round(score, 4),
                    "similarity_method": "jaccard_word",
                    "topic_keywords": _topic_keywords(text_a, text_b),
                    "created_at": _now_iso(),
                }
                try:
                    validator.validate(pattern)
                except jsonschema.ValidationError:
                    continue
                patterns.append(pattern)

        # Write to agency/patterns.jsonl (repo root level).
        agency_root.mkdir(parents=True, exist_ok=True)
        patterns_path = agency_root / "patterns.jsonl"
        with patterns_path.open("w", encoding="utf-8") as fh:
            for p in patterns:
                fh.write(
                    json.dumps(p, sort_keys=True, separators=(",", ":")) + "\n"
                )

        # Projection.
        try:
            from ..ingestion.obsidian_projection import ObsidianProjection

            ObsidianProjection().write_patterns_projection(
                patterns, str(repo_root_path)
            )
        except (FileNotFoundError, OSError):
            pass

        return {
            "status": "success",
            "pattern_count": len(patterns),
            "reason": "",
        }
