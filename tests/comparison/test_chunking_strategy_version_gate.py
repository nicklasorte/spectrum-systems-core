"""Phase 2.B: comparison-engine ``chunking_strategy_mismatch`` halt.

The gate fires when the two artifacts under comparison declare
different ``chunking_strategy_version`` values. ``None`` / absent on
either side is treated as ``speaker_turn_v1`` so pre-Phase-2.B
artifacts compared against default-off Phase-2.B artifacts do NOT
halt.

Fixtures mirror tests/comparison/test_phase2_gates.py — same
data-lake layout, same ``cmp.run_comparison`` entry point, same
``dry_run=True`` to skip the artifact / eval_history write.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import compare_opus_haiku as cmp  # noqa: E402


# --------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------
def _seed_transcript(dl: Path, sid: str, text: str) -> None:
    raw = dl / "store" / "raw" / "transcripts"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / f"{sid}.txt").write_text(text, encoding="utf-8")


def _seed_baseline(
    dl: Path,
    sid: str,
    *,
    chunking_strategy_version: str | None,
    text: str = "approved the threshold",
) -> None:
    bdir = dl / "store" / "processed" / "meetings" / sid / "reference_baselines"
    bdir.mkdir(parents=True, exist_ok=True)
    row: dict = {
        "extraction_type": "decisions",
        "ground_truth_text": text,
        "model_id": "OPUS-REF",
        "schema_version": "1.4.0",
    }
    if chunking_strategy_version is not None:
        row["chunking_strategy_version"] = chunking_strategy_version
    (bdir / "opus_reference_minutes.jsonl").write_text(
        json.dumps(row) + "\n", encoding="utf-8"
    )


def _seed_haiku_artifact(
    dl: Path,
    sid: str,
    *,
    chunking_strategy_version: str | None,
    text: str = "approved the threshold",
) -> Path:
    mdir = dl / "store" / "processed" / "meetings" / sid
    mdir.mkdir(parents=True, exist_ok=True)
    provenance: dict = {"produced_by": "meeting_minutes_llm"}
    if chunking_strategy_version is not None:
        provenance["chunking_strategy_version"] = chunking_strategy_version
    payload = {
        "artifact_type": "meeting_minutes",
        "schema_version": "1.4.0",
        "title": "T",
        "summary": "S",
        "decisions": [text],
        "action_items": [],
        "open_questions": [],
        "provenance": provenance,
    }
    artifact = {
        "artifact_id": "art-1",
        "artifact_type": "meeting_minutes",
        "schema_version": "1.4.0",
        "status": "promoted",
        "created_at": "2026-05-20T00:00:00+00:00",
        "trace_id": "trace-1",
        "input_refs": [],
        "content_hash": "h",
        "payload": payload,
    }
    out = mdir / "meeting_minutes__llm-1.json"
    out.write_text(json.dumps(artifact), encoding="utf-8")
    return out


# --------------------------------------------------------------------
# Rejection — v1 vs v1_overlap2
# --------------------------------------------------------------------
def test_v1_baseline_vs_overlap_haiku_halts(tmp_path: Path) -> None:
    """Haiku produced under overlap=2 vs Opus baseline at v1 → halt."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_transcript(dl, sid, "CHAIR: approved the threshold.")
    _seed_baseline(dl, sid, chunking_strategy_version="speaker_turn_v1")
    _seed_haiku_artifact(
        dl, sid, chunking_strategy_version="speaker_turn_v1_overlap2",
    )
    with pytest.raises(cmp.ComparisonError) as ei:
        cmp.run_comparison(data_lake=dl, source_id=sid, dry_run=True)
    assert ei.value.reason == "chunking_strategy_mismatch"
    assert "speaker_turn_v1_overlap2" in ei.value.detail
    assert "speaker_turn_v1" in ei.value.detail


