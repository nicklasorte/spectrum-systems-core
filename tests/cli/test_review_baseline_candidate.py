"""Phase O.5 — tests for review-baseline-candidate CLI command."""
from __future__ import annotations

import io
import json
import uuid
from pathlib import Path

import pytest

from spectrum_systems_core.cli import review_baseline_candidate


def _seed_eval_summary(
    sdl: Path,
    pipeline_run_id: str = "run-x",
    partial: bool = False,
) -> Path:
    target = sdl / "evals"
    target.mkdir(parents=True, exist_ok=True)
    fp = target / f"eval_summary_{pipeline_run_id}.json"
    fp.write_text(
        json.dumps(
            {
                "eval_summary_id": str(uuid.uuid4()),
                "pipeline_run_id": pipeline_run_id,
                "artifact_type": "eval_summary",
                "schema_version": "1.1.0",
                "created_at": "2026-05-11T00:00:00+00:00",
                "pairs_evaluated": 1,
                "pairs_skipped_pending_review": 0,
                "aggregate_coverage": 0.8,
                "aggregate_precision": 0.7,
                "total_items_requiring_review": 0,
                "by_chunking_strategy": {
                    "speaker_turn": {
                        "coverage": 0.8, "precision": 0.7, "pairs_count": 1
                    },
                    "character_count_fallback": {
                        "coverage": 0.0, "precision": 0.0, "pairs_count": 0
                    }
                },
                "eval_results": [],
                "is_baseline": False,
                "baseline_eval_summary_id": None,
                "regression_detected": False,
                "regression_detail": [],
                "partial_run_warning": partial,
                "partial_run_detail": None,
                "provenance": {"produced_by": "EvalRunner"},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return fp


def _seed_meeting_extraction(
    sdl: Path,
    source_id: str,
    *,
    total_chunks_classified: int = 100,
    regulatory_verb_fallback_count: int = 0,
    off_topic_count: int = 0,
    requires_human_dedup_count: int = 0,
    decisions: int = 1,
    claims: int = 1,
    action_items: int = 1,
) -> Path:
    target = sdl / "extractions"
    target.mkdir(parents=True, exist_ok=True)
    fp = target / f"{source_id}_meeting_extraction.json"
    fp.write_text(
        json.dumps(
            {
                "artifact_type": "meeting_extraction",
                "schema_version": "1.0.0",
                "meeting_extraction_id": str(uuid.uuid4()),
                "source_id": source_id,
                "total_chunks_classified": total_chunks_classified,
                "off_topic_count": off_topic_count,
                "regulatory_verb_fallback_count": regulatory_verb_fallback_count,
                "requires_human_dedup_count": requires_human_dedup_count,
                "decisions": [{}] * decisions,
                "claims": [{}] * claims,
                "action_items": [{}] * action_items,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return fp


def test_review_outputs_markdown_checklist(
    tmp_path: Path, monkeypatch
) -> None:
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    _seed_eval_summary(sdl)
    _seed_meeting_extraction(sdl, "a")
    monkeypatch.setenv("SDL_ROOT", str(sdl))

    buf = io.StringIO()
    rc = review_baseline_candidate(
        data_lake=str(tmp_path), out_stream=buf
    )
    assert rc == 0
    text = buf.getvalue()
    assert "regulatory_verb_fallback_rate" in text
    assert "human_dedup_rate" in text
    assert "off_topic_rate" in text
    assert "PASS" in text or "REVIEW" in text


def test_review_does_not_set_baseline(tmp_path: Path, monkeypatch) -> None:
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    _seed_eval_summary(sdl)
    _seed_meeting_extraction(sdl, "a")

    monkeypatch.setenv("SDL_ROOT", str(sdl))
    buf = io.StringIO()
    review_baseline_candidate(data_lake=str(tmp_path), out_stream=buf)
    # The command is read-only: no baseline file may appear on disk.
    assert not (sdl / "evals" / "baseline_eval_summary.json").exists()


def test_review_flags_high_regulatory_verb_fallback_rate(
    tmp_path: Path, monkeypatch
) -> None:
    """45 / 100 chunks classified via fallback => REVIEW label."""
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    _seed_eval_summary(sdl)
    _seed_meeting_extraction(
        sdl,
        "a",
        total_chunks_classified=100,
        regulatory_verb_fallback_count=45,
    )
    monkeypatch.setenv("SDL_ROOT", str(sdl))

    buf = io.StringIO()
    rc = review_baseline_candidate(
        data_lake=str(tmp_path), out_stream=buf
    )
    text = buf.getvalue()
    assert rc == 0
    # Locate the line for the metric and verify the label + value.
    matching = [
        line for line in text.splitlines()
        if "regulatory_verb_fallback_rate" in line
    ]
    assert matching, f"metric line missing in:\n{text}"
    line = matching[0]
    assert "REVIEW" in line
    assert "0.450" in line


def test_review_flags_partial_run_warning(
    tmp_path: Path, monkeypatch
) -> None:
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    _seed_eval_summary(sdl, partial=True)
    _seed_meeting_extraction(sdl, "a")
    monkeypatch.setenv("SDL_ROOT", str(sdl))
    buf = io.StringIO()
    review_baseline_candidate(data_lake=str(tmp_path), out_stream=buf)
    text = buf.getvalue()
    assert "partial_run_warning" in text
    assert "REVIEW" in text


def test_review_handles_zero_chunks_without_divide_by_zero(
    tmp_path: Path, monkeypatch
) -> None:
    """Sev-1 Red-Team scenario #4."""
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    _seed_eval_summary(sdl)
    _seed_meeting_extraction(
        sdl,
        "a",
        total_chunks_classified=0,
        regulatory_verb_fallback_count=0,
        off_topic_count=0,
        decisions=0,
        claims=0,
        action_items=0,
    )
    monkeypatch.setenv("SDL_ROOT", str(sdl))
    buf = io.StringIO()
    rc = review_baseline_candidate(
        data_lake=str(tmp_path), out_stream=buf
    )
    assert rc == 0
    text = buf.getvalue()
    # With zero denominators, every rate is "n/a (no data)" and the
    # label is REVIEW.
    for line in text.splitlines():
        if "regulatory_verb_fallback_rate" in line:
            assert "REVIEW" in line
            assert "n/a" in line
