"""``python -m spectrum_systems_core.health <subcommand>``.

Thin CLI dispatcher used by the GitHub Actions workflows. Provides
the three workflow-callable entry points:

* ``preflight``       — feature-flag presence check (run-pipeline).
* ``upstream-gate``   — Class 1+2 eval gating (eval-ground-truth).
* ``smoke-filter``    — Class 6 path-filter guard (smoke-test).

Each subcommand exits non-zero on any halt finding so the workflow
step fails the build.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .eval_integrity import (
    EVAL_INTEGRITY_ENV_VAR,
    append_github_summary,
    eval_integrity_enabled,
    evaluate_upstream,
    persist_findings,
)
from .preflight import main as preflight_main
from .smoke_filter import main as smoke_filter_main

_LOG = logging.getLogger("spectrum_systems_core.health")


def _upstream_gate(args: argparse.Namespace) -> int:
    """Class 1+2 entry point.

    Runs ``evaluate_upstream`` and exits non-zero if the eval should
    not run. Writes the BLOCKED message to ``GITHUB_STEP_SUMMARY`` so
    the operator sees the cause instead of a misleading ``0.000``.

    When ``EVAL_INTEGRITY_ENABLED=false`` the check is bypassed (a
    warning is logged) and the entry point exits 0.
    """
    if not eval_integrity_enabled():
        print(
            "warning: eval integrity bypassed via "
            f"{EVAL_INTEGRITY_ENV_VAR}=false",
            file=sys.stderr,
        )
        return 0

    if not args.data_lake:
        print("error: --data-lake required", file=sys.stderr)
        return 2
    if not args.pipeline_run_id:
        print(
            "error: --pipeline-run-id required (resolve from orchestration "
            "record before invoking)",
            file=sys.stderr,
        )
        return 2

    findings, should_run = evaluate_upstream(
        args.pipeline_run_id, args.data_lake
    )
    persist_findings(findings, data_lake_path=args.data_lake)
    blocked_message: str | None = None
    if not should_run:
        blocked_message = (
            "Eval blocked: synthesize failed upstream. Fix synthesize "
            "before scoring."
        )
    append_github_summary(findings, blocked_message=blocked_message)

    if not should_run:
        return 1
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m spectrum_systems_core.health",
        description="Automated silent-failure detection.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pre = sub.add_parser("preflight", help="Feature-flag presence check.")
    pre.add_argument("--data-lake", default=os.environ.get("DATA_LAKE_PATH", ""))
    pre.add_argument("--pipeline-run-id", default=os.environ.get("PIPELINE_RUN_ID"))

    up = sub.add_parser(
        "upstream-gate",
        help="Block eval if synthesize failed upstream (Classes 1+2).",
    )
    up.add_argument("--data-lake", default=os.environ.get("DATA_LAKE_PATH", ""))
    up.add_argument(
        "--pipeline-run-id", default=os.environ.get("PIPELINE_RUN_ID")
    )

    sf = sub.add_parser(
        "smoke-filter",
        help="Fail if smoke-test workflow has a pull_request path filter.",
    )
    sf.add_argument("--repo-root", default=".")

    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "preflight":
        return preflight_main(
            ["--data-lake", args.data_lake]
            + (
                ["--pipeline-run-id", args.pipeline_run_id]
                if args.pipeline_run_id
                else []
            )
        )
    if args.cmd == "upstream-gate":
        return _upstream_gate(args)
    if args.cmd == "smoke-filter":
        return smoke_filter_main(["--repo-root", args.repo_root])
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
