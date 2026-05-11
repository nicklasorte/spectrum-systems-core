"""Phase P — tests for check-preflight CLI command.

All tests stage a real data-lake under tmp_path and let
``check-preflight`` invoke the real ``scan_pipeline_state`` against it.
We do not mock verify-pipeline-state output: the point of pre-flight is
that it must operate on **fresh** scan results, never on cached records.
Test the real path or the test proves nothing.
"""
from __future__ import annotations

import io
import json
import uuid
from pathlib import Path
from typing import Any, Dict

import pytest

from spectrum_systems_core.cli import check_preflight


def _stage_data_lake(tmp_path: Path) -> Path:
    root = tmp_path / "data-lake"
    (root / "store" / "artifacts").mkdir(parents=True)
    (root / "store" / "processed" / "meetings").mkdir(parents=True)
    (root / "store" / "raw" / "transcripts").mkdir(parents=True)
    return root


def _write_processed_source_record(data_lake: Path, source_id: str) -> None:
    sid_dir = data_lake / "store" / "processed" / "meetings" / source_id
    sid_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "artifact_type": "source_record",
        "schema_version": "1.0.0",
        "artifact_id": str(uuid.uuid4()),
        "payload": {"source_id": source_id, "source_family": "meetings"},
    }
    (sid_dir / "source_record.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _write_chunks_jsonl(data_lake: Path, source_id: str) -> None:
    sid_dir = data_lake / "store" / "processed" / "meetings" / source_id
    sid_dir.mkdir(parents=True, exist_ok=True)
    (sid_dir / "chunks.jsonl").write_text(
        '{"chunk_id": "0", "text": "hi"}\n', encoding="utf-8"
    )


def _write_kind_only_artifact(sdl_root: Path) -> None:
    """An artifact that carries artifact_kind but not artifact_type."""
    target = sdl_root / "minutes"
    target.mkdir(parents=True, exist_ok=True)
    obj: Dict[str, Any] = {
        "artifact_kind": "minutes_record",
        "schema_version": "1.0.0",
        "minutes_id": str(uuid.uuid4()),
        "artifact_id": str(uuid.uuid4()),
        "payload": {},
    }
    (target / f"{obj['minutes_id']}.json").write_text(
        json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_blocks_when_artifact_kind_only_present(
    tmp_path: Path, monkeypatch
) -> None:
    """Sev-1 guard: pre-flight must refuse to run when migration is incomplete."""
    data_lake = _stage_data_lake(tmp_path)
    sdl_root = data_lake / "store" / "artifacts"
    _write_processed_source_record(data_lake, "a")
    _write_chunks_jsonl(data_lake, "a")
    # Plant 5 legacy artifact_kind-only artifacts.
    for _ in range(5):
        _write_kind_only_artifact(sdl_root)

    monkeypatch.setenv("SDL_ROOT", str(sdl_root))

    buf = io.StringIO()
    rc = check_preflight(data_lake=str(data_lake), out_stream=buf)
    assert rc == 1, buf.getvalue()
    text = buf.getvalue()
    assert "migrate-artifact-kind" in text
    # Cross-link to the runbook is mandatory per Part D2.
    assert "verification-cycle-recovery.md" in text


def test_allows_with_emergency_flag_logs_bypass_loudly(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """--allow-mixed-migration bypasses guard 1, but must log the bypass."""
    data_lake = _stage_data_lake(tmp_path)
    sdl_root = data_lake / "store" / "artifacts"
    _write_processed_source_record(data_lake, "a")
    _write_chunks_jsonl(data_lake, "a")
    _write_kind_only_artifact(sdl_root)

    monkeypatch.setenv("SDL_ROOT", str(sdl_root))

    buf = io.StringIO()
    rc = check_preflight(
        data_lake=str(data_lake),
        allow_mixed_migration=True,
        out_stream=buf,
    )
    captured = capsys.readouterr()
    assert rc == 0, buf.getvalue()
    # Bypass must be loud: stdout AND stderr both carry the warning.
    assert "BYPASS ACTIVE" in buf.getvalue()
    assert "BYPASS ACTIVE" in captured.err


def test_allows_with_emergency_flag_writes_to_step_summary(
    tmp_path: Path, monkeypatch
) -> None:
    """The bypass also appends to $GITHUB_STEP_SUMMARY when set."""
    data_lake = _stage_data_lake(tmp_path)
    sdl_root = data_lake / "store" / "artifacts"
    _write_processed_source_record(data_lake, "a")
    _write_chunks_jsonl(data_lake, "a")
    _write_kind_only_artifact(sdl_root)

    monkeypatch.setenv("SDL_ROOT", str(sdl_root))
    step_path = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step_path))

    buf = io.StringIO()
    rc = check_preflight(
        data_lake=str(data_lake),
        allow_mixed_migration=True,
        out_stream=buf,
    )
    assert rc == 0
    assert step_path.is_file()
    body = step_path.read_text(encoding="utf-8")
    assert "EMERGENCY BYPASS ACTIVE" in body


def test_warns_on_missing_chunks_but_exits_zero(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Missing chunks.jsonl is a warning, not a block (force-run may regenerate)."""
    data_lake = _stage_data_lake(tmp_path)
    sdl_root = data_lake / "store" / "artifacts"
    # Two source_records; only one has chunks.jsonl.
    _write_processed_source_record(data_lake, "with_chunks")
    _write_chunks_jsonl(data_lake, "with_chunks")
    _write_processed_source_record(data_lake, "no_chunks_a")
    _write_processed_source_record(data_lake, "no_chunks_b")

    monkeypatch.setenv("SDL_ROOT", str(sdl_root))

    buf = io.StringIO()
    rc = check_preflight(data_lake=str(data_lake), out_stream=buf)
    captured = capsys.readouterr()
    assert rc == 0, buf.getvalue()
    # Warning must list the specific missing source_ids and go to stderr.
    assert "chunks.jsonl missing" in captured.err
    assert "no_chunks_a" in captured.err
    assert "no_chunks_b" in captured.err


def test_blocks_when_no_source_records(
    tmp_path: Path, monkeypatch
) -> None:
    """Zero source_records => ingestion has not been run => block."""
    data_lake = _stage_data_lake(tmp_path)
    sdl_root = data_lake / "store" / "artifacts"
    monkeypatch.setenv("SDL_ROOT", str(sdl_root))

    buf = io.StringIO()
    rc = check_preflight(data_lake=str(data_lake), out_stream=buf)
    assert rc == 1, buf.getvalue()
    text = buf.getvalue()
    assert "No source_records" in text
    assert "verification-cycle-recovery.md" in text


def test_allows_when_migration_complete(
    tmp_path: Path, monkeypatch
) -> None:
    """Happy-path counterpart: clean data lake => exit 0."""
    data_lake = _stage_data_lake(tmp_path)
    sdl_root = data_lake / "store" / "artifacts"
    for sid in ("a", "b", "c"):
        _write_processed_source_record(data_lake, sid)
        _write_chunks_jsonl(data_lake, sid)
    # No artifact_kind-only artifacts seeded.
    monkeypatch.setenv("SDL_ROOT", str(sdl_root))

    buf = io.StringIO()
    rc = check_preflight(data_lake=str(data_lake), out_stream=buf)
    assert rc == 0, buf.getvalue()
    assert "OK: pre-flight passed." in buf.getvalue()


def test_uses_fresh_scan_not_cached_record(
    tmp_path: Path, monkeypatch
) -> None:
    """Red-team scenario 1: a stale pipeline_state_record claims migration
    is complete, but the live data-lake still has kind-only artifacts.
    check-preflight must read the live state and block."""
    data_lake = _stage_data_lake(tmp_path)
    sdl_root = data_lake / "store" / "artifacts"
    _write_processed_source_record(data_lake, "a")
    _write_chunks_jsonl(data_lake, "a")
    # Seed a STALE pipeline_state_record that lies about migration state.
    verif_dir = sdl_root / "verifications"
    verif_dir.mkdir(parents=True, exist_ok=True)
    stale_id = str(uuid.uuid4())
    (verif_dir / f"{stale_id}.json").write_text(
        json.dumps(
            {
                "pipeline_state_record_id": stale_id,
                "artifact_type": "pipeline_state_record",
                "schema_version": "1.0.0",
                "created_at": "2099-01-01T00:00:00+00:00",
                "data_lake_path": str(data_lake),
                "sdl_root": str(sdl_root),
                "total_artifacts_scanned": 1,
                "artifacts_by_type": {"source_record": 1},
                "artifacts_by_schema_version": {"1.0.0": 1},
                "validation_failures_by_type": {},
                "artifacts_with_artifact_kind_only": 0,
                "artifacts_with_both_fields": 0,
                "artifacts_with_artifact_type_only": 1,
                "expected_artifacts": {
                    "source_record_count": 1,
                    "minutes_record_count": 0,
                    "confirmed_pair_count": 0,
                    "chunks_files_present": 1,
                    "meeting_extraction_count": 0,
                    "alignment_result_count": 0,
                    "eval_result_count": 0,
                    "baseline_eval_summary_present": False,
                    "glossary_term_count": 0,
                },
                "next_required_actions": [],
                "warnings": [],
                "provenance": {"produced_by": "verify-pipeline-state"},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    # But the live data lake DOES have a kind-only artifact:
    _write_kind_only_artifact(sdl_root)

    monkeypatch.setenv("SDL_ROOT", str(sdl_root))

    buf = io.StringIO()
    rc = check_preflight(data_lake=str(data_lake), out_stream=buf)
    # Fresh scan must see the live kind-only artifact and block.
    assert rc == 1, buf.getvalue()
    assert "migrate-artifact-kind" in buf.getvalue()


def test_does_not_write_pipeline_state_record(
    tmp_path: Path, monkeypatch
) -> None:
    """Pre-flight must not pollute $SDL_ROOT/verifications/ on each call."""
    data_lake = _stage_data_lake(tmp_path)
    sdl_root = data_lake / "store" / "artifacts"
    for sid in ("a",):
        _write_processed_source_record(data_lake, sid)
        _write_chunks_jsonl(data_lake, sid)
    monkeypatch.setenv("SDL_ROOT", str(sdl_root))

    buf = io.StringIO()
    rc = check_preflight(data_lake=str(data_lake), out_stream=buf)
    assert rc == 0, buf.getvalue()
    # No pipeline_state_record artifacts should have been written.
    verif_dir = sdl_root / "verifications"
    if verif_dir.is_dir():
        leftovers = list(verif_dir.glob("*.json"))
        # The directory may exist (mkdir from sdl resolution); but no
        # pipeline_state_record JSONs should be in it.
        for path in leftovers:
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            assert (
                obj.get("artifact_type") != "pipeline_state_record"
            ), f"check-preflight wrote a pipeline_state_record: {path}"
