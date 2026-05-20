"""Tests for scripts/verify_rollback_contracts.py (Phase 2 Step 2.9).

Every gate added by Step 2.9 has a paired rejection test:

* PR-not-in-contracts -> rejection
* PR-entry-doesn't-reference-changed-files -> rejection
* verification-command-not-in-whitelist -> rejection
* fully-compliant entry -> pass
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import verify_rollback_contracts as vrc  # noqa: E402


def _write_contracts(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


_COMPLIANT_ENTRY = (
    "# Rollback Contracts\n\n"
    "Document ID: ROLLBACK\n\n"
    "---\n"
    "\n## Phase 2 — eval-path alignment (PR #197)\n\n"
    "### What this change adds\n"
    "- New module src/spectrum_systems_core/pipeline/governed_run.py\n\n"
    "### To roll back\n"
    "1. Revert this PR.\n\n"
    "### Verification\n"
    "```bash\n"
    "pytest tests/pipeline/\n"
    "python scripts/reconcile_invocation_logs.py\n"
    "```\n"
    "\n---\n"
)


def test_pr_missing_from_contracts_is_rejected(tmp_path: Path) -> None:
    """Pass-1 / Pass-3 rejection: a PR without a contracts entry fails."""
    cf = _write_contracts(
        tmp_path / "rc.md",
        "# Rollback Contracts\n\nNothing about PR #197.\n",
    )
    with pytest.raises(vrc.RollbackCheckError) as ei:
        vrc.verify_pr(
            197,
            contracts_file=cf,
            changed_files=["src/spectrum_systems_core/pipeline/governed_run.py"],
        )
    assert "PR #197" in str(ei.value)


def test_entry_without_changed_file_is_rejected(tmp_path: Path) -> None:
    """The entry must reference at least one changed file."""
    cf = _write_contracts(tmp_path / "rc.md", _COMPLIANT_ENTRY)
    with pytest.raises(vrc.RollbackCheckError) as ei:
        vrc.verify_pr(
            197,
            contracts_file=cf,
            changed_files=["completely/unrelated/file.py"],
        )
    assert "changed" in str(ei.value).lower() or "reference" in str(
        ei.value
    ).lower()


def test_entry_without_whitelisted_verification_command_is_rejected(
    tmp_path: Path,
) -> None:
    body = (
        "# Rollback Contracts\n\n"
        "---\n"
        "\n## Phase 2 — eval-path alignment (PR #197)\n\n"
        "- src/spectrum_systems_core/pipeline/governed_run.py changed.\n\n"
        "### Verification\n"
        "```bash\n"
        "rm -rf /tmp/x\n"
        "curl http://example.com\n"
        "```\n"
        "\n---\n"
    )
    cf = _write_contracts(tmp_path / "rc.md", body)
    with pytest.raises(vrc.RollbackCheckError) as ei:
        vrc.verify_pr(
            197,
            contracts_file=cf,
            changed_files=["src/spectrum_systems_core/pipeline/governed_run.py"],
        )
    assert "whitelist" in str(ei.value).lower() or "verification" in str(
        ei.value
    ).lower()


def test_compliant_entry_passes(tmp_path: Path) -> None:
    cf = _write_contracts(tmp_path / "rc.md", _COMPLIANT_ENTRY)
    assert vrc.verify_pr(
        197,
        contracts_file=cf,
        changed_files=["src/spectrum_systems_core/pipeline/governed_run.py"],
    ) is True


def test_whitelist_accepts_python_module_invocation(tmp_path: Path) -> None:
    body = (
        "---\n"
        "\n## Phase X (PR #200)\n\n"
        "- src/spectrum_systems_core/calibration/budget.py\n\n"
        "```\n"
        "python -m spectrum_systems_core.calibration.budget\n"
        "```\n"
        "\n---\n"
    )
    cf = _write_contracts(tmp_path / "rc.md", body)
    assert vrc.verify_pr(
        200,
        contracts_file=cf,
        changed_files=["src/spectrum_systems_core/calibration/budget.py"],
    ) is True


def test_whitelist_accepts_script_invocation(tmp_path: Path) -> None:
    body = (
        "---\n"
        "\n## Phase X (PR #201)\n\n"
        "- scripts/reconcile_invocation_logs.py\n\n"
        "```\n"
        "python scripts/reconcile_invocation_logs.py\n"
        "```\n"
        "\n---\n"
    )
    cf = _write_contracts(tmp_path / "rc.md", body)
    assert vrc.verify_pr(
        201,
        contracts_file=cf,
        changed_files=["scripts/reconcile_invocation_logs.py"],
    ) is True
