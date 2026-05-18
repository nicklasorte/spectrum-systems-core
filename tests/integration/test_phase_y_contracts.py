"""Phase Y — integration contract tests.

Per CLAUDE.md: artifacts come from the fixture factories (which call
the REAL writers — extract_ceiling / compare_extractions /
build_false_negative_set / evaluate_candidate / run_improvement_cycle),
are written to a real temp directory, and the touched artifact-reading
script (scripts/check_auto_pr_eligibility.py) is exercised via
subprocess against that directory.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration.fixtures import (
    make_candidate_evaluation_artifact,
    make_extraction_alignment_comparison_artifact,
    make_false_negative_set_artifact,
    make_improvement_cycle_result_artifact,
    make_opus_ceiling_artifact,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_auto_pr_eligibility.py"


def _run(path: Path):
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(path)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


def test_check_auto_pr_eligibility_eligible(tmp_path):
    art = make_candidate_evaluation_artifact(
        target_baseline_f1=0.0,   # -> f1 0.0 branch
        target_candidate_f1=1.0,  # -> f1 1.0 branch  (delta +1.0)
        holdout_baseline_f1=0.5,  # -> mid (~0.667)
        holdout_candidate_f1=1.0,  # -> 1.0 (no regression)
    )
    p = tmp_path / "candidate_evaluation.json"
    p.write_text(json.dumps(art.payload), encoding="utf-8")
    res = _run(p)
    assert res.returncode == 0, res.stdout + res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["eligible"] is True
    assert out["reasons"] == []


def test_check_auto_pr_eligibility_ineligible_holdout_regression(tmp_path):
    art = make_candidate_evaluation_artifact(
        target_baseline_f1=0.0,
        target_candidate_f1=1.0,
        holdout_baseline_f1=1.0,   # -> 1.0
        holdout_candidate_f1=0.0,  # -> 0.0  (holdout regression)
    )
    p = tmp_path / "candidate_evaluation.json"
    p.write_text(json.dumps(art.payload), encoding="utf-8")
    res = _run(p)
    assert res.returncode == 1, res.stdout + res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["eligible"] is False
    assert "holdout_regression" in out["reasons"]


def test_check_auto_pr_eligibility_invalid_artifact_fails_closed(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"artifact_type": "candidate_evaluation"}),
                 encoding="utf-8")
    res = _run(p)
    assert res.returncode == 2, res.stdout + res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["eligible"] is False


@pytest.mark.parametrize(
    "factory,expected_type",
    [
        (make_opus_ceiling_artifact, "opus_ceiling"),
        (
            make_extraction_alignment_comparison_artifact,
            "extraction_alignment_comparison",
        ),
        (make_false_negative_set_artifact, "false_negative_set"),
        (make_candidate_evaluation_artifact, "candidate_evaluation"),
        (
            make_improvement_cycle_result_artifact,
            "improvement_cycle_result",
        ),
    ],
)
def test_factory_artifacts_validate_against_schema(
    factory, expected_type, tmp_path
):
    """Every new artifact type, produced by its REAL writer, validates
    against its schema (catches writer/schema drift at the factory)."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from _artifact_validator import validate_artifact  # noqa: WPS433

    art = factory()
    payload = art.payload
    assert payload["artifact_type"] == expected_type
    out = tmp_path / f"{expected_type}.json"
    out.write_text(json.dumps(payload), encoding="utf-8")
    # Raises ArtifactValidationError on any schema/type drift.
    validate_artifact(payload, expected_type, str(out))
