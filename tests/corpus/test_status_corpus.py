"""Phase 4 — ``spectrum-core status --corpus`` integration tests.

Pass-2 #3 (status corpus rollup correctness) is the headline case: a
fixture lake with multiple states (comparison_complete, validated,
quarantined, under_review, orphaned_in_lake) and the rollup asserts
all rows are present with the expected (state, recommendation).

Pass-1 #6/#7 — state-enum and recommendation-enum drift tests live in
``test_status_enum_drift.py``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


from spectrum_systems_core.corpus.manifest_loader import compute_manifest_hash
from spectrum_systems_core.corpus.status import (
    RECOMMENDATION_BASELINE_OPUS,
    RECOMMENDATION_INGEST,
    RECOMMENDATION_INVESTIGATE_ORPHAN,
    RECOMMENDATION_NONE,
    RECOMMENDATION_REVIEW_QUARANTINED,
    RECOMMENDATION_RUN_COMPARISON,
    STATE_BASELINE_COMPLETE,
    STATE_COMPARISON_COMPLETE,
    STATE_ORPHANED_IN_LAKE,
    STATE_PENDING,
    STATE_QUARANTINED,
    STATE_UNDER_REVIEW,
    STATE_VALIDATED,
    build_corpus_status_report,
)
from spectrum_systems_core.data_lake.cli import main as cli_main


def _entry(
    sid: str,
    *,
    ingestion_status: str = "pending",
    expected_path: str | None = None,
) -> Dict[str, Any]:
    return {
        "source_id": sid,
        "declared": {
            "expected_path": expected_path or f"raw/transcripts/{sid}.txt",
            "meeting_date": "2026-05-20",
            "meeting_type": "working_group",
            "supersedes": None,
        },
        "observed": {
            "detected_speaker_count": None,
            "detected_word_count": None,
            "ingestion_status": ingestion_status,
            "last_updated": None,
        },
    }


def _write_manifest(tmp_path: Path, sources: List[Dict[str, Any]]) -> Path:
    p = tmp_path / "manifest.json"
    p.write_text(
        json.dumps(
            {
                "artifact_type": "corpus_manifest",
                "schema_version": "1.0.0",
                "manifest_hash": compute_manifest_hash(sources),
                "sources": sources,
            }
        ),
        encoding="utf-8",
    )
    return p


def _seed_dir(lake: Path, sid: str) -> Path:
    d = lake / "processed" / "meetings" / sid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _seed_source_record(lake: Path, sid: str) -> None:
    d = _seed_dir(lake, sid)
    (d / "source_record.json").write_text(
        json.dumps({"artifact_type": "source_record", "source_id": sid}),
        encoding="utf-8",
    )


def _seed_opus_baseline(lake: Path, sid: str) -> None:
    d = _seed_dir(lake, sid)
    (d / "meeting_minutes_opus__abc.json").write_text(
        json.dumps({"artifact_type": "meeting_minutes_opus"}),
        encoding="utf-8",
    )


def _seed_comparison(lake: Path, sid: str) -> None:
    d = _seed_dir(lake, sid)
    (d / "comparison_result__abc.json").write_text(
        json.dumps({"artifact_type": "comparison_result"}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Pass 2 #3 — comprehensive rollup.
# ---------------------------------------------------------------------------


def test_rollup_reports_all_six_state_categories(tmp_path: Path) -> None:
    """A fixture lake with all six expected states produces six rows
    in the rollup."""
    lake = tmp_path / "lake"
    (lake / "processed" / "meetings").mkdir(parents=True)
    (lake / "raw" / "transcripts").mkdir(parents=True)

    sources = [
        _entry("src-cc", ingestion_status="comparison_complete"),
        _entry("src-bc", ingestion_status="baseline_complete"),
        _entry("src-val", ingestion_status="validated"),
        _entry("src-pend", ingestion_status="pending"),
        _entry("src-quar", ingestion_status="quarantined"),
        _entry("src-ur", ingestion_status="under_review"),
        _entry("src-sup", ingestion_status="superseded"),
    ]
    manifest = _write_manifest(tmp_path, sources)

    # Build the on-disk artifact set so the derived state matches the
    # manifest's intent.
    _seed_source_record(lake, "src-cc")
    _seed_opus_baseline(lake, "src-cc")
    _seed_comparison(lake, "src-cc")

    _seed_source_record(lake, "src-bc")
    _seed_opus_baseline(lake, "src-bc")

    _seed_source_record(lake, "src-val")

    # An orphan dir with artifacts but not in the manifest.
    orphan_dir = lake / "processed" / "meetings" / "src-orphan"
    orphan_dir.mkdir()
    (orphan_dir / "source_record.json").write_text(
        json.dumps({"artifact_type": "source_record", "source_id": "src-orphan"}),
        encoding="utf-8",
    )

    report = build_corpus_status_report(lake_root=lake, manifest_path=manifest)
    rows = {r["source_id"]: r for r in report["rows"]}

    assert rows["src-cc"]["state"] == STATE_COMPARISON_COMPLETE
    assert rows["src-cc"]["recommendation"] == RECOMMENDATION_NONE

    assert rows["src-bc"]["state"] == STATE_BASELINE_COMPLETE
    assert rows["src-bc"]["recommendation"] == RECOMMENDATION_RUN_COMPARISON

    assert rows["src-val"]["state"] == STATE_VALIDATED
    assert rows["src-val"]["recommendation"] == RECOMMENDATION_BASELINE_OPUS

    assert rows["src-pend"]["state"] == STATE_PENDING
    assert rows["src-pend"]["recommendation"] == RECOMMENDATION_INGEST

    assert rows["src-quar"]["state"] == STATE_QUARANTINED
    assert rows["src-quar"]["recommendation"] == RECOMMENDATION_REVIEW_QUARANTINED

    assert rows["src-ur"]["state"] == STATE_UNDER_REVIEW
    assert rows["src-ur"]["recommendation"] == RECOMMENDATION_BASELINE_OPUS

    assert rows["src-orphan"]["state"] == STATE_ORPHANED_IN_LAKE
    assert rows["src-orphan"]["recommendation"] == RECOMMENDATION_INVESTIGATE_ORPHAN

    # Report carries the manifest hash for replay.
    assert report["manifest_hash"] == compute_manifest_hash(sources)


# ---------------------------------------------------------------------------
# Pass 2 #6 — schema additivity: a status report validates.
# ---------------------------------------------------------------------------


def test_status_report_validates_against_schema(tmp_path: Path) -> None:
    """The built rollup validates against status_report.schema.json
    in-process (build_corpus_status_report calls the validator)."""
    lake = tmp_path / "lake"
    (lake / "processed" / "meetings").mkdir(parents=True)
    sources = [_entry("src-a")]
    manifest = _write_manifest(tmp_path, sources)
    report = build_corpus_status_report(lake_root=lake, manifest_path=manifest)
    # The schema validation already ran in build_corpus_status_report.
    # Re-validate explicitly for the record.
    import jsonschema

    from spectrum_systems_core.schemas import schema_path

    schema = json.loads(schema_path("status_report").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(report)


# ---------------------------------------------------------------------------
# CLI surface.
# ---------------------------------------------------------------------------


def test_cli_requires_corpus_flag(tmp_path: Path, capsys) -> None:
    lake = tmp_path / "lake"
    lake.mkdir()
    rc = cli_main(["status", "--lake", str(lake)])
    assert rc == 2
    assert "--corpus" in capsys.readouterr().out


def test_cli_emits_json(tmp_path: Path, capsys) -> None:
    lake = tmp_path / "lake"
    (lake / "processed" / "meetings").mkdir(parents=True)
    sources = [_entry("src-a")]
    manifest = _write_manifest(tmp_path, sources)
    rc = cli_main(
        [
            "status",
            "--lake",
            str(lake),
            "--corpus",
            "--manifest",
            str(manifest),
            "--json",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["artifact_type"] == "status_report"
    assert parsed["schema_version"] == "1.0.0"
    assert any(r["source_id"] == "src-a" for r in parsed["rows"])


def test_orphan_dir_without_artifacts_not_flagged(tmp_path: Path) -> None:
    """An empty processed/meetings/<sid>/ dir is NOT an orphan — it
    must contain a recognised pipeline artifact to surface."""
    lake = tmp_path / "lake"
    (lake / "processed" / "meetings" / "src-empty").mkdir(parents=True)
    sources = [_entry("src-real")]
    manifest = _write_manifest(tmp_path, sources)
    report = build_corpus_status_report(lake_root=lake, manifest_path=manifest)
    ids = {r["source_id"] for r in report["rows"]}
    assert "src-empty" not in ids
