"""Shared fixtures for harness tests. Pure helpers, no LLM calls."""
from __future__ import annotations

import datetime
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List


def utcnow_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def write_synthesis_run(
    repo_root: Path,
    run_id: str | None = None,
    *,
    audience: str = "policy",
    purpose: str = "report",
    grounded_sections: int = 2,
    ungrounded_sections: int = 0,
    arc_beats: int = 3,
    cost_usd: float = 0.0123,
    started_at: str | None = None,
    completed_at: str | None = None,
) -> str:
    """Write a complete synthesis/<run_id>/ directory with manifest, report, scaffold."""
    run_id = run_id or str(uuid.uuid4())
    run_dir = repo_root / "synthesis" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    started = started_at or utcnow_iso()
    completed = completed_at or utcnow_iso()
    manifest = {
        "run_id": run_id,
        "audience": audience,
        "purpose": purpose,
        "source_ids_included": ["src-A", "src-B"],
        "story_ids_included": [],
        "claim_ids_included": [],
        "theme_ids_included": [],
        "total_input_tokens": 1000,
        "total_output_tokens": 500,
        "total_estimated_cost_usd": cost_usd,
        "started_at": started,
        "completed_at": completed,
    }
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    sections: List[Dict[str, Any]] = []
    for i in range(grounded_sections):
        sections.append(
            {
                "section_title": f"section_{i}",
                "section_type": "context",
                "content": "...",
                "grounded": True,
                "inline_citations": ["c-1"],
                "unverified_citations": [],
            }
        )
    for i in range(ungrounded_sections):
        sections.append(
            {
                "section_title": f"un_section_{i}",
                "section_type": "context",
                "content": "...",
                "grounded": False,
                "inline_citations": [],
                "unverified_citations": ["bad-c-1"],
            }
        )
    if purpose in ("report", "both"):
        report_draft = {
            "draft_id": str(uuid.uuid4()),
            "run_id": run_id,
            "bundle_id": "bundle-x",
            "bundle_hash": "sha256:abc",
            "audience": audience,
            "status": "draft",
            "title": "Test Report",
            "sections": sections,
        }
        (run_dir / "report_draft.json").write_text(
            json.dumps(report_draft, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    if purpose in ("keynote", "both"):
        scaffold = {
            "scaffold_id": str(uuid.uuid4()),
            "run_id": run_id,
            "bundle_id": "bundle-x",
            "bundle_hash": "sha256:abc",
            "audience": audience,
            "status": "draft",
            "title": "Test Keynote",
            "central_tension": "...",
            "opener": {
                "story_id": "story-x",
                "hook_text": "...",
                "why_this_story": "...",
            },
            "arc": [
                {"beat_type": "context", "content": f"beat_{i}"}
                for i in range(arc_beats)
            ],
            "closing_call_to_action": "do something",
        }
        (run_dir / "keynote_scaffold.json").write_text(
            json.dumps(scaffold, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    return run_id


def make_failure(
    reason_code: str = "ungrounded_section",
    detail: str = "section context_a was missing inline citation evidence",
) -> Dict[str, Any]:
    return {"reason_code": reason_code, "failure_detail": detail}


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    out: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out
