"""Integration contract test for scripts/review_gt_pairs.py.

Phase P1: ensures the review script reads ground_truth_pair artifacts
in the real production shape, writes ``gt_pair_review`` artifacts that
validate against the live schema, and that the EvalRunner gate consumes
the review correctly.

This test is required by CLAUDE.md's "Integration test requirement"
because ``scripts/review_gt_pairs.py`` reads pipeline artifacts
(``ground_truth_pair``). It uses the fixture factories in
``tests/integration/fixtures.py`` so a future schema or path drift
breaks the factory (and every consumer) instead of silently
producing stale shape.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from spectrum_systems_core.evals.m4.runner import EvalRunner
from spectrum_systems_core.validation import validate_artifact

from .fixtures import (
    make_gt_pair_review,
    make_ground_truth_pair_from_decision,
    make_meeting_extraction_artifact,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "review_gt_pairs.py"


def _stage_data_lake(tmp_path: Path) -> tuple[Path, dict]:
    """Lay out a minimal data-lake that satisfies the Phase P1 eval path.

    Returns ``(data_lake_root, gt_pair_dict)``.
    """
    data_lake = tmp_path / "data-lake"
    sdl_root = data_lake / "store" / "artifacts"
    sdl_root.mkdir(parents=True)

    source_id = "p1-fixture-source"
    source_artifact_id = "11111111-2222-3333-4444-555555555555"

    # 1. Ground truth pair via the real generate_gt_pairs.build_pair.
    gt_pair = make_ground_truth_pair_from_decision(
        source_id=source_id,
        source_artifact_id=source_artifact_id,
        minutes_artifact_id=f"synthesized-from-extraction:{source_id}",
        decision_text="Group approved ITU two-point criteria at 80th percentile",
        decision_outcome="approval",
    )
    gt_dir = sdl_root / "ground_truth"
    gt_dir.mkdir(parents=True)
    pair_path = gt_dir / f"{gt_pair['pair_id']}.json"
    pair_path.write_text(
        json.dumps(gt_pair, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # 2. meeting_extraction via the real merger.
    decisions = [
        {
            "decision_text": "Group approved ITU two-point criteria 80th percentile",
            "decision_type": "approved",
            "decision_outcome": "approval",
            "stakeholders": ["FCC"],
            "rationale": "Matches GT fixture.",
            "speaker": "Chair",
            "confidence": 0.95,
            "grounding_verified": True,
            "source_turn_ids": ["turn-1"],
            "source_turn_validation": "verified",
        }
    ]
    extraction = make_meeting_extraction_artifact(
        source_artifact_id=source_artifact_id,
        decisions=decisions,
    )
    ext_dir = sdl_root / "extractions"
    ext_dir.mkdir(parents=True)
    ext_path = ext_dir / f"{source_artifact_id}_meeting_extraction.json"
    ext_path.write_text(
        json.dumps(extraction, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return data_lake, gt_pair


def test_review_gt_pairs_writes_validating_artifact(tmp_path: Path) -> None:
    """Running the script with --confirm-all writes a schema-valid review."""
    data_lake, gt_pair = _stage_data_lake(tmp_path)
    pair_id = gt_pair["pair_id"]
    review_path = (
        data_lake
        / "store"
        / "artifacts"
        / "ground_truth"
        / f"{pair_id}_review.json"
    )
    assert not review_path.exists(), "review must not exist before script runs"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--data-lake",
            str(data_lake),
            "--confirm-all",
            "--reviewer-id",
            "contract-test-reviewer",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stderr={result.stderr}\nstdout={result.stdout}"
    assert review_path.is_file(), "script must write the review artifact"

    review = json.loads(review_path.read_text(encoding="utf-8"))
    # Validates against the local write-time schema.
    validate_artifact(review, "gt_pair_review")
    assert review["pair_id"] == pair_id
    assert review["outcome_confirmed"] is True
    assert review["reviewer_id"] == "contract-test-reviewer"


def test_eval_runner_gate_blocks_when_review_missing(tmp_path: Path) -> None:
    """No review on disk -> gt_pair_not_reviewed finding, pair skipped."""
    data_lake, gt_pair = _stage_data_lake(tmp_path)

    runner = EvalRunner(
        data_lake_path=str(data_lake),
        pipeline_run_id="contract-test-no-review",
        prompt_version="contract",
    )
    result = runner.run()
    assert result["status"] == "completed"
    # Pair is skipped because the gate fires.
    assert result["pairs_evaluated"] == 0
    # The pair was the only one on disk; aggregate stays at zero.
    summary = result["summary"]
    assert summary["aggregate_coverage"] == 0.0

    # A halt finding must have been emitted.
    findings_dir = data_lake / "store" / "artifacts" / "health"
    assert findings_dir.is_dir(), "health findings dir should be created"
    matching = []
    for path in findings_dir.glob("*.json"):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("finding_code") == "gt_pair_not_reviewed":
            matching.append(doc)
    assert matching, "expected gt_pair_not_reviewed finding"
    assert matching[0]["severity"] == "halt"
    assert matching[0]["context"]["pair_id"] == gt_pair["pair_id"]


def test_eval_runner_gate_blocks_on_rejected_outcome(tmp_path: Path) -> None:
    """outcome_confirmed=false -> gt_pair_outcome_rejected finding."""
    data_lake, gt_pair = _stage_data_lake(tmp_path)
    # Use the fixture factory (which calls the real script) for the
    # rejected review artifact.
    rejection = make_gt_pair_review(
        pair_id=gt_pair["pair_id"],
        reviewer_id="contract-test-reviewer",
        outcome_confirmed=False,
        expected_decision_outcome=gt_pair.get("expected_decision_outcome"),
    )
    rev_path = (
        data_lake
        / "store"
        / "artifacts"
        / "ground_truth"
        / f"{gt_pair['pair_id']}_review.json"
    )
    rev_path.write_text(
        json.dumps(rejection, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    runner = EvalRunner(
        data_lake_path=str(data_lake),
        pipeline_run_id="contract-test-rejected",
        prompt_version="contract",
    )
    result = runner.run()
    assert result["pairs_evaluated"] == 0
    summary = result["summary"]
    assert summary["aggregate_coverage"] == 0.0

    findings_dir = data_lake / "store" / "artifacts" / "health"
    matching = []
    for path in findings_dir.glob("*.json"):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("finding_code") == "gt_pair_outcome_rejected":
            matching.append(doc)
    assert matching, "expected gt_pair_outcome_rejected finding"
    assert matching[0]["severity"] == "halt"


def test_eval_runner_p1_path_produces_nonzero_coverage(tmp_path: Path) -> None:
    """End-to-end happy path: review present + extraction present -> coverage > 0."""
    data_lake, gt_pair = _stage_data_lake(tmp_path)
    review = make_gt_pair_review(
        pair_id=gt_pair["pair_id"],
        reviewer_id="contract-test-reviewer",
        outcome_confirmed=True,
        expected_decision_outcome=gt_pair["expected_decision_outcome"],
    )
    rev_path = (
        data_lake
        / "store"
        / "artifacts"
        / "ground_truth"
        / f"{gt_pair['pair_id']}_review.json"
    )
    rev_path.write_text(
        json.dumps(review, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    runner = EvalRunner(
        data_lake_path=str(data_lake),
        pipeline_run_id="contract-test-happy",
        prompt_version="contract",
    )
    result = runner.run()
    assert result["pairs_evaluated"] == 1
    summary = result["summary"]
    assert summary["aggregate_coverage"] > 0.0, summary
    assert summary["aggregate_precision"] > 0.0, summary
    # Phase P1 new fields are present.
    assert "spurious_add_rate" in summary
    assert summary["spurious_add_rate"] == 0.0
    assert "per_outcome_f1" in summary
    assert "approval" in summary["per_outcome_f1"]
    assert summary["alignment_threshold"] == pytest.approx(0.15)


def test_review_artifact_validates_against_schema_via_writer(tmp_path: Path) -> None:
    """The factory produces an artifact that passes ``validate_artifact``."""
    review = make_gt_pair_review(
        pair_id="11111111-1111-1111-1111-111111111111",
        reviewer_id="fixture",
        outcome_confirmed=True,
        expected_decision_outcome="approval",
        notes="schema-validation smoke check",
    )
    validate_artifact(review, "gt_pair_review")
    # And the JSON-serialised round-trip stays identical (no NaN /
    # non-ASCII / dict-order surprises).
    encoded = json.dumps(review, sort_keys=True)
    decoded = json.loads(encoded)
    assert decoded == review
