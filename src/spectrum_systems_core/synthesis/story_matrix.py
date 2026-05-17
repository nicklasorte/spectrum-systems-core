"""StoryMatrix: deterministic audience-weighted story selection.

For each (theme, audience) pair, picks the highest-relevance promoted
tier-1/2/3 story. Audience is one of the fixed enum values
(FINDING-F-003). No LLM. Ties are broken by promoted_at (most recent
wins) and then story_id (lexicographic) for full determinism.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import jsonschema

from ..ingestion.source_loader import SOURCE_FAMILIES
from ..utils.text_similarity import jaccard
from ._paths import synthesis_run_dir, synthesis_schema_path

_COMPONENT_NAME = "story_matrix"
_COMPONENT_VERSION = "1.0.0"

VALID_AUDIENCES = ("technical", "policy", "executive", "public")

AUDIENCE_WEIGHT: dict[str, dict[str, float]] = {
    "technical": {"tier_1": 1.0, "tier_2": 0.7, "tier_3": 0.3},
    "policy":    {"tier_1": 1.0, "tier_2": 0.8, "tier_3": 0.4},
    "executive": {"tier_1": 1.0, "tier_2": 0.5, "tier_3": 0.1},
    "public":    {"tier_1": 1.0, "tier_2": 0.6, "tier_3": 0.2},
}


def _read_promoted_dir(dir_path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
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


class StoryMatrix:
    """Build the per-(theme,audience) story selection matrix."""

    def build(
        self,
        run_id: str,
        audience: str,
        themes: list[dict[str, Any]],
        repo_root: str,
    ) -> dict[str, Any]:
        if audience not in VALID_AUDIENCES:
            return {
                "status": "failure",
                "matrix_entries": 0,
                "reason": f"invalid_audience: {audience}",
                "entries": [],
            }
        repo_root_path = Path(repo_root).resolve()

        # Collect promoted stories from every source.
        stories: list[dict[str, Any]] = []
        any_promoted_dir_found = False
        for source_id, source_dir in _iter_processed_dirs(repo_root_path):
            promoted_dir = source_dir / "stories" / "promoted"
            if promoted_dir.is_dir():
                any_promoted_dir_found = True
            for story in _read_promoted_dir(promoted_dir):
                if str(story.get("status") or "") != "promoted":
                    continue
                story = dict(story)
                story["__source_id"] = source_id
                stories.append(story)

        if not any_promoted_dir_found or not stories:
            return {
                "status": "failure",
                "matrix_entries": 0,
                "reason": "no_promoted_stories",
                "entries": [],
            }

        weights = AUDIENCE_WEIGHT[audience]
        schema = json.loads(
            synthesis_schema_path("story_matrix_entry")
            .read_text(encoding="utf-8")
        )
        validator = jsonschema.Draft202012Validator(schema)

        entries: list[dict[str, Any]] = []
        for theme in themes:
            theme_name = str(theme.get("theme_name", ""))
            best: dict[str, Any] | None = None
            best_score = -1.0
            best_promoted_at = ""
            best_story_id = ""
            for story in stories:
                tier = str(story.get("tier_guess") or "tier_3")
                weight = weights.get(tier, 0.0)
                sim = jaccard(
                    theme_name, str(story.get("possible_theme", ""))
                )
                score = round(sim * weight, 4)
                promoted_at = str(story.get("created_at", ""))
                story_id = str(story.get("story_id", ""))
                key = (
                    score,
                    promoted_at,
                    story_id,
                )
                cur_key = (
                    best_score,
                    best_promoted_at,
                    best_story_id,
                )
                if key > cur_key:
                    best = story
                    best_score = score
                    best_promoted_at = promoted_at
                    best_story_id = story_id
            if best is None or best_score <= 0.0:
                continue
            entry = {
                "entry_id": str(uuid.uuid4()),
                "run_id": run_id,
                "theme_name": theme_name,
                "audience": audience,
                "story_id": best["story_id"],
                "story_title": str(best.get("possible_theme", "")) or theme_name,
                "story_source_id": best["__source_id"],
                "relevance_score": float(best_score),
                "selection_reason": (
                    f"jaccard_word_similarity*audience_weight[{audience}][{best.get('tier_guess')}]"
                ),
            }
            try:
                validator.validate(entry)
            except jsonschema.ValidationError:
                continue
            entries.append(entry)

        run_dir = synthesis_run_dir(repo_root_path, run_id, create=True)
        target = run_dir / "story_matrix.json"
        target.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "audience": audience,
                    "entries": entries,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return {
            "status": "success",
            "matrix_entries": len(entries),
            "reason": "",
            "entries": entries,
        }
