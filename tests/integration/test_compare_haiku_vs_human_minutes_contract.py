"""Integration contract test for ``scripts/compare_haiku_vs_human_minutes.py``.

The script reads two on-disk artifacts (the ``human_minutes`` gold
standard and a promoted ``meeting_minutes`` extraction) and writes a
``human_minutes_comparison`` artifact. CLAUDE.md requires an integration
test that uses the real writers from ``tests/integration/fixtures.py``,
not hand-rolled dicts — that way a field-name drift between writer and
reader fails the test before the script's metric logic runs.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.integration.fixtures import (
    make_human_minutes_artifact,
    make_promoted_meeting_minutes_artifact,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "compare_haiku_vs_human_minutes.py"
SOURCE_ID = "test-source-compare-haiku-human"

MINUTES_TEXT = """\
Synthetic Minutes for Compare Integration Test

Meeting Name: | Synthetic Compare | Meeting Date: | 12/18/2025

Discussion/Questions Log

# | Category | Question/Topic | Asked By | Initial Response / Discussion | Follow-up / Action Item

1 | Scope / Geography | Why does the study cover the US&P territories instead of only the continental US? | Keri P. | NTIA explained the scope rationale. | N/A

Action Items

Item | Responsible Party | Due Date | Status

Review and provide comments on the Draft 7 GHz Study Plan circulated for agency review. | Agencies | 12/19/25 | Completed
"""


def _seed(tmp_path: Path) -> Path:
    data_lake = tmp_path / "data-lake"
    data_lake.mkdir()
    sid_dir = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    sid_dir.mkdir(parents=True)

    # Real writers produce both artifacts.
    make_human_minutes_artifact(
        data_lake_root=data_lake,
        source_id=SOURCE_ID,
        minutes_text=MINUTES_TEXT,
    )

    decisions = ["The group approved a study-plan review schedule."]
    action_items = [
        {"action": "Review and provide comments on the Draft 7 GHz Study Plan circulated for agency review."}
    ]
    open_questions = ["What is the coordination distance for federal incumbents?"]

    make_promoted_meeting_minutes_artifact(
        lake_root=data_lake / "store",
        source_id=SOURCE_ID,
        decisions=decisions,
        action_items=action_items,
        open_questions=open_questions,
    )
    return data_lake


def test_compare_writes_valid_comparison_artifact(tmp_path: Path):
    data_lake = _seed(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
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

    out_path = (
        data_lake / "store" / "processed" / "meetings" / SOURCE_ID
        / f"human_minutes_comparison__{SOURCE_ID}.json"
    )
    assert out_path.is_file()
    artifact = json.loads(out_path.read_text(encoding="utf-8"))
    assert artifact["artifact_type"] == "human_minutes_comparison"
    assert "artifact_kind" not in artifact  # constitution uses artifact_type
    assert artifact["schema_version"] == "1.0.0"
    assert artifact["source_id"] == SOURCE_ID
    assert artifact["match_threshold"] == 0.45

    # The seeded action item matches the human action item verbatim, so
    # at least one true positive must be reported.
    assert artifact["true_positives"] >= 1
    assert artifact["total_human_items"] == 2  # 1 discussion + 1 action

    # The stdout payload includes a summary block.
    stdout_obj = json.loads(result.stdout)
    assert stdout_obj["status"] == "success"
    assert "f1_vs_human" in stdout_obj["summary"]


def test_compare_dry_run_does_not_write(tmp_path: Path):
    data_lake = _seed(tmp_path)
    out_path = (
        data_lake / "store" / "processed" / "meetings" / SOURCE_ID
        / f"human_minutes_comparison__{SOURCE_ID}.json"
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--source-id",
            SOURCE_ID,
            "--data-lake",
            str(data_lake),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not out_path.exists(), "dry-run must not write the artifact"
    # stdout in dry-run prints the artifact as JSON.
    parsed = json.loads(result.stdout)
    assert parsed["artifact_type"] == "human_minutes_comparison"


def test_compare_halts_when_human_minutes_missing(tmp_path: Path):
    """Removing the human_minutes file makes the comparison halt fail-closed."""
    data_lake = _seed(tmp_path)
    human_path = (
        data_lake / "store" / "processed" / "meetings" / SOURCE_ID
        / f"human_minutes__{SOURCE_ID}.json"
    )
    human_path.unlink()

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--source-id",
            SOURCE_ID,
            "--data-lake",
            str(data_lake),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    halt = json.loads(result.stdout)
    assert halt["status"] == "halt"
    assert halt["reason"] == "missing_human_minutes"
