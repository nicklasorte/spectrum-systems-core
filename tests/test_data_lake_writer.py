import json

import pytest

from spectrum_systems_core.artifacts import new_artifact
from spectrum_systems_core.data_lake import (
    WriterError,
    write_promoted_artifact,
)


def _promoted(payload, artifact_type="meeting_minutes"):
    a = new_artifact(artifact_type, payload, trace_id="trace-1", status="draft")
    a.status = "promoted"
    return a


def test_writer_writes_promoted_artifact_under_processed(tmp_path):
    artifact = _promoted({
        "meeting_id": "m-001",
        "title": "Q3 sync",
        "summary": "ok",
    })
    path = write_promoted_artifact(tmp_path, artifact)

    assert path.is_file()
    rel = path.relative_to(tmp_path)
    assert rel.parts[0] == "processed"
    assert rel.parts[1] == "meetings"
    assert rel.parts[2] == "m-001"
    assert path.name.startswith("meeting_minutes__")
    assert path.suffix == ".json"

    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["artifact_type"] == "meeting_minutes"
    assert body["status"] == "promoted"
    assert body["payload"]["meeting_id"] == "m-001"


def test_writer_filename_includes_artifact_type_and_slug(tmp_path):
    artifact = _promoted({"meeting_id": "m-1", "title": "Spectrum-Plan v2!"})
    path = write_promoted_artifact(tmp_path, artifact, slug="spectrum-plan-v2")
    assert path.name == "meeting_minutes__spectrum-plan-v2.json"


def test_writer_rejects_draft_artifact(tmp_path):
    artifact = new_artifact(
        "meeting_minutes",
        {"meeting_id": "m-1", "title": "x"},
        trace_id="t",
        status="draft",
    )
    with pytest.raises(WriterError, match="status 'draft'"):
        write_promoted_artifact(tmp_path, artifact)


def test_writer_rejects_evaluated_artifact(tmp_path):
    artifact = new_artifact(
        "meeting_minutes",
        {"meeting_id": "m-1", "title": "x"},
        trace_id="t",
        status="evaluated",
    )
    with pytest.raises(WriterError, match="status 'evaluated'"):
        write_promoted_artifact(tmp_path, artifact)


def test_writer_rejects_rejected_artifact(tmp_path):
    artifact = new_artifact(
        "meeting_minutes",
        {"meeting_id": "m-1", "title": "x"},
        trace_id="t",
        status="rejected",
    )
    with pytest.raises(WriterError, match="status 'rejected'"):
        write_promoted_artifact(tmp_path, artifact)


@pytest.mark.parametrize(
    "artifact_type", ["context_bundle", "eval_result", "control_decision"]
)
def test_writer_rejects_run_internal_artifact_types(tmp_path, artifact_type):
    artifact = _promoted({"meeting_id": "m-1"}, artifact_type=artifact_type)
    with pytest.raises(WriterError, match="run-internal"):
        write_promoted_artifact(tmp_path, artifact)


def test_writer_rejects_artifact_without_meeting_id(tmp_path):
    artifact = _promoted({"title": "x"})
    with pytest.raises(WriterError, match="meeting_id"):
        write_promoted_artifact(tmp_path, artifact)


def test_writer_can_route_via_explicit_meeting_id(tmp_path):
    artifact = _promoted({"title": "no meeting id in payload"})
    path = write_promoted_artifact(tmp_path, artifact, meeting_id="m-explicit", slug="x")
    assert "m-explicit" in str(path)


def test_writer_output_is_byte_deterministic(tmp_path, tmp_path_factory):
    payload = {
        "meeting_id": "m-1",
        "title": "Q3 sync",
        "decisions": ["B", "A"],
        "summary": "Same payload produces same bytes.",
    }
    a = _promoted(payload)
    a.artifact_id = "fixed-id"
    a.created_at = "2026-05-09T00:00:00+00:00"

    p1 = write_promoted_artifact(tmp_path, a, slug="run")
    other_root = tmp_path_factory.mktemp("other")
    p2 = write_promoted_artifact(other_root, a, slug="run")

    assert p1.read_bytes() == p2.read_bytes()


def test_writer_uses_sorted_keys(tmp_path):
    artifact = _promoted({"meeting_id": "m-1", "z": 1, "a": 2, "m": 3, "title": "t"})
    path = write_promoted_artifact(tmp_path, artifact, slug="s")
    text = path.read_text(encoding="utf-8")
    payload_obj = json.loads(text)["payload"]
    keys = list(payload_obj.keys())
    assert keys == sorted(keys)
