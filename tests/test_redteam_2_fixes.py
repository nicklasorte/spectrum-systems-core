"""Regression tests for must_fix and should_fix items in
docs/reviews/ssc_next_memory_redteam_2.md."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from spectrum_systems_core.data_lake import (
    collect_index_records,
    eval_history_path,
    experience_history_path,
    process_meeting,
    run_history_path,
)
from spectrum_systems_core.data_lake.debug import _INSPECTION_HINTS
from spectrum_systems_core.data_lake.eval_history import _coerce_score


FIXTURES = Path(__file__).parent / "fixtures" / "golden_meetings"


def _seed(lake_root: Path, meeting_id: str) -> None:
    src = FIXTURES / meeting_id
    dst = lake_root / "raw" / "meetings" / meeting_id
    dst.mkdir(parents=True)
    shutil.copy(src / "transcript.txt", dst / "transcript.txt")
    shutil.copy(src / "metadata.json", dst / "metadata.json")


def _records(p: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_M4_run_history_records_have_pointer_fields_not_lesson_fields(tmp_path):
    """`run_history` is a 'where to look' projection."""
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    records = _records(run_history_path(tmp_path, meeting_id))
    for r in records:
        assert "manifest_path" in r
        assert "debug_path" in r
        assert "run_markdown_path" in r
        # Lesson-shape fields belong to experience records only.
        assert "experience_id" not in r
        assert "input_hash" not in r
        assert "output_hash" not in r
        assert "human_readable_summary" not in r


def test_M4_experience_history_records_have_lesson_fields_not_pointers(tmp_path):
    """`experience_history` is the 'what happened' projection."""
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    records = _records(experience_history_path(tmp_path, meeting_id))
    for r in records:
        assert "experience_id" in r
        assert "input_hash" in r
        assert "human_readable_summary" in r
        # Pointer-shape fields belong to run_history only.
        assert "manifest_path" not in r
        assert "debug_path" not in r
        assert "run_markdown_path" not in r


def test_M5_score_is_float_or_none_only(tmp_path):
    """Eval score column is well-typed."""
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    records = _records(eval_history_path(tmp_path, meeting_id))
    assert records, "fixture must produce at least one eval record"
    for r in records:
        score = r["score"]
        assert score is None or isinstance(score, float), (
            f"eval score must be float or None, got {score!r} ({type(score)})"
        )


def test_M5_score_coercion_rejects_string_and_bool():
    assert _coerce_score("high") is None
    assert _coerce_score(True) is None
    assert _coerce_score(False) is None
    assert _coerce_score(0.0) == 0.0
    assert _coerce_score(1) == 1.0
    assert _coerce_score(None) is None


def test_S3_known_reason_codes_have_inspection_hints():
    """Every known fail:<eval_type> code maps to a non-fallback hint."""
    expected = {
        "failed:transcript_evidence",
        "failed:source_grounding",
        "failed:non_empty_payload",
        "failed:content_signal",
        "missing_required_evals",
    }
    for code in expected:
        assert code in _INSPECTION_HINTS, (
            f"reason code {code!r} must have a plain-English inspection hint"
        )


def test_S4_jsonl_files_are_not_picked_up_by_artifact_index(tmp_path):
    """The artifact index walker must skip every JSONL projection file."""
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    records = collect_index_records(tmp_path)
    assert records, "expected at least one promoted artifact"
    for r in records:
        assert not r.path.endswith(".jsonl"), r
        assert "run_history" not in r.path
        assert "experience_history" not in r.path
        assert "eval_history" not in r.path
