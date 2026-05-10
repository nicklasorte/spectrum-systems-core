"""Tests for GroundTruthLinker (Phase L.2).

No LLM calls. No hard-coded UUIDs.
"""
from __future__ import annotations

import io
import json
import uuid
from pathlib import Path
from typing import Any, Dict

import jsonschema
import pytest

from spectrum_systems_core.cli import link_ground_truth
from spectrum_systems_core.ingestion._paths import contracts_root
from spectrum_systems_core.ingestion.ground_truth_linker import GroundTruthLinker


# ---------------------------------------------------------------------------
# Fixtures: minimal data-lake plus helpers to seed transcripts and minutes.
# ---------------------------------------------------------------------------


def _make_data_lake(tmp_path: Path) -> Path:
    dl = tmp_path / "data-lake"
    (dl / "store" / "raw" / "minutes").mkdir(parents=True)
    (dl / "store" / "artifacts").mkdir(parents=True)
    (dl / "store" / "processed" / "meetings").mkdir(parents=True)
    return dl


@pytest.fixture
def data_lake(tmp_path: Path, monkeypatch) -> Path:
    dl = _make_data_lake(tmp_path)
    monkeypatch.setenv("DATA_LAKE_PATH", str(dl))
    monkeypatch.setenv("SDL_ROOT", str(dl / "store" / "artifacts"))
    return dl


def _write_transcript(
    data_lake: Path,
    *,
    source_id: str,
    title: str,
    date: str | None,
) -> str:
    """Write a source_record to processed/meetings/<source_id>/source_record.json."""
    artifact_id = str(uuid.uuid4())
    metadata: Dict[str, Any] = {"source_id": source_id, "source_family": "meetings"}
    if date is not None:
        metadata["date"] = date
    rec = {
        "artifact_kind": "source_record",
        "artifact_id": artifact_id,
        "payload": {
            "source_id": source_id,
            "source_family": "meetings",
            "source_type": "transcript",
            "title": title,
            "metadata": metadata,
        },
    }
    sid_dir = data_lake / "store" / "processed" / "meetings" / source_id
    sid_dir.mkdir(parents=True, exist_ok=True)
    (sid_dir / "source_record.json").write_text(
        json.dumps(rec, sort_keys=True, indent=2) + "\n"
    )
    return artifact_id


def _write_minutes(
    data_lake: Path,
    *,
    meeting_date: str | None,
    meeting_name: str,
) -> str:
    """Write a minutes_record artifact to SDL_ROOT/minutes/<id>.json."""
    minutes_id = str(uuid.uuid4())
    rec: Dict[str, Any] = {
        "minutes_id": minutes_id,
        "docx_path": "/fake/path.docx",
        "txt_path": "/fake/path.txt",
        "meeting_date": meeting_date,
        "meeting_name": meeting_name,
        "text_unit_count": 10,
        "character_count": 500,
        "table_count": 0,
        "raw_hash": "sha256:" + ("a" * 64),
        "created_at": "2026-01-01T00:00:00+00:00",
        "schema_version": "1.0.0",
        "provenance": {"produced_by": "MinutesProcessor"},
    }
    minutes_dir = data_lake / "store" / "artifacts" / "minutes"
    minutes_dir.mkdir(parents=True, exist_ok=True)
    (minutes_dir / f"{minutes_id}.json").write_text(
        json.dumps(rec, sort_keys=True, indent=2) + "\n"
    )
    return minutes_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_exact_date_match_produces_high_confidence_pair(data_lake: Path) -> None:
    _write_transcript(
        data_lake, source_id="m_2026_02_19", title="TIG Meeting", date="2026-02-19"
    )
    _write_minutes(data_lake, meeting_date="2026-02-19", meeting_name="TIG Meeting")
    result = GroundTruthLinker().link(str(data_lake))
    assert result["status"] == "success"
    assert result["pairs_produced"] == 1
    assert result["pairs_pending_review"] == 0
    assert result["unmatched_transcripts"] == []
    assert result["unmatched_minutes"] == []
    # The pair is on disk.
    pairs_dir = data_lake / "store" / "artifacts" / "ground_truth"
    pair_files = list(pairs_dir.glob("*.json"))
    assert len(pair_files) == 1
    pair = json.loads(pair_files[0].read_text())
    assert pair["match_confidence"] == "high"
    assert pair["status"] == "confirmed"


