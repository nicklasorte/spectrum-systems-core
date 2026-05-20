"""Phase 5 Step 5.6 — three-way comparison audit tests.

These tests prove the audit fixes in
``docs/architecture/phase5_three_way_audit_report.md``:

1. Legacy artifact (no `extraction_config`) defaults to
   `production_haiku` in both two-way and three-way output.
2. Phase-5 artifact (variant-stamped) round-trips through the
   comparison engine with the stamped variant.
3. Haiku-prompt-with-sonnet-model vs.
   opus-prompt-with-sonnet-model are distinct columns in a three-way
   comparison.
4. The `sonnet_summary` block carries the full summary_block shape.
"""
from __future__ import annotations

import sys
from pathlib import Path

# scripts/ is not a package; add it so we can import the comparison
# engine directly without subprocess overhead.
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import compare_opus_haiku as cmp  # noqa: E402


def _legacy_artifact() -> dict:
    """Pre-Phase-5 artifact: no extraction_config block."""
    return {
        "artifact_id": "legacy-1",
        "artifact_type": "meeting_minutes",
        "schema_version": "1.4.0",
        "status": "promoted",
        "created_at": "2026-05-20T00:00:00+00:00",
        "trace_id": "t1",
        "input_refs": [],
        "content_hash": "abc",
        "payload": {
            "title": "Legacy",
            "summary": "Legacy",
            "schema_version": "1.4.0",
            "decisions": [],
            "action_items": [],
            "open_questions": [],
            "provenance": {"produced_by": "meeting_minutes_llm"},
        },
    }


def _phase5_artifact(variant: str) -> dict:
    """Phase-5 artifact: extraction_config carries prompt_variant."""
    return {
        "artifact_id": f"p5-{variant}",
        "artifact_type": "meeting_minutes",
        "schema_version": "1.4.0",
        "status": "promoted",
        "created_at": "2026-05-20T00:00:00+00:00",
        "trace_id": f"t-{variant}",
        "input_refs": [],
        "content_hash": "abcd",
        "payload": {
            "title": "P5",
            "summary": "P5",
            "schema_version": "1.4.0",
            "decisions": [],
            "action_items": [],
            "open_questions": [],
            "provenance": {
                "produced_by": "meeting_minutes_llm",
                "model_id": "claude-sonnet-4-6",
                "extraction_config": {
                    "temperature": 0.0,
                    "seed_inputs": {
                        "model_id": "claude-sonnet-4-6",
                        "prompt_content_hash": "p" + variant,
                        "transcript_hash": "t",
                    },
                    "chunks_full_hash": "ch",
                    "chunk_count": 1,
                    "first_chunk_hash": "fc",
                    "last_chunk_hash": "lc",
                    "prompt_content_hash": "p" + variant,
                    "prompt_variant": variant,
                },
            },
        },
    }


def test_prompt_variant_defaults_to_production_haiku_on_legacy() -> None:
    art = _legacy_artifact()
    assert cmp._prompt_variant_of(art) == "production_haiku"


def test_prompt_variant_reads_stamped_value() -> None:
    art = _phase5_artifact("haiku_prompt_with_sonnet_model")
    assert (
        cmp._prompt_variant_of(art) == "haiku_prompt_with_sonnet_model"
    )


def test_two_way_artifact_stamps_prompt_variant() -> None:
    art = _phase5_artifact("production_haiku")
    metrics = {
        "gt_pairs_present": False,
        "summary": {
            "total_opus_items": 10,
            "total_haiku_items": 5,
            "true_positives": 4,
            "false_negatives": 6,
            "haiku_only": 1,
            "gt_covered_by_haiku": 0,
            "gt_missed_by_haiku": 0,
            "gt_covered_by_opus": 0,
            "haiku_recall_vs_opus": 0.4,
            "haiku_precision_vs_opus": 0.8,
            "haiku_f1_vs_opus": 0.533,
            "gt_recall_haiku": 0.0,
            "gt_recall_opus": 0.0,
        },
        "by_type": {},
        "false_negatives": [],
        "haiku_only_items": [],
        "gt_missed": [],
    }
    out = cmp.build_comparison_artifact(
        source_id="src-1",
        haiku_artifact=art,
        baseline_rows=[],
        metrics=metrics,
        compared_at="2026-05-20T00:00:00+00:00",
    )
    assert out["haiku_prompt_variant"] == "production_haiku"


