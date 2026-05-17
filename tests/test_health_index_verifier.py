"""Class 8: post-write index verification."""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from spectrum_systems_core.health.index_verifier import (
    INDEX_VERIFY_ENV_VAR,
    verify_artifact_indexed,
)


@pytest.fixture(autouse=True)
def _clear_env() -> Iterator[None]:
    saved = os.environ.pop(INDEX_VERIFY_ENV_VAR, None)
    yield
    if saved is not None:
        os.environ[INDEX_VERIFY_ENV_VAR] = saved
    else:
        os.environ.pop(INDEX_VERIFY_ENV_VAR, None)


def _write_index(path: Path, ids: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"artifact_id": aid}) + "\n" for aid in ids]
    path.write_text("".join(lines), encoding="utf-8")


def test_artifact_present_no_finding(tmp_path: Path) -> None:
    idx = tmp_path / "indexes" / "meetings" / "artifact_index.jsonl"
    _write_index(idx, ["a", "b", "c"])
    assert verify_artifact_indexed("b", "meeting_extraction", idx) is None


def test_artifact_missing_emits_halt(tmp_path: Path) -> None:
    idx = tmp_path / "indexes" / "meetings" / "artifact_index.jsonl"
    _write_index(idx, ["a", "c"])
    finding = verify_artifact_indexed("b", "meeting_extraction", idx)
    assert finding is not None
    assert finding.severity == "halt"
    assert finding.finding_code == "artifact_not_indexed"
    assert finding.context["artifact_id"] == "b"
    assert finding.context["artifact_type"] == "meeting_extraction"


def test_index_file_missing_halt(tmp_path: Path) -> None:
    idx = tmp_path / "indexes" / "meetings" / "artifact_index.jsonl"
    finding = verify_artifact_indexed("b", "meeting_extraction", idx)
    assert finding is not None
    assert finding.severity == "halt"
    assert finding.context["error"] == "index_missing"


def test_remediation_present(tmp_path: Path) -> None:
    idx = tmp_path / "indexes" / "meetings" / "artifact_index.jsonl"
    _write_index(idx, ["a"])
    finding = verify_artifact_indexed("b", "meeting_extraction", idx)
    assert finding is not None
    assert finding.remediation


def test_bypass_via_env_var(tmp_path: Path) -> None:
    os.environ[INDEX_VERIFY_ENV_VAR] = "false"
    idx = tmp_path / "indexes" / "meetings" / "artifact_index.jsonl"
    # Index does not exist; with bypass we still return None.
    assert verify_artifact_indexed("b", "meeting_extraction", idx) is None


def test_tail_lines_window(tmp_path: Path) -> None:
    """The verifier reads only the last N lines (perf invariant)."""
    idx = tmp_path / "indexes" / "meetings" / "artifact_index.jsonl"
    # Write 200 ids; old ones fall outside the default 100-line window.
    _write_index(idx, [f"id-{i}" for i in range(200)])
    # id-0 is outside the last 100 lines -> reported missing.
    finding = verify_artifact_indexed("id-0", "meeting_extraction", idx, tail_lines=100)
    assert finding is not None
    # id-199 is the most-recently-written -> present.
    assert verify_artifact_indexed("id-199", "meeting_extraction", idx, tail_lines=100) is None


def test_halt_pipeline_simulated_via_exit_code(tmp_path: Path) -> None:
    """Red Team 2: halt finding must drive non-zero exit at the caller.

    The verifier returns a finding; tests assert callers escalate.
    """
    idx = tmp_path / "indexes" / "meetings" / "artifact_index.jsonl"
    _write_index(idx, [])
    finding = verify_artifact_indexed("missing", "x", idx)
    assert finding is not None and finding.is_halt()
    # Simulated caller logic:
    exit_code = 1 if finding.is_halt() else 0
    assert exit_code == 1
