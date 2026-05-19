"""Phase 1 — correction miner reads grounding_rejection_report artifacts.

The miner's pattern detection has been extended to surface
hallucination patterns from `grounding_rejection_report` artifacts as
their own failure category (Step 1.3). This test proves the loader
finds them and the classifier assigns the right pattern_type per
reason_code.

These tests do NOT exercise the miner's full orchestration (that
requires comparisons too) — they isolate the new code paths.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import correction_miner as miner  # noqa: E402


def _write_report(
    dl: Path,
    source_id: str,
    run_id: str,
    rejected: list[dict],
    *,
    blocked: bool = False,
    block_reason: str | None = None,
) -> Path:
    diag_dir = (
        dl / "store" / "processed" / "meetings" / source_id / "diagnostics"
    )
    diag_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "artifact_type": "grounding_rejection_report",
        "schema_version": "1.0.0",
        "target_artifact_id": f"art-{run_id}",
        "target_artifact_type": "meeting_minutes",
        "trace_id": f"trace-{run_id}",
        "source_id": source_id,
        "run_id": run_id,
        "grounding_rate": (
            len(rejected) / max(1, len(rejected))
            if rejected
            else 1.0
        ),
        "accepted_count": 0,
        "rejected_count": len(rejected),
        "artifact_blocked": blocked,
        "block_reason_code": block_reason,
        "rejected_items": rejected,
    }
    path = diag_dir / f"grounding_rejection_report__{run_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_loader_returns_empty_when_diagnostics_dir_missing(tmp_path: Path):
    out = miner.load_grounding_rejection_reports(tmp_path, "no-such-src")
    assert out == []


def test_loader_finds_reports_under_diagnostics_dir(tmp_path: Path):
    sid = "src"
    _write_report(
        tmp_path,
        sid,
        "run-1",
        [
            {
                "item_type": "decisions",
                "item": {"text": "Hello", "source_quote": "Hello world."},
                "reason_code": "grounding_exact_text_not_in_transcript",
                "detail": "not present",
                "expected_quote_normalized": "hello world",
                "actual_at_offset_normalized": "",
                "offset_checked": 0,
            }
        ],
    )
    reports = miner.load_grounding_rejection_reports(tmp_path, sid)
    assert len(reports) == 1
    assert reports[0]["artifact_type"] == "grounding_rejection_report"


def test_loader_skips_schema_invalid_files(tmp_path: Path):
    """The read-path validator must reject a malformed report rather
    than letting the miner crash deep inside the analyzer."""
    sid = "src"
    diag_dir = (
        tmp_path / "store" / "processed" / "meetings" / sid / "diagnostics"
    )
    diag_dir.mkdir(parents=True)
    (diag_dir / "grounding_rejection_report__bad.json").write_text(
        "{}",
        encoding="utf-8",
    )
    out = miner.load_grounding_rejection_reports(tmp_path, sid)
    assert out == []


def test_analyzer_classifies_each_gate_reason_code():
    """Every gate reason_code must map to one of the four
    hallucination_* pattern_types defined in PATTERN_TYPES."""
    reports = [
        {
            "rejected_items": [
                {
                    "item_type": "decisions",
                    "item": {"source_quote": "made-up"},
                    "reason_code": "grounding_exact_text_not_in_transcript",
                    "detail": "...",
                    "expected_quote_normalized": "made-up",
                },
                {
                    "item_type": "claims",
                    "item": {"source_quote": "real but wrong offset"},
                    "reason_code": "grounding_offset_mismatch",
                    "detail": "...",
                    "expected_quote_normalized": "real but wrong offset",
                },
                {
                    "item_type": "risks",
                    "item": {},
                    "reason_code": "grounding_missing_field",
                    "detail": "...",
                },
                {
                    "item_type": "attendees",
                    "item": {"source_turn_ids": [9999]},
                    "reason_code": "grounding_unknown_turn_id",
                    "detail": "...",
                },
            ]
        }
    ]
    patterns = miner.analyze_failure_patterns(
        [], grounding_rejections=reports
    )
    pat_types = {p.pattern_type for p in patterns}
    assert pat_types == {
        "hallucination_paraphrase",
        "hallucination_offset_drift",
        "hallucination_missing_field",
        "hallucination_unknown_turn",
    }


def test_analyzer_folds_grounding_into_total_denominator():
    """The percentage_of_fns figure must consider grounding rejections
    in its denominator so the analysis is comparable run-to-run."""
    reports = [
        {
            "rejected_items": [
                {
                    "item_type": "decisions",
                    "item": {"source_quote": "fake"},
                    "reason_code": "grounding_exact_text_not_in_transcript",
                    "detail": "x",
                    "expected_quote_normalized": "fake",
                }
            ]
        }
    ]
    patterns = miner.analyze_failure_patterns(
        [], grounding_rejections=reports
    )
    assert len(patterns) == 1
    assert patterns[0].percentage_of_fns == 1.0


def test_analyzer_ignores_unknown_reason_codes_safely():
    """An unknown reason_code must not crash the analyzer or pollute
    a wrong bucket — it's skipped."""
    reports = [
        {
            "rejected_items": [
                {
                    "item_type": "decisions",
                    "item": {},
                    "reason_code": "completely_unknown",
                    "detail": "x",
                }
            ]
        }
    ]
    patterns = miner.analyze_failure_patterns(
        [], grounding_rejections=reports
    )
    assert patterns == []
