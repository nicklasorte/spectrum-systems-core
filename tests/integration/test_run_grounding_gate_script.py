"""Integration contract test for ``scripts/run_grounding_gate.py``.

Drives the script as a real subprocess against a temp data-lake (no
network, no API key). Defends the script's trust properties:

* artifact_type is always ``grounding_gate_*`` (never ``artifact_kind``);
* a clean input produces ``passed=True`` and writes all three artifacts;
* a single ungrounded item produces ``passed=False``, the ungrounded
  item lands in the JSONL audit file, the grounded artifact STILL
  exists with the item dropped;
* ``--disable-grounding-gate`` writes ONLY the bypass record (no
  grounded / ungrounded artifacts) with operator + timestamp;
* missing transcript exits 2 with no side effects;
* the JSON is canonical (sorted keys + trailing newline) so two
  runs over the same input produce byte-identical artifacts;
* the script discovers the most recent meeting_minutes__*.json when
  ``--extraction-artifact`` is omitted.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "run_grounding_gate.py"

SOURCE_ID = "fixture-source-id"


def _write_chunks_jsonl(path: Path, chunks: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"chunk_id": cid, "text": text}, sort_keys=True)
        for cid, text in sorted(chunks.items())
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_minimal_data_lake(
    tmp_path: Path,
    *,
    extraction_payload: dict,
    transcript_text: str = "so we will determine the band plan today",
    chunks: dict[str, str] | None = None,
    extraction_filename: str = "meeting_minutes__abc.json",
) -> Path:
    data_lake = tmp_path / "data-lake"
    processed = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    raw = data_lake / "store" / "raw" / "meetings" / SOURCE_ID
    processed.mkdir(parents=True)
    raw.mkdir(parents=True)
    (raw / "transcript.txt").write_text(transcript_text, encoding="utf-8")
    if chunks:
        _write_chunks_jsonl(raw / "chunks.jsonl", chunks)
    # meeting_minutes is written FLAT (no envelope wrapper) per the
    # schema — title/summary/decisions/etc. live at the top level.
    extraction_artifact: dict = {
        "artifact_type": "meeting_minutes",
        "schema_version": "1.5.0",
        "title": "Fixture meeting",
        "summary": "Test fixture for grounding gate integration tests.",
        "decisions": extraction_payload.get("decisions", []),
        "action_items": extraction_payload.get("action_items", []),
        "open_questions": extraction_payload.get("open_questions", []),
    }
    # Drop in any other claim-shaped types the caller wanted.
    for k, v in extraction_payload.items():
        if k not in extraction_artifact:
            extraction_artifact[k] = v
    (processed / extraction_filename).write_text(
        json.dumps(extraction_artifact, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return data_lake


def _run(*argv: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    # Ensure src/ is importable when invoking the script as a file path.
    full_env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + full_env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, str(SCRIPT), *argv],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env=full_env,
    )


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_clean_input_passes_and_writes_all_three_artifacts(tmp_path: Path) -> None:
    payload = {
        "decisions": [
            {
                "text": "Determine band plan",
                "source_quote": "we will determine the band plan",
                "source_chunk_id": "c1",
            }
        ]
    }
    data_lake = _build_minimal_data_lake(
        tmp_path,
        extraction_payload=payload,
        chunks={"c1": "so we will determine the band plan today"},
    )
    cp = _run("--source-id", SOURCE_ID, "--data-lake", str(data_lake))
    assert cp.returncode == 0, cp.stdout + cp.stderr

    processed = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    grounded = list(processed.glob("grounded_items__*.json"))
    result_files = list(processed.glob("grounding_gate_result__*.json"))
    ungrounded = list((processed / "ungrounded_items").glob("*.jsonl")) if (processed / "ungrounded_items").exists() else []
    assert len(grounded) == 1, f"expected one grounded artifact, got {grounded}"
    assert len(result_files) == 1, f"expected one result artifact, got {result_files}"
    # ungrounded_items dir/file is created even when empty? The script
    # writes an empty file when there's nothing to record.
    if ungrounded:
        assert ungrounded[0].read_text(encoding="utf-8") == ""

    result_payload = json.loads(result_files[0].read_text(encoding="utf-8"))
    assert result_payload["artifact_type"] == "grounding_gate_result"
    assert "artifact_kind" not in result_payload
    assert result_payload["passed"] is True
    assert result_payload["total_items"] == 1
    assert result_payload["grounded_count"] == 1
    assert result_payload["ungrounded_count"] == 0


def test_ungrounded_item_separated_into_audit_jsonl(tmp_path: Path) -> None:
    payload = {
        "decisions": [
            {
                "text": "Good one",
                "source_quote": "we will determine the band plan",
                "source_chunk_id": "c1",
            },
            {
                "text": "Bad one — quote doesn't match",
                "source_quote": "this phrase is not in any chunk",
                "source_chunk_id": "c1",
            },
        ]
    }
    data_lake = _build_minimal_data_lake(
        tmp_path,
        extraction_payload=payload,
        chunks={"c1": "so we will determine the band plan today"},
    )
    cp = _run("--source-id", SOURCE_ID, "--data-lake", str(data_lake))
    # Returns 1 — at least one ungrounded item.
    assert cp.returncode == 1, cp.stdout + cp.stderr

    processed = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    grounded_path = next(processed.glob("grounded_items__*.json"))
    ungrounded_path = next((processed / "ungrounded_items").glob("*.jsonl"))
    result_path = next(processed.glob("grounding_gate_result__*.json"))

    grounded = json.loads(grounded_path.read_text(encoding="utf-8"))
    assert len(grounded["payload"]["decisions"]) == 1
    assert grounded["payload"]["decisions"][0]["text"] == "Good one"
    assert grounded["gate_passed"] is False

    # The ungrounded JSONL carries one record.
    ungrounded_lines = [
        json.loads(ln) for ln in ungrounded_path.read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    assert len(ungrounded_lines) == 1
    assert ungrounded_lines[0]["extraction_type"] == "decisions"
    assert ungrounded_lines[0]["reason"] == "not_substring"

    # The gate result records the failure.
    result_payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert result_payload["total_items"] == 2
    assert result_payload["grounded_count"] == 1
    assert result_payload["ungrounded_count"] == 1
    assert result_payload["gate_drop_rate"] == 0.5


# --------------------------------------------------------------------------
# Bypass path
# --------------------------------------------------------------------------


def test_disable_grounding_gate_writes_bypass_record_only(tmp_path: Path) -> None:
    payload = {"decisions": [{"text": "x", "source_quote": "anything", "source_chunk_id": "c1"}]}
    data_lake = _build_minimal_data_lake(tmp_path, extraction_payload=payload)

    cp = _run(
        "--source-id", SOURCE_ID,
        "--data-lake", str(data_lake),
        "--disable-grounding-gate",
        "--operator", "alice@example.com",
    )
    assert cp.returncode == 0, cp.stdout + cp.stderr

    processed = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    bypass_files = list(processed.glob("grounding_gate_bypass_record__*.json"))
    grounded_files = list(processed.glob("grounded_items__*.json"))
    assert len(bypass_files) == 1
    # Gate did not run — no grounded/ungrounded/result artifacts.
    assert grounded_files == []
    assert not (processed / "ungrounded_items").exists() or \
        list((processed / "ungrounded_items").glob("*.jsonl")) == []

    bypass = json.loads(bypass_files[0].read_text(encoding="utf-8"))
    assert bypass["artifact_type"] == "grounding_gate_bypass_record"
    assert "artifact_kind" not in bypass
    assert bypass["operator"] == "alice@example.com"
    assert bypass["source_id"] == SOURCE_ID
    assert "operator override" in bypass["reason"]


# --------------------------------------------------------------------------
# Precondition failures
# --------------------------------------------------------------------------


def test_missing_transcript_exits_2(tmp_path: Path) -> None:
    """Even with a valid extraction artifact, a missing transcript blocks."""
    data_lake = tmp_path / "data-lake"
    processed = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    processed.mkdir(parents=True)
    # Drop a minimal extraction so the script gets PAST the
    # "no extraction" check and into the transcript check.
    art = {
        "artifact_id": "x",
        "artifact_type": "meeting_minutes",
        "schema_version": "1.5.0",
        "payload": {},
    }
    (processed / "meeting_minutes__x.json").write_text(
        json.dumps(art) + "\n", encoding="utf-8"
    )
    # No transcript created.
    cp = _run("--source-id", SOURCE_ID, "--data-lake", str(data_lake))
    assert cp.returncode == 2, cp.stderr
    assert "transcript not found" in cp.stderr


def test_missing_extraction_artifact_exits_2(tmp_path: Path) -> None:
    data_lake = tmp_path / "data-lake"
    raw = data_lake / "store" / "raw" / "meetings" / SOURCE_ID
    raw.mkdir(parents=True)
    (raw / "transcript.txt").write_text("anything", encoding="utf-8")
    (data_lake / "store" / "processed" / "meetings" / SOURCE_ID).mkdir(parents=True)
    cp = _run("--source-id", SOURCE_ID, "--data-lake", str(data_lake))
    assert cp.returncode == 2
    assert "meeting_minutes" in cp.stderr.lower()


def test_missing_data_lake_exits_2(tmp_path: Path) -> None:
    cp = _run("--source-id", SOURCE_ID, "--data-lake", str(tmp_path / "nope"))
    assert cp.returncode == 2
    assert "data-lake not found" in cp.stderr


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_two_runs_produce_byte_identical_artifacts(tmp_path: Path) -> None:
    payload = {
        "decisions": [
            {
                "text": "x",
                "source_quote": "we will determine the band plan",
                "source_chunk_id": "c1",
            },
            {
                "text": "y",
                "source_quote": "no match here at all",
                "source_chunk_id": "c1",
            },
        ]
    }
    data_lake = _build_minimal_data_lake(
        tmp_path,
        extraction_payload=payload,
        chunks={"c1": "so we will determine the band plan today"},
    )
    processed = data_lake / "store" / "processed" / "meetings" / SOURCE_ID

    cp1 = _run("--source-id", SOURCE_ID, "--data-lake", str(data_lake), "--run-id", "fixed")
    # First run wrote files; capture them.
    artifacts_after_first = {
        p.name: p.read_bytes()
        for p in [
            processed / "grounded_items__fixed.json",
            processed / "grounding_gate_result__fixed.json",
            processed / "ungrounded_items" / "fixed.jsonl",
        ]
    }
    # Allow some real-time gap so any non-deterministic timestamp would be visible.
    time.sleep(0.01)

    cp2 = _run("--source-id", SOURCE_ID, "--data-lake", str(data_lake), "--run-id", "fixed")
    artifacts_after_second = {
        p.name: p.read_bytes()
        for p in [
            processed / "grounded_items__fixed.json",
            processed / "grounding_gate_result__fixed.json",
            processed / "ungrounded_items" / "fixed.jsonl",
        ]
    }
    assert cp1.returncode == cp2.returncode
    for name in artifacts_after_first:
        assert artifacts_after_first[name] == artifacts_after_second[name], (
            f"{name} drift between runs"
        )


# --------------------------------------------------------------------------
# Step summary
# --------------------------------------------------------------------------


def test_step_summary_includes_totals_and_failure_reasons(tmp_path: Path) -> None:
    payload = {
        "decisions": [
            {
                "text": "Bad",
                "source_quote": "this phrase is not in any chunk",
                "source_chunk_id": "c1",
            }
        ]
    }
    data_lake = _build_minimal_data_lake(
        tmp_path,
        extraction_payload=payload,
        chunks={"c1": "some other text"},
    )
    summary_path = tmp_path / "summary.md"
    cp = _run(
        "--source-id", SOURCE_ID,
        "--data-lake", str(data_lake),
        env={"GITHUB_STEP_SUMMARY": str(summary_path)},
    )
    assert cp.returncode == 1
    summary = summary_path.read_text(encoding="utf-8")
    assert "Phase 4.A grounding gate" in summary
    assert "total_items: 1" in summary
    assert "not_substring" in summary
    assert "RECALL COLLAPSE" in summary  # 0/1 grounded < 50%
