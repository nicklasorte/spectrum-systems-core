"""StoryReviewGateway: human review gate for Tier-1 admitted stories.

No auto-promotion. The pipeline can only progress when a human edits the
review form in the vault and sets ``review_status: submitted`` plus a
non-blank ``reviewer_id`` and a ``decision`` of approve/revise/reject.

Timeouts produce a blocked candidate (not a crash). Approved candidates
are copied to processed/<family>/<source_id>/stories/promoted/<story_id>.json
with status="promoted" — this is the single Phase C promotion path
(constitutional analogue to ``promotion/promoter.py``).
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ..obsidian_bridge import _frontmatter
from ._paths import find_processed_dir


REVIEW_FORM_TEMPLATE = """---
story_id: "{story_id}"
source_id: "{source_id}"
review_status: pending
reviewer_id: ""
decision: ""
reviewed_at: ""
notes: ""
ingestion_at: "{ingestion_at}"
---

# Story Review: {story_id}

**Theme:** {theme}
**Tier guess:** {tier_guess}
**Storyworthy score:** {score_total}/15
**Pages:** {page_numbers}

## Source Excerpt (verbatim)

> {excerpt}

## Story Summary

{summary}

## Why It Might Work

{why}

## Risk Flags

{risks}

---

Set `decision` to: approve | revise | reject
Set `reviewer_id` to your reviewer identifier.
Set `review_status` to: submitted

