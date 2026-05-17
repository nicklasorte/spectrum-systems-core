"""RunHistoryStore — rolling 90-day window of synthesis run history.

FINDING-G-001: bounded growth. Active index is capped at MAX_ACTIVE_RUN_HISTORY
entries and the retention window is RUN_HISTORY_RETENTION_DAYS days. Older
runs move to harness/runs/archive/<run_id>.json (append-only) and are removed
from index.json. Archive is never read by active pipeline code.

All methods fail-closed: errors return {"status": "failure", ...} dicts and
never raise — the synthesis pipeline must not depend on the memory layer.
"""
from __future__ import annotations

import datetime
import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

from . import MAX_ACTIVE_RUN_HISTORY, RUN_HISTORY_RETENTION_DAYS
from ._io import (
    parse_iso,
    read_json,
    utcnow_iso,
    write_json,
)
from ._paths import (
    ensure_harness_tree,
    runs_archive_dir,
    runs_index_path,
)
from ._schema import validate_harness_artifact

_LOG = logging.getLogger(__name__)


def _empty_index() -> dict[str, Any]:
    return {"last_archived_at": None, "runs": []}


def _load_index(repo_root: str | Path) -> dict[str, Any]:
    idx_path = runs_index_path(repo_root)
    data = read_json(idx_path)
    if not isinstance(data, dict) or "runs" not in data:
        return _empty_index()
    if not isinstance(data.get("runs"), list):
        return _empty_index()
    return data


def _summarize_synthesis_outcome(
    manifest: dict[str, Any],
    report_draft: dict[str, Any] | None,
    keynote_scaffold: dict[str, Any] | None,
) -> tuple[str, list[str]]:
    """Return (outcome, block_reason_codes)."""
    if not manifest.get("completed_at"):
        return "failure", ["incomplete_run"]

    block_codes: list[str] = []
    sections = (report_draft or {}).get("sections", []) or []
    if any(s.get("unverified_citations") for s in sections):
        block_codes.append("unverified_citations_present")
    if any(not s.get("grounded") for s in sections):
        block_codes.append("ungrounded_sections_present")
    if (keynote_scaffold or {}).get("status") == "blocked":
        block_codes.append("keynote_blocked")

    if block_codes:
        return "blocked", block_codes
    return "success", []


def _count_eval_results(
    report_draft: dict[str, Any] | None,
    keynote_scaffold: dict[str, Any] | None,
) -> tuple[int, int, int]:
    """Heuristic from grounded section flags. Returns (pass, fail, warn)."""
    sections = (report_draft or {}).get("sections", []) or []
    grounded_pass = sum(1 for s in sections if s.get("grounded"))
    grounded_fail = sum(1 for s in sections if not s.get("grounded"))
    arc = (keynote_scaffold or {}).get("arc", []) or []
    if arc and (keynote_scaffold or {}).get("status") not in {"blocked", "rejected"}:
        grounded_pass += 1
    elif arc:
        grounded_fail += 1
    return grounded_pass, grounded_fail, 0


