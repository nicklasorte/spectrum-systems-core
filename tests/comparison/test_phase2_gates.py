"""Phase 2 paired rejection tests for comparison-engine gates.

Each gate added in Phase 2 has a fail-before / pass-after test pair:

* ``prompt_drift_post_merge`` rejection (Step 2.6)
* schema-version coherence with re-baseline requirement (Step 2.7)
* ``legacy_eval`` flag emission (Step 2.8)
* ``is_legacy_eval`` classifier correctness
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# scripts/ is not a package; threading sys.path lets us import the
# module under test the same way the script entry points do.
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


def _seed_baseline(dl: Path, sid: str, *, schema_version: str, text: str) -> None:
    bdir = dl / "store" / "processed" / "meetings" / sid / "reference_baselines"
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "opus_reference_minutes.jsonl").write_text(
        json.dumps(
            {
                "extraction_type": "decisions",
                "ground_truth_text": text,
                "model_id": "OPUS-REF",
                "schema_version": schema_version,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _seed_haiku_artifact(
    dl: Path,
    sid: str,
    *,
    schema_version: str,
    prompt_content_hash: str | None = "live-prompt-hash-v1",
    text: str = "approved the threshold",
) -> Path:
    mdir = dl / "store" / "processed" / "meetings" / sid
    mdir.mkdir(parents=True, exist_ok=True)
    payload = {
        "artifact_type": "meeting_minutes",
        "schema_version": schema_version,
        "title": "T",
        "summary": "S",
        "decisions": [text],
        "action_items": [],
        "open_questions": [],
        "provenance": {
            "produced_by": "meeting_minutes_llm",
        },
    }
    if prompt_content_hash is not None:
        payload["provenance"]["extraction_config"] = {
            "temperature": 0.0,
            "seed_inputs": {
                "model_id": "haiku-ZZ",
                "prompt_content_hash": prompt_content_hash,
                "transcript_hash": "transcript-h-1",
            },
            "chunks_full_hash": "chunks-h-1",
            "chunk_count": 1,
            "first_chunk_hash": "c1",
            "last_chunk_hash": "c1",
            "prompt_content_hash": prompt_content_hash,
        }
    artifact = {
        "artifact_id": "art-1",
        "artifact_type": "meeting_minutes",
        "schema_version": schema_version,
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


def _seed_miner_run(
    dl: Path,
    sid: str,
    *,
    expected_post_merge_prompt_hash: str,
) -> None:
    mdir = dl / "store" / "processed" / "meetings" / sid
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "correction_miner_run__abc.json").write_text(
        json.dumps(
            {
                "expected_post_merge_prompt_hash": expected_post_merge_prompt_hash,
                "candidate_id": "cand-1",
            }
        ),
        encoding="utf-8",
    )


# --------------------------------------------------------------------
# Step 2.6 — prompt-drift rejection
# --------------------------------------------------------------------
def test_prompt_drift_post_merge_blocks(tmp_path: Path) -> None:
    dl = tmp_path / "dl"
    sid = "src"
    _seed_transcript(dl, sid, "CHAIR: approved the threshold.")
    _seed_baseline(dl, sid, schema_version="1.4.0", text="approved the threshold")
    _seed_haiku_artifact(
        dl, sid,
        schema_version="1.4.0",
        prompt_content_hash="live-prompt-hash-v1",
    )
    # Miner expected a DIFFERENT post-merge hash than what production
    # actually has — the gate must fire.
    _seed_miner_run(
        dl, sid,
        expected_post_merge_prompt_hash="expected-hash-v2",
    )
    with pytest.raises(cmp.ComparisonError) as ei:
        cmp.run_comparison(data_lake=dl, source_id=sid, dry_run=True)
    assert ei.value.reason == "prompt_drift_post_merge"


def test_prompt_drift_passes_when_hashes_match(tmp_path: Path) -> None:
    dl = tmp_path / "dl"
    sid = "src"
    _seed_transcript(dl, sid, "CHAIR: approved the threshold.")
    _seed_baseline(dl, sid, schema_version="1.4.0", text="approved the threshold")
    _seed_haiku_artifact(
        dl, sid,
        schema_version="1.4.0",
        prompt_content_hash="hash-XYZ",
    )
    _seed_miner_run(
        dl, sid,
        expected_post_merge_prompt_hash="hash-XYZ",
    )
    # No exception expected.
    result = cmp.run_comparison(data_lake=dl, source_id=sid, dry_run=True)
    assert result is not None


def test_prompt_drift_gate_off_when_no_miner_run(tmp_path: Path) -> None:
    """Fresh checkout: no miner run, no drift gate active."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_transcript(dl, sid, "CHAIR: approved the threshold.")
    _seed_baseline(dl, sid, schema_version="1.4.0", text="approved the threshold")
    _seed_haiku_artifact(
        dl, sid,
        schema_version="1.4.0",
        prompt_content_hash="live-prompt-hash-v1",
    )
    # No correction_miner_run__*.json file at all.
    result = cmp.run_comparison(data_lake=dl, source_id=sid, dry_run=True)
    assert result is not None


