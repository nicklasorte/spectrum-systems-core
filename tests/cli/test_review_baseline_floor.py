"""Phase P — tests for the absolute-minimum sanity floor.

Verifies the ``total_extracted_items >= 50`` floor added to
``review-baseline-candidate``. We invoke the real CLI function over a
staged SDL root rather than mocking metric values — the floor is only
trustworthy if it computes from real meeting_extraction artifacts.
"""
from __future__ import annotations

import io
import json
import uuid
from pathlib import Path

from spectrum_systems_core.cli import (
    _count_total_extracted_items,
    review_baseline_candidate,
)


def _seed_eval_summary(sdl: Path, pipeline_run_id: str = "run-x") -> Path:
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
                        "coverage": 0.8,
                        "precision": 0.7,
                        "pairs_count": 1,
                    },
                    "character_count_fallback": {
                        "coverage": 0.0,
                        "precision": 0.0,
                        "pairs_count": 0,
                    },
                },
                "eval_results": [],
                "is_baseline": False,
                "baseline_eval_summary_id": None,
                "regression_detected": False,
                "regression_detail": [],
                "partial_run_warning": False,
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
    decisions: int,
    claims: int,
    action_items: int,
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
                "total_chunks_classified": 100,
                "off_topic_count": 0,
                "regulatory_verb_fallback_count": 0,
                "requires_human_dedup_count": 0,
                "decisions": [{"id": i} for i in range(decisions)],
                "claims": [{"id": i} for i in range(claims)],
                "action_items": [{"id": i} for i in range(action_items)],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return fp


# ---------------------------------------------------------------------------
# Floor checks
# ---------------------------------------------------------------------------


def test_review_flags_low_total_extracted_items(
    tmp_path: Path, monkeypatch
) -> None:
    """30 items across all meetings => REVIEW, with rationale in output."""
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    _seed_eval_summary(sdl)
    # 30 items total across 3 meetings: 10 each.
    for sid in ("a", "b", "c"):
        _seed_meeting_extraction(
            sdl, sid, decisions=4, claims=3, action_items=3
        )

    monkeypatch.setenv("SDL_ROOT", str(sdl))

    buf = io.StringIO()
    rc = review_baseline_candidate(
        data_lake=str(tmp_path), out_stream=buf
    )
    text = buf.getvalue()
    assert rc == 0
    # The floor line must carry REVIEW and the rationale phrase.
    floor_lines = [
        ln for ln in text.splitlines() if "total_extracted_items" in ln
    ]
    assert floor_lines, f"floor line missing from:\n{text}"
    joined = "\n".join(floor_lines + [text])
    assert "REVIEW" in joined
    assert "under-producing" in text
    assert "30" in joined


def test_review_passes_with_sufficient_items(
    tmp_path: Path, monkeypatch
) -> None:
    """75 items across all meetings => PASS for the floor."""
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    _seed_eval_summary(sdl)
    # 75 items total: 25 each across 3 meetings.
    for sid in ("a", "b", "c"):
        _seed_meeting_extraction(
            sdl, sid, decisions=10, claims=10, action_items=5
        )

    monkeypatch.setenv("SDL_ROOT", str(sdl))

    buf = io.StringIO()
    rc = review_baseline_candidate(
        data_lake=str(tmp_path), out_stream=buf
    )
    text = buf.getvalue()
    assert rc == 0
    floor_lines = [
        ln for ln in text.splitlines() if "total_extracted_items" in ln
    ]
    assert floor_lines, f"floor line missing from:\n{text}"
    line = floor_lines[0]
    assert "PASS" in line
    assert "75" in line


def test_review_threshold_is_strict_less_than(
    tmp_path: Path, monkeypatch
) -> None:
    """Red-team scenario 4: total_extracted_items == 50 must PASS (>= 50)."""
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    _seed_eval_summary(sdl)
    # 50 items exactly.
    _seed_meeting_extraction(
        sdl, "only", decisions=20, claims=20, action_items=10
    )

    monkeypatch.setenv("SDL_ROOT", str(sdl))

    buf = io.StringIO()
    rc = review_baseline_candidate(
        data_lake=str(tmp_path), out_stream=buf
    )
    text = buf.getvalue()
    assert rc == 0
    floor_lines = [
        ln for ln in text.splitlines() if "total_extracted_items" in ln
    ]
    line = floor_lines[0]
    assert "PASS" in line
    assert "50" in line


def test_count_total_extracted_items_helper() -> None:
    """The helper sums decisions + claims + action_items across meetings."""
    extractions = [
        {"decisions": [{}, {}], "claims": [{}], "action_items": []},
        {"decisions": [], "claims": [{}, {}], "action_items": [{}]},
    ]
    assert _count_total_extracted_items(extractions) == 6
    # Non-list values must not crash the helper.
    assert _count_total_extracted_items([{"decisions": "oops"}]) == 0
    assert _count_total_extracted_items([]) == 0
