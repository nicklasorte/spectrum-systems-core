"""Phase 3P negative-transfer guard tests.

The guard reads fixture comparison_result__*.json files in a tmp lake
and reports an F1 regression. Uses sentinel hashes so the test is
decoupled from the live prompt file.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "verify_fewshot_no_regression.py"
FAIL_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "few_shot_regression"
PASS_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "few_shot_regression_passing"


def _run(lake: Path, current_hash: str = "POST_HASH_SENTINEL") -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--lake",
            str(lake),
            "--current-hash",
            current_hash,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_passing_fixture_exits_zero() -> None:
    result = _run(PASS_FIXTURE)
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "PASS" in result.stdout


def test_failing_fixture_exits_one() -> None:
    result = _run(FAIL_FIXTURE)
    assert result.returncode == 1, result.stdout
    assert "FAIL" in result.stderr
    assert "source_regressed" in result.stderr


def test_failing_fixture_reports_pre_and_post() -> None:
    result = _run(FAIL_FIXTURE)
    assert "source_regressed" in result.stdout
    # The pre/post deltas are printed in the stdout summary.
    assert "pre=0.500" in result.stdout
    assert "post=0.430" in result.stdout


def test_stable_source_does_not_trigger_regression() -> None:
    """In the failing fixture both sources are scanned; the regression
    is reported only on source_regressed, not source_stable."""
    result = _run(FAIL_FIXTURE)
    # source_stable's delta is +0.010 — must NOT appear as a regression
    # in stderr (it does appear in the stdout summary).
    fail_lines = [
        ln for ln in result.stderr.splitlines()
        if "regressed" in ln.lower() or "FAIL" in ln
    ]
    found_in_fail = any("source_stable" in ln for ln in fail_lines)
    assert not found_in_fail, (
        "source_stable must not trigger the regression alarm; "
        f"fail lines: {fail_lines}"
    )


def test_missing_lake_exits_two(tmp_path: Path) -> None:
    result = _run(tmp_path / "does_not_exist")
    assert result.returncode == 2
