"""Phase X2 follow-up — mobile workflow_dispatch YAMLs.

The five mobile workflows let an operator drive the human-only Phase X2
steps from a phone (no laptop required). These tests assert the YAML
files exist, parse, expose ``workflow_dispatch`` triggers with the
expected inputs / choices, and respect the ANTHROPIC_API_KEY policy
(only the validate-and-baseline workflow has a defensible use for the
secret; the others must NOT exfiltrate it).
"""
from __future__ import annotations

import pathlib
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")


WORKFLOWS_DIR = (
    pathlib.Path(__file__).resolve().parents[2] / ".github" / "workflows"
)

MOBILE_WORKFLOW_FILES: dict[str, str] = {
    "select": "select-few-shot-candidates.yml",
    "verify": "verify-few-shot-example.yml",
    "annotate": "annotate-gt-rubric.yml",
    "confirm": "confirm-rubric-annotations.yml",
    "baseline": "validate-and-baseline.yml",
}


def _load(name: str) -> dict[str, Any]:
    path = WORKFLOWS_DIR / MOBILE_WORKFLOW_FILES[name]
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _trigger(doc: dict[str, Any]) -> dict[str, Any]:
    # PyYAML parses the bare key ``on:`` as a Python ``True`` so we
    # accept both shapes.
    trigger = doc.get("on") if "on" in doc else doc.get(True)
    assert isinstance(trigger, dict), (
        f"workflow trigger should be a mapping, got {type(trigger).__name__}"
    )
    return trigger


def test_all_five_workflow_files_exist_and_parse() -> None:
    for name, filename in MOBILE_WORKFLOW_FILES.items():
        path = WORKFLOWS_DIR / filename
        assert path.is_file(), f"missing workflow file: {filename}"
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(doc, dict), f"{filename} did not parse as a mapping"


def test_all_five_workflows_have_workflow_dispatch_trigger() -> None:
    for name in MOBILE_WORKFLOW_FILES:
        doc = _load(name)
        trigger = _trigger(doc)
        assert "workflow_dispatch" in trigger, (
            f"{MOBILE_WORKFLOW_FILES[name]} missing workflow_dispatch"
        )


def test_verify_workflow_has_approve_reject_choice() -> None:
    doc = _load("verify")
    trigger = _trigger(doc)
    inputs = (trigger.get("workflow_dispatch") or {}).get("inputs") or {}
    decision = inputs.get("decision") or {}
    assert decision.get("type") == "choice"
    options = decision.get("options") or []
    assert "approve" in options
    assert "reject" in options


def test_confirm_workflow_has_all_six_outcome_types() -> None:
    doc = _load("confirm")
    trigger = _trigger(doc)
    inputs = (trigger.get("workflow_dispatch") or {}).get("inputs") or {}
    override = inputs.get("override_outcome") or {}
    assert override.get("type") == "choice"
    options = set(override.get("options") or [])
    # Mirrors the rubric_notes.expected_decision_outcome enum
    # (ground_truth_pair.schema.json).
    expected = {
        "approval", "rejection", "deferral",
        "action_required", "noted", "question",
    }
    missing = expected - options
    assert not missing, (
        f"confirm workflow missing outcome options: {sorted(missing)}"
    )


def test_validate_baseline_has_enable_judge_input() -> None:
    doc = _load("baseline")
    trigger = _trigger(doc)
    inputs = (trigger.get("workflow_dispatch") or {}).get("inputs") or {}
    judge = inputs.get("enable_judge")
    assert judge is not None, "validate-and-baseline missing enable_judge input"
    # Treated as a boolean-style choice (true/false strings) so the
    # mobile UI renders a dropdown.
    options = judge.get("options") or []
    if options:
        assert set(options) == {"true", "false"}


def test_validate_baseline_commit_uses_skip_ci_and_baseline_commit() -> None:
    body = (WORKFLOWS_DIR / MOBILE_WORKFLOW_FILES["baseline"]).read_text(
        encoding="utf-8"
    )
    assert "[skip ci]" in body
    assert "[baseline-commit]" in body


def test_verify_workflow_passes_force_to_verify_example() -> None:
    """The CI-refusal guard in verify_example.py refuses to run when
    ANTHROPIC_API_KEY is set unless --force is also passed. The mobile
    verify workflow must pass --force so the human-approved review can
    proceed in a controlled GitHub Actions context."""
    body = (WORKFLOWS_DIR / MOBILE_WORKFLOW_FILES["verify"]).read_text(
        encoding="utf-8"
    )
    assert "scripts/verify_example.py" in body
    assert "--force" in body


def test_only_validate_baseline_uses_anthropic_api_key() -> None:
    """ANTHROPIC_API_KEY is a high-value secret. Only the pipeline-run
    workflow (validate-and-baseline) has a defensible use for it; the
    other four mobile workflows must NOT actually pull the secret into
    their job env.

    We match the secret-USE pattern (``secrets.ANTHROPIC_API_KEY``)
    rather than any mention of the string, so workflow docstrings can
    still explain WHY they don't need the secret without tripping the
    assertion.
    """
    secret_use_marker = "secrets.ANTHROPIC_API_KEY"
    for name, filename in MOBILE_WORKFLOW_FILES.items():
        body = (WORKFLOWS_DIR / filename).read_text(encoding="utf-8")
        if name == "baseline":
            assert secret_use_marker in body, (
                "validate-and-baseline must wire secrets.ANTHROPIC_API_KEY "
                "(it runs the extraction pipeline)"
            )
        else:
            assert secret_use_marker not in body, (
                f"{filename} must NOT pull secrets.ANTHROPIC_API_KEY "
                "into its job env"
            )
