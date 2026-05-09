"""StoryEval: deterministic evals on extracted story candidates.

Runs after StoryExtractor. No LLM. Updates each candidate's grounded /
status fields in place and rewrites candidates.jsonl. Block reasons are
recorded as plain strings for debuggability (FINDING-C-001 / RT5-002).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import jsonschema

from ..ingestion._paths import schema_path
from ..ingestion.grounding import GroundingHelper
from ._paths import find_processed_dir


_VALID_TIERS = {"tier_1", "tier_2", "tier_3"}


def _set_block(candidate: Dict[str, Any], reason: str) -> None:
    candidate["status"] = "blocked"
    existing = candidate.get("block_reason")
    if isinstance(existing, str) and existing:
        candidate["block_reason"] = existing + "; " + reason
    else:
        candidate["block_reason"] = reason


class StoryEval:
    """Run schema, grounding, tier, length, unit-id evals on candidates."""

    EVAL_IDS: Tuple[str, ...] = (
        "EVAL-STORY-001",
        "EVAL-STORY-002",
        "EVAL-STORY-003",
        "EVAL-STORY-004",
        "EVAL-STORY-005",
    )

    def __init__(self, grounding: GroundingHelper | None = None) -> None:
        self._grounding = grounding or GroundingHelper()

    def run(
        self,
        candidates: List[Dict[str, Any]],
        source_id: str,
        repo_root: str,
    ) -> Dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        processed_dir, _ = find_processed_dir(repo_root_path, source_id)

        try:
            schema = json.loads(
                schema_path("story_candidate").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError):
            schema = None
        validator = (
            jsonschema.Draft202012Validator(schema) if schema else None
        )

        blocked_count = 0
        pass_count = 0

        for candidate in candidates:
            if candidate.get("status") == "blocked":
                blocked_count += 1
                continue
            if candidate.get("status") != "candidate":
                continue

            # EVAL-STORY-001: schema_conformance
            if validator is not None:
                try:
                    validator.validate(candidate)
                except jsonschema.ValidationError as exc:
                    _set_block(candidate, f"schema_violation: {exc.message}")
                    blocked_count += 1
                    continue

            # EVAL-STORY-004: source_excerpt minimum length
            excerpt = candidate.get("source_excerpt") or ""
            if len(excerpt.strip()) < 10:
                _set_block(candidate, "source_excerpt_too_short")
                blocked_count += 1
                continue

            # EVAL-STORY-005: unit_ids non-empty
            if not candidate.get("unit_ids"):
                _set_block(candidate, "unit_ids_empty")
                blocked_count += 1
                continue

            # EVAL-STORY-003: tier_guess valid
            if candidate.get("tier_guess") not in _VALID_TIERS:
                _set_block(candidate, "tier_guess_invalid")
                blocked_count += 1
                continue

            # EVAL-STORY-002: grounding (FINDING-C-001 fix). Failures default
            # to grounded=False so any exception in the grounding helper means
            # the candidate is blocked, not silently allowed (RT5-003).
            try:
                result = self._grounding.verify_excerpt(
                    excerpt, source_id, repo_root
                )
                grounded = bool(result.get("grounded"))
                matching_unit_ids = list(result.get("matching_unit_ids") or [])
            except Exception as exc:  # noqa: BLE001 — must be defensive here
                grounded = False
                matching_unit_ids = []
                _set_block(
                    candidate,
                    f"grounding_check_failed: {type(exc).__name__}: {exc}",
                )
                candidate["grounded"] = False
                candidate["grounded_unit_ids"] = []
                blocked_count += 1
                continue

            candidate["grounded"] = grounded
            candidate["grounded_unit_ids"] = matching_unit_ids
            if not grounded:
                _set_block(candidate, "excerpt_not_grounded_in_source")
                blocked_count += 1
                continue

            pass_count += 1

        # Persist updated candidates.jsonl (overwrite).
        if processed_dir is not None:
            out_path = processed_dir / "stories" / "candidates.jsonl"
            try:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with out_path.open("w", encoding="utf-8") as fh:
                    for candidate in candidates:
                        fh.write(
                            json.dumps(
                                candidate,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                            + "\n"
                        )
            except OSError:
                pass

        return {
            "evaluated_candidates": candidates,
            "blocked_count": blocked_count,
            "pass_count": pass_count,
        }
