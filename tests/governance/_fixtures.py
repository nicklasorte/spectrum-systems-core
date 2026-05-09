"""Shared fixtures for governance tests. Deterministic. No LLM calls."""
from __future__ import annotations

import datetime
import json
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List


def utcnow_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def iso_days_ago(days: int) -> str:
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=days
    )
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


_REPO_ROOT = Path(__file__).resolve().parents[2]


def stage_minimal_repo(tmp_root: Path) -> Path:
    """Copy contracts/ + pyproject.toml so schema lookups work."""
    contracts_src = _REPO_ROOT / "contracts"
    contracts_dst = tmp_root / "contracts"
    if contracts_src.is_dir():
        shutil.copytree(contracts_src, contracts_dst, dirs_exist_ok=True)
    pyproject = _REPO_ROOT / "pyproject.toml"
    if pyproject.is_file():
        shutil.copy(pyproject, tmp_root / "pyproject.toml")
    return tmp_root


def stage_full_repo_copy(tmp_root: Path) -> Path:
    """Copy contracts/ + src/ + pyproject.toml — for whole-codebase scanners."""
    stage_minimal_repo(tmp_root)
    src_src = _REPO_ROOT / "src"
    src_dst = tmp_root / "src"
    if src_src.is_dir():
        shutil.copytree(src_src, src_dst, dirs_exist_ok=True)
    return tmp_root


def write_run_history(
    repo_root: Path,
    entries: List[Dict[str, Any]],
) -> None:
    runs_dir = repo_root / "harness" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    index = {"runs": entries, "last_archived_at": None}
    (runs_dir / "index.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def make_run_entry(
    *,
    audience: str | None = "policy",
    purpose: str | None = "report",
    recipe_id: str | None = "default_report_v1",
    outcome: str = "success",
    cost_usd: float = 0.01,
    days_ago: int = 0,
) -> Dict[str, Any]:
    iso = iso_days_ago(days_ago)
    return {
        "entry_id": str(uuid.uuid4()),
        "run_id": str(uuid.uuid4()),
        "run_type": "synthesis",
        "task_type": purpose,
        "recipe_id": recipe_id,
        "source_ids": ["src-a"],
        "audience": audience,
        "purpose": purpose,
        "started_at": iso,
        "completed_at": iso,
        "outcome": outcome,
        "eval_pass_count": 1 if outcome == "success" else 0,
        "eval_fail_count": 0 if outcome == "success" else 1,
        "eval_warn_count": 0,
        "block_reason_codes": [] if outcome == "success" else ["x"],
        "total_cost_usd": cost_usd,
        "artifact_ids_produced": [],
        "recorded_at": iso,
    }


def write_eval_history(
    repo_root: Path,
    artifact_type: str,
    eval_name: str,
    statuses: List[str],
) -> None:
    """Write harness/evals/<artifact_type>_history.jsonl entries."""
    target = repo_root / "harness" / "evals" / f"{artifact_type}_history.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    iso = utcnow_iso()
    with target.open("a", encoding="utf-8") as fh:
        for status in statuses:
            entry = {
                "run_id": str(uuid.uuid4()),
                "artifact_type": artifact_type,
                "eval_name": eval_name,
                "status": status,
                "score": None,
                "recorded_at": iso,
            }
            fh.write(
                json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n"
            )


def write_override(
    repo_root: Path,
    *,
    decision_context: str,
    overridden_eval_or_block: str,
    expires_days: int = 365,
) -> str:
    overrides_dir = repo_root / "harness" / "overrides"
    overrides_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    expires_at = now + datetime.timedelta(days=expires_days)
    override = {
        "override_id": str(uuid.uuid4()),
        "decision_context": decision_context,
        "overridden_artifact_id": str(uuid.uuid4()),
        "overridden_eval_or_block": overridden_eval_or_block,
        "rationale": "test",
        "overriding_human_id": "tester",
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "superseded_by": None,
        "status": "active",
    }
    target = overrides_dir / f"{override['override_id']}.json"
    target.write_text(
        json.dumps(override, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return override["override_id"]


def write_py_file(repo_root: Path, rel_path: str, content: str) -> Path:
    target = repo_root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target
