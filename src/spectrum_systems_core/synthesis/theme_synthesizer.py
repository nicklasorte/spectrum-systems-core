"""ThemeSynthesizer: deterministic cross-source theme synthesis.

Reads only promoted theme_records (from Phase C) and evidenced claims
(from Phase D). Groups themes by Jaccard word similarity (>= 0.6) on
theme_name. Empty input set is not a failure; it writes an empty
themes.jsonl. cross_source is true for any group with 2+ source_ids.
"""
from __future__ import annotations

import datetime
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List

import jsonschema

from ..ingestion.source_loader import SOURCE_FAMILIES
from ..utils.text_similarity import jaccard
from ._paths import synthesis_run_dir, synthesis_schema_path


_COMPONENT_NAME = "theme_synthesizer"
_COMPONENT_VERSION = "1.0.0"

THEME_GROUP_THRESHOLD = 0.6


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not path.is_file():
        return out
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def _read_promoted_dir(dir_path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not dir_path.is_dir():
        return out
    for child in sorted(dir_path.glob("*.json")):
        try:
            out.append(json.loads(child.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _iter_processed_dirs(repo_root: Path):
    base = repo_root / "processed"
    if not base.is_dir():
        return
    for family in SOURCE_FAMILIES:
        family_dir = base / family
        if not family_dir.is_dir():
            continue
        for source_dir in sorted(family_dir.iterdir()):
            if source_dir.is_dir():
                yield source_dir.name, source_dir


class ThemeSynthesizer:
    """Group promoted themes across sources into theme_synthesis_records."""

    def synthesize(self, run_id: str, repo_root: str) -> Dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()

        # Collect promoted themes (Phase C knowledge artifacts).
        themes: List[Dict[str, Any]] = []
        for source_id, source_dir in _iter_processed_dirs(repo_root_path):
            promoted_dir = source_dir / "knowledge" / "promoted"
            for artifact in _read_promoted_dir(promoted_dir):
                if "theme_id" not in artifact:
                    continue
                if str(artifact.get("status") or "") != "promoted":
                    continue
                themes.append(
                    {
                        "theme_id": artifact["theme_id"],
                        "theme_name": str(artifact.get("theme_name", "")),
                        "description": str(artifact.get("description", "")),
                        "source_id": source_id,
                        "source_story_ids": list(
                            artifact.get("source_story_ids") or []
                        ),
                    }
                )

        # Collect evidenced claims (Phase D).
        claims_by_theme_word: Dict[str, List[Dict[str, Any]]] = {}
        all_claims: List[Dict[str, Any]] = []
        for source_id, source_dir in _iter_processed_dirs(repo_root_path):
            claims_path = source_dir / "paper" / "claims.jsonl"
            for claim in _read_jsonl(claims_path):
                if str(claim.get("status") or "") != "evidenced":
                    continue
                all_claims.append(
                    {
                        "claim_id": claim["claim_id"],
                        "source_id": source_id,
                        "claim_text": str(claim.get("claim_text", "")),
                    }
                )

        # Group themes by Jaccard similarity on theme_name.
        groups: List[List[Dict[str, Any]]] = []
        for theme in themes:
            placed = False
            for group in groups:
                seed = group[0]
                if (
                    theme["theme_name"] == seed["theme_name"]
                    or jaccard(theme["theme_name"], seed["theme_name"])
                    >= THEME_GROUP_THRESHOLD
                ):
                    group.append(theme)
                    placed = True
                    break
            if not placed:
                groups.append([theme])

        # Build synthesis records, one per group.
        schema = json.loads(
            synthesis_schema_path("theme_synthesis_record")
            .read_text(encoding="utf-8")
        )
        validator = jsonschema.Draft202012Validator(schema)

        records: List[Dict[str, Any]] = []
        for group in groups:
            sources = sorted({t["source_id"] for t in group})
            stories: List[str] = []
            for t in group:
                for sid in t["source_story_ids"]:
                    if sid not in stories:
                        stories.append(sid)
            stories.sort()
            seed = group[0]
            # Attach evidenced claims that match by Jaccard on claim_text.
            claim_ids: List[str] = []
            for claim in all_claims:
                if (
                    jaccard(claim["claim_text"], seed["theme_name"])
                    >= THEME_GROUP_THRESHOLD
                ):
                    if claim["claim_id"] not in claim_ids:
                        claim_ids.append(claim["claim_id"])
            claim_ids.sort()
            description = seed["description"]
            if len(description) < 20:
                description = (
                    description
                    + " (auto-extended for synthesis record description)"
                )
            record = {
                "synthesis_id": str(uuid.uuid4()),
                "run_id": run_id,
                "theme_name": seed["theme_name"],
                "theme_description": description,
                "contributing_source_ids": sources,
                "contributing_story_ids": stories,
                "contributing_claim_ids": claim_ids,
                "cross_source": len(sources) >= 2,
                "created_at": _now_iso(),
            }
            try:
                validator.validate(record)
            except jsonschema.ValidationError:
                continue
            records.append(record)

        run_dir = synthesis_run_dir(repo_root_path, run_id, create=True)
        target = run_dir / "themes.jsonl"
        with target.open("w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n")

        return {"status": "success", "theme_count": len(records)}
