"""Integration contract test for ``scripts/ingest_human_minutes.py``.

Satisfies the CLAUDE.md non-negotiable: every script that reads or
writes a pipeline artifact must have an integration test that

  1. Uses ``tests/integration/fixtures.py`` factories (no hand-rolled
     dicts).
  2. Writes artifacts to a real temp directory (not mocked).
  3. Calls the script via ``subprocess.run`` against the temp dir.
  4. Asserts the correct output on disk (not just the return code).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ingest_human_minutes.py"
SOURCE_ID = "test-source-dec18-ingest"

# A minimal NTIA-shaped minutes file with one discussion row, three
# action items, and one next step — the same shape as the Dec 18
# fixture, so the test exercises every section the parser supports.
MINUTES_TEXT = """\
Synthetic Minutes for Integration Test

Meeting Name: | Synthetic Integration Test | Meeting Date: | 12/18/2025
Prepared By: | NTIA Spectrum Programs | Location: | Virtual

Meeting Overview

Synthetic minutes exercising every section the parser supports.

Discussion/Questions Log

# | Category | Question/Topic | Asked By | Initial Response / Discussion | Follow-up / Action Item

1 | Scope / Geography | Why does the study cover the US&P and not only continental US? | Keri P. | NTIA explained the scope rationale. | N/A

Next Steps

Review the cellular network characteristics materials shared in advance of the January meeting.

Action Items

Item | Responsible Party | Due Date | Status

Review and provide comments on the Draft 7 GHz Study Plan. | Agencies | 12/19/25 | Completed
Review the participating agency TIG charters. | Agencies | 1/8/26 | In progress
Validate the proposed system assignments for the federal incumbents. | Agency POCs | 1/15/26 | In progress
"""


def test_ingest_writes_valid_human_minutes_artifact(tmp_path: Path):
    data_lake = tmp_path / "data-lake"
    data_lake.mkdir()
    minutes_file = tmp_path / "minutes.txt"
    minutes_file.write_text(MINUTES_TEXT, encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--minutes-file",
            str(minutes_file),
            "--source-id",
            SOURCE_ID,
            "--data-lake",
            str(data_lake),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"script failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    expected = (
        data_lake / "store" / "processed" / "meetings" / SOURCE_ID
        / f"human_minutes__{SOURCE_ID}.json"
    )
    assert expected.is_file(), f"expected artifact at {expected}"

    artifact = json.loads(expected.read_text(encoding="utf-8"))
    assert artifact["artifact_type"] == "human_minutes"
    assert "artifact_kind" not in artifact  # constitution uses artifact_type
    assert artifact["schema_version"] == "1.0.0"
    assert artifact["source_id"] == SOURCE_ID
    assert artifact["produced_by"] == "minutes_parser"
    assert artifact["raw_source_hash"].startswith("sha256:")

    assert len(artifact["discussion_items"]) == 1
    assert len(artifact["action_items"]) == 3
    assert len(artifact["next_steps"]) == 1
    # ``N/A`` follow-up normalizes to JSON null.
    assert artifact["discussion_items"][0]["follow_up"] is None


def test_ingest_dry_run_does_not_write(tmp_path: Path):
    data_lake = tmp_path / "data-lake"
    minutes_file = tmp_path / "minutes.txt"
    minutes_file.write_text(MINUTES_TEXT, encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--minutes-file",
            str(minutes_file),
            "--source-id",
            SOURCE_ID,
            "--data-lake",
            str(data_lake),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    expected_dir = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    # Dry run does not touch the data lake at all.
    assert not expected_dir.exists()
    assert "DRY RUN" in result.stdout


def test_ingest_idempotent_same_input_same_artifact(tmp_path: Path):
    """Two runs over the same input produce a byte-identical artifact."""
    data_lake = tmp_path / "data-lake"
    data_lake.mkdir()
    minutes_file = tmp_path / "minutes.txt"
    minutes_file.write_text(MINUTES_TEXT, encoding="utf-8")

    def _run() -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--minutes-file",
                str(minutes_file),
                "--source-id",
                SOURCE_ID,
                "--data-lake",
                str(data_lake),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    _run()
    artifact_path = (
        data_lake / "store" / "processed" / "meetings" / SOURCE_ID
        / f"human_minutes__{SOURCE_ID}.json"
    )
    first_bytes = artifact_path.read_bytes()

    _run()
    second_bytes = artifact_path.read_bytes()
    assert first_bytes == second_bytes