class RunHistoryStore:
    def record_run(
        self,
        run_manifest: dict[str, Any],
        repo_root: str | Path,
    ) -> dict[str, Any]:
        """Record a completed run into harness/runs/index.json. Never raises."""
        try:
            ensure_harness_tree(repo_root)
            run_id = run_manifest.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                return {
                    "status": "failure",
                    "entry_id": "",
                    "reason": "missing_run_id",
                }

            run_dir = Path(repo_root).resolve() / "synthesis" / run_id
            report_draft = read_json(run_dir / "report_draft.json")
            keynote_scaffold = read_json(run_dir / "keynote_scaffold.json")

            outcome, block_codes = _summarize_synthesis_outcome(
                run_manifest, report_draft, keynote_scaffold
            )
            pass_count, fail_count, warn_count = _count_eval_results(
                report_draft, keynote_scaffold
            )

            artifact_ids: list[str] = []
            if report_draft and report_draft.get("draft_id"):
                artifact_ids.append(str(report_draft["draft_id"]))
            if keynote_scaffold and keynote_scaffold.get("scaffold_id"):
                artifact_ids.append(str(keynote_scaffold["scaffold_id"]))

            entry: dict[str, Any] = {
                "entry_id": str(uuid.uuid4()),
                "run_id": run_id,
                "run_type": "synthesis",
                "source_ids": list(run_manifest.get("source_ids_included", []) or []),
                "audience": run_manifest.get("audience"),
                "purpose": run_manifest.get("purpose"),
                "started_at": run_manifest.get("started_at") or utcnow_iso(),
                "completed_at": run_manifest.get("completed_at"),
                "outcome": outcome,
                "eval_pass_count": int(pass_count),
                "eval_fail_count": int(fail_count),
                "eval_warn_count": int(warn_count),
                "block_reason_codes": block_codes,
                "total_cost_usd": float(
                    run_manifest.get("total_estimated_cost_usd", 0.0) or 0.0
                ),
                "artifact_ids_produced": artifact_ids,
                "recorded_at": utcnow_iso(),
            }

            ok, err = validate_harness_artifact(entry, "run_history_entry")
            if not ok:
                return {
                    "status": "failure",
                    "entry_id": "",
                    "reason": f"schema_violation: {err}",
                }

            index = _load_index(repo_root)
            index["runs"].append(entry)
            write_json(runs_index_path(repo_root), index)
            self._apply_retention(repo_root)

            return {
                "status": "success",
                "entry_id": entry["entry_id"],
                "reason": "",
            }
        except Exception as exc:  # pragma: no cover — fail-closed catch-all
            _LOG.warning("RunHistoryStore.record_run failed: %s", exc)
            return {
                "status": "failure",
                "entry_id": "",
                "reason": f"unexpected_error: {exc}",
            }

    def _apply_retention(self, repo_root: str | Path) -> None:
        """Trim and archive. Never raises."""
        try:
            index = _load_index(repo_root)
            runs: list[dict[str, Any]] = list(index.get("runs", []))
            now = datetime.datetime.now(datetime.timezone.utc)
            cutoff = now - datetime.timedelta(days=RUN_HISTORY_RETENTION_DAYS)

            keep: list[dict[str, Any]] = []
            archived_any = False
            for entry in runs:
                completed = parse_iso(entry.get("completed_at"))
                if completed is not None and completed < cutoff:
                    self._archive_entry(entry, repo_root)
                    archived_any = True
                else:
                    keep.append(entry)

            if len(keep) > MAX_ACTIVE_RUN_HISTORY:
                # Drop oldest by recorded_at first.
                keep.sort(
                    key=lambda e: e.get("recorded_at") or e.get("started_at") or ""
                )
                overflow = keep[: len(keep) - MAX_ACTIVE_RUN_HISTORY]
                keep = keep[len(keep) - MAX_ACTIVE_RUN_HISTORY:]
                for entry in overflow:
                    self._archive_entry(entry, repo_root)
                    archived_any = True

            if archived_any:
                index["last_archived_at"] = utcnow_iso()
            index["runs"] = keep
            write_json(runs_index_path(repo_root), index)
        except Exception as exc:  # pragma: no cover
            _LOG.warning("RunHistoryStore._apply_retention failed: %s", exc)
            print(
                f"warning: harness run-history retention failed: {exc}",
                file=sys.stderr,
            )

    def _archive_entry(self, entry: dict[str, Any], repo_root: str | Path) -> None:
        archive_dir = runs_archive_dir(repo_root)
        archive_dir.mkdir(parents=True, exist_ok=True)
        run_id = entry.get("run_id") or entry.get("entry_id") or str(uuid.uuid4())
        target = archive_dir / f"{run_id}.json"
        # Try to capture the full synthesis run_manifest if it exists.
        run_dir = Path(repo_root).resolve() / "synthesis" / run_id
        full_manifest = read_json(run_dir / "run_manifest.json")
        archived = {
            "history_entry": entry,
            "run_manifest": full_manifest,
            "archived_at": utcnow_iso(),
        }
        try:
            target.write_text(
                json.dumps(archived, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:  # pragma: no cover
            _LOG.warning("archive write failed for %s: %s", run_id, exc)

    def get_recent_runs(
        self, repo_root: str | Path, n: int = 10
    ) -> list[dict[str, Any]]:
        index = _load_index(repo_root)
        runs = list(index.get("runs", []))
        runs.sort(
            key=lambda e: e.get("recorded_at") or e.get("started_at") or "",
            reverse=True,
        )
        return runs[: max(0, int(n))]

    def get_runs_by_outcome(
        self, outcome: str, repo_root: str | Path
    ) -> list[dict[str, Any]]:
        index = _load_index(repo_root)
        return [e for e in index.get("runs", []) if e.get("outcome") == outcome]

    def write_run_history_projection(
        self,
        repo_root: str | Path,
        vault_root: str | Path | None = None,
    ) -> str:
        from ..ingestion.obsidian_projection import ObsidianProjection

        return ObsidianProjection().write_run_history_projection(
            repo_root, vault_root
        )
