import pytest

from spectrum_systems_core.artifacts import (
    ALLOWED_STATUSES,
    Artifact,
    ArtifactStore,
    compute_content_hash,
    new_artifact,
)


def test_content_hash_is_deterministic_for_identical_payloads():
    payload = {"a": 1, "b": [1, 2, 3], "c": {"x": "y"}}
    a = new_artifact("meeting_minutes", payload, trace_id="t-1")
    b = new_artifact("meeting_minutes", payload, trace_id="t-2")
    assert a.content_hash == b.content_hash
    assert a.content_hash == compute_content_hash(payload)


def test_content_hash_differs_for_different_payloads():
    a = new_artifact("meeting_minutes", {"x": 1}, trace_id="t")
    b = new_artifact("meeting_minutes", {"x": 2}, trace_id="t")
    assert a.content_hash != b.content_hash


def test_content_hash_independent_of_key_order():
    a = compute_content_hash({"a": 1, "b": 2})
    b = compute_content_hash({"b": 2, "a": 1})
    assert a == b


def test_artifact_status_constrained_to_allowed_values():
    assert ALLOWED_STATUSES == {"draft", "evaluated", "promoted", "rejected"}
    new_artifact("x", {"k": "v"}, trace_id="t", status="draft")
    with pytest.raises(ValueError):
        Artifact(
            artifact_type="x",
            schema_version=1,
            status="bogus",
            payload={"k": "v"},
            trace_id="t",
        )


def test_store_put_get_list_and_update_status():
    store = ArtifactStore()
    a = new_artifact("meeting_minutes", {"k": "v"}, trace_id="t")
    store.put(a)
    assert store.get(a.artifact_id) is a
    assert a in store.list()
    store.update_status(a.artifact_id, "promoted")
    assert store.get(a.artifact_id).status == "promoted"
    with pytest.raises(ValueError):
        store.update_status(a.artifact_id, "nope")


def test_store_rejects_duplicate_ids():
    store = ArtifactStore()
    a = new_artifact("meeting_minutes", {"k": "v"}, trace_id="t")
    store.put(a)
    with pytest.raises(ValueError):
        store.put(a)


def test_required_envelope_fields_are_present():
    a = new_artifact("meeting_minutes", {"k": "v"}, trace_id="trace-1")
    for field in (
        "artifact_id",
        "artifact_type",
        "schema_version",
        "status",
        "created_at",
        "trace_id",
        "input_refs",
        "content_hash",
        "payload",
    ):
        assert hasattr(a, field), f"missing field {field}"
