"""Contract tests for ``scripts/_few_shot_preflight.py``.

The preflight runs from the ``select-few-shot-candidates`` workflow
BEFORE ``pip install -e`` and BEFORE ``select_few_shot_examples.py`` so
a missing ``meeting_extraction`` artifact surfaces in the GitHub step
summary instead of failing inside the script with logs only the
operator scrolling through Actions can read.

Tests assert:

  - Exit code is non-zero when no extraction is on disk.
  - Exit code is zero when an extraction artifact (produced by
    ``ExtractionMerger.merge`` via the fixture) exists for the
    requested source_id.
  - When ``GITHUB_STEP_SUMMARY`` is set, the missing-artifact run
    writes an actionable block (with the source_id and a pointer to
    debug-single-transcript) to that file.

Uses ``tests/integration/fixtures.py`` factories — the integration-test
requirement in CLAUDE.md forbids hand-rolled dicts here.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from tests.integration.fixtures import (
    make_meeting_extraction_artifact,
    make_source_record,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "_few_shot_preflight.py"
SOURCE_ID = "7-ghz-downlink-tig-meeting-kickoff---transcript-20251218"


def _run(args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=full_env,
    )


def test_preflight_exits_nonzero_when_no_extraction(tmp_path: Path) -> None:
    """No extraction artifact on disk -> non-zero exit + stderr explains why."""
    dl = tmp_path / "data-lake"
    (dl / "store").mkdir(parents=True)

    result = _run([
        "--source-id", SOURCE_ID,
        "--data-lake", str(dl),
    ])

    assert result.returncode != 0, (
        "Preflight must fail when no extraction artifact exists"
    )
    combined = result.stdout + result.stderr
    assert SOURCE_ID in combined, "Failure message must name the source_id"
    assert "debug-single-transcript" in combined.lower(), (
        "Failure message must point the operator at the upstream workflow"
    )


def test_preflight_writes_actionable_step_summary(tmp_path: Path) -> None:
    """When ``GITHUB_STEP_SUMMARY`` is set, the failure surfaces there.

    This is the bug class this preflight exists to defend: previously
    the only diagnostic was the Python script's stderr, which a mobile
    operator never sees.
    """
    dl = tmp_path / "data-lake"
    (dl / "store").mkdir(parents=True)
    summary_path = tmp_path / "step_summary.md"
    summary_path.write_text("")

    result = _run(
        [
            "--source-id", SOURCE_ID,
            "--data-lake", str(dl),
        ],
        env={"GITHUB_STEP_SUMMARY": str(summary_path)},
    )

    assert result.returncode != 0
    summary = summary_path.read_text(encoding="utf-8")
    assert "[BLOCKED]" in summary, "Step summary must carry a BLOCKED banner"
    assert SOURCE_ID in summary
    assert "Debug single transcript" in summary, (
        "Step summary must instruct the operator to run the upstream workflow"
    )


def test_preflight_succeeds_when_extraction_exists(tmp_path: Path) -> None:
    """Seed the data-lake with a real-shape extraction artifact and
    assert the preflight exits 0 — i.e., the workflow proceeds.

    Uses the same factories the live workflow's writers exercise so a
    rename of ``source_artifact_id`` would fail at fixture build time
    rather than silently producing a stale assertion here.
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

    result = _run([
        "--source-id", SOURCE_ID,
        "--data-lake", str(dl),
    ])

    assert result.returncode == 0, (
        f"Preflight should succeed when the extraction matches. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_preflight_strips_trailing_whitespace_on_source_id(tmp_path: Path) -> None:
    """Mobile workflow_dispatch inputs often arrive with a trailing
    space pasted from a phone keyboard. The preflight strips them so
    the lookup matches the artifact on disk.
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

    result = _run([
        "--source-id", f"{SOURCE_ID} ",
        "--data-lake", str(dl),
    ])

    assert result.returncode == 0, (
        f"Preflight must strip trailing whitespace on --source-id; "
        f"stderr={result.stderr!r}"
    )
