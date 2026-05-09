"""SynthesisReviewGateway: human review gate for report + keynote drafts.

Mirrors the Phase C ``StoryReviewGateway`` pattern (FINDING-F-006). The
form structure, polling rules, and approve/revise/reject decision
vocabulary are identical. Approved synthesis runs flip the status of
report_draft and keynote_scaffold to ``approved`` (or ``rejected``).
"""
from __future__ import annotations

import datetime
import json
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..obsidian_bridge import _frontmatter
from ._paths import synthesis_run_dir


SYNTHESIS_REVIEW_FORM_TEMPLATE = """---
run_id: "{run_id}"
review_type: "{review_type}"
audience: "{audience}"
purpose: "{purpose}"
reviewer_id: ""
decision: ""
reviewed_at: ""
notes: ""
review_status: pending
ingestion_at: "{ingestion_at}"
---

# Synthesis Review: {review_type} — {run_id}

**Audience:** {audience}
**Purpose:** {purpose}

## What was generated

{summary_markdown}

---

## Report sections (if generated)

{report_summary}

---

## Keynote arc (if generated)

{keynote_summary}

---

## Cost summary

{cost_summary}

---

Set `decision` to: approve | revise | reject
Set `reviewer_id` to your reviewer identifier.
Set `review_status` to: submitted when complete.
"""


_TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _now_utc(now: Optional[Callable[[], datetime.datetime]] = None) -> datetime.datetime:
    if now is not None:
        return now()
    return datetime.datetime.utcnow()


def _summarize_report(report_draft: Optional[Dict[str, Any]]) -> str:
    if not report_draft:
        return "_no report draft generated for this run_"
    sections = report_draft.get("sections", []) or []
    grounded = sum(1 for s in sections if s.get("grounded"))
    lines = [
        f"- draft_id: `{report_draft.get('draft_id', '')}`",
        f"- bundle_hash: `{report_draft.get('bundle_hash', '')}`",
        f"- sections: {len(sections)} ({grounded} grounded)",
        f"- status: `{report_draft.get('status', '')}`",
    ]
    for section in sections:
        lines.append(
            f"  - **{section.get('section_title', '?')}** "
            f"(`{section.get('section_type', '?')}`) "
            f"— grounded: {bool(section.get('grounded'))}, "
            f"citations: {len(section.get('inline_citations', []))}, "
            f"unverified: {len(section.get('unverified_citations', []))}"
        )
    return "\n".join(lines)


def _summarize_keynote(keynote_scaffold: Optional[Dict[str, Any]]) -> str:
    if not keynote_scaffold:
        return "_no keynote scaffold generated for this run_"
    arc = keynote_scaffold.get("arc", []) or []
    lines = [
        f"- scaffold_id: `{keynote_scaffold.get('scaffold_id', '')}`",
        f"- bundle_hash: `{keynote_scaffold.get('bundle_hash', '')}`",
        f"- title: {keynote_scaffold.get('title', '')}",
        f"- arc beats: {len(arc)} ({', '.join(b.get('beat_type', '?') for b in arc)})",
        f"- status: `{keynote_scaffold.get('status', '')}`",
    ]
    return "\n".join(lines)


def _summarize_cost(cost_total: float) -> str:
    return f"- total_estimated_cost_usd: **${cost_total:.4f}**"


def _summarize_what_was_generated(
    report_draft: Optional[Dict[str, Any]],
    keynote_scaffold: Optional[Dict[str, Any]],
) -> str:
    parts: List[str] = []
    if report_draft:
        parts.append("- report_draft.json")
    if keynote_scaffold:
        parts.append("- keynote_scaffold.json")
    parts.append("- context_bundle.json")
    parts.append("- themes.jsonl")
    parts.append("- story_matrix.json")
    parts.append("- cost.jsonl")
    return "\n".join(parts) or "_nothing generated_"


class SynthesisReviewGateway:
    """Emit, poll, and finalize synthesis review forms in the Obsidian vault."""

    def emit_review_form(
        self,
        run_id: str,
        audience: str,
        purpose: str,
        report_draft: Optional[Dict[str, Any]] = None,
        keynote_scaffold: Optional[Dict[str, Any]] = None,
        cost_total: float = 0.0,
        vault_root: Optional[str] = None,
        repo_root: Optional[str] = None,
        *,
        now: Optional[Callable[[], datetime.datetime]] = None,
    ) -> str:
        if not vault_root:
            raise ValueError("vault_root is required to emit a review form")
        pending_dir = Path(vault_root) / "Reviews" / "Synthesis" / "Pending"
        pending_dir.mkdir(parents=True, exist_ok=True)
        target = pending_dir / f"{run_id}_review.md"

        review_type = purpose
        rendered = SYNTHESIS_REVIEW_FORM_TEMPLATE.format(
            run_id=run_id,
            review_type=review_type,
            audience=audience,
            purpose=purpose,
            ingestion_at=_now_utc(now).strftime(_TIMESTAMP_FMT),
            summary_markdown=_summarize_what_was_generated(
                report_draft, keynote_scaffold
            ),
            report_summary=_summarize_report(report_draft),
            keynote_summary=_summarize_keynote(keynote_scaffold),
            cost_summary=_summarize_cost(float(cost_total)),
        )
        target.write_text(rendered, encoding="utf-8")
        return str(target)

    def poll_for_completion(
        self,
        run_id: str,
        vault_root: str,
        repo_root: str,
        *,
        timeout_hours: int = 72,
        now: Optional[Callable[[], datetime.datetime]] = None,
    ) -> Dict[str, Any]:
        review_path = (
            Path(vault_root) / "Reviews" / "Synthesis" / "Pending"
            / f"{run_id}_review.md"
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
                review_path,
                frontmatter,
                run_id=run_id,
                vault_root=vault_root,
                repo_root=repo_root,
                now=now,
                timeout_hours=timeout_hours,
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
            run_id=run_id,
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
        run_id: str,
        vault_root: str,
        repo_root: str,
        now: Optional[Callable[[], datetime.datetime]],
        timeout_hours: int,
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
            self._update_artifacts_status(
                run_id=run_id,
                repo_root=repo_root,
                new_status="rejected",
            )
            try:
                completed_dir = (
                    Path(vault_root) / "Reviews" / "Synthesis" / "Completed"
                )
                completed_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(review_path), str(completed_dir / review_path.name))
            except OSError:
                pass
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
        run_id: str,
        vault_root: str,
        repo_root: str,
        decision: str,
        reviewer_id: str,
    ) -> Dict[str, Any]:
        if decision == "approve":
            self._update_artifacts_status(
                run_id=run_id, repo_root=repo_root, new_status="approved"
            )
        elif decision == "reject":
            self._update_artifacts_status(
                run_id=run_id, repo_root=repo_root, new_status="rejected"
            )
        # 'revise' leaves status unchanged.

        completed_dir = Path(vault_root) / "Reviews" / "Synthesis" / "Completed"
        completed_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(review_path), str(completed_dir / review_path.name))
        except OSError:
            pass

        return {"status": "complete", "decision": decision, "reason": ""}

    def _update_artifacts_status(
        self, *, run_id: str, repo_root: str, new_status: str
    ) -> None:
        run_dir = synthesis_run_dir(Path(repo_root).resolve(), run_id)
        for filename in ("report_draft.json", "keynote_scaffold.json"):
            target = run_dir / filename
            if not target.is_file():
                continue
            try:
                doc = json.loads(target.read_text(encoding="utf-8"))
                doc["status"] = new_status
                target.write_text(
                    json.dumps(doc, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            except (OSError, json.JSONDecodeError):
                continue
