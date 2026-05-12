"""Phase X2.7 — validate-and-baseline workflow YAML structure tests.

These tests do not run the workflow; they assert that the file exists,
parses as YAML, has the required jobs / guards, and references the
expected CLI commands. CI-runtime correctness is out of scope for
unit tests.
"""
from __future__ import annotations

import pathlib

import pytest

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:  # pragma: no cover
    YAML_AVAILABLE = False

WORKFLOW_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / ".github" / "workflows" / "validate-and-baseline.yml"
)


@pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML required")
def test_workflow_yaml_parses() -> None:
    assert WORKFLOW_PATH.is_file(), f"workflow missing at {WORKFLOW_PATH}"
    yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


@pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML required")
def test_workflow_has_two_jobs_with_guard_ordering() -> None:
    doc = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    jobs = doc.get("jobs") or {}
    assert "early-exit-check" in jobs
    assert "validate-and-baseline" in jobs
    # The main job depends on the guard job (Phase X2 attack-2 fix).
    assert jobs["validate-and-baseline"].get("needs") == "early-exit-check"


@pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML required")
def test_workflow_has_skip_ci_and_baseline_commit_tags() -> None:
    body = WORKFLOW_PATH.read_text(encoding="utf-8")
    # Two-layer guard per the Phase X2 attack 7 mitigation: [skip ci]
    # is the standard GH-honored tag; [baseline-commit] is the
    # defense-in-depth marker that the early-exit step inspects.
    assert "[skip ci]" in body
    assert "[baseline-commit]" in body


@pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML required")
def test_workflow_calls_eval_ground_truth_set_baseline() -> None:
    body = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "eval-ground-truth" in body
    assert "--set-baseline" in body
    assert "--specific-source-id" in body


@pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML required")
def test_workflow_verifies_five_wiring_signals() -> None:
    body = WORKFLOW_PATH.read_text(encoding="utf-8")
    expected_signals = [
        "agenda_item_id_nonnull",
        "few_shot_present_with_verified",
        "glossary_terms_injected_present",
        "binding_taxonomy_present",
        "generalization_check_ran",
    ]
    for sig in expected_signals:
        assert sig in body, f"signal {sig!r} missing from workflow body"


@pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML required")
def test_workflow_set_baseline_only_runs_when_signals_green() -> None:
    """The 'Set development baseline' step runs AFTER 'Verify Phase W
    wiring signals'. If verify fails (exit 1) the baseline step is
    skipped because workflow steps abort on the first failure by
    default. We assert step ordering here so a future refactor that
    puts baseline-set before verify is caught at PR review time."""
    body = WORKFLOW_PATH.read_text(encoding="utf-8")
    verify_idx = body.find("Verify Phase W wiring signals")
    baseline_idx = body.find("Set development baseline")
    assert verify_idx != -1
    assert baseline_idx != -1
    assert verify_idx < baseline_idx, (
        "baseline-set step must follow the wiring-signal verify step"
    )
