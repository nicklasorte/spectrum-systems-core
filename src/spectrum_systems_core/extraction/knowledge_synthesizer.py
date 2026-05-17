"""KnowledgeSynthesizer: deterministic concept / theme / analogy synthesis.

Input: promoted story candidates under processed/<family>/<source_id>/stories/
       promoted/.
Output: knowledge/{concepts,themes,analogies}.jsonl, all status="candidate".
Humans promote individual records via the `promote-knowledge` CLI command
(FINDING-C-003). No LLM. No auto-promotion.
"""
from __future__ import annotations

import datetime
import json
import logging
import uuid
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import jsonschema

from ..ingestion._paths import schema_path
from ..ingestion.source_loader import SOURCE_FAMILIES
from ._paths import find_processed_dir

_LOG = logging.getLogger(__name__)


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


def _supporting_excerpt(story: dict[str, Any]) -> dict[str, Any] | None:
    excerpt = story.get("source_excerpt") or ""
    grounded = story.get("grounded_unit_ids") or story.get("unit_ids") or []
    unit_id = grounded[0] if grounded else None
    if not (excerpt and unit_id):
        return None
    return {
        "unit_id": str(unit_id),
        "excerpt": excerpt,
        "source_id": str(story.get("source_id") or ""),
    }


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(
                json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            )


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                out.append(json.loads(line))
    except (OSError, json.JSONDecodeError):
        return []
    return out


