"""Phase AB.3 — compare-extraction integration contract.

CLAUDE.md integration-test rule: a module that reads a pipeline
artifact gets an integration test that (1) produces the artifact via
the REAL writer (not a hand-rolled dict), (2) writes to a real temp
directory, (3) invokes the consumer via ``subprocess.run``, (4)
asserts the on-disk output (not just the return code).

The artifact the runner reads is the chunked ``source_record``. Its
canonical producer is the real pipeline
(``run_transcript_pipeline`` → ``_write_source_record``); there is no
hand-rolled dict — the real writer puts it on disk here, exactly as
the factory-function rule intends.

Subprocess + ``COMPARE_EXTRACTION_STUB=1`` keeps it API-free in CI.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from spectrum_systems_core.data_lake import run_transcript_pipeline

FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures" / "comparison_gold" / "meeting_real_001"
)
MEETING_ID = "meeting_real_001"


def _seed_real_source_record(lake_root: Path) -> Path:
    raw = lake_root / "raw" / "meetings" / MEETING_ID
    raw.mkdir(parents=True)
    shutil.copy(FIXTURE / "transcript.txt", raw / "transcript.txt")
    (raw / "metadata.json").write_text(
        json.dumps(
            {
                "meeting_id": MEETING_ID,
                "title": "Comparison gold meeting",
                "date": "2026-05-16",
                "source_type": "transcript",
            }
        ),
        encoding="utf-8",
    )
    result = run_transcript_pipeline(
        lake_root=lake_root,
        meeting_id=MEETING_ID,
        workflow_name="meeting_minutes",
    )
    sr = Path(result.source_record_path)
    assert sr.is_file(), "real pipeline did not write source_record"
    return sr


def test_compare_extraction_subprocess_writes_expected_artifacts(tmp_path):
    _seed_real_source_record(tmp_path)

    env = dict(os.environ)
    env["COMPARE_EXTRACTION_STUB"] = "1"
    env.pop("ANTHROPIC_API_KEY", None)
    # Ensure the package under test is importable in the subprocess.
    repo_src = str(Path(__file__).resolve().parents[2] / "src")
    env["PYTHONPATH"] = repo_src + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.run(
        [
            sys.executable, "-m", "spectrum_systems_core.data_lake.cli",
            "compare-extraction",
            "--lake", str(tmp_path),
            "--meeting-id", MEETING_ID,
        ],
        env=env,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, (
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )

    meeting_dir = tmp_path / "processed" / "meetings" / MEETING_ID
    comp = list(meeting_dir.glob("extraction_comparison__*.json"))
    tele = list(meeting_dir.glob("extraction_telemetry__*.json"))
    unc = list(meeting_dir.glob("extraction_unconstrained__*.json"))
    assert len(comp) == 1 and len(tele) == 1 and len(unc) == 1

    comparison = json.loads(comp[0].read_text(encoding="utf-8"))
    assert comparison["artifact_type"] == "extraction_comparison"
    assert comparison["status"] == "promoted"
    payload = comparison["payload"]
    assert payload["meeting_id"] == MEETING_ID
    assert payload["extractor_status"] == {
        "regex": "ok", "haiku": "ok", "opus": "ok",
    }
    # opus_output_ref points at the unconstrained artifact on disk.
    unconstrained = json.loads(unc[0].read_text(encoding="utf-8"))
    assert payload["opus_output_ref"] == unconstrained["artifact_id"]
    assert unconstrained["artifact_type"] == "extraction_unconstrained"
    assert "raw_output" in unconstrained["payload"]

    telemetry = json.loads(tele[0].read_text(encoding="utf-8"))
    assert telemetry["payload"]["comparison_artifact_id"] == (
        comparison["artifact_id"]
    )

    md = meeting_dir / "markdown" / "extraction_comparison.md"
    assert md.is_file()
    body = md.read_text(encoding="utf-8")
    assert f"# Extraction Comparison — {MEETING_ID}" in body


def test_compare_extraction_transcript_file_subprocess(tmp_path):
    """Flat-file mode: no source_record, no raw/ tree — only a flat
    transcript file. meeting_id is derived from the slugified stem and
    the instrument artifacts land under the derived directory."""
    lake = tmp_path / "lake"
    tf = tmp_path / "7 GHz Downlink TIG Meeting Kickoff - transcript 20251218.txt"
    tf.write_text(
        (FIXTURE / "transcript.txt").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["COMPARE_EXTRACTION_STUB"] = "1"
    env.pop("ANTHROPIC_API_KEY", None)
    repo_src = str(Path(__file__).resolve().parents[2] / "src")
    env["PYTHONPATH"] = repo_src + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.run(
        [
            sys.executable, "-m", "spectrum_systems_core.data_lake.cli",
            "compare-extraction",
            "--lake", str(lake),
            "--transcript-file", str(tf),
        ],
        env=env,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, (
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )

    derived = "7-ghz-downlink-tig-meeting-kickoff-transcript-20251218"
    meeting_dir = lake / "processed" / "meetings" / derived
    comp = list(meeting_dir.glob("extraction_comparison__*.json"))
    tele = list(meeting_dir.glob("extraction_telemetry__*.json"))
    unc = list(meeting_dir.glob("extraction_unconstrained__*.json"))
    assert len(comp) == 1 and len(tele) == 1 and len(unc) == 1

    comparison = json.loads(comp[0].read_text(encoding="utf-8"))
    assert comparison["artifact_type"] == "extraction_comparison"
    assert comparison["status"] == "promoted"
    assert comparison["payload"]["meeting_id"] == derived
    # Slug in the filename equals the derived meeting_id.
    assert comp[0].name == f"extraction_comparison__{derived}.json"
    # No source_record / raw tree was needed.
    assert not (lake / "raw").exists()

    md = meeting_dir / "markdown" / "extraction_comparison.md"
    assert md.is_file()
    assert f"# Extraction Comparison — {derived}" in md.read_text(
        encoding="utf-8"
    )
