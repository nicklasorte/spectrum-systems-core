"""Smoke-test workflow path-filter guard (Class 6).

The extraction smoke test must run on every PR. A ``paths:`` filter
on the smoke-test workflow's ``pull_request`` trigger can silently
skip the test on PRs that don't touch extraction code — schema or
governance changes then ship green.

This module exposes :func:`detect_smoke_test_path_filter` and a CLI
that the smoke-test workflow itself calls. If a filter is present
the helper emits a ``smoke_test_skipped`` halt finding so the check
fails even when the workflow ran.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

import yaml

from .finding import HealthFinding

SMOKE_WORKFLOW_REL_PATH: str = ".github/workflows/smoke-test.yml"


def _load_workflow(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _on_block(workflow: dict[str, Any]) -> dict[str, Any]:
    """Return the ``on:`` block as a normalised dict.

    PyYAML parses the bare key ``on`` as Python ``True`` because
    YAML 1.1 treats ``on`` as a truthy boolean. We accept either key.
    """
    if "on" in workflow:
        block = workflow["on"]
    elif True in workflow:
        block = workflow[True]
    else:
        return {}
    if isinstance(block, dict):
        return block
    if isinstance(block, list):
        return {key: {} for key in block}
    return {}


def detect_smoke_test_path_filter(
    repo_root: Path | str,
) -> Optional[HealthFinding]:
    """Return a halt finding if smoke-test.yml has a ``paths:`` filter.

    Returns ``None`` when the file does not exist (caller decides
    whether to treat that as an error) or when no filter is present.
    """
    workflow_path = Path(repo_root) / SMOKE_WORKFLOW_REL_PATH
    if not workflow_path.is_file():
        return HealthFinding(
            finding_code="smoke_test_skipped",
            severity="halt",
            context={
                "workflow_path": str(workflow_path),
                "reason": "workflow_missing",
            },
            remediation=(
                "Smoke-test workflow file is missing. Restore "
                ".github/workflows/smoke-test.yml from main; the "
                "smoke test is a release gate."
            ),
        )

    try:
        workflow = _load_workflow(workflow_path)
    except yaml.YAMLError as exc:
        return HealthFinding(
            finding_code="smoke_test_skipped",
            severity="halt",
            context={
                "workflow_path": str(workflow_path),
                "reason": "yaml_parse_error",
                "error": str(exc),
            },
            remediation=(
                "Smoke-test workflow YAML is malformed. Fix the file "
                "so the smoke test can run."
            ),
        )

    on_block = _on_block(workflow)
    pr_block = on_block.get("pull_request")
    if isinstance(pr_block, dict) and "paths" in pr_block:
        return HealthFinding(
            finding_code="smoke_test_skipped",
            severity="halt",
            context={
                "workflow_path": str(workflow_path),
                "filter_key": "paths",
                "filter_value": pr_block.get("paths"),
            },
            remediation=(
                "Remove the 'paths:' filter from the smoke-test "
                "workflow's pull_request trigger. The smoke test must "
                "run on every PR — a 60-second cost does not justify "
                "skipping it on schema/governance changes."
            ),
        )
    if isinstance(pr_block, dict) and "paths-ignore" in pr_block:
        return HealthFinding(
            finding_code="smoke_test_skipped",
            severity="halt",
            context={
                "workflow_path": str(workflow_path),
                "filter_key": "paths-ignore",
                "filter_value": pr_block.get("paths-ignore"),
            },
            remediation=(
                "Remove the 'paths-ignore:' filter from the smoke-test "
                "workflow's pull_request trigger."
            ),
        )
    return None


def main(argv: list[str] | None = None) -> int:
    """CLI: exit 0 if the smoke workflow is unfiltered, 1 otherwise."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m spectrum_systems_core.health.smoke_filter",
        description=(
            "Fail if the extraction smoke-test workflow declares a "
            "pull_request path filter."
        ),
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Path to the repository root (default: cwd).",
    )
    args = parser.parse_args(argv)
    finding = detect_smoke_test_path_filter(args.repo_root)
    if finding is None:
        print("smoke_filter: OK — no path filter on smoke-test workflow.")
        return 0
    print(
        f"smoke_filter: HALT — {finding.finding_code}: "
        f"{finding.context} -- {finding.remediation}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
