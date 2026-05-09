"""IssueRegistry: append-only registry of issue_record artifacts.

FINDING-D-002: each new issue is compared via Jaccard similarity against
all existing issues. Similar issues (> 0.7) are linked but never merged —
human resolves.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import jsonschema

from ..extraction._paths import find_processed_dir
from ._paths import paper_schema_path

JACCARD_THRESHOLD = 0.7


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


class IssueRegistry:
    """Append issues to issues.jsonl with similarity-based duplicate flags."""

    def add_issue(
        self,
        issue: Dict[str, Any],
        repo_root: str,
        working_paper_source_id: str,
    ) -> Dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        processed_dir, _ = find_processed_dir(
            repo_root_path, working_paper_source_id
        )
        if processed_dir is None:
            return {
                "status": "failure",
                "similar_count": 0,
                "reason": "source_not_found",
            }
        paper_dir = processed_dir / "paper"
        paper_dir.mkdir(parents=True, exist_ok=True)
        issues_path = paper_dir / "issues.jsonl"

        existing = _read_jsonl(issues_path)
        new_description = issue.get("description", "") or ""
        similar_ids: List[str] = []
        for prior in existing:
            score = self._jaccard(new_description, prior.get("description", "") or "")
            if score > JACCARD_THRESHOLD:
                pid = prior.get("issue_id")
                if isinstance(pid, str) and pid not in similar_ids:
                    similar_ids.append(pid)
        if similar_ids:
            issue["similar_issue_ids"] = similar_ids
        else:
            issue.setdefault("similar_issue_ids", [])

        try:
            schema = json.loads(
                paper_schema_path("issue_record").read_text(encoding="utf-8")
            )
            jsonschema.Draft202012Validator(schema).validate(issue)
        except (FileNotFoundError, OSError) as exc:
            return {
                "status": "failure",
                "similar_count": 0,
                "reason": f"schema_unreadable: {exc}",
            }
        except jsonschema.ValidationError as exc:
            return {
                "status": "failure",
                "similar_count": 0,
                "reason": f"schema_violation: {exc.message}",
            }

        try:
            with issues_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(issue, sort_keys=True, separators=(",", ":")) + "\n"
                )
        except OSError as exc:
            return {
                "status": "failure",
                "similar_count": 0,
                "reason": f"write_error: {exc}",
            }
        return {
            "status": "success",
            "similar_count": len(similar_ids),
            "reason": "",
        }

    def _jaccard(self, text_a: str, text_b: str) -> float:
        words_a = {w.lower() for w in text_a.split() if len(w) > 3}
        words_b = {w.lower() for w in text_b.split() if len(w) > 3}
        if not words_a or not words_b:
            return 0.0
        return len(words_a & words_b) / len(words_a | words_b)

    def get_all(
        self, working_paper_source_id: str, repo_root: str
    ) -> List[Dict[str, Any]]:
        repo_root_path = Path(repo_root).resolve()
        processed_dir, _ = find_processed_dir(
            repo_root_path, working_paper_source_id
        )
        if processed_dir is None:
            return []
        return _read_jsonl(processed_dir / "paper" / "issues.jsonl")

    def write_issues_projection(
        self, working_paper_source_id: str, repo_root: str
    ) -> str:
        # Delegate to ObsidianProjection for projection authoring.
        from ..ingestion.obsidian_projection import ObsidianProjection

        issues = self.get_all(working_paper_source_id, repo_root)
        return ObsidianProjection().write_paper_issues_projection(
            working_paper_source_id, issues, repo_root
        )