def test_prompt_drift_gate_off_for_legacy_artifact(tmp_path: Path) -> None:
    """Legacy artifact (no extraction_config) silently skips the drift gate."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_transcript(dl, sid, "CHAIR: approved the threshold.")
    _seed_baseline(dl, sid, schema_version="1.4.0", text="approved the threshold")
    _seed_haiku_artifact(
        dl, sid,
        schema_version="1.4.0",
        prompt_content_hash=None,  # legacy: no extraction_config
    )
    _seed_miner_run(
        dl, sid,
        expected_post_merge_prompt_hash="expected-hash-v2",
    )
    # Legacy artifact -> drift gate inactive -> no halt.
    result = cmp.run_comparison(data_lake=dl, source_id=sid, dry_run=True)
    assert result is not None


# --------------------------------------------------------------------
# Step 2.7 — schema-version coherence with re-baseline
# --------------------------------------------------------------------
def test_schema_version_mismatch_with_no_matching_baseline_blocks(
    tmp_path: Path,
) -> None:
    dl = tmp_path / "dl"
    sid = "src"
    _seed_transcript(dl, sid, "CHAIR: approved the threshold.")
    _seed_baseline(dl, sid, schema_version="1.0.0", text="approved the threshold")
    _seed_haiku_artifact(
        dl, sid,
        schema_version="1.4.0",
    )
    with pytest.raises(cmp.ComparisonError) as ei:
        cmp.run_comparison(data_lake=dl, source_id=sid, dry_run=True)
    assert ei.value.reason == "schema_version_mixed"


def test_schema_version_mismatch_allowed_with_override(tmp_path: Path) -> None:
    """--allow-mixed-schema overrides the halt (last-resort operator
    knob preserved from PR #192)."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_transcript(dl, sid, "CHAIR: approved the threshold.")
    _seed_baseline(dl, sid, schema_version="1.0.0", text="approved the threshold")
    _seed_haiku_artifact(
        dl, sid,
        schema_version="1.4.0",
    )
    result = cmp.run_comparison(
        data_lake=dl,
        source_id=sid,
        dry_run=True,
        allow_mixed_schema=True,
    )
    assert result is not None


# --------------------------------------------------------------------
# Step 2.8 — legacy_eval classifier
# --------------------------------------------------------------------
def test_is_legacy_eval_true_for_pre_1_4_artifact() -> None:
    art = {
        "artifact_type": "meeting_minutes",
        "schema_version": "1.3.0",
        "payload": {"provenance": {}},
    }
    assert cmp.is_legacy_eval(art) is True


def test_is_legacy_eval_true_when_extraction_config_missing() -> None:
    art = {
        "artifact_type": "meeting_minutes",
        "schema_version": "1.4.0",
        "payload": {"provenance": {"produced_by": "x"}},
    }
    assert cmp.is_legacy_eval(art) is True


def test_is_legacy_eval_false_for_full_phase2_artifact() -> None:
    art = {
        "artifact_type": "meeting_minutes",
        "schema_version": "1.4.0",
        "payload": {
            "provenance": {
                "produced_by": "meeting_minutes_llm",
                "extraction_config": {
                    "prompt_content_hash": "h" * 64,
                    "temperature": 0.0,
                    "seed_inputs": {},
                    "chunks_full_hash": "x",
                    "chunk_count": 1,
                    "first_chunk_hash": "x",
                    "last_chunk_hash": "x",
                },
            }
        },
    }
    assert cmp.is_legacy_eval(art) is False