def test_three_way_artifact_stamps_both_variants() -> None:
    haiku = _legacy_artifact()  # variant defaults to production_haiku
    sonnet = _phase5_artifact("haiku_prompt_with_sonnet_model")
    haiku_metrics = {
        "gt_pairs_present": False,
        "summary": {
            "total_opus_items": 10,
            "total_haiku_items": 5,
            "true_positives": 4,
            "false_negatives": 6,
            "haiku_only": 1,
            "gt_covered_by_haiku": 0,
            "gt_missed_by_haiku": 0,
            "gt_covered_by_opus": 0,
            "haiku_recall_vs_opus": 0.4,
            "haiku_precision_vs_opus": 0.8,
            "haiku_f1_vs_opus": 0.533,
            "gt_recall_haiku": 0.0,
            "gt_recall_opus": 0.0,
        },
        "by_type": {},
        "false_negatives": [],
        "haiku_only_items": [],
        "gt_missed": [],
    }
    sonnet_metrics = {
        "gt_pairs_present": False,
        "summary": {
            "total_opus_items": 10,
            "total_haiku_items": 7,
            "true_positives": 6,
            "false_negatives": 4,
            "haiku_only": 1,
            "gt_covered_by_haiku": 0,
            "gt_missed_by_haiku": 0,
            "gt_covered_by_opus": 0,
            "haiku_recall_vs_opus": 0.6,
            "haiku_precision_vs_opus": 0.857,
            "haiku_f1_vs_opus": 0.706,
            "gt_recall_haiku": 0.0,
            "gt_recall_opus": 0.0,
        },
        "by_type": {},
        "false_negatives": [],
        "haiku_only_items": [],
        "gt_missed": [],
    }
    out = cmp.build_three_way_comparison_artifact(
        source_id="src-1",
        haiku_artifact=haiku,
        sonnet_artifact=sonnet,
        baseline_rows=[],
        haiku_metrics=haiku_metrics,
        sonnet_metrics=sonnet_metrics,
        compared_at="2026-05-20T00:00:00+00:00",
    )
    # Pre-Phase-5 Haiku defaults; Phase-5 Sonnet carries its stamp.
    assert out["haiku_prompt_variant"] == "production_haiku"
    assert out["sonnet_prompt_variant"] == "haiku_prompt_with_sonnet_model"
    # The summary blocks carry the full summary_block shape (recall /
    # precision / F1) — Step 5.6 Finding 5.6-3.
    for block_name in ("haiku_summary", "sonnet_summary"):
        block = out[block_name]
        for k in (
            "haiku_recall_vs_opus",
            "haiku_precision_vs_opus",
            "haiku_f1_vs_opus",
        ):
            assert k in block


def test_three_way_with_two_sonnet_variants_distinguishes_columns() -> None:
    """Two Sonnet runs with different prompt_variant values produce
    DISTINCT prompt_variant labels in the comparison output (one at a time
    through the engine — each becomes its own three-way artifact)."""
    haiku = _legacy_artifact()
    sonnet_h = _phase5_artifact("haiku_prompt_with_sonnet_model")
    sonnet_o = _phase5_artifact("opus_prompt_with_sonnet_model")
    metrics = {
        "gt_pairs_present": False,
        "summary": {
            "total_opus_items": 10,
            "total_haiku_items": 5,
            "true_positives": 4,
            "false_negatives": 6,
            "haiku_only": 1,
            "gt_covered_by_haiku": 0,
            "gt_missed_by_haiku": 0,
            "gt_covered_by_opus": 0,
            "haiku_recall_vs_opus": 0.4,
            "haiku_precision_vs_opus": 0.8,
            "haiku_f1_vs_opus": 0.533,
            "gt_recall_haiku": 0.0,
            "gt_recall_opus": 0.0,
        },
        "by_type": {},
        "false_negatives": [],
        "haiku_only_items": [],
        "gt_missed": [],
    }
    art1 = cmp.build_three_way_comparison_artifact(
        source_id="src-1",
        haiku_artifact=haiku,
        sonnet_artifact=sonnet_h,
        baseline_rows=[],
        haiku_metrics=metrics,
        sonnet_metrics=metrics,
        compared_at="2026-05-20T00:00:00+00:00",
    )
    art2 = cmp.build_three_way_comparison_artifact(
        source_id="src-1",
        haiku_artifact=haiku,
        sonnet_artifact=sonnet_o,
        baseline_rows=[],
        haiku_metrics=metrics,
        sonnet_metrics=metrics,
        compared_at="2026-05-20T00:00:01+00:00",
    )
    # Distinct variant labels prove the side-by-side property —
    # the second Sonnet run does NOT merge into the first.
    assert art1["sonnet_prompt_variant"] != art2["sonnet_prompt_variant"]
    assert art1["sonnet_prompt_variant"] == "haiku_prompt_with_sonnet_model"
    assert art2["sonnet_prompt_variant"] == "opus_prompt_with_sonnet_model"