class KnowledgeSynthesizer:
    """Group promoted stories into concept / theme / analogy candidates."""

    def synthesize_concepts(
        self, source_id: str, repo_root: str
    ) -> dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        processed_dir, _ = find_processed_dir(repo_root_path, source_id)
        if processed_dir is None:
            return {"status": "failure", "concept_count": 0, "reason": "source_not_found"}
        stories = _read_promoted_stories(processed_dir)

        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for story in stories:
            theme = (story.get("possible_theme") or "").strip()
            if not theme or len(theme) < 3:
                continue
            groups[theme].append(story)

        try:
            schema = json.loads(
                schema_path("concept_record").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError):
            schema = None
        validator = jsonschema.Draft202012Validator(schema) if schema else None

        records: list[dict[str, Any]] = []
        for theme, group in groups.items():
            if len(group) < 2:
                continue
            supporting: list[dict[str, Any]] = []
            for story in group:
                excerpt = _supporting_excerpt(story)
                if excerpt is not None:
                    supporting.append(excerpt)
            if not supporting:
                continue  # RT4-001: no excerpts → skip (block this concept).
            definition = (group[0].get("why_it_might_work") or "").strip()
            if len(definition) < 20:
                definition = (
                    f"Concept derived from {len(group)} stories sharing the "
                    f"theme '{theme}'. Human reviewer must refine this "
                    f"definition before promotion."
                )
            record = {
                "concept_id": str(uuid.uuid4()),
                "concept_name": theme,
                "definition": definition,
                "source_story_ids": [str(s["story_id"]) for s in group],
                "source_ids": sorted({str(s["source_id"]) for s in group}),
                "supporting_excerpts": supporting,
                "related_concepts": [],
                "status": "candidate",
                "created_at": _now_iso(),
            }
            if validator is not None:
                try:
                    validator.validate(record)
                except jsonschema.ValidationError as exc:
                    _LOG.warning(
                        "concept_record schema violation for theme %r: %s",
                        theme,
                        exc.message,
                    )
                    continue
            records.append(record)

        out_path = processed_dir / "knowledge" / "concepts.jsonl"
        _write_jsonl(out_path, records)
        return {"status": "success", "concept_count": len(records), "reason": ""}

    def synthesize_themes(
        self, source_id: str, repo_root: str
    ) -> dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        processed_dir, _ = find_processed_dir(repo_root_path, source_id)
        if processed_dir is None:
            return {"status": "failure", "theme_count": 0, "reason": "source_not_found"}
        stories = _read_promoted_stories(processed_dir)

        # Tier-1 stories grouped by distinct possible_theme.
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for story in stories:
            if story.get("tier_guess") != "tier_1":
                continue
            theme = (story.get("possible_theme") or "").strip()
            if not theme or len(theme) < 3:
                continue
            groups[theme].append(story)

        try:
            schema = json.loads(
                schema_path("theme_record").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError):
            schema = None
        validator = jsonschema.Draft202012Validator(schema) if schema else None

        records: list[dict[str, Any]] = []
        for theme, group in groups.items():
            supporting: list[dict[str, Any]] = []
            for story in group:
                excerpt = _supporting_excerpt(story)
                if excerpt is not None:
                    supporting.append(excerpt)
            if not supporting:
                continue
            description = group[0].get("story_summary") or theme
            if len(description) < 20:
                description = (
                    f"Tier-1 theme '{theme}' supported by {len(group)} "
                    f"promoted stories from this source."
                )
            record = {
                "theme_id": str(uuid.uuid4()),
                "theme_name": theme,
                "description": description,
                "source_story_ids": [str(s["story_id"]) for s in group],
                "source_ids": sorted({str(s["source_id"]) for s in group}),
                "supporting_excerpts": supporting,
                "status": "candidate",
                "created_at": _now_iso(),
            }
            if validator is not None:
                try:
                    validator.validate(record)
                except jsonschema.ValidationError as exc:
                    _LOG.warning(
                        "theme_record schema violation for %r: %s",
                        theme,
                        exc.message,
                    )
                    continue
            records.append(record)

        out_path = processed_dir / "knowledge" / "themes.jsonl"
        _write_jsonl(out_path, records)
        return {"status": "success", "theme_count": len(records), "reason": ""}

    def synthesize_analogies(
        self, source_id: str, repo_root: str
    ) -> dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        processed_dir, _ = find_processed_dir(repo_root_path, source_id)
        if processed_dir is None:
            return {"status": "failure", "analogy_count": 0, "reason": "source_not_found"}

        sources_with_stories = _all_sources_with_promoted_stories(repo_root_path)
        if len(sources_with_stories) < 2:
            _LOG.info(
                "analogy synthesis skipped: only %d source(s) have promoted stories",
                len(sources_with_stories),
            )
            return {
                "status": "success",
                "analogy_count": 0,
                "reason": "insufficient_sources",
            }

        local_stories = _read_promoted_stories(processed_dir)
        if not local_stories:
            return {"status": "success", "analogy_count": 0, "reason": "no_local_stories"}

        try:
            schema = json.loads(
                schema_path("analogy_record").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError):
            schema = None
        validator = jsonschema.Draft202012Validator(schema) if schema else None

        records: list[dict[str, Any]] = []
        for other_source_id, other_dir in sources_with_stories:
            if other_source_id == source_id:
                continue
            other_stories = _read_promoted_stories(other_dir)
            for local in local_stories:
                ltheme = (local.get("possible_theme") or "").strip()
                ltier = local.get("tier_guess")
                if not ltheme or not ltier:
                    continue
                for other in other_stories:
                    otheme = (other.get("possible_theme") or "").strip()
                    if not _themes_match(ltheme, otheme):
                        continue
                    if other.get("tier_guess") != ltier:
                        continue
                    excerpts = [
                        _supporting_excerpt(local),
                        _supporting_excerpt(other),
                    ]
                    excerpts = [e for e in excerpts if e is not None]
                    if len(excerpts) < 2:
                        continue
                    record = {
                        "analogy_id": str(uuid.uuid4()),
                        "analogy_name": f"{ltheme} ↔ {otheme}",
                        "description": (
                            f"Cross-source analogy at tier {ltier}: "
                            f"{ltheme!r} matches {otheme!r}."
                        ),
                        "source_story_ids": [
                            str(local["story_id"]),
                            str(other["story_id"]),
                        ],
                        "source_ids": sorted(
                            {
                                str(local["source_id"]),
                                str(other["source_id"]),
                            }
                        ),
                        "supporting_excerpts": excerpts,
                        "status": "candidate",
                        "created_at": _now_iso(),
                    }
                    if validator is not None:
                        try:
                            validator.validate(record)
                        except jsonschema.ValidationError as exc:
                            _LOG.warning(
                                "analogy_record schema violation: %s", exc.message
                            )
                            continue
                    records.append(record)

        out_path = processed_dir / "knowledge" / "analogies.jsonl"
        _write_jsonl(out_path, records)
        return {"status": "success", "analogy_count": len(records), "reason": ""}


def _all_sources_with_promoted_stories(
    repo_root: Path,
) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for family in SOURCE_FAMILIES:
        family_dir = repo_root / "processed" / family
        if not family_dir.is_dir():
            continue
        for source_dir in sorted(family_dir.iterdir()):
            if not source_dir.is_dir():
                continue
            promoted_dir = source_dir / "stories" / "promoted"
            if not promoted_dir.is_dir():
                continue
            if any(promoted_dir.glob("*.json")):
                out.append((source_dir.name, source_dir))
    return out


def _themes_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    # Partial match: a 3+ word phrase shared between the two themes.
    a_words = a.lower().split()
    b_words = b.lower().split()
    if len(a_words) < 3 or len(b_words) < 3:
        return False
    for i in range(len(a_words) - 2):
        phrase = " ".join(a_words[i : i + 3])
        if phrase in " ".join(b_words):
            return True
    return False
