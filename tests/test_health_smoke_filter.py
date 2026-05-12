"""Class 6: smoke-test workflow path-filter guard."""
from __future__ import annotations

from pathlib import Path

from spectrum_systems_core.health.smoke_filter import (
    detect_smoke_test_path_filter,
    main,
)


def _write_workflow(repo_root: Path, body: str) -> None:
    target = repo_root / ".github" / "workflows" / "smoke-test.yml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def test_no_path_filter_returns_none(tmp_path: Path) -> None:
    _write_workflow(
        tmp_path,
        """
name: Smoke
on:
  pull_request:
    branches: [main]
  workflow_dispatch:
jobs:
  smoke:
    runs-on: ubuntu-latest
    steps: []
""",
    )
    assert detect_smoke_test_path_filter(tmp_path) is None


def test_paths_filter_emits_halt(tmp_path: Path) -> None:
    _write_workflow(
        tmp_path,
        """
name: Smoke
on:
  pull_request:
    paths:
      - src/**
jobs:
  smoke:
    runs-on: ubuntu-latest
    steps: []
""",
    )
    finding = detect_smoke_test_path_filter(tmp_path)
    assert finding is not None
    assert finding.finding_code == "smoke_test_skipped"
    assert finding.severity == "halt"
    assert finding.context["filter_key"] == "paths"


def test_paths_ignore_filter_also_halts(tmp_path: Path) -> None:
    _write_workflow(
        tmp_path,
        """
name: Smoke
on:
  pull_request:
    paths-ignore:
      - docs/**
jobs:
  smoke:
    runs-on: ubuntu-latest
    steps: []
""",
    )
    finding = detect_smoke_test_path_filter(tmp_path)
    assert finding is not None
    assert finding.context["filter_key"] == "paths-ignore"


def test_missing_workflow_file_halts(tmp_path: Path) -> None:
    finding = detect_smoke_test_path_filter(tmp_path)
    assert finding is not None
    assert finding.severity == "halt"
    assert finding.context["reason"] == "workflow_missing"


def test_cli_exits_nonzero_on_filter(tmp_path: Path) -> None:
    _write_workflow(
        tmp_path,
        """
name: Smoke
on:
  pull_request:
    paths: [src/**]
jobs:
  smoke:
    runs-on: ubuntu-latest
    steps: []
""",
    )
    rc = main(["--repo-root", str(tmp_path)])
    assert rc == 1


def test_cli_exits_zero_when_clean(tmp_path: Path) -> None:
    _write_workflow(
        tmp_path,
        """
name: Smoke
on:
  pull_request:
    branches: [main]
jobs:
  smoke:
    runs-on: ubuntu-latest
    steps: []
""",
    )
    rc = main(["--repo-root", str(tmp_path)])
    assert rc == 0


def test_real_repo_has_no_path_filter() -> None:
    """The shipped smoke-test workflow must pass its own guard."""
    repo_root = Path(__file__).resolve().parents[1]
    finding = detect_smoke_test_path_filter(repo_root)
    assert finding is None, (
        f"smoke-test.yml in this repo declares a path filter: "
        f"{finding.context if finding else None}"
    )
