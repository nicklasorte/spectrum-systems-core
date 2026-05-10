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
    """Write a source_record to processed/meetings/<source_id>/source_record.json.

    Mirrors production: the orchestrator stores the original transcript
    filename in ``payload.title``. When the test passes a ``date``, we
    inject it into the title in YYYYMMDD form so the linker can extract
    it via ``date_utils.extract_meeting_date`` — exactly the path real
    records take. ``payload.metadata.date`` is intentionally NOT
    consulted by the linker (the orchestrator seeds it to an epoch
    sentinel for raw drops, which would silently collide).
    """
    artifact_id = str(uuid.uuid4())
    full_title = title
    if date is not None:
        # YYYYMMDD form is the most common production filename pattern.
        full_title = f"{title} {date.replace('-', '')}"
    metadata: Dict[str, Any] = {"source_id": source_id, "source_family": "meetings"}
    rec = {
        "artifact_kind": "source_record",
        "artifact_id": artifact_id,
        "payload": {
            "source_id": source_id,
            "source_family": "meetings",
            "source_type": "transcript",
            "title": full_title,
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
    """meeting_date None on either side never produces a pair (sev-1 guard).

    The transcript's title carries no date, so the linker records
    ``no_date_extractable`` (regex looked at a candidate string and found
    nothing). The minutes side has meeting_date explicitly None and
    records ``no_meeting_date``.
    """
    _write_transcript(
        data_lake, source_id="m_no_date", title="undated transcript", date=None
    )
    _write_minutes(data_lake, meeting_date=None, meeting_name="undated minutes")
    result = GroundTruthLinker().link(str(data_lake))
    assert result["pairs_produced"] == 0
    assert len(result["unmatched_transcripts"]) == 1
    assert len(result["unmatched_minutes"]) == 1
    assert (
        result["unmatched_transcripts"][0]["reason"] == "no_date_extractable"
    )
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
            # Mirror production: the orchestrator stores the original
            # filename in ``title``; the date is extracted from there.
            "title": "Sdl Only Meeting 20260401",
            "metadata": {"source_id": "sdl_only_meeting"},
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


# ---------------------------------------------------------------------------
# Filename-derived meeting_date (the production-bug fix).
# ---------------------------------------------------------------------------


def _write_transcript_with_payload(
    data_lake: Path,
    *,
    source_id: str,
    payload_overrides: Dict[str, Any],
) -> str:
    """Write a source_record where the payload can be customised explicitly.

    Used by the filename-extraction tests below: real production records
    carry the original transcript filename in ``payload.title`` (and/or
    ``raw_path`` / ``processed_path``) but generally have no
    ``payload.metadata.date``.
    """
    artifact_id = str(uuid.uuid4())
    payload: Dict[str, Any] = {
        "source_id": source_id,
        "source_family": "meetings",
        "source_type": "transcript",
        "title": payload_overrides.get("title", source_id),
        "metadata": payload_overrides.get(
            "metadata", {"source_id": source_id, "source_family": "meetings"}
        ),
    }
    for k in ("raw_path", "processed_path"):
        if k in payload_overrides:
            payload[k] = payload_overrides[k]
    rec = {
        "artifact_kind": "source_record",
        "artifact_id": artifact_id,
        "payload": payload,
    }
    sid_dir = data_lake / "store" / "processed" / "meetings" / source_id
    sid_dir.mkdir(parents=True, exist_ok=True)
    (sid_dir / "source_record.json").write_text(
        json.dumps(rec, sort_keys=True, indent=2) + "\n"
    )
    return artifact_id


def test_date_extracted_from_transcript_title(data_lake: Path) -> None:
    """Title carries the full original filename with a date — extract it."""
    _write_transcript_with_payload(
        data_lake,
        source_id="m_22jan2026",
        payload_overrides={
            "title": "7 GHz Downlink TIG Meeting Transcript 22Jan2026",
        },
    )
    _write_minutes(data_lake, meeting_date="2026-01-22", meeting_name="DL TIG")
    result = GroundTruthLinker().link(str(data_lake))
    assert result["pairs_produced"] == 1, result
    pair_files = list(
        (data_lake / "store" / "artifacts" / "ground_truth").glob("*.json")
    )
    assert len(pair_files) == 1
    pair = json.loads(pair_files[0].read_text())
    assert pair["meeting_date"] == "2026-01-22"
    assert pair["match_confidence"] == "high"


def test_date_extracted_from_raw_path(data_lake: Path) -> None:
    """Title is generic; raw_path filename carries the date."""
    _write_transcript_with_payload(
        data_lake,
        source_id="m_20260115",
        payload_overrides={
            "title": "untitled",
            "raw_path": "raw/meetings/m_20260115/transcript 20260115.txt",
        },
    )
    _write_minutes(data_lake, meeting_date="2026-01-15", meeting_name="WG")
    result = GroundTruthLinker().link(str(data_lake))
    assert result["pairs_produced"] == 1, result
    pair = json.loads(
        next(
            (data_lake / "store" / "artifacts" / "ground_truth").glob("*.json")
        ).read_text()
    )
    assert pair["meeting_date"] == "2026-01-15"


def test_no_date_extractable_records_as_unmatched_not_collision(
    data_lake: Path,
) -> None:
    """Two transcripts with no extractable date must NOT collide on epoch."""
    _write_transcript_with_payload(
        data_lake,
        source_id="m_undated_a",
        payload_overrides={"title": "untitled meeting notes"},
    )
    _write_transcript_with_payload(
        data_lake,
        source_id="m_undated_b",
        payload_overrides={"title": "another file with no date"},
    )
    result = GroundTruthLinker().link(str(data_lake))
    assert result["pairs_produced"] == 0
    assert len(result["unmatched_transcripts"]) == 2
    reasons = {e["reason"] for e in result["unmatched_transcripts"]}
    assert reasons == {"no_date_extractable"}, result["unmatched_transcripts"]


# Real transcript filenames from production. Pairs the linker must produce
# when given source_records carrying the original filename in ``title``.
_REAL_TRANSCRIPT_FILENAMES: List[Tuple[str, str, str]] = [
    # (source_id, transcript filename, expected meeting_date)
    ("m_t01", "20251216 - P2P TIG Meeting 16Dec2025 - Transcript", "2025-12-16"),
    ("m_t02", "7 GHz UL Kickoff transcript 20251217", "2025-12-17"),
    (
        "m_t03",
        "7 GHz Downlink TIG Meeting Kickoff - transcript 20251218",
        "2025-12-18",
    ),
    (
        "m_t04",
        "7 GHz Study Working Group Meeting - transcript 20260115",
        "2026-01-15",
    ),
    (
        "m_t05",
        "7 GHz Fixed_Transportable Point to Point (P2P) TIG Meeting Transcript 20260120",
        "2026-01-20",
    ),
    (
        "m_t06",
        "7 GHz Study Plan Comment Adjudication Meeting with Working Group - Transcript 20260121",
        "2026-01-21",
    ),
    ("m_t07", "7 GHz Uplink TIG Meeting Transcript 21Jan26", "2026-01-21"),
    (
        "m_t08",
        "7 GHz Downlink TIG Meeting Transcript - 22Jan2026",
        "2026-01-22",
    ),
    (
        "m_t09",
        "7 GHz Study Working Group Meeting 5Feb2026 - Transcript",
        "2026-02-05",
    ),
    ("m_t10", "7 GHz P2P TIG - Transcript 2-17-26", "2026-02-17"),
    ("m_t11", "7 GHz Uplink TIG - Transcript 2-18-26", "2026-02-18"),
    (
        "m_t12",
        "7 GHz Downlink TIG Meeting - transcript 2-19-26",
        "2026-02-19",
    ),
    (
        "m_t13",
        "7 GHz Study Working Group Meeting - 5Mar2026 - Transcript",
        "2026-03-05",
    ),
]

# Real minutes filenames from production. Each maps to one transcript date,
# except 2026-01-21 which has two minutes (UL TIG Kickoff + adjudication) on
# the same calendar date as one transcript — the linker rule routes the
# whole date to ``duplicate_date_collision`` rather than guessing.
_REAL_MINUTES: List[Tuple[str, str]] = [
    # (meeting_name, expected meeting_date)
    ("P2P TIG Kickoff Meeting Minutes 20251216 FINAL", "2025-12-16"),
    ("7 GHz Uplink TIG Kickoff Meeting Minutes 20251217 FINAL", "2025-12-17"),
    ("7 GHz Downlink TIG Kickoff Meeting Minutes 20251218 FINAL", "2025-12-18"),
    ("7 GHz WG Meeting Minutes 20260115 - Final", "2026-01-15"),
    ("P2P TIG Meeting Minutes 20260120 Final", "2026-01-20"),
    (
        "7 GHz Study Plan Comment Adjudication Meeting Minutes - 20260121 - Final",
        "2026-01-21",
    ),
    ("7 GHz Uplink TIG Meeting Minutes 20260121 Final", "2026-01-21"),
    ("7 GHz Downlink TIG Meeting Minutes 20260122 Final", "2026-01-22"),
    ("7 GHz WG Meeting Minutes 20260205 Final", "2026-02-05"),
    ("7 GHz P2P TIG Meeting Minutes 20260217 Final", "2026-02-17"),
    ("7 GHz Uplink TIG Meeting Minutes 20260218 Final", "2026-02-18"),
    ("7 GHz Downlink TIG Meeting Minutes 20260219 Final", "2026-02-19"),
    ("7 GHz WG Meeting Minutes 20260305 Final", "2026-03-05"),
]


def test_all_13_pairs_match_with_real_filenames(data_lake: Path) -> None:
    """End-to-end: real production filenames must produce 11 high-confidence
    pairs.

    The 2026-01-21 calendar day has two real meetings (Uplink TIG and
    the comment-adjudication WG) — two transcripts and two minutes
    share that date. The linker's ``duplicate_date_collision`` rule
    refuses to silently guess: all four records are routed to
    unmatched. The remaining eleven transcripts pair cleanly with
    their eleven same-date minutes, which is what proves the
    filename-derived date extraction works for every other format in
    production (YYYYMMDD, DDMonYYYY, DDMonYY, M-DD-YY, single-digit-day
    DDMonYYYY).
    """
    for source_id, title, _expected_date in _REAL_TRANSCRIPT_FILENAMES:
        _write_transcript_with_payload(
            data_lake,
            source_id=source_id,
            payload_overrides={"title": title},
        )
    for meeting_name, meeting_date in _REAL_MINUTES:
        _write_minutes(
            data_lake,
            meeting_date=meeting_date,
            meeting_name=meeting_name,
        )
    result = GroundTruthLinker().link(str(data_lake))

    # 11 dates have a clean 1T+1M → 11 pairs. 2026-01-21 has 2T+2M →
    # all four records routed to ``duplicate_date_collision``.
    assert result["pairs_produced"] == 11, result
    assert result["pairs_pending_review"] == 0
    collisions_t = [
        e for e in result["unmatched_transcripts"]
        if e["reason"] == "duplicate_date_collision"
    ]
    collisions_m = [
        e for e in result["unmatched_minutes"]
        if e["reason"] == "duplicate_date_collision"
    ]
    assert len(collisions_t) == 2, result["unmatched_transcripts"]
    assert len(collisions_m) == 2, result["unmatched_minutes"]
    assert all(e["meeting_date"] == "2026-01-21" for e in collisions_t)
    assert all(e["meeting_date"] == "2026-01-21" for e in collisions_m)

    # No transcript should be tagged ``no_date_extractable`` — every
    # production filename in ``_REAL_TRANSCRIPT_FILENAMES`` carries an
    # extractable date.
    no_date = [
        e for e in result["unmatched_transcripts"]
        if e["reason"] == "no_date_extractable"
    ]
    assert no_date == [], no_date

    # Every produced pair must carry a meeting_date that is exactly one
    # of the expected dates in the fixture set.
    expected_dates = {d for _, _, d in _REAL_TRANSCRIPT_FILENAMES}
    pairs_dir = data_lake / "store" / "artifacts" / "ground_truth"
    produced_dates = []
    for path in pairs_dir.glob("*.json"):
        pair = json.loads(path.read_text())
        produced_dates.append(pair["meeting_date"])
        assert pair["match_confidence"] == "high"
        assert pair["status"] == "confirmed"
    assert set(produced_dates).issubset(expected_dates), produced_dates
    # 2026-01-21 must NOT appear in any produced pair (it's collided).
    assert "2026-01-21" not in produced_dates


# ---------------------------------------------------------------------------
# Fix C: --deduplicate flag (CLI) + deduplicate_minutes module
# ---------------------------------------------------------------------------


def _write_minutes_with(
    data_lake: Path,
    *,
    meeting_date: str | None,
    meeting_name: str,
    raw_hash: str,
    created_at: str,
) -> tuple[str, Path]:
    """Variant of _write_minutes that lets the test pin raw_hash and
    created_at — the dedup unit needs collisions on raw_hash and a
    determined oldest-by-created_at order."""
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
        "raw_hash": raw_hash,
        "created_at": created_at,
        "schema_version": "1.0.0",
        "provenance": {"produced_by": "MinutesProcessor"},
    }
    minutes_dir = data_lake / "store" / "artifacts" / "minutes"
    minutes_dir.mkdir(parents=True, exist_ok=True)
    target = minutes_dir / f"{minutes_id}.json"
    target.write_text(
        json.dumps(rec, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return minutes_id, target


def test_deduplicate_flag_retires_duplicate_minutes_records(
    data_lake: Path,
) -> None:
    """Two minutes_records with the same raw_hash → one kept (oldest),
    one moved to retired/. Linking sees only the kept one."""
    from spectrum_systems_core.ingestion.minutes_deduplicator import (
        deduplicate_minutes,
    )

    rh = "sha256:" + ("b" * 64)
    keep_id, keep_path = _write_minutes_with(
        data_lake,
        meeting_date="2026-02-19",
        meeting_name="TIG Meeting",
        raw_hash=rh,
        created_at="2026-02-20T00:00:00+00:00",
    )
    dup_id, dup_path = _write_minutes_with(
        data_lake,
        meeting_date="2026-02-19",
        meeting_name="TIG Meeting",
        raw_hash=rh,
        created_at="2026-02-21T00:00:00+00:00",
    )

    result = deduplicate_minutes(str(data_lake))

    assert result["status"] == "success"
    assert result["groups_found"] == 1
    assert result["records_kept"] == 1
    assert result["records_retired"] == 1

    # Keeper is still in place.
    assert keep_path.is_file()
    # Duplicate moved to retired/.
    retired_dir = data_lake / "store" / "artifacts" / "minutes" / "retired"
    assert (retired_dir / dup_path.name).is_file()
    assert not dup_path.exists()
    # Sidecar reason file.
    sidecar = retired_dir / (dup_path.stem + ".retired_reason.json")
    assert sidecar.is_file()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["retired_reason"] == "duplicate"
    assert payload["minutes_id"] == dup_id
    assert payload["kept_minutes_id"] == keep_id


def test_deduplicate_keeps_oldest_by_created_at(data_lake: Path) -> None:
    """Tie-breaker: oldest created_at wins regardless of file order."""
    from spectrum_systems_core.ingestion.minutes_deduplicator import (
        deduplicate_minutes,
    )

    rh = "sha256:" + ("c" * 64)
    older_id, older_path = _write_minutes_with(
        data_lake,
        meeting_date="2026-02-19",
        meeting_name="A",
        raw_hash=rh,
        created_at="2025-01-01T00:00:00+00:00",
    )
    newer_id, newer_path = _write_minutes_with(
        data_lake,
        meeting_date="2026-02-19",
        meeting_name="A",
        raw_hash=rh,
        created_at="2026-12-31T23:59:59+00:00",
    )

    result = deduplicate_minutes(str(data_lake))
    assert result["records_retired"] == 1
    # Older kept, newer retired.
    assert older_path.is_file()
    assert not newer_path.exists()
    retired_dir = data_lake / "store" / "artifacts" / "minutes" / "retired"
    assert (retired_dir / newer_path.name).is_file()


def test_deduplicate_noop_when_no_duplicates(data_lake: Path) -> None:
    """Single record per raw_hash → nothing retired, nothing moved."""
    from spectrum_systems_core.ingestion.minutes_deduplicator import (
        deduplicate_minutes,
    )

    _write_minutes_with(
        data_lake,
        meeting_date="2026-02-19",
        meeting_name="X",
        raw_hash="sha256:" + ("d" * 64),
        created_at="2026-01-01T00:00:00+00:00",
    )
    _write_minutes_with(
        data_lake,
        meeting_date="2026-03-19",
        meeting_name="Y",
        raw_hash="sha256:" + ("e" * 64),
        created_at="2026-01-02T00:00:00+00:00",
    )

    result = deduplicate_minutes(str(data_lake))
    assert result["status"] == "success"
    assert result["groups_found"] == 0
    assert result["records_retired"] == 0
    retired_dir = data_lake / "store" / "artifacts" / "minutes" / "retired"
    assert not retired_dir.exists() or list(retired_dir.iterdir()) == []


def test_linking_after_deduplication_produces_correct_pairs(
    data_lake: Path,
) -> None:
    """End-to-end: two duplicate minutes_records on the same date used to
    fail the linker (`duplicate_date_collision` for both). After dedup,
    the surviving single record matches the lone transcript and produces
    one high-confidence pair."""
    from spectrum_systems_core.ingestion.minutes_deduplicator import (
        deduplicate_minutes,
    )

    _write_transcript(
        data_lake,
        source_id="tig-meeting",
        title="TIG Meeting",
        date="2026-02-19",
    )
    rh = "sha256:" + ("f" * 64)
    _write_minutes_with(
        data_lake,
        meeting_date="2026-02-19",
        meeting_name="TIG Meeting",
        raw_hash=rh,
        created_at="2026-02-20T00:00:00+00:00",
    )
    _write_minutes_with(
        data_lake,
        meeting_date="2026-02-19",
        meeting_name="TIG Meeting",
        raw_hash=rh,
        created_at="2026-02-21T00:00:00+00:00",
    )

    # Pre-dedup: linker sees a duplicate_date_collision and produces
    # zero pairs.
    pre = GroundTruthLinker().link(str(data_lake))
    assert pre["pairs_produced"] == 0
    assert any(
        e["reason"] == "duplicate_date_collision"
        for e in pre["unmatched_minutes"]
    )

    # Wipe stale linking_report between runs (linker rewrites pairs).
    pairs_dir = data_lake / "store" / "artifacts" / "ground_truth"
    for p in pairs_dir.glob("*.json"):
        p.unlink()

    # Run dedup, then link again.
    dedup = deduplicate_minutes(str(data_lake))
    assert dedup["records_retired"] == 1

    post = GroundTruthLinker().link(str(data_lake))
    assert post["pairs_produced"] == 1
    assert post["pairs_pending_review"] == 0
    # The kept minutes_record is still on disk; the retired one is in
    # retired/ and was excluded from linking.
    minutes_dir = data_lake / "store" / "artifacts" / "minutes"
    assert len(list(minutes_dir.glob("*.json"))) == 1


def test_cli_deduplicate_flag_invokes_dedup_before_linking(
    data_lake: Path, capsys
) -> None:
    """Verify the CLI wiring: --deduplicate moves the duplicate before
    the linker runs and prints the summary line."""
    from spectrum_systems_core.cli import main as cli_main

    _write_transcript(
        data_lake,
        source_id="tig-meeting",
        title="TIG Meeting",
        date="2026-02-19",
    )
    rh = "sha256:" + ("9" * 64)
    _write_minutes_with(
        data_lake,
        meeting_date="2026-02-19",
        meeting_name="TIG Meeting",
        raw_hash=rh,
        created_at="2026-02-20T00:00:00+00:00",
    )
    _write_minutes_with(
        data_lake,
        meeting_date="2026-02-19",
        meeting_name="TIG Meeting",
        raw_hash=rh,
        created_at="2026-02-21T00:00:00+00:00",
    )

    rc = cli_main(
        [
            "link-ground-truth",
            "--deduplicate",
            "--data-lake",
            str(data_lake),
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "Found 1 duplicate groups" in captured.out
    assert "Retired 1 duplicate records" in captured.out
    assert "Pairs produced (high confidence): 1" in captured.out
