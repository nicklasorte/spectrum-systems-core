"""SSC-033 — Eval score history."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from spectrum_systems_core.data_lake import (
    EVAL_HISTORY_FILENAME,
    eval_history_path,
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
    p = eval_history_path(lake_root, meeting_id)
    return [
        json.loads(line)
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_eval_history_file_is_written(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    p = eval_history_path(tmp_path, meeting_id)
    assert p.is_file()
    assert p.name == EVAL_HISTORY_FILENAME
    assert result.eval_history_path == p


def test_eval_history_includes_all_evals_from_all_workflows(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    records = _records(tmp_path, meeting_id)
    workflows = {r["workflow_name"] for r in records}
    assert workflows == {
        "meeting_minutes",
        "meeting_action_log",
        "agency_question_summary",
        "decision_brief",
    }
    eval_types = {r["eval_type"] for r in records}
    # The pipeline runs at least these evals on every workflow.
    for required in (
        "non_empty_payload",
        "source_grounding",
        "transcript_evidence",
        "content_signal",
    ):
        assert required in eval_types, required


def test_failed_eval_records_carry_reason_codes(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    records = _records(tmp_path, meeting_id)
    failures = [r for r in records if r["status"] == "fail"]
    assert failures, "this fixture must produce some failed evals"
    for r in failures:
        assert isinstance(r["reason_codes"], list)
        assert r["reason_codes"], (
            f"failed eval {r['eval_type']!r} for {r['workflow_name']!r} "
            "must list reason codes"
        )


def test_eval_history_is_byte_deterministic(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)

    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    p = eval_history_path(tmp_path, meeting_id)
    first = p.read_bytes()

    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    second = p.read_bytes()
    assert first == second


def test_eval_history_is_sorted(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    records = _records(tmp_path, meeting_id)
    keys = [
        (r["workflow_name"], r["eval_type"], r["target_artifact_id"])
        for r in records
    ]
    assert keys == sorted(keys)
