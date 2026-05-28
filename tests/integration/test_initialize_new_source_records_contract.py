"""Integration contract for scripts/initialize_new_source_records.py.

Builds a synthetic data-lake with ``store/raw/meetings/<sid>/source.txt``
and ``metadata.json`` for every slug the script targets, runs the script
as a subprocess, and asserts that
``store/processed/meetings/<sid>/source_record.json`` was produced and
validates against the canonical source_record schema.

Also exercises the companion verifier and idempotency: a second run
reports every slug as ``already_present`` and never overwrites.
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

import jsonschema
import pytest

from spectrum_systems_core.ingestion._paths import schema_path

# Adding scripts/ to sys.path the way the script does keeps the import
# below resolvable without making scripts/ a real package.
_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

from initialize_new_source_records import (  # noqa: E402
    SOURCE_IDS,
    initialize_all,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _stage_synthetic_data_lake(data_lake: Path) -> None:
    """Mirror the manual ingestion that produced the 32 missing records."""
    for sid in SOURCE_IDS:
        raw_dir = data_lake / "store" / "raw" / "meetings" / sid
        raw_dir.mkdir(parents=True)
        (raw_dir / "source.txt").write_text(
            f"CHAIR: Welcome to {sid}.\n"
            "SPEAKER A: Item one.\n"
            "SPEAKER B: Item two.\n",
            encoding="utf-8",
        )
        (raw_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "source_id": sid,
                    "source_family": "meetings",
                    "source_type": "transcript",
                    "title": sid,
                    "description": "synthetic test fixture",
                    "date": "2026-05-01",
                    "author": "test",
                    "tags": [],
                    "raw_format": "txt",
                    "private_use_only": False,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )


def test_source_ids_count_is_thirty_two() -> None:
    """The backfill list is frozen at 32 — drift is a code review event."""
    assert len(SOURCE_IDS) == 32
    assert len(set(SOURCE_IDS)) == 32  # no duplicates


def test_initialize_writes_valid_source_record_for_each_slug(
    tmp_path: Path,
) -> None:
    _stage_synthetic_data_lake(tmp_path)
    summary = initialize_all(data_lake=tmp_path)
    assert summary["status"] == "success", summary
    assert summary["counts"] == {"written": 32}

    schema = json.loads(
        schema_path("source_record").read_text(encoding="utf-8")
    )
    validator = jsonschema.Draft202012Validator(schema)

    for sid in SOURCE_IDS:
        sr_path = (
            tmp_path
            / "store"
            / "processed"
            / "meetings"
            / sid
            / "source_record.json"
        )
        assert sr_path.is_file(), sid
        record = json.loads(sr_path.read_text(encoding="utf-8"))
        validator.validate(record)
        assert record["artifact_type"] == "source_record"
        uuid.UUID(record["artifact_id"])  # must parse as UUID
        assert record["payload"]["source_id"] == sid


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    _stage_synthetic_data_lake(tmp_path)
    first = initialize_all(data_lake=tmp_path)
    assert first["counts"] == {"written": 32}

    # Capture artifact_ids from the first run so we can prove the second
    # run did not overwrite them.
    artifact_ids = {}
    for sid in SOURCE_IDS:
        sr_path = (
            tmp_path
            / "store"
            / "processed"
            / "meetings"
            / sid
            / "source_record.json"
        )
        artifact_ids[sid] = json.loads(sr_path.read_text())["artifact_id"]

    second = initialize_all(data_lake=tmp_path)
    assert second["counts"] == {"already_present": 32}
    for sid in SOURCE_IDS:
        sr_path = (
            tmp_path
            / "store"
            / "processed"
            / "meetings"
            / sid
            / "source_record.json"
        )
        assert (
            json.loads(sr_path.read_text())["artifact_id"]
            == artifact_ids[sid]
        ), sid


def test_initialize_reports_failure_when_raw_dir_missing(
    tmp_path: Path,
) -> None:
    # Stage NO raw dirs — every slug must report failure but the script
    # must not crash and must report all 32 individually.
    summary = initialize_all(data_lake=tmp_path)
    assert summary["status"] == "failure"
    assert summary["counts"].get("failure") == 32


def test_verify_script_passes_after_initialize_subprocess(
    tmp_path: Path,
) -> None:
    """End-to-end via subprocess (the workflow's call shape)."""
    _stage_synthetic_data_lake(tmp_path)

    init_result = subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "scripts/initialize_new_source_records.py"),
            "--data-lake",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert init_result.returncode == 0, init_result.stderr
    init_summary = json.loads(init_result.stdout)
    assert init_summary["status"] == "success"

    verify_result = subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "scripts/verify_initialized_source_records.py"),
            "--data-lake",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify_result.returncode == 0, verify_result.stderr
    verify_summary = json.loads(verify_result.stdout)
    assert verify_summary["status"] == "success"
    assert verify_summary["passed"] == 32
    assert verify_summary["counts"] == {"pass": 32}


def test_verify_script_detects_missing_record(tmp_path: Path) -> None:
    """Empty data lake → verifier fails closed for every slug."""
    verify_result = subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "scripts/verify_initialized_source_records.py"),
            "--data-lake",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify_result.returncode != 0
    summary = json.loads(verify_result.stdout)
    assert summary["status"] == "failure"
    assert summary["counts"].get("fail") == 32
