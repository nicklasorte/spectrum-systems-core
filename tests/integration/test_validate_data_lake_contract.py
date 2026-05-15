"""Contract tests for ``scripts/validate_data_lake.py``.

These tests build a data-lake from the actual writers — ``seed_glossary.py``
for the versioned glossary aggregate, and the
``make_decision_few_shot_placeholder`` factory in
``tests/integration/fixtures.py`` for the decision_few_shot_examples
artifact — and assert the validator's exit code and stdout on disk.

This is the CLAUDE.md-mandated integration layer: it catches field
name drift between the writers and the validator's readers. The
``data_lake/`` unit tests cover field-level mutations; this file
covers the writer-to-reader contract.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.integration.fixtures import make_decision_few_shot_placeholder

REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATE_SCRIPT = REPO_ROOT / "scripts" / "validate_data_lake.py"
SEED_GLOSSARY_SCRIPT = REPO_ROOT / "scripts" / "seed_glossary.py"


def _run(args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=full_env,
    )


def _seed_glossary_aggregate(lake: Path) -> None:
    """Call the real seed_glossary.py writer to produce the versioned
    aggregate the validator expects. Using the writer keeps the test
    honest: a rename of the aggregate filename or its top-level shape
    breaks fixture build, not the assertion.
    """
    out_dir = lake / "store" / "artifacts" / "glossary"
    out_dir.mkdir(parents=True, exist_ok=True)
    result = _run(
        [str(SEED_GLOSSARY_SCRIPT), "--out", str(out_dir)]
    )
    assert result.returncode == 0, (
        f"seed_glossary.py failed: stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


def _write_few_shot(lake: Path, doc: dict) -> Path:
    fs_dir = lake / "store" / "artifacts" / "evals" / "few_shot"
    fs_dir.mkdir(parents=True, exist_ok=True)
    path = fs_dir / "decision_examples_v1.json"
    path.write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def _write_verified_few_shot(lake: Path) -> Path:
    """Variant of the placeholder factory: one example verified True.

    The placeholder factory ships ``verified: false`` on every example
    so the wiring signal predicate cannot be satisfied. Tests that
    need a GREEN signal flip exactly one example's ``verified`` to the
    boolean ``True`` (NOT the string ``"true"`` — that's the bug class).
    """
    doc = make_decision_few_shot_placeholder()
    # Append a verified-True example so the wiring predicate matches.
    doc["examples"].append(
        {
            "example_id": "contract-verified-example",
            "source_meeting_id": "contract-source",
            "input_text": "real verified decision body",
            "expected_output": {"decision_outcome": "approval"},
            "verified": True,
            "verified_by": "contract-operator",
            "verified_at": "2026-05-13T00:00:00+00:00",
            "selected_at": "2026-05-12T00:00:00+00:00",
            "selection_reason": "contract-test",
        }
    )
    doc.setdefault("audit_log", []).extend(
        [
            {
                "action": "selected",
                "example_id": "contract-verified-example",
                "at": "2026-05-12T00:00:00+00:00",
                "actor": "operator",
                "notes": None,
            },
            {
                "action": "verified",
                "example_id": "contract-verified-example",
                "at": "2026-05-13T00:00:00+00:00",
                "actor": "operator",
                "notes": None,
            },
        ]
    )
    return _write_few_shot(lake, doc)


def test_validate_data_lake_passes_on_factory_built_lake(tmp_path: Path) -> None:
    """Build the data-lake from the real writers and assert PASS.

    If ``seed_glossary.py`` ever changes the aggregate path or shape, OR
    the factory ever drifts away from what the validator expects, this
    test fails at fixture-build time instead of in the assertion.
    """
    lake = tmp_path / "data-lake"
    lake.mkdir()
    _seed_glossary_aggregate(lake)
    _write_verified_few_shot(lake)

    result = _run([str(VALIDATE_SCRIPT), "--data-lake", str(lake)])
    assert result.returncode == 0, (
        f"validator must pass on factory-built lake. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "0 FAIL" in result.stdout


def test_validate_data_lake_catches_placeholder_only_few_shot(
    tmp_path: Path,
) -> None:
    """The shipped Phase V placeholder fixture has ``verified: false``
    on every example. The wiring-signal predicate must reject it.

    This is the exact failure mode the validator exists to catch
    BEFORE validate-and-baseline runs.
    """
    lake = tmp_path / "data-lake"
    lake.mkdir()
    _seed_glossary_aggregate(lake)
    _write_few_shot(lake, make_decision_few_shot_placeholder())

    result = _run([str(VALIDATE_SCRIPT), "--data-lake", str(lake)])
    assert result.returncode == 1, (
        "validator must fail when no example has verified is True. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "few_shot_present_with_verified" in result.stdout
    assert "zero examples with verified is True" in result.stdout


def test_validate_data_lake_catches_string_verified(tmp_path: Path) -> None:
    """The exact bug class from PR #77 / #78 / #79: ``verified: "true"``
    instead of the boolean. The validator must reject it AND the
    wiring-signal predicate must report it as MISSING.
    """
    lake = tmp_path / "data-lake"
    lake.mkdir()
    _seed_glossary_aggregate(lake)

    doc = make_decision_few_shot_placeholder()
    doc["examples"][0]["verified"] = "true"  # the bug
    _write_few_shot(lake, doc)

    result = _run([str(VALIDATE_SCRIPT), "--data-lake", str(lake)])
    assert result.returncode == 1
    assert "must be a bool" in result.stdout
    assert "wiring_signal:few_shot_present_with_verified" in result.stdout


def test_validate_data_lake_catches_missing_glossary_aggregate(
    tmp_path: Path,
) -> None:
    """Without the versioned glossary aggregate, the term injector
    loads zero terms. The validator must fail-closed on this.
    """
    lake = tmp_path / "data-lake"
    lake.mkdir()
    # Deliberately do NOT seed the glossary.
    _write_verified_few_shot(lake)

    result = _run([str(VALIDATE_SCRIPT), "--data-lake", str(lake)])
    assert result.returncode == 1
    assert "spectrum_glossary" in result.stdout
    assert "glossary_aggregate_nonempty" in result.stdout


def test_validate_data_lake_writes_to_step_summary(tmp_path: Path) -> None:
    """When ``GITHUB_STEP_SUMMARY`` is set, the report is appended there
    so a mobile operator sees it in the workflow run page UI.
    """
    lake = tmp_path / "data-lake"
    lake.mkdir()
    _seed_glossary_aggregate(lake)
    _write_verified_few_shot(lake)

    summary_path = tmp_path / "step_summary.md"
    summary_path.write_text("")

    result = _run(
        [str(VALIDATE_SCRIPT), "--data-lake", str(lake)],
        env={"GITHUB_STEP_SUMMARY": str(summary_path)},
    )
    assert result.returncode == 0
    summary = summary_path.read_text(encoding="utf-8")
    assert "Data-lake validation report" in summary
    assert "SUMMARY:" in summary