def test_one_day_difference_produces_medium_confidence_pair(data_lake: Path) -> None:
    _write_transcript(
        data_lake, source_id="m_2026_02_19", title="TIG", date="2026-02-19"
    )
    _write_minutes(data_lake, meeting_date="2026-02-20", meeting_name="TIG")
    result = GroundTruthLinker().link(str(data_lake))
    assert result["pairs_produced"] == 1
    assert result["pairs_pending_review"] == 1
    pairs_dir = data_lake / "store" / "artifacts" / "ground_truth"
    pair = json.loads(next(pairs_dir.glob("*.json")).read_text())
    assert pair["match_confidence"] == "medium"
    assert pair["status"] == "pending_review"


def test_no_date_match_produces_unmatched_not_pair(data_lake: Path) -> None:
    _write_transcript(
        data_lake, source_id="m_2026_02_19", title="TIG", date="2026-02-19"
    )
    _write_minutes(data_lake, meeting_date="2026-05-10", meeting_name="Other")
    result = GroundTruthLinker().link(str(data_lake))
    assert result["pairs_produced"] == 0
    assert len(result["unmatched_transcripts"]) == 1
    assert len(result["unmatched_minutes"]) == 1
    assert result["unmatched_transcripts"][0]["reason"] == "no_candidate"
    assert result["unmatched_minutes"][0]["reason"] == "no_candidate"


def test_unmatched_transcripts_recorded_in_report(data_lake: Path) -> None:
    _write_transcript(
        data_lake, source_id="m_alone", title="Alone", date="2026-02-19"
    )
    result = GroundTruthLinker().link(str(data_lake))
    assert result["pairs_produced"] == 0
    assert len(result["unmatched_transcripts"]) == 1
    report = json.loads(Path(result["linking_report_path"]).read_text())
    assert report["total_transcripts"] == 1
    assert report["total_minutes"] == 0
    assert len(report["unmatched_transcripts"]) == 1


def test_unmatched_minutes_recorded_in_report(data_lake: Path) -> None:
    _write_minutes(data_lake, meeting_date="2026-02-19", meeting_name="Solo")
    result = GroundTruthLinker().link(str(data_lake))
    assert result["pairs_produced"] == 0
    assert len(result["unmatched_minutes"]) == 1
    report = json.loads(Path(result["linking_report_path"]).read_text())
    assert report["total_minutes"] == 1
    assert len(report["unmatched_minutes"]) == 1


def test_ground_truth_pair_schema_validates(data_lake: Path) -> None:
    _write_transcript(
        data_lake, source_id="m_2026_02_19", title="TIG", date="2026-02-19"
    )
    _write_minutes(data_lake, meeting_date="2026-02-19", meeting_name="TIG")
    GroundTruthLinker().link(str(data_lake))
    pair_schema = json.loads(
        (
            contracts_root()
            / "schemas"
            / "ingestion"
            / "ground_truth_pair.schema.json"
        ).read_text()
    )
    pairs_dir = data_lake / "store" / "artifacts" / "ground_truth"
    for path in pairs_dir.glob("*.json"):
        pair = json.loads(path.read_text())
        jsonschema.Draft202012Validator(pair_schema).validate(pair)


