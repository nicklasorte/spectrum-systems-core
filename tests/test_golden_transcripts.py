"""Golden transcript suite. Pinned outcomes for known inputs.

Layout: tests/fixtures/golden_meetings/<meeting_id>/{transcript.txt,
metadata.json, expected.json}.
"""
import json
import shutil
from pathlib import Path

import pytest

from spectrum_systems_core.data_lake import (
    LoaderError,
    load_meeting,
    run_transcript_pipeline,
)


FIXTURES = Path(__file__).parent / "fixtures" / "golden_meetings"


def _seed(lake_root: Path, meeting_id: str) -> None:
    src = FIXTURES / meeting_id
    dst = lake_root / "raw" / "meetings" / meeting_id
    dst.mkdir(parents=True)
    shutil.copy(src / "transcript.txt", dst / "transcript.txt")
    shutil.copy(src / "metadata.json", dst / "metadata.json")


def _expected(meeting_id: str) -> dict:
    return json.loads((FIXTURES / meeting_id / "expected.json").read_text(encoding="utf-8"))


# --- valid golden -----------------------------------------------------


def test_golden_good_meeting_promotes_with_expected_payload(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    expected = _expected(meeting_id)

    result = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id=meeting_id, workflow_name="meeting_minutes"
    )

    assert result.promoted is expected["promoted"]
    assert result.target.artifact_type == expected["artifact_type"]
    assert result.control_decision.payload["decision"] == expected["decision"]

    payload = result.target.payload
    assert payload["decisions"] == expected["decisions"]
    assert payload["action_items"] == expected["action_items"]
    assert payload["open_questions"] == expected["open_questions"]

    actual_kinds = sorted(g["kind"] for g in payload["grounding"])
    assert actual_kinds == sorted(expected["grounding_kinds"])


def test_golden_good_grounding_excerpts_are_real_transcript_lines(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    result = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id=meeting_id, workflow_name="meeting_minutes"
    )
    transcript = result.transcript_input.transcript_text
    for g in result.target.payload["grounding"]:
        assert g["source_excerpt"] in transcript


def test_golden_good_writes_promoted_artifact_to_disk(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    result = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id=meeting_id, workflow_name="meeting_minutes"
    )
    assert result.written_paths
    p = Path(result.written_paths[0])
    assert p.is_file()
    body = json.loads(p.read_text(encoding="utf-8"))
    assert body["status"] == "promoted"


# --- malformed metadata blocks at loader ------------------------------


def test_golden_malformed_meeting_is_rejected_by_loader(tmp_path):
    meeting_id = "m-golden-malformed"
    _seed(tmp_path, meeting_id)
    expected = _expected(meeting_id)
    assert expected["loader_should_reject"] is True
    with pytest.raises(LoaderError, match=expected["missing_field"]):
        load_meeting(tmp_path, meeting_id)


# --- weak transcript blocks at evidence eval --------------------------


def test_golden_weak_meeting_is_blocked_by_transcript_evidence(tmp_path):
    meeting_id = "m-golden-weak"
    _seed(tmp_path, meeting_id)
    expected = _expected(meeting_id)
    result = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id=meeting_id, workflow_name="meeting_minutes"
    )
    assert result.promoted is expected["promoted"]
    assert result.control_decision.payload["decision"] == expected["decision"]
    reason_codes = result.control_decision.payload["reason_codes"]
    assert any(expected["reason_code_includes"] in r for r in reason_codes)
    # No promoted artifact file should have been written
    processed_dir = tmp_path / "processed" / "meetings" / meeting_id
    if processed_dir.is_dir():
        product_files = [
            f for f in processed_dir.glob("*.json")
            if not f.name.startswith("manifest__") and not f.name.startswith("debug__")
        ]
        assert product_files == []


# --- the golden suite as a whole is deterministic --------------------


def test_golden_suite_runs_are_deterministic_across_workdirs(tmp_path, tmp_path_factory):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    a = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id=meeting_id, workflow_name="meeting_minutes"
    )

    other = tmp_path_factory.mktemp("other-lake")
    _seed(other, meeting_id)
    b = run_transcript_pipeline(
        lake_root=other, meeting_id=meeting_id, workflow_name="meeting_minutes"
    )

    assert Path(a.written_paths[0]).read_bytes() == Path(b.written_paths[0]).read_bytes()
    assert Path(a.manifest_path).read_bytes() == Path(b.manifest_path).read_bytes()
