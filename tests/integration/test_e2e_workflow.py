"""End-to-end workflow smoke test (mobile workflow sequence).

Simulates ``debug-single-transcript`` -> ``select-few-shot-candidates``
without live API calls or real data-lake commits. Proves the full
seam works: the debug workflow's artifact write format matches what
the select workflow reads, and the data-lake path conventions align.

This test would have caught PRs #77 / #78 / #79 before they were
needed. If the data-lake path conventions or merger field names ever
drift again, this test fails immediately.
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

from tests.integration.fixtures import (
    make_decision_few_shot_placeholder,
    make_meeting_extraction_artifact,
    make_source_record,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ID = "7-ghz-downlink-tig-meeting-kickoff---transcript-20251218"


def test_select_few_shot_after_debug_run(tmp_path: Path) -> None:
    """Simulates: debug-single-transcript commits artifacts, then
    select-few-shot-candidates reads them.

    Step 1 writes ``source_record.json`` + ``meeting_extraction.json``
    using the real ``ExtractionMerger`` factory -- matching what
    debug-single-transcript writes via the pipeline.

    Step 2 runs ``select_few_shot_examples.py`` as a subprocess (same
    invocation pattern the mobile workflow uses) and asserts every
    post-condition the workflow's step summary checks.
    """
    dl = tmp_path / "data-lake"
    artifact_id = str(uuid.uuid4())

    # Step 1: Simulate "debug single transcript" committing artifacts.
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

    # Seed the Phase V placeholder few-shot artifact (the shipped state).
    few_shot_dir = dl / "store" / "artifacts" / "evals" / "few_shot"
    few_shot_dir.mkdir(parents=True)
    (few_shot_dir / "decision_examples_v1.json").write_text(
        json.dumps(make_decision_few_shot_placeholder())
    )

    # Step 2: Simulate "select few-shot candidates" workflow step.
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "select_few_shot_examples.py"),
            "--source-id", SOURCE_ID,
            "--data-lake", str(dl),
            "--max-examples", "3",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, (
        f"select_few_shot_examples failed:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    output = json.loads(
        (few_shot_dir / "decision_examples_v1.json").read_text()
    )
    examples = output["examples"]

    # Placeholders must be gone.
    placeholder_ids = [
        e["example_id"] for e in examples
        if e["example_id"].startswith("phase-v-placeholder")
    ]
    assert not placeholder_ids, (
        f"Placeholders survived the workflow sequence: {placeholder_ids}"
    )

    # Every example must carry real source_turn_ids.
    for ex in examples:
        turns = ex.get("source_turn_ids", [])
        assert turns, f"Example {ex['example_id']} missing source_turn_ids"

    # Success path: no NEEDS_REAL_EXAMPLES.md marker should be on disk.
    assert not (few_shot_dir / "NEEDS_REAL_EXAMPLES.md").exists(), (
        "NEEDS_REAL_EXAMPLES.md marker present after a successful run -- "
        "indicates the script could not find real decisions even though "
        "the fixture seeded them."
    )


def test_e2e_fails_loudly_when_debug_step_skipped(tmp_path: Path) -> None:
    """When step 1 is skipped (no artifacts on disk), step 2 must fail
    with rc != 0 AND leave NEEDS_REAL_EXAMPLES.md so the operator sees
    durable evidence in the workflow's git commit.

    This pair (this test + test_select_few_shot_after_debug_run) is
    the minimum that proves the workflow seam is correct in both the
    happy path and the missing-input path.
    """
    dl = tmp_path / "data-lake"
    few_shot_dir = dl / "store" / "artifacts" / "evals" / "few_shot"
    few_shot_dir.mkdir(parents=True)
    (few_shot_dir / "decision_examples_v1.json").write_text(
        json.dumps(make_decision_few_shot_placeholder())
    )

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "select_few_shot_examples.py"),
            "--source-id", SOURCE_ID,
            "--data-lake", str(dl),
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode != 0
    assert (few_shot_dir / "NEEDS_REAL_EXAMPLES.md").exists()