The pipeline resumes when `review_status == submitted` AND `reviewer_id`
is non-blank AND `decision` is one of approve / revise / reject.
"""


_TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _now_utc(now: Optional[Callable[[], datetime.datetime]] = None) -> datetime.datetime:
    if now is not None:
        return now()
    return datetime.datetime.utcnow()


class StoryReviewGateway:
    """Emit, poll, and finalize story review forms in the Obsidian vault."""

    def emit_review_form(
        self,
        story_id: str,
        candidate: Dict[str, Any],
        vault_root: str,
        *,
        now: Optional[Callable[[], datetime.datetime]] = None,
    ) -> str:
        pending_dir = Path(vault_root) / "Reviews" / "Stories" / "Pending"
        pending_dir.mkdir(parents=True, exist_ok=True)
        target = pending_dir / f"{story_id}_review.md"

        score = candidate.get("storyworthy_score", {}) or {}
        rendered = REVIEW_FORM_TEMPLATE.format(
            story_id=story_id,
            source_id=candidate.get("source_id", ""),
            ingestion_at=_now_utc(now).strftime(_TIMESTAMP_FMT),
            theme=candidate.get("possible_theme", ""),
            tier_guess=candidate.get("tier_guess", ""),
            score_total=score.get("total", 0),
            page_numbers=candidate.get("page_numbers", []),
            excerpt=str(candidate.get("source_excerpt", "")).replace("\n", "\n> "),
            summary=candidate.get("story_summary", ""),
            why=candidate.get("why_it_might_work", ""),
            risks=", ".join(candidate.get("risk_flags", []) or []) or "—",
        )
        target.write_text(rendered, encoding="utf-8")
        return str(target)

    def poll_and_promote(
        self,
        story_id: str,
        source_id: str,
        vault_root: str,
        repo_root: str,
        *,
        timeout_hours: int = 72,
        now: Optional[Callable[[], datetime.datetime]] = None,
    ) -> Dict[str, Any]:
        review_path = (
            Path(vault_root) / "Reviews" / "Stories" / "Pending"
            / f"{story_id}_review.md"
        )
        if not review_path.is_file():
            return {"status": "awaiting", "decision": "", "reason": "form_not_found"}

        try:
            raw = review_path.read_text(encoding="utf-8")
            frontmatter, _body = _frontmatter.split(raw)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            return {"status": "awaiting", "decision": "", "reason": f"unparseable: {exc}"}

        review_status = str(frontmatter.get("review_status") or "").strip()
        reviewer_id = str(frontmatter.get("reviewer_id") or "").strip()
        decision = str(frontmatter.get("decision") or "").strip()

        if review_status != "submitted":
            return self._maybe_timeout(
                review_path, frontmatter, now=now, timeout_hours=timeout_hours,
                story_id=story_id, source_id=source_id, repo_root=repo_root,
            )

        if not reviewer_id:
            return {
                "status": "awaiting",
                "decision": decision,
                "reason": "blank_reviewer_id",
            }

        if decision not in {"approve", "revise", "reject"}:
            return {
                "status": "awaiting",
                "decision": decision,
                "reason": "invalid_decision",
            }

        return self._finalize(
            review_path,
            story_id=story_id,
            source_id=source_id,
            vault_root=vault_root,
            repo_root=repo_root,
            decision=decision,
            reviewer_id=reviewer_id,
        )

    def _maybe_timeout(
        self,
        review_path: Path,
        frontmatter: Dict[str, Any],
        *,
        now: Optional[Callable[[], datetime.datetime]],
        timeout_hours: int,
        story_id: str,
        source_id: str,
        repo_root: str,
    ) -> Dict[str, Any]:
        ingestion_at = str(frontmatter.get("ingestion_at") or "").strip()
        if not ingestion_at:
            return {"status": "awaiting", "decision": "", "reason": "no_ingestion_at"}
        try:
            started = datetime.datetime.strptime(ingestion_at, _TIMESTAMP_FMT)
        except ValueError:
            return {"status": "awaiting", "decision": "", "reason": "bad_ingestion_at"}
        elapsed = _now_utc(now) - started
        if elapsed >= datetime.timedelta(hours=timeout_hours):
            self._update_candidate_status(
                source_id=source_id,
                repo_root=repo_root,
                story_id=story_id,
                new_status="blocked",
                block_reason="review_timeout",
            )
            return {
                "status": "timeout",
                "decision": "",
                "reason": "review_timeout",
            }
        return {"status": "awaiting", "decision": "", "reason": "pending"}

    def _finalize(
        self,
        review_path: Path,
        *,
        story_id: str,
        source_id: str,
        vault_root: str,
        repo_root: str,
        decision: str,
        reviewer_id: str,
    ) -> Dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        processed_dir, _ = find_processed_dir(repo_root_path, source_id)
        if processed_dir is None:
            return {
                "status": "awaiting",
                "decision": decision,
                "reason": "source_not_found_in_processed",
            }
        candidates_path = processed_dir / "stories" / "candidates.jsonl"
        if not candidates_path.is_file():
            return {
                "status": "awaiting",
                "decision": decision,
                "reason": "candidates_jsonl_missing",
            }

        candidates = []
        with candidates_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                candidates.append(json.loads(line))

        target_candidate = None
        for candidate in candidates:
            if candidate.get("story_id") == story_id:
                target_candidate = candidate
                break
        if target_candidate is None:
            return {
                "status": "awaiting",
                "decision": decision,
                "reason": "story_not_in_candidates",
            }

        if decision == "approve":
            target_candidate["status"] = "promoted"
            target_candidate["reviewer_id"] = reviewer_id
            promoted_dir = processed_dir / "stories" / "promoted"
            promoted_dir.mkdir(parents=True, exist_ok=True)
            (promoted_dir / f"{story_id}.json").write_text(
                json.dumps(target_candidate, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        elif decision == "reject":
            target_candidate["status"] = "blocked"
            existing = target_candidate.get("block_reason") or ""
            target_candidate["block_reason"] = (
                (existing + "; " if existing else "")
                + f"reviewer_rejected:{reviewer_id}"
            )
        else:  # revise
            target_candidate["status"] = "candidate"
            target_candidate["reviewer_id"] = reviewer_id

        with candidates_path.open("w", encoding="utf-8") as fh:
            for candidate in candidates:
                fh.write(
                    json.dumps(candidate, sort_keys=True, separators=(",", ":"))
                    + "\n"
                )

        completed_dir = Path(vault_root) / "Reviews" / "Stories" / "Completed"
        completed_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(review_path), str(completed_dir / review_path.name))
        except OSError:
            pass

        return {"status": "complete", "decision": decision, "reason": ""}

    def _update_candidate_status(
        self,
        *,
        source_id: str,
        repo_root: str,
        story_id: str,
        new_status: str,
        block_reason: str,
    ) -> None:
        repo_root_path = Path(repo_root).resolve()
        processed_dir, _ = find_processed_dir(repo_root_path, source_id)
        if processed_dir is None:
            return
        path = processed_dir / "stories" / "candidates.jsonl"
        if not path.is_file():
            return
        candidates = []
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    candidates.append(json.loads(line))
        except (OSError, json.JSONDecodeError):
            return
        for candidate in candidates:
            if candidate.get("story_id") == story_id:
                candidate["status"] = new_status
                if block_reason:
                    existing = candidate.get("block_reason") or ""
                    candidate["block_reason"] = (
                        (existing + "; " if existing else "") + block_reason
                    )
        try:
            with path.open("w", encoding="utf-8") as fh:
                for candidate in candidates:
                    fh.write(
                        json.dumps(
                            candidate, sort_keys=True, separators=(",", ":")
                        )
                        + "\n"
                    )
        except OSError:
            return
