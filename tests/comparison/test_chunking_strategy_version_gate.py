"""Phase 2.B: comparison-engine cross-strategy halt behaviour.

The comparator fails closed when the haiku and Opus artifacts cannot
be honestly diffed because their inputs differed structurally
(different ``chunking_strategy_version`` values). ``None`` / absent on
either side is treated as ``speaker_turn_v1`` so pre-Phase-2.B
artifacts compared against default-off Phase-2.B artifacts do NOT
halt.

Two halt reasons are exercised here:

* ``no_haiku_artifact_matching_strategy`` — the strategy-aware
  selector filters candidates to those matching the baseline's
  strategy BEFORE picking the most recent. When only wrong-strategy
  haiku artifacts exist on disk, the halt fires fail-closed with this
  reason rather than silently selecting one and emitting a cross-
  strategy F1 number.
* ``chunking_strategy_mismatch`` — fires when the resulting haiku
  artifact's strategy disagrees with the baseline's. With the
  strategy-aware selector this is only reachable via the
  ``--chunking-strategy`` override (or, separately, in cascade mode).

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
    artifact_slug: str = "llm-1",
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
        "artifact_id": f"art-{artifact_slug}",
        "artifact_type": "meeting_minutes",
        "schema_version": "1.4.0",
        "status": "promoted",
        "created_at": "2026-05-20T00:00:00+00:00",
        "trace_id": f"trace-{artifact_slug}",
        "input_refs": [],
        "content_hash": "h",
        "payload": payload,
    }
    out = mdir / f"meeting_minutes__{artifact_slug}.json"
    out.write_text(json.dumps(artifact), encoding="utf-8")
    return out


# --------------------------------------------------------------------
# Rejection — v1 vs v1_overlap2
# --------------------------------------------------------------------
def test_v1_baseline_vs_overlap_haiku_halts(tmp_path: Path) -> None:
    """Haiku produced under overlap=2 vs Opus baseline at v1 → halt.

    Strategy-aware selection filters candidates to the baseline's
    strategy first; with only a wrong-strategy haiku on disk the halt
    fires as ``no_haiku_artifact_matching_strategy`` (the selector
    refuses to fall back to the wrong artifact). The halt is
    fail-closed at the same point in the loop the old
    ``chunking_strategy_mismatch`` halt fired — just with a more
    specific reason that names what's missing.
    """
    dl = tmp_path / "dl"
    sid = "src"
    _seed_transcript(dl, sid, "CHAIR: approved the threshold.")
    _seed_baseline(dl, sid, chunking_strategy_version="speaker_turn_v1")
    _seed_haiku_artifact(
        dl, sid, chunking_strategy_version="speaker_turn_v1_overlap2",
    )
    with pytest.raises(cmp.ComparisonError) as ei:
        cmp.run_comparison(data_lake=dl, source_id=sid, dry_run=True)
    assert ei.value.reason == "no_haiku_artifact_matching_strategy"
    assert "speaker_turn_v1" in ei.value.detail
    assert "speaker_turn_v1_overlap2" in ei.value.detail


def test_overlap_baseline_vs_v1_haiku_halts(tmp_path: Path) -> None:
    """Direction reversed — still halts (no_haiku_artifact_matching_strategy)."""
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
    assert ei.value.reason == "no_haiku_artifact_matching_strategy"


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
    """Null baseline + explicit overlap haiku MUST halt — null is v1.

    Baseline rows omit the field (default v1), only a haiku at
    overlap2 is on disk. With strategy-aware selection the halt is now
    ``no_haiku_artifact_matching_strategy`` (the selector refuses to
    pick the wrong-strategy artifact even when it is the only
    candidate).
    """
    dl = tmp_path / "dl"
    sid = "src"
    _seed_transcript(dl, sid, "CHAIR: approved the threshold.")
    _seed_baseline(dl, sid, chunking_strategy_version=None)
    _seed_haiku_artifact(
        dl, sid, chunking_strategy_version="speaker_turn_v1_overlap2",
    )
    with pytest.raises(cmp.ComparisonError) as ei:
        cmp.run_comparison(data_lake=dl, source_id=sid, dry_run=True)
    assert ei.value.reason == "no_haiku_artifact_matching_strategy"


# --------------------------------------------------------------------
# Strategy-aware selection — multi-artifact directory
# --------------------------------------------------------------------
def test_selects_artifact_matching_opus_strategy(tmp_path: Path) -> None:
    """Two haiku artifacts at different strategies; selector picks the
    one whose ``chunking_strategy_version`` MATCHES the Opus baseline,
    not whichever is most recent.

    The wrong-strategy artifact is forced strictly NEWER on disk so
    pure mtime-based selection would pick it (the pre-fix behaviour).
    Post-fix, strategy filtering happens BEFORE recency / content
    ordering, so the matching-strategy artifact wins regardless of
    mtime.
    """
    import os
    dl = tmp_path / "dl"
    sid = "src"
    _seed_transcript(dl, sid, "CHAIR: approved the threshold.")
    _seed_baseline(
        dl, sid, chunking_strategy_version="speaker_turn_v1_overlap2",
    )
    # Wrong-strategy haiku, strictly NEWER mtime.
    wrong_path = _seed_haiku_artifact(
        dl, sid,
        chunking_strategy_version="speaker_turn_v1",
        artifact_slug="wrong-strategy",
    )
    # Matching-strategy haiku, strictly OLDER mtime.
    matching_path = _seed_haiku_artifact(
        dl, sid,
        chunking_strategy_version="speaker_turn_v1_overlap2",
        artifact_slug="matching-strategy",
    )
    os.utime(matching_path, (1_000_000, 1_000_000))
    os.utime(wrong_path, (2_000_000, 2_000_000))

    result = cmp.run_comparison(data_lake=dl, source_id=sid, dry_run=True)
    assert result["status"] == "success"
    assert result["haiku_artifact_path"] == str(matching_path), (
        "selector must filter by chunking_strategy_version BEFORE "
        "recency; the matching-strategy artifact must win even when "
        "the wrong-strategy artifact is strictly newer on disk"
    )


def test_halts_when_no_matching_haiku_artifact(tmp_path: Path) -> None:
    """Opus baseline at ``overlap2`` but only a ``v1`` haiku on disk
    → halt ``no_haiku_artifact_matching_strategy``.

    The reason MUST be the strategy-specific code (not the generic
    ``missing_haiku_llm_output``) so the operator sees the precise fix:
    re-run extraction at the matching strategy, not "no haiku at all".
    The detail MUST name the strategy version that was missing so the
    operator can copy-paste it into the re-run command.
    """
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
    assert ei.value.reason == "no_haiku_artifact_matching_strategy"
    assert "speaker_turn_v1_overlap2" in ei.value.detail
    assert "speaker_turn_v1" in ei.value.detail


def test_missing_strategy_field_treated_as_v1(tmp_path: Path) -> None:
    """Pre-Phase-2.B haiku artifact (no ``chunking_strategy_version``
    field) is treated as ``speaker_turn_v1`` for selection purposes.

    When the baseline is at v1, the legacy artifact MUST be selectable
    so the refactor breaks no existing comparison.
    """
    dl = tmp_path / "dl"
    sid = "src"
    _seed_transcript(dl, sid, "CHAIR: approved the threshold.")
    _seed_baseline(dl, sid, chunking_strategy_version="speaker_turn_v1")
    legacy_path = _seed_haiku_artifact(
        dl, sid, chunking_strategy_version=None,
    )
    result = cmp.run_comparison(data_lake=dl, source_id=sid, dry_run=True)
    assert result["status"] == "success"
    assert result["haiku_artifact_path"] == str(legacy_path)


def test_override_flag_controls_selection_not_gate(tmp_path: Path) -> None:
    """``--chunking-strategy`` selects the right artifact AND the
    cross-check halt still fires when the override disagrees with the
    Opus baseline.

    Baseline is at ``speaker_turn_v1``; only an overlap=2 haiku is on
    disk. Without the override the script would halt
    ``no_haiku_artifact_matching_strategy``. WITH the override
    ``--chunking-strategy speaker_turn_v1_overlap2`` the selector
    finds the overlap=2 artifact — but the
    ``chunking_strategy_mismatch`` halt then fires because the
    selected artifact's strategy still disagrees with the baseline's.
    The flag is a selection override, not a gate bypass.
    """
    dl = tmp_path / "dl"
    sid = "src"
    _seed_transcript(dl, sid, "CHAIR: approved the threshold.")
    _seed_baseline(dl, sid, chunking_strategy_version="speaker_turn_v1")
    _seed_haiku_artifact(
        dl, sid, chunking_strategy_version="speaker_turn_v1_overlap2",
    )
    with pytest.raises(cmp.ComparisonError) as ei:
        cmp.run_comparison(
            data_lake=dl,
            source_id=sid,
            dry_run=True,
            chunking_strategy_override="speaker_turn_v1_overlap2",
        )
    assert ei.value.reason == "chunking_strategy_mismatch", (
        "override controls selection, NOT gate bypass — when the "
        "operator picks a strategy that disagrees with the baseline "
        "the mismatch halt must still fire"
    )
    assert "speaker_turn_v1_overlap2" in ei.value.detail
    assert "speaker_turn_v1" in ei.value.detail


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