def test_linking_report_written_even_when_zero_pairs(data_lake: Path) -> None:
    # Empty: no transcripts, no minutes.
    result = GroundTruthLinker().link(str(data_lake))
    assert result["pairs_produced"] == 0
    report_path = Path(result["linking_report_path"])
    assert report_path.is_file()
    report = json.loads(report_path.read_text())
    schema = json.loads(
        (
            contracts_root()
            / "schemas"
            / "ingestion"
            / "linking_report.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(report)
    assert report["total_transcripts"] == 0
    assert report["total_minutes"] == 0


def test_medium_confidence_pair_status_pending_review(data_lake: Path) -> None:
    _write_transcript(
        data_lake, source_id="m_a", title="A", date="2026-02-19"
    )
    _write_minutes(data_lake, meeting_date="2026-02-18", meeting_name="A")
    result = GroundTruthLinker().link(str(data_lake))
    pairs_dir = data_lake / "store" / "artifacts" / "ground_truth"
    pair = json.loads(next(pairs_dir.glob("*.json")).read_text())
    assert pair["match_confidence"] == "medium"
    assert pair["status"] == "pending_review"
    assert pair["confirmed_at"] is None
    assert pair["confirmed_by"] is None
    # Reported counter consistency.
    assert result["pairs_pending_review"] == 1


def test_link_never_raises(tmp_path: Path) -> None:
    # Nonexistent data lake path. SDL_ROOT also unset.
    result = GroundTruthLinker().link("/totally/missing/path/here")
    assert result["status"] == "failure"
    assert "sdl_root_unresolved" in result["reason"]


def test_none_meeting_date_never_matches(data_lake: Path) -> None:
    """meeting_date None on either side never produces a pair (sev-1 guard)."""
    _write_transcript(
        data_lake, source_id="m_no_date", title="undated transcript", date=None
    )
    _write_minutes(data_lake, meeting_date=None, meeting_name="undated minutes")
    result = GroundTruthLinker().link(str(data_lake))
    assert result["pairs_produced"] == 0
    assert len(result["unmatched_transcripts"]) == 1
    assert len(result["unmatched_minutes"]) == 1
    assert result["unmatched_transcripts"][0]["reason"] == "no_meeting_date"
    assert result["unmatched_minutes"][0]["reason"] == "no_meeting_date"


def test_duplicate_date_collision_routes_all_to_unmatched(data_lake: Path) -> None:
    """Two minutes on the same date as one transcript → none auto-pair."""
    _write_transcript(
        data_lake, source_id="m_only", title="solo", date="2026-02-19"
    )
    _write_minutes(data_lake, meeting_date="2026-02-19", meeting_name="A")
    _write_minutes(data_lake, meeting_date="2026-02-19", meeting_name="B")
    result = GroundTruthLinker().link(str(data_lake))
    assert result["pairs_produced"] == 0
    assert len(result["unmatched_transcripts"]) == 1
    assert len(result["unmatched_minutes"]) == 2
    assert all(
        e["reason"] == "duplicate_date_collision"
        for e in result["unmatched_minutes"]
    )
    assert (
        result["unmatched_transcripts"][0]["reason"] == "duplicate_date_collision"
    )


def test_ambiguous_fuzzy_match_routes_to_unmatched(data_lake: Path) -> None:
    """A transcript on D with minutes on D-1 and D+1 → no pair.

    Both the transcript AND the two blocked minutes records must carry
    ``ambiguous_fuzzy_match`` (not the misleading ``no_candidate``) —
    each Mn's only fuzzy candidate was the now-blocked T.
    """
    _write_transcript(
        data_lake, source_id="m_2026_02_19", title="T", date="2026-02-19"
    )
    _write_minutes(data_lake, meeting_date="2026-02-18", meeting_name="A")
    _write_minutes(data_lake, meeting_date="2026-02-20", meeting_name="B")
    result = GroundTruthLinker().link(str(data_lake))
    assert result["pairs_produced"] == 0
    assert len(result["unmatched_transcripts"]) == 1
    assert (
        result["unmatched_transcripts"][0]["reason"] == "ambiguous_fuzzy_match"
    )
    assert len(result["unmatched_minutes"]) == 2
    assert all(
        e["reason"] == "ambiguous_fuzzy_match"
        for e in result["unmatched_minutes"]
    ), result["unmatched_minutes"]


def test_two_day_difference_does_not_match(data_lake: Path) -> None:
    _write_transcript(
        data_lake, source_id="m_2026_02_19", title="T", date="2026-02-19"
    )
    _write_minutes(data_lake, meeting_date="2026-02-21", meeting_name="A")
    result = GroundTruthLinker().link(str(data_lake))
    assert result["pairs_produced"] == 0
    assert result["unmatched_transcripts"][0]["reason"] == "no_candidate"
    assert result["unmatched_minutes"][0]["reason"] == "no_candidate"


def test_cli_link_ground_truth_exits_0(data_lake: Path) -> None:
    _write_transcript(
        data_lake, source_id="m_2026_02_19", title="TIG", date="2026-02-19"
    )
    _write_minutes(data_lake, meeting_date="2026-02-19", meeting_name="TIG")
    out = io.StringIO()
    rc = link_ground_truth(
        data_lake=str(data_lake), process_minutes=False, out_stream=out
    )
    assert rc == 0
    text = out.getvalue()
    assert "Pairs produced (high confidence): 1" in text
    assert "Linking report:" in text


def test_cli_missing_data_lake_exits_1(monkeypatch) -> None:
    monkeypatch.delenv("DATA_LAKE_PATH", raising=False)
    out = io.StringIO()
    rc = link_ground_truth(
        data_lake=None, process_minutes=False, out_stream=out
    )
    assert rc == 1
    assert "DATA_LAKE_PATH not set" in out.getvalue()


def test_cli_missing_path_on_disk_exits_1(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DATA_LAKE_PATH", raising=False)
    out = io.StringIO()
    rc = link_ground_truth(
        data_lake=str(tmp_path / "does-not-exist"),
        process_minutes=False,
        out_stream=out,
    )
    assert rc == 1
    assert "data lake path does not exist" in out.getvalue()


def test_sdl_root_only_transcripts_also_picked_up(data_lake: Path) -> None:
    """A source_record present only as flat $SDL_ROOT/<id>.json is found."""
    artifact_id = str(uuid.uuid4())
    rec = {
        "artifact_kind": "source_record",
        "artifact_id": artifact_id,
        "payload": {
            "source_id": "sdl_only_meeting",
            "source_family": "meetings",
            "source_type": "transcript",
            "title": "Sdl Only",
            "metadata": {"date": "2026-04-01"},
        },
    }
    sdl = data_lake / "store" / "artifacts"
    (sdl / f"{artifact_id}.json").write_text(
        json.dumps(rec, sort_keys=True, indent=2) + "\n"
    )
    _write_minutes(data_lake, meeting_date="2026-04-01", meeting_name="Sdl Only")
    result = GroundTruthLinker().link(str(data_lake))
    assert result["pairs_produced"] == 1


def test_dedup_when_processed_and_sdl_root_carry_same_source_id(
    data_lake: Path,
) -> None:
    """Same source_id in both processed/ and SDL_ROOT must not double-count."""
    aid = _write_transcript(
        data_lake,
        source_id="m_dedup",
        title="dup",
        date="2026-02-19",
    )
    # Now also write the same source_record into SDL_ROOT root.
    sdl = data_lake / "store" / "artifacts"
    rec_path = (
        data_lake / "store" / "processed" / "meetings" / "m_dedup" / "source_record.json"
    )
    (sdl / f"{aid}.json").write_text(rec_path.read_text())
    _write_minutes(data_lake, meeting_date="2026-02-19", meeting_name="dup")
    result = GroundTruthLinker().link(str(data_lake))
    # If dedup is broken the linker would see 2 transcripts on the same
    # date and route to duplicate_date_collision.
    assert result["pairs_produced"] == 1
    report = json.loads(Path(result["linking_report_path"]).read_text())
    assert report["total_transcripts"] == 1
