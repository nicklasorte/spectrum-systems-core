"""Integration test for the Phase 3 measurement helper.

``scripts/print_comparison_delta.py`` is the operator-facing diagnostic
the runbook (``scripts/run_glossary_measurement.sh``) shells out to.
It reads on-disk JSON artifacts and prints F1 / delta / glossary
provenance — never calls an LLM, never mutates the data lake.

These tests exercise the script via ``subprocess.run`` against a real
temp directory (per CLAUDE.md's integration-test rule). They cover:

* the happy path with a glossary-enabled extraction artifact;
* the legacy (no glossary metadata) extraction artifact path;
* the rejection of an inconsistent extraction_config (hash without
  tokens) — the helper surfaces the reason and exits non-zero.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "print_comparison_delta.py"


def _build_meeting_dir(
    tmp_path: Path,
    source_id: str,
    *,
    f1: float,
    extraction_config: dict | None,
    tainted: bool | None = None,
) -> Path:
    """Write a comparison_result + meeting_minutes pair under the
    canonical data-lake layout. Returns the data-lake root."""
    dl = tmp_path / "dl"
    meeting_dir = dl / "store" / "processed" / "meetings" / source_id
    meeting_dir.mkdir(parents=True)

    cmp_doc = {
        "artifact_type": "comparison_result",
        "schema_version": "1.0.0",
        "source_id": source_id,
        "compared_at": "2026-05-20T00:00:00+00:00",
        "summary": {
            "haiku_f1_vs_opus": f1,
            "haiku_recall_vs_opus": 0.30,
            "haiku_precision_vs_opus": 0.55,
        },
        "legacy_eval": False,
    }
    if tainted is not None:
        cmp_doc["tainted_glossary_drift"] = bool(tainted)
    (meeting_dir / "comparison_result__abc.json").write_text(
        json.dumps(cmp_doc), encoding="utf-8"
    )

    mm_payload = {
        "title": "X",
        "summary": "Y",
        "decisions": [],
        "action_items": [],
        "open_questions": [],
        "provenance": {"produced_by": "meeting_minutes_llm"},
    }
    if extraction_config is not None:
        mm_payload["provenance"]["extraction_config"] = extraction_config
    mm_doc = {
        "artifact_id": "mm-1",
        "artifact_type": "meeting_minutes",
        "schema_version": "1.4.0",
        "status": "promoted",
        "created_at": "2026-05-20T00:00:00+00:00",
        "trace_id": "trace-1",
        "input_refs": [],
        "content_hash": "abc",
        "payload": mm_payload,
    }
    (meeting_dir / "meeting_minutes__zzz.json").write_text(
        json.dumps(mm_doc), encoding="utf-8"
    )
    return dl


def _run(args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_prints_f1_delta_and_glossary_provenance(tmp_path: Path) -> None:
    """Happy path: an artifact with the new Phase 3 fields produces a
    formatted report that surfaces hash, tokens, and the F1 delta."""
    ec = {
        "temperature": 0.0,
        "seed_inputs": {
            "model_id": "haiku",
            "prompt_content_hash": "abc",
            "transcript_hash": "def",
        },
        "chunks_full_hash": "x",
        "chunk_count": 1,
        "first_chunk_hash": "h",
        "last_chunk_hash": "h",
        "prompt_content_hash": "abc",
        "glossary_version_hash": "deadbeefcafef00d",
        "glossary_tokens_added": 128,
        "tainted_glossary_drift": False,
    }
    dl = _build_meeting_dir(tmp_path, "src-x", f1=0.45, extraction_config=ec)
    result = _run(
        [
            "--source-id",
            "src-x",
            "--baseline-f1",
            "0.395",
            "--data-lake",
            str(dl),
        ]
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "deadbeefcafef00d" in out
    assert "128" in out
    # F1 0.45 vs baseline 0.395 -> delta +0.0550 (formatted to 4 dp)
    assert "+0.0550" in out
    assert "tainted_glossary_drift: False" in out


def test_legacy_artifact_without_glossary_metadata(tmp_path: Path) -> None:
    """A pre-Phase-3 extraction artifact (no glossary fields) still
    prints a report — the missing values surface as 'unset' / None."""
    ec = {
        "temperature": 0.0,
        "seed_inputs": {
            "model_id": "haiku",
            "prompt_content_hash": "abc",
            "transcript_hash": "def",
        },
        "chunks_full_hash": "x",
        "chunk_count": 1,
        "first_chunk_hash": "h",
        "last_chunk_hash": "h",
        "prompt_content_hash": "abc",
    }
    dl = _build_meeting_dir(tmp_path, "src-y", f1=0.40, extraction_config=ec)
    result = _run(
        [
            "--source-id",
            "src-y",
            "--baseline-f1",
            "0.395",
            "--data-lake",
            str(dl),
        ]
    )
    assert result.returncode == 0, result.stderr
    assert "glossary_version_hash: (unset)" in result.stdout
    assert "glossary_tokens_added: None" in result.stdout


def test_inconsistent_extraction_config_surfaces_reason(tmp_path: Path) -> None:
    """An artifact whose extraction_config carries
    ``glossary_version_hash`` without the paired tokens (or vice-versa)
    is logically inconsistent; the helper prints the reason token and
    exits non-zero so an operator does not silently report a delta
    against a half-stamped artifact."""
    # Deliberately omit glossary_tokens_added so the pair is broken.
    ec = {
        "temperature": 0.0,
        "seed_inputs": {
            "model_id": "haiku",
            "prompt_content_hash": "abc",
            "transcript_hash": "def",
        },
        "chunks_full_hash": "x",
        "chunk_count": 1,
        "first_chunk_hash": "h",
        "last_chunk_hash": "h",
        "prompt_content_hash": "abc",
        "glossary_version_hash": "deadbeefcafef00d",
    }
    dl = _build_meeting_dir(tmp_path, "src-z", f1=0.45, extraction_config=ec)
    result = _run(
        [
            "--source-id",
            "src-z",
            "--baseline-f1",
            "0.395",
            "--data-lake",
            str(dl),
        ]
    )
    assert result.returncode == 1, (
        f"expected exit 1 for inconsistent ec; got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "glossary_metadata_inconsistent" in result.stdout


def test_missing_data_lake_argument_exits_two(tmp_path: Path) -> None:
    """Without --data-lake (and no DATA_LAKE_PATH env var) the script
    halts with exit 2 — a fail-closed boundary condition."""
    env = {"PATH": "/usr/bin:/bin"}  # no DATA_LAKE_PATH
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--source-id", "src-x", "--baseline-f1", "0.395"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 2
    assert "DATA_LAKE_PATH" in result.stderr or "--data-lake" in result.stderr
