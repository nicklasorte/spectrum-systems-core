"""Phase 3P CLI-flag tests.

Asserts the contract documented on ``--enable-few-shot``:

- Default behaviour (no flag) strips the section.
- The env var ``ENABLE_FEW_SHOT=true`` is IGNORED — the flag is
  CLI-only.
- ``--enable-few-shot`` keeps the section.
- ``--enable-few-shot`` + ``--disable-few-shot`` together is a CLI
  argument error (mutually exclusive).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "cli_few_shot.py"


def _run(args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        env=merged,
    )


def test_disabled_by_default() -> None:
    result = _run([])
    assert result.returncode == 0, result.stderr
    assert "Few-Shot Examples" not in result.stdout
    # The negative-patterns section is always present.
    assert "# Do Not Extract (additive)" in result.stdout


def test_env_var_bypass_is_ignored() -> None:
    result = _run([], env={"ENABLE_FEW_SHOT": "true"})
    assert result.returncode == 0, result.stderr
    # Default-off must hold even when the env var is set to a truthy
    # value. The flag is CLI-only.
    assert "Few-Shot Examples" not in result.stdout


def test_env_var_bypass_alt_spelling_is_ignored() -> None:
    result = _run([], env={"FEW_SHOT_ENABLED": "1"})
    assert result.returncode == 0, result.stderr
    assert "Few-Shot Examples" not in result.stdout


def test_enabled_includes_section() -> None:
    result = _run(["--enable-few-shot"])
    assert result.returncode == 0, result.stderr
    assert "Few-Shot Examples" in result.stdout
    assert "Implicit / Guidance-Phrased Decision" in result.stdout


def test_disabled_explicitly_strips_section() -> None:
    result = _run(["--disable-few-shot"])
    assert result.returncode == 0, result.stderr
    assert "Few-Shot Examples" not in result.stdout


def test_mutex_pair_rejected() -> None:
    result = _run(["--enable-few-shot", "--disable-few-shot"])
    assert result.returncode != 0
    assert "not allowed with" in result.stderr or "mutually exclusive" in result.stderr.lower()


def test_enabled_with_corrupt_manifest_fails_closed(tmp_path: Path) -> None:
    bad_dir = tmp_path / "few_shot"
    bad_dir.mkdir()
    # No MANIFEST, no JSONL -> fail closed when --enable-few-shot is set.
    result = _run(
        ["--enable-few-shot", "--few-shot-dir", str(bad_dir)],
    )
    assert result.returncode != 0
    assert "few_shot_manifest_unreadable" in result.stderr
