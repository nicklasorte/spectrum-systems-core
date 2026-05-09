"""SSC-031 — Run history index.

Run history is harness memory, not authority. The fail-closed control
gate is unaffected; these tests only assert that the projection is
produced, ordered, and points at the canonical run records.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from spectrum_systems_core.data_lake import (
    DEFAULT_WORKFLOWS,
    RUN_HISTORY_FILENAME,
    RUNS_SUBDIR,
    markdown_dir,
    process_meeting,
    run_history_path,
)


FIXTURES = Path(__file__).parent / "fixtures" / "golden_meetings"


def _seed(lake_root: Path, meeting_id: str) -> None:
    src = FIXTURES / meeting_id
    dst = lake_root / "raw" / "meetings" / meeting_id
    dst.mkdir(parents=True)
    shutil.copy(src / "transcript.txt", dst / "transcript.txt")
    shutil.copy(src / "metadata.json", dst / "metadata.json")


def _run_history_records(lake_root: Path, meeting_id: str) -> list[dict]:
    p = run_history_path(lake_root, meeting_id)
    return [
        json.loads(line)
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_run_history_file_is_written_at_meeting_root(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    p = run_history_path(tmp_path, meeting_id)
    assert p.is_file()
    assert p.name == RUN_HISTORY_FILENAME
    assert result.run_history_path == p


def test_run_history_has_one_record_per_workflow(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    records = _run_history_records(tmp_path, meeting_id)
    workflow_names = sorted(r["workflow_name"] for r in records)
    assert workflow_names == sorted(DEFAULT_WORKFLOWS)


def test_run_history_includes_promoted_and_blocked(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    records = _run_history_records(tmp_path, meeting_id)
    promoted = {r["workflow_name"] for r in records if r["promoted"]}
    blocked = {r["workflow_name"] for r in records if not r["promoted"]}
    assert "meeting_minutes" in promoted
    assert "decision_brief" in blocked


def test_run_history_record_points_to_manifest_and_debug(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    for record in _run_history_records(tmp_path, meeting_id):
        assert record["manifest_path"], record
        assert record["debug_path"], record
        assert Path(record["manifest_path"]).is_file()
        assert Path(record["debug_path"]).is_file()


def test_run_history_is_byte_deterministic(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)

    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    p = run_history_path(tmp_path, meeting_id)
    first = p.read_bytes()

    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    second = p.read_bytes()
    assert first == second


def test_run_history_is_sorted_deterministically(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    records = _run_history_records(tmp_path, meeting_id)
    keys = [(r["workflow_name"], r["run_id"]) for r in records]
    assert keys == sorted(keys)


def test_run_note_markdown_written_per_run(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    runs_dir = markdown_dir(tmp_path, meeting_id) / RUNS_SUBDIR
    assert runs_dir.is_dir()
    for r in result.pipeline_results:
        run_md = runs_dir / f"{r.run_id}.md"
        assert run_md.is_file(), f"missing run note for {r.run_id}"


def test_run_note_markdown_explains_block(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    runs_dir = markdown_dir(tmp_path, meeting_id) / RUNS_SUBDIR
    blocked = next(
        r for r in result.pipeline_results
        if r.workflow_name == "decision_brief"
    )
    text = (runs_dir / f"{blocked.run_id}.md").read_text(encoding="utf-8")
    assert "decision_brief" in text
    assert "Decision: `block`" in text
    assert "promoted: no" in text