def test_overlap_baseline_vs_v1_haiku_halts(tmp_path: Path) -> None:
    """Direction reversed — still halts."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_transcript(dl, sid, "CHAIR: approved the threshold.")
    _seed_baseline(
        dl, sid, chunking_strategy_version="speaker_turn_v1_overlap2",
    )
    _seed_haiku_artifact(
        dl, sid, chunking_strategy_version="speaker_turn_v1",
    )
    with pytest.raises(cmp.ComparisonError) as ei:
        cmp.run_comparison(data_lake=dl, source_id=sid, dry_run=True)
    assert ei.value.reason == "chunking_strategy_mismatch"


# --------------------------------------------------------------------
# Happy paths
# --------------------------------------------------------------------
def test_both_v1_proceeds(tmp_path: Path) -> None:
    dl = tmp_path / "dl"
    sid = "src"
    _seed_transcript(dl, sid, "CHAIR: approved the threshold.")
    _seed_baseline(dl, sid, chunking_strategy_version="speaker_turn_v1")
    _seed_haiku_artifact(
        dl, sid, chunking_strategy_version="speaker_turn_v1",
    )
    # Comparison proceeds (no chunking-strategy halt). Downstream
    # halts may still raise; we tolerate them as long as it's NOT
    # the strategy-mismatch reason. The point of this test is that
    # the strategy gate does not over-fire on matched inputs.
    try:
        cmp.run_comparison(data_lake=dl, source_id=sid, dry_run=True)
    except cmp.ComparisonError as exc:
        assert exc.reason != "chunking_strategy_mismatch", (
            "matched chunking strategies must NOT trigger the gate"
        )


def test_both_null_treated_as_v1_and_proceeds(tmp_path: Path) -> None:
    """Pre-Phase-2.B artifacts omit the field entirely. Both null = v1."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_transcript(dl, sid, "CHAIR: approved the threshold.")
    _seed_baseline(dl, sid, chunking_strategy_version=None)
    _seed_haiku_artifact(dl, sid, chunking_strategy_version=None)
    try:
        cmp.run_comparison(data_lake=dl, source_id=sid, dry_run=True)
    except cmp.ComparisonError as exc:
        assert exc.reason != "chunking_strategy_mismatch"


def test_one_null_one_explicit_v1_proceeds(tmp_path: Path) -> None:
    """Mixed null+v1 must be treated as matching (per backward-compat)."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_transcript(dl, sid, "CHAIR: approved the threshold.")
    _seed_baseline(dl, sid, chunking_strategy_version=None)
    _seed_haiku_artifact(
        dl, sid, chunking_strategy_version="speaker_turn_v1",
    )
    try:
        cmp.run_comparison(data_lake=dl, source_id=sid, dry_run=True)
    except cmp.ComparisonError as exc:
        assert exc.reason != "chunking_strategy_mismatch"


def test_one_null_one_overlap_halts(tmp_path: Path) -> None:
    """Null + explicit overlap MUST halt — null is v1, overlap is not."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_transcript(dl, sid, "CHAIR: approved the threshold.")
    _seed_baseline(dl, sid, chunking_strategy_version=None)
    _seed_haiku_artifact(
        dl, sid, chunking_strategy_version="speaker_turn_v1_overlap2",
    )
    with pytest.raises(cmp.ComparisonError) as ei:
        cmp.run_comparison(data_lake=dl, source_id=sid, dry_run=True)
    assert ei.value.reason == "chunking_strategy_mismatch"


# --------------------------------------------------------------------
# RED TEAM PASS 2: inverted specificity
# --------------------------------------------------------------------
def test_matched_overlap_2_does_not_halt(tmp_path: Path) -> None:
    """Both sides at overlap=2 → no halt (specificity proof)."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_transcript(dl, sid, "CHAIR: approved the threshold.")
    _seed_baseline(
        dl, sid, chunking_strategy_version="speaker_turn_v1_overlap2",
    )
    _seed_haiku_artifact(
        dl, sid, chunking_strategy_version="speaker_turn_v1_overlap2",
    )
    try:
        cmp.run_comparison(data_lake=dl, source_id=sid, dry_run=True)
    except cmp.ComparisonError as exc:
        assert exc.reason != "chunking_strategy_mismatch", (
            "matched overlap=2 inputs must NOT trigger the gate"
        )
