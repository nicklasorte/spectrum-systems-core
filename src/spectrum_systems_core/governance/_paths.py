"""Path helpers for governance/ directory tree."""
from __future__ import annotations

from pathlib import Path


def governance_root(repo_root: str | Path) -> Path:
    return Path(repo_root).resolve() / "governance"


def audits_dir(repo_root: str | Path) -> Path:
    return governance_root(repo_root) / "audits"


def audits_index_path(repo_root: str | Path) -> Path:
    return audits_dir(repo_root) / "index.json"


def candidates_dir(repo_root: str | Path) -> Path:
    return governance_root(repo_root) / "candidates"


def candidates_archive_dir(repo_root: str | Path) -> Path:
    return candidates_dir(repo_root) / "archive"


def drift_dir(repo_root: str | Path) -> Path:
    return governance_root(repo_root) / "drift"


def skipped_runs_path(repo_root: str | Path) -> Path:
    return drift_dir(repo_root) / "skipped_runs.jsonl"


def dashboard_dir(repo_root: str | Path) -> Path:
    return governance_root(repo_root) / "dashboard"


def dashboard_latest_path(repo_root: str | Path) -> Path:
    return dashboard_dir(repo_root) / "latest.json"


def markdown_dir(repo_root: str | Path) -> Path:
    return governance_root(repo_root) / "markdown"


def ensure_governance_tree(repo_root: str | Path) -> None:
    """Create the entire governance/ tree if missing."""
    audits_dir(repo_root).mkdir(parents=True, exist_ok=True)
    candidates_archive_dir(repo_root).mkdir(parents=True, exist_ok=True)
    drift_dir(repo_root).mkdir(parents=True, exist_ok=True)
    dashboard_dir(repo_root).mkdir(parents=True, exist_ok=True)
    markdown_dir(repo_root).mkdir(parents=True, exist_ok=True)
