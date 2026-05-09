"""Path helpers for harness/ directory tree."""
from __future__ import annotations

from pathlib import Path


def harness_root(repo_root: str | Path) -> Path:
    return Path(repo_root).resolve() / "harness"


def runs_dir(repo_root: str | Path) -> Path:
    return harness_root(repo_root) / "runs"


def runs_index_path(repo_root: str | Path) -> Path:
    return runs_dir(repo_root) / "index.json"


def runs_archive_dir(repo_root: str | Path) -> Path:
    return runs_dir(repo_root) / "archive"


def evals_dir(repo_root: str | Path) -> Path:
    return harness_root(repo_root) / "evals"


def failures_dir(repo_root: str | Path) -> Path:
    return harness_root(repo_root) / "failures"


def patterns_path(repo_root: str | Path) -> Path:
    return failures_dir(repo_root) / "patterns.jsonl"


def eval_candidates_path(repo_root: str | Path) -> Path:
    return failures_dir(repo_root) / "eval_candidates.jsonl"


def pending_failures_path(repo_root: str | Path) -> Path:
    """Single-occurrence failures buffered here until they cluster (CHECK-RT3-001)."""
    return failures_dir(repo_root) / "pending_failures.jsonl"


def outcomes_dir(repo_root: str | Path) -> Path:
    return harness_root(repo_root) / "outcomes"


def outcomes_memory_path(repo_root: str | Path) -> Path:
    return outcomes_dir(repo_root) / "memory.jsonl"


def comparisons_dir(repo_root: str | Path) -> Path:
    return harness_root(repo_root) / "comparisons"


def overrides_dir(repo_root: str | Path) -> Path:
    return harness_root(repo_root) / "overrides"


def overrides_archive_dir(repo_root: str | Path) -> Path:
    return overrides_dir(repo_root) / "archive"


def entropy_dir(repo_root: str | Path) -> Path:
    return harness_root(repo_root) / "entropy"


def entropy_reports_path(repo_root: str | Path) -> Path:
    return entropy_dir(repo_root) / "reports.jsonl"


def markdown_dir(repo_root: str | Path) -> Path:
    return harness_root(repo_root) / "markdown"


def ensure_harness_tree(repo_root: str | Path) -> None:
    """Create the entire harness/ tree if missing."""
    runs_archive_dir(repo_root).mkdir(parents=True, exist_ok=True)
    evals_dir(repo_root).mkdir(parents=True, exist_ok=True)
    failures_dir(repo_root).mkdir(parents=True, exist_ok=True)
    outcomes_dir(repo_root).mkdir(parents=True, exist_ok=True)
    comparisons_dir(repo_root).mkdir(parents=True, exist_ok=True)
    overrides_archive_dir(repo_root).mkdir(parents=True, exist_ok=True)
    entropy_dir(repo_root).mkdir(parents=True, exist_ok=True)
    markdown_dir(repo_root).mkdir(parents=True, exist_ok=True)
