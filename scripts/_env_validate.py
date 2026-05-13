"""Environment validation: assert the workspace is coherent before a
pipeline step starts.

Runs as the first step of pipeline-bearing workflows so a
misconfigured workspace fails LOUDLY in <1 second instead of producing
a confusing failure 10 minutes into a real extraction. Pure stdlib so
it can run before ``pip install -e``.

Checks:
  1. ``data-lake/`` exists and has the expected ``store/`` subtree.
  2. Required env vars are set (``ANTHROPIC_API_KEY`` by default;
     skipped when ``--skip-env`` is passed for read-only steps that
     don't make API calls).
  3. Every git-tracked artifact path in the manifest is un-ignored
     (delegates to ``scripts/_gitignore_audit.py``).

Usage:

    python scripts/_env_validate.py --data-lake data-lake/
    python scripts/_env_validate.py --data-lake data-lake/ --skip-env
    python scripts/_env_validate.py --data-lake data-lake/ --strict

Exit codes:

    0 — every check passed.
    1 — at least one check failed.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys
from typing import List

# Subdirectories we expect under ``<data_lake>/`` for any pipeline
# step. ``store/raw/`` is intentionally absent: many workflows write
# only into ``store/processed/`` or ``store/artifacts/`` and creating
# ``store/raw/`` early would mask a missing-transcript bug.
REQUIRED_DATA_LAKE_DIRS: tuple[str, ...] = (
    "store",
    "store/artifacts",
)

# Env vars required by default. ``ANTHROPIC_API_KEY`` is the only
# hard requirement for live extraction; mobile and read-only
# workflows pass ``--skip-env`` to bypass.
REQUIRED_ENV_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
)


def _here() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent


def check_data_lake(data_lake: pathlib.Path) -> List[str]:
    findings: List[str] = []
    if not data_lake.exists():
        findings.append(f"data-lake not found at {data_lake}")
        return findings
    if not data_lake.is_dir():
        findings.append(f"data-lake path is not a directory: {data_lake}")
        return findings
    for d in REQUIRED_DATA_LAKE_DIRS:
        target = data_lake / d
        if not target.exists():
            findings.append(f"missing required directory: {target}")
        elif not target.is_dir():
            findings.append(f"path exists but is not a directory: {target}")
    return findings


def check_env_vars(skip: bool) -> List[str]:
    if skip:
        return []
    return [
        f"required env var not set: {var}"
        for var in REQUIRED_ENV_VARS
        if not os.environ.get(var)
    ]


def check_gitignore_audit() -> List[str]:
    audit_script = _here() / "_gitignore_audit.py"
    if not audit_script.is_file():
        return [f"gitignore audit script not found at {audit_script}"]
    result = subprocess.run(
        [sys.executable, str(audit_script)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        details = (result.stdout + result.stderr).strip()
        return [f"gitignore audit failed:\n{details}"]
    return []


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-lake",
        default="data-lake/",
        help="Path to the data-lake (default: data-lake/)",
    )
    parser.add_argument(
        "--skip-env",
        action="store_true",
        help="Skip the env-var check (use for read-only steps)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Reserved: future warning-as-error escalation. Currently a no-op.",
    )
    args = parser.parse_args(argv)
    # ``--strict`` is reserved for a future iteration that adds
    # warning-tier checks; today every finding is already a hard
    # error so the flag is documented but ignored.
    _ = args.strict

    data_lake = pathlib.Path(args.data_lake).resolve()
    findings: List[str] = []
    findings.extend(check_data_lake(data_lake))
    findings.extend(check_env_vars(args.skip_env))
    findings.extend(check_gitignore_audit())

    if findings:
        print(
            f"ENVIRONMENT VALIDATION FAILED ({len(findings)} finding(s)):",
            file=sys.stderr,
        )
        for f in findings:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print("OK: environment validation passed")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
