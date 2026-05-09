"""SSC-032 — Harness experience records.

A compressed record per workflow run that captures what happened
without granting authority. No autonomous optimization, no model
calls, no self-modifying code. Just a deterministic JSONL projection.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from spectrum_systems_core.data_lake import (
    DEFAULT_WORKFLOWS,
    EXPERIENCE_HISTORY_FILENAME,
    experience_history_path,
    process_meeting,
)


FIXTURES = Path(__file__).parent / "fixtures" / "golden_meetings"


def _seed(lake_root: Path, meeting_id: str) -> None:
    src = FIXTURES / meeting_id
    dst = lake_root / "raw" / "meetings" / meeting_id
    dst.mkdir(parents=True)
    shutil.copy(src / "transcript.txt", dst / "transcript.txt")
    shutil.copy(src / "metadata.json", dst / "metadata.json")


def _records(lake_root: Path, meeting_id: str) -> list[dict]:
    p = experience_history_path(lake_root, meeting_id)
    return [
        json.loads(line)
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_experience_history_file_is_written(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    p = experience_history_path(tmp_path, meeting_id)
    assert p.is_file()
    assert p.name == EXPERIENCE_HISTORY_FILENAME
    assert result.experience_history_path == p


def test_experience_record_per_workflow(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    records = _records(tmp_path, meeting_id)
    assert sorted(r["workflow_name"] for r in records) == sorted(DEFAULT_WORKFLOWS)


def test_blocked_workflow_gets_an_experience_record(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    records = _records(tmp_path, meeting_id)
    blocked_dec = next(r for r in records if r["workflow_name"] == "decision_brief")
    assert blocked_dec["decision"] == "block"
    assert blocked_dec["output_hash"] is None
    assert "blocked" in blocked_dec["human_readable_summary"]
    assert blocked_dec["reason_codes"], "blocked records must list reason codes"


def test_promoted_workflow_record_has_output_hash(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    records = _records(tmp_path, meeting_id)
    minutes = next(r for r in records if r["workflow_name"] == "meeting_minutes")
    assert minutes["decision"] == "allow"
    assert minutes["output_hash"], "promoted record must include output_hash"
    assert minutes["eval_summary"]["passed"]
    assert minutes["eval_summary"]["failed"] == []


def test_experience_history_is_byte_deterministic(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)

    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    p = experience_history_path(tmp_path, meeting_id)
    first = p.read_bytes()

    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    second = p.read_bytes()
    assert first == second


def test_experience_record_does_not_alter_promotion(tmp_path):
    """Recording experience is a side effect; it must not change which
    workflows promote or block."""
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    promoted = set(result.promoted_workflows)
    blocked = set(result.blocked_workflows)
    # m-golden-good fixture: meeting_minutes + meeting_action_log promote;
    # agency_question_summary + decision_brief block. Re-running with
    # experience already on disk must not change that.
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    result2 = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    assert set(result2.promoted_workflows) == promoted
    assert set(result2.blocked_workflows) == blocked
