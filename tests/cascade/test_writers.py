"""Phase 6 cascade writer tests.

Asserts:
  * `write_filtered_artifact` writes a schema-valid envelope to the
    expected per-meeting directory and records the
    `source_artifact_path` as a real, resolvable path (Pass 1 #9).
  * `write_cascade_filter_log` writes a schema-valid log to the
    diagnostics subtree.
  * `extraction_config` (when passed) lands on the filtered envelope
    with `prompt_variant` carrying the Phase 6 discriminator
    (`production_haiku_with_cascade_filter`).
  * Round-trip: a re-read filtered artifact retains every kept item.
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from spectrum_systems_core.cascade.executor import (
    run_cascade_filter,
    write_cascade_filter_log,
    write_filtered_artifact,
)
from spectrum_systems_core.schemas import schema_path

from ._helpers import (
    DeterministicFilterClient,
    always_keep_rule,
    drop_indexes_rule,
    make_chunk,
    make_decision,
    make_source_artifact,
    make_source_payload,
)


def _load_schema(name: str) -> dict:
    return json.loads(schema_path(name).read_text(encoding="utf-8"))


def test_write_filtered_artifact_round_trip(tmp_path: Path) -> None:
    chunk_text = "a b c d"
    payload = make_source_payload(
        decisions=[make_decision(t) for t in ["a", "b", "c", "d"]],
    )
    source = make_source_artifact(payload)
    client = DeterministicFilterClient(
        decision_rule=drop_indexes_rule([1, 3])
    )
    result = run_cascade_filter(
        source_artifact=source,
        chunks=[make_chunk(chunk_text)],
        api_client=client,
    )

    source_path = tmp_path / "fake_source.json"
    source_path.write_text("{}", encoding="utf-8")

    out_path, envelope = write_filtered_artifact(
        data_lake_path=tmp_path,
        source_id="srcA",
        source_artifact_path=str(source_path),
        result=result,
        extraction_config={
            "prompt_variant": "production_haiku_with_cascade_filter"
        },
        timestamp_suffix="2026-05-20T12-00-00",
    )

    assert out_path.is_file()
    # Sits under store/processed/meetings/<source_id>/.
    assert out_path.parent == (
        tmp_path / "store" / "processed" / "meetings" / "srcA"
    )
    # Round-trip schema validity.
    read_back = json.loads(out_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(
        _load_schema("meeting_minutes_filtered")
    ).validate(read_back)
    # Items survived the round-trip.
    assert (
        read_back["filtered_items"]["decisions"]
        == [d for i, d in enumerate(payload["decisions"]) if i in (0, 2)]
    )
    # source_artifact_path resolves to a real file (Pass 1 #9).
    assert Path(read_back["source_artifact_path"]).is_file()
    # extraction_config carries the Phase-6 discriminator.
    assert (
        read_back["extraction_config"]["prompt_variant"]
        == "production_haiku_with_cascade_filter"
    )


def test_write_cascade_filter_log(tmp_path: Path) -> None:
    payload = make_source_payload(
        decisions=[make_decision("x")],
    )
    source = make_source_artifact(payload)
    client = DeterministicFilterClient(decision_rule=always_keep_rule)
    result = run_cascade_filter(
        source_artifact=source,
        chunks=[make_chunk("x")],
        api_client=client,
    )

    log_path, env = write_cascade_filter_log(
        data_lake_path=tmp_path,
        source_id="srcA",
        source_artifact_path="src.json",
        filtered_artifact_path="filtered.json",
        result=result,
        timestamp_suffix="2026-05-20T12-00-00",
    )
    assert log_path.is_file()
    assert log_path.parent == (
        tmp_path
        / "store"
        / "processed"
        / "meetings"
        / "srcA"
        / "diagnostics"
    )
    jsonschema.Draft202012Validator(
        _load_schema("cascade_filter_log")
    ).validate(json.loads(log_path.read_text(encoding="utf-8")))
    assert env["summary"]["items_in"] == 1
    assert env["summary"]["items_kept"] == 1
    assert env["summary"]["items_dropped"] == 0


def test_write_filtered_artifact_rejects_extra_field(tmp_path: Path) -> None:
    """write_filtered_artifact validates the envelope BEFORE writing —
    a bogus extra field on the result's filter_metadata must surface
    as a ValidationError, not silently land on disk."""
    payload = make_source_payload(decisions=[make_decision("a")])
    source = make_source_artifact(payload)
    client = DeterministicFilterClient(decision_rule=always_keep_rule)
    result = run_cascade_filter(
        source_artifact=source,
        chunks=[make_chunk("a")],
        api_client=client,
    )
    # Mutate filter_metadata to include an unknown key.
    result.filter_metadata["totally_bogus"] = 1
    with pytest.raises(Exception):
        write_filtered_artifact(
            data_lake_path=tmp_path,
            source_id="srcA",
            source_artifact_path="src.json",
            result=result,
        )
