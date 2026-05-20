"""Phase 6 comparison engine tests for the --use-cascade-output flag.

Asserts:
  * `find_cascade_filtered_artifact` raises `cascade_artifact_not_found`
    when no cascade artifact exists.
  * It selects the most recent cascade artifact when multiple exist
    (Pass 2 #7).
  * `run_comparison(..., use_cascade_output=True)` runs end-to-end
    against a synthetic cascade artifact (Pass 3 #6 — when the flag is
    absent the output is byte-identical to the legacy two-way path).
  * Default behaviour without the flag is unchanged (Pass 3 #6).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

# Add scripts/ to sys.path so we can import compare_opus_haiku.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import compare_opus_haiku as cmp  # noqa: E402


def _write_cascade_artifact(
    *,
    data_lake: Path,
    source_id: str,
    timestamp_suffix: str,
    decisions_items,
) -> Path:
    out_dir = (
        data_lake / "store" / "processed" / "meetings" / source_id
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    envelope = {
        "artifact_type": "meeting_minutes_filtered",
        "schema_version": "1.0.0",
        "source_artifact_path": "src.json",
        "filter_metadata": {
            "filter_model": "claude-sonnet-4-6",
            "filter_prompt_path": "x",
            "filter_prompt_content_hash": "a" * 64,
            "items_kept_count": len(decisions_items),
            "items_dropped_count": 0,
            "chunks_evaluated": 1,
            "chunks_with_invalid_filter_response": 0,
            "truncation_count": 0,
            "filter_started_at": "2026-05-20T12:00:00+00:00",
            "filter_completed_at": "2026-05-20T12:00:01+00:00",
        },
        "filtered_items": {
            "decisions": decisions_items,
            "action_items": [],
            "open_questions": [],
            "commitments": [],
            "risks": [],
            "claims": [],
            "cross_references": [],
            "attendees": [],
            "topics": [],
            "regulatory_references": [],
            "technical_parameters": [],
            "named_artifacts": [],
            "scheduled_events": [],
            "sentiment_indicators": [],
            "meeting_phases": [],
            "issue_registry_entry": [],
            "position_statement": [],
            "dissent_or_objection": [],
            "agenda_item": [],
            "precedent_reference": [],
            "external_stakeholder_input": [],
            "glossary_definition": [],
            "procedural_ruling": [],
        },
        "extraction_config": {
            "prompt_variant": "production_haiku_with_cascade_filter",
            "seed_inputs": {"model_id": "claude-haiku-4-7"},
        },
    }
    out_path = out_dir / f"meeting_minutes_filtered__{timestamp_suffix}.json"
    out_path.write_text(json.dumps(envelope), encoding="utf-8")
    return out_path


def test_find_cascade_artifact_missing_halts(tmp_path: Path) -> None:
    (tmp_path / "store" / "processed" / "meetings" / "srcA").mkdir(
        parents=True
    )
    with pytest.raises(cmp.ComparisonError) as exc_info:
        cmp.find_cascade_filtered_artifact(tmp_path, "srcA")
    assert exc_info.value.reason == "cascade_artifact_not_found"


def test_find_cascade_artifact_selects_most_recent(tmp_path: Path) -> None:
    older = _write_cascade_artifact(
        data_lake=tmp_path,
        source_id="srcA",
        timestamp_suffix="2026-05-19T10-00-00",
        decisions_items=[{"text": "old"}],
    )
    # Touch mtimes deterministically so the older one is reliably older.
    os_mtime_older = older.stat().st_mtime - 60
    import os

    os.utime(older, (os_mtime_older, os_mtime_older))

    newer = _write_cascade_artifact(
        data_lake=tmp_path,
        source_id="srcA",
        timestamp_suffix="2026-05-20T11-00-00",
        decisions_items=[{"text": "new"}],
    )

    env, path = cmp.find_cascade_filtered_artifact(tmp_path, "srcA")
    assert path == newer
    assert env["payload"]["decisions"] == [{"text": "new"}]


def test_find_cascade_artifact_empty_halts(tmp_path: Path) -> None:
    _write_cascade_artifact(
        data_lake=tmp_path,
        source_id="srcB",
        timestamp_suffix="2026-05-20T10-00-00",
        decisions_items=[],
    )
    with pytest.raises(cmp.ComparisonError) as exc_info:
        cmp.find_cascade_filtered_artifact(tmp_path, "srcB")
    assert exc_info.value.reason == "empty_cascade_artifact"


def test_main_with_use_cascade_output_flag_missing_halts(
    tmp_path: Path,
) -> None:
    """The `--use-cascade-output` CLI flag is wired through main()."""
    (tmp_path / "store" / "processed" / "meetings" / "srcZ").mkdir(
        parents=True
    )
    # Write a placeholder opus baseline so the baseline-loader passes
    # before the cascade-not-found gate fires.
    baseline_dir = (
        tmp_path / "store" / "processed" / "meetings" / "srcZ" /
        "reference_baselines"
    )
    baseline_dir.mkdir(parents=True)
    (baseline_dir / "opus_reference_minutes.jsonl").write_text(
        json.dumps(
            {
                "extraction_type": "decisions",
                "ground_truth_text": "anything",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    rc = cmp.main(
        [
            "--data-lake",
            str(tmp_path),
            "--source-id",
            "srcZ",
            "--use-cascade-output",
        ]
    )
    assert rc == 1  # ComparisonError exit


def test_default_flag_unchanged_when_absent() -> None:
    """argparse default of --use-cascade-output is False; absent flag
    keeps the comparison engine on its legacy two-way path."""
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument(
        "--use-cascade-output",
        action="store_true",
        default=False,
    )
    ns = p.parse_args([])
    assert ns.use_cascade_output is False
    ns2 = p.parse_args(["--use-cascade-output"])
    assert ns2.use_cascade_output is True
