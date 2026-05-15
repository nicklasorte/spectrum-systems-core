"""Integration contract tests for scripts that read pipeline artifacts.

Each test:
  1. Uses the factory functions in ``tests/integration/fixtures.py`` to
     produce artifacts via the real writer (``ExtractionMerger.merge``).
     Never hand-rolls a dict — drift between the writer and a reader
     must surface here, not in production.
  2. Writes the artifact to a real temp ``data-lake`` directory.
  3. Calls the script via ``subprocess.run`` so the same code path
     mobile workflows exercise is the one under test.
  4. Asserts the post-conditions on disk (artifact contents, marker
     files), not just the script's exit code.

If a factory ever fails to produce a valid artifact, these tests fail
at setup before any script logic runs — catching schema drift earlier
than a unit test could.
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Tuple

import pytest

from tests.integration.fixtures import (
    make_decision_few_shot_placeholder,
    make_ground_truth_pair_from_decision,
    make_meeting_extraction_artifact,
    make_source_record,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ID = "7-ghz-downlink-tig-meeting-kickoff---transcript-20251218"


def _seed_data_lake(tmp_path: Path) -> Tuple[Path, str]:
    """Build a minimal data-lake with real artifact shapes.

    Returns (data_lake_path, source_artifact_id). Tests that need the
    seeded UUID for assertions should keep the returned id rather than
    hard-coding one.
    """
    dl = tmp_path / "data-lake"
    artifact_id = str(uuid.uuid4())

    proc_dir = dl / "store" / "processed" / "meetings" / SOURCE_ID
    proc_dir.mkdir(parents=True)
    (proc_dir / "source_record.json").write_text(
        json.dumps(make_source_record(SOURCE_ID, artifact_id))
    )

    ext_dir = dl / "store" / "artifacts" / "extractions"
    ext_dir.mkdir(parents=True)
    extraction = make_meeting_extraction_artifact(artifact_id)
    (ext_dir / f"{artifact_id}_meeting_extraction.json").write_text(
        json.dumps(extraction)
    )

    few_shot_dir = dl / "store" / "artifacts" / "evals" / "few_shot"
    few_shot_dir.mkdir(parents=True)
    (few_shot_dir / "decision_examples_v1.json").write_text(
        json.dumps(make_decision_few_shot_placeholder())
    )

    return dl, artifact_id


@pytest.fixture
def data_lake(tmp_path: Path) -> Path:
    """Provide a data-lake seeded with real-shape artifacts."""
    dl, _ = _seed_data_lake(tmp_path)
    return dl


def _run_script(script: str, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / script), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


def test_select_few_shot_finds_real_decisions(data_lake: Path) -> None:
    """select_few_shot_examples must locate real decisions in the real
    artifact shape produced by ``ExtractionMerger.merge``.

    This test catches the PR #78 bug class: the script previously
    looked up ``source_id`` at the top level, but the real merger
    emits ``source_artifact_id``. The factory uses the real merger,
    so any future drift surfaces here.
    """
    result = _run_script(
        "select_few_shot_examples.py",
        [
            "--source-id", SOURCE_ID,
            "--data-lake", str(data_lake),
            "--max-examples", "3",
        ],
    )
    assert result.returncode == 0, (
        f"Script failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )

    output_path = (
        data_lake / "store" / "artifacts" / "evals" / "few_shot"
        / "decision_examples_v1.json"
    )
    doc = json.loads(output_path.read_text())
    examples = doc["examples"]

    leftover_placeholders = [
        e["example_id"] for e in examples
        if e["example_id"].startswith("phase-v-placeholder")
    ]
    assert not leftover_placeholders, (
        f"Placeholders survived: {leftover_placeholders}"
    )

    assert len(examples) >= 1, "No real examples written"
    for ex in examples:
        turns = ex.get("source_turn_ids", [])
        assert turns, f"Example {ex['example_id']} has no source_turn_ids"
        # The factory uses "real-turn-NNN" ids; reject the legacy
        # synthetic "turn_00" pattern explicitly so a regression to
        # hand-rolled fixtures shows up immediately.
        assert not any("turn_00" in t for t in turns), (
            f"Example {ex['example_id']} has synthetic turn IDs: {turns}"
        )


def test_select_few_shot_fails_loudly_when_no_extraction(tmp_path: Path) -> None:
    """When no extraction artifact exists, the script must exit
    non-zero AND drop a ``NEEDS_REAL_EXAMPLES.md`` marker.

    This catches the PR #79 bug class: the script previously exited 0
    with placeholders intact. The marker file is the artifact-on-disk
    evidence the operator needs when a mobile workflow runs.
    """
    dl = tmp_path / "data-lake"
    few_shot_dir = dl / "store" / "artifacts" / "evals" / "few_shot"
    few_shot_dir.mkdir(parents=True)
    (few_shot_dir / "decision_examples_v1.json").write_text(
        json.dumps(make_decision_few_shot_placeholder())
    )

    result = _run_script(
        "select_few_shot_examples.py",
        [
            "--source-id", SOURCE_ID,
            "--data-lake", str(dl),
        ],
    )
    assert result.returncode != 0, (
        "Script must exit non-zero when no extraction is on disk"
    )

    marker = few_shot_dir / "NEEDS_REAL_EXAMPLES.md"
    assert marker.exists(), "NEEDS_REAL_EXAMPLES.md marker must be written"
    assert SOURCE_ID in marker.read_text(), (
        "NEEDS_REAL_EXAMPLES.md must mention the source_id under review"
    )


def test_select_few_shot_uses_real_merger_field_names(data_lake: Path) -> None:
    """Direct contract assertion: the extraction artifact on disk uses
    the field names the script looks for.

    If ``ExtractionMerger`` ever renames ``source_artifact_id`` (e.g.
    back to ``source_id``), this test fails at the assertion below
    instead of failing mysteriously inside the script.
    """
    ext_dir = data_lake / "store" / "artifacts" / "extractions"
    paths = list(ext_dir.glob("*_meeting_extraction.json"))
    assert paths, "No extraction artifact written by the fixture"
    doc = json.loads(paths[0].read_text())
    assert "source_artifact_id" in doc, (
        "ExtractionMerger no longer writes source_artifact_id — "
        "scripts that read this field will break."
    )
    assert doc["artifact_type"] == "meeting_extraction"
    assert isinstance(doc.get("decisions"), list)


def test_verify_example_validates_few_shot_artifact_shape(
    tmp_path: Path,
) -> None:
    """``verify_example.py`` reads ``decision_examples_v1.json``. The
    integration-hardening gate must refuse to verify against a
    misshapen artifact (wrong artifact_type or missing required field)
    instead of silently flipping ``verified`` on the wrong file.
    """
    artifact_path = tmp_path / "few_shot.json"
    # Hand-rolled WRONG shape: missing ``examples_version`` and
    # ``extraction_type`` -- both required by the schema.
    artifact_path.write_text(json.dumps({
        "artifact_type": "decision_few_shot_examples",
        "schema_version": "1.0.0",
        "examples": [],
    }))
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "verify_example.py"),
            "--example-id", "anything",
            "--reviewer-id", "tester",
            "--artifact-path", str(artifact_path),
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode != 0, (
        "verify_example must refuse to flip ``verified`` on a malformed artifact"
    )
    assert "validation failed" in result.stderr.lower() or "examples_version" in result.stderr, (
        f"error must name the failing field; got stderr: {result.stderr}"
    )


def test_submit_review_validates_correction_candidate_shape(
    tmp_path: Path,
) -> None:
    """``submit_review.py`` reads ``correction_candidate`` artifacts. A
    malformed candidate (wrong ``artifact_type``) must trip the
    validator before the script writes a referencing review artifact.
    """
    dl = tmp_path / "data-lake"
    cand_dir = dl / "store" / "artifacts" / "correction_candidates"
    cand_dir.mkdir(parents=True)
    bad_candidate = {
        "artifact_type": "wrong_type",  # not a correction_candidate
        "correction_candidate_id": "cand-1",
        "source_id": "test-source",
    }
    (cand_dir / "cand-1.json").write_text(json.dumps(bad_candidate))
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "submit_review.py"),
            "--candidate-id", "cand-1",
            "--reviewer-id", "tester",
            "--decision", "accept",
            "--data-lake", str(dl),
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode != 0
    assert "wrong_type" in result.stderr or "correction_candidate" in result.stderr, (
        f"validator must name the bad artifact_type; got: {result.stderr}"
    )


def test_annotate_rubric_validates_pair_shape(tmp_path: Path) -> None:
    """``annotate_rubric.py --apply-from`` must refuse to annotate a
    ground_truth_pair whose schema shape is invalid. The script keeps
    walking the annotations file but skips the bad pair with a
    diagnostic on stderr.
    """
    dl = tmp_path / "data-lake"
    sdl_root = dl / "store" / "artifacts"
    gt_dir = sdl_root / "ground_truth"
    gt_dir.mkdir(parents=True)
    bad_pair = {
        "pair_id": "00000000-0000-4000-8000-000000000001",
        "source_artifact_id": "src-1",
        # Missing: minutes_artifact_id, meeting_date, ...
        "schema_version": "1.0.0",
    }
    (gt_dir / f"{bad_pair['pair_id']}.json").write_text(json.dumps(bad_pair))

    annotations_path = tmp_path / "annotations.json"
    annotations_path.write_text(json.dumps([
        {
            "pair_id": bad_pair["pair_id"],
            "expected_decision_outcome": "approval",
            "verb_discrimination_example": True,
            "annotator_id": "tester",
        },
    ]))
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "annotate_rubric.py"),
            "--apply-from", str(annotations_path),
            "--data-lake", str(dl),
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    # The script must skip the malformed pair, not crash.
    assert result.returncode == 0
    assert "failed schema" in result.stderr.lower() or "skip" in result.stderr.lower(), (
        f"validator must surface a skip diagnostic; got: {result.stderr}"
    )
    # And the pair must remain un-annotated.
    saved = json.loads((gt_dir / f"{bad_pair['pair_id']}.json").read_text())
    assert "rubric_notes" not in saved


def _run_annotate_list(data_lake: Path) -> subprocess.CompletedProcess:
    """List candidates for SOURCE_ID (the human-readable slug) via the
    same subprocess path the mobile annotate-gt-rubric workflow uses."""
    return subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "annotate_rubric.py"),
            "--data-lake", str(data_lake),
            "--source-id", SOURCE_ID,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


def test_annotate_rubric_matches_pair_by_slug(tmp_path: Path) -> None:
    """SLUG MATCH (end-to-end). A real ``ground_truth_pair`` written by
    the production writer (``generate_gt_pairs.build_pair`` via the
    fixture factory) carries a top-level ``source_id`` slug. The script,
    invoked exactly as the mobile workflow invokes it (--source-id
    <slug>), must find it. Factory-backed so a writer-side field rename
    breaks setup, not production."""
    dl = tmp_path / "data-lake"
    artifact_id = str(uuid.uuid4())

    proc_dir = dl / "store" / "processed" / "meetings" / SOURCE_ID
    proc_dir.mkdir(parents=True)
    (proc_dir / "source_record.json").write_text(
        json.dumps(make_source_record(SOURCE_ID, artifact_id))
    )

    pair = make_ground_truth_pair_from_decision(
        source_id=SOURCE_ID,
        source_artifact_id=artifact_id,
        minutes_artifact_id="min-001",
        decision_text="The Committee approved the 7 GHz downlink plan.",
    )
    gt_dir = dl / "store" / "artifacts" / "ground_truth"
    gt_dir.mkdir(parents=True)
    (gt_dir / f"{pair['pair_id']}.json").write_text(json.dumps(pair))

    result = _run_annotate_list(dl)
    assert result.returncode == 0, (
        f"slug must match the factory pair; stderr: {result.stderr}"
    )
    assert pair["pair_id"] in result.stdout


def test_annotate_rubric_matches_uuid_only_pair_via_source_record(
    tmp_path: Path,
) -> None:
    """UUID MATCH (end-to-end) — the bug this PR fixes.

    A GroundTruthLinker-shaped pair carries ONLY ``source_artifact_id``
    (the opaque UUID); the optional top-level ``source_id`` slug is
    absent (the GT-pair schema requires only ``source_artifact_id``).
    The operator passes the human-readable slug. Pre-fix this exited
    "source_id matched 0 pairs" even though the pair existed. The fix
    resolves the slug to the UUID through ``source_record.json`` and
    matches on ``source_artifact_id``.

    The envelope is still produced by the real writer; only the
    optional ``source_id`` is dropped to model the GroundTruthLinker
    producer's shape (schema-valid — ``source_id`` is not required)."""
    dl = tmp_path / "data-lake"
    artifact_id = str(uuid.uuid4())

    proc_dir = dl / "store" / "processed" / "meetings" / SOURCE_ID
    proc_dir.mkdir(parents=True)
    (proc_dir / "source_record.json").write_text(
        json.dumps(make_source_record(SOURCE_ID, artifact_id))
    )

    pair = make_ground_truth_pair_from_decision(
        source_id=SOURCE_ID,
        source_artifact_id=artifact_id,
        minutes_artifact_id="min-001",
        decision_text="The Committee approved the 7 GHz downlink plan.",
    )
    # GroundTruthLinker shape: no top-level source_id slug. Slug-only
    # matching CANNOT find this — only source_record resolution can.
    pair.pop("source_id", None)
    assert "source_id" not in pair
    assert pair["source_artifact_id"] == artifact_id

    gt_dir = dl / "store" / "artifacts" / "ground_truth"
    gt_dir.mkdir(parents=True)
    (gt_dir / f"{pair['pair_id']}.json").write_text(json.dumps(pair))

    result = _run_annotate_list(dl)
    assert result.returncode == 0, (
        "slug must resolve to the UUID via source_record.json and match "
        f"the pair's source_artifact_id; stderr: {result.stderr}"
    )
    assert pair["pair_id"] in result.stdout
