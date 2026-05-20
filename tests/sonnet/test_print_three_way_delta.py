"""Phase 5 — print_three_way_delta.py variant-filter tests.

Codex review P2: when both Sonnet variants exist for a source, the
script must pick the artifact whose ``sonnet_prompt_variant`` matches
the operator-requested ``--variant``, not just the newest.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# scripts/ is not a package; add it so we can import the helper directly.
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import print_three_way_delta as p3d  # noqa: E402


def _write_three_way(
    cmp_dir: Path,
    *,
    slug: str,
    sonnet_variant: str,
    sonnet_f1: float,
) -> Path:
    cmp_dir.mkdir(parents=True, exist_ok=True)
    summary_block = lambda f1: {
        "total_opus_items": 100,
        "total_haiku_items": 40,
        "true_positives": 38,
        "false_negatives": 62,
        "haiku_only": 2,
        "gt_covered_by_haiku": 0,
        "gt_missed_by_haiku": 0,
        "gt_covered_by_opus": 0,
        "haiku_recall_vs_opus": 0.38,
        "haiku_precision_vs_opus": 0.95,
        "haiku_f1_vs_opus": float(f1),
        "gt_recall_haiku": 0,
        "gt_recall_opus": 0,
    }
    art = {
        "artifact_type": "comparison_result",
        "schema_version": "1.0.0",
        "comparison_mode": "three_way",
        "source_id": cmp_dir.parent.name,
        "haiku_run_id": "h",
        "sonnet_run_id": "s",
        "opus_model_id": "claude-opus-4-7",
        "compared_at": "2026-05-20T00:00:00+00:00",
        "haiku_summary": summary_block(0.543),
        "sonnet_summary": summary_block(sonnet_f1),
        "by_type": {},
        "gt_pairs_present": False,
        "haiku_prompt_variant": "production_haiku",
        "sonnet_prompt_variant": sonnet_variant,
    }
    p = cmp_dir / f"three_way_{slug}.json"
    p.write_text(json.dumps(art), encoding="utf-8")
    return p


def test_no_variant_filter_picks_newest(tmp_path: Path) -> None:
    """Legacy callers (no want_sonnet_variant) get the newest artifact."""
    import os
    import time

    cmp_dir = tmp_path / "store" / "processed" / "meetings" / "a" / "comparisons"
    p_old = _write_three_way(
        cmp_dir, slug="old",
        sonnet_variant="haiku_prompt_with_sonnet_model",
        sonnet_f1=0.50,
    )
    os.utime(p_old, (time.time() - 3600, time.time() - 3600))
    _write_three_way(
        cmp_dir, slug="new",
        sonnet_variant="opus_prompt_with_sonnet_model",
        sonnet_f1=0.72,
    )
    found = p3d._latest_three_way_artifact(tmp_path, "a")
    assert found is not None
    assert found.name == "three_way_new.json"


def test_variant_filter_picks_matching_artifact(tmp_path: Path) -> None:
    """`--variant haiku-prompt` selects the haiku-prompt-variant artifact
    even when the opus-prompt artifact is newer (review-comment P2)."""
    import os
    import time

    cmp_dir = tmp_path / "store" / "processed" / "meetings" / "a" / "comparisons"
    p_old = _write_three_way(
        cmp_dir, slug="old_haiku_prompt",
        sonnet_variant="haiku_prompt_with_sonnet_model",
        sonnet_f1=0.50,
    )
    os.utime(p_old, (time.time() - 3600, time.time() - 3600))
    p_new = _write_three_way(
        cmp_dir, slug="new_opus_prompt",
        sonnet_variant="opus_prompt_with_sonnet_model",
        sonnet_f1=0.72,
    )
    # Operator requested the haiku-prompt variant — must pick the OLD
    # file (the only one with that variant), not the new opus-prompt one.
    found = p3d._latest_three_way_artifact(
        tmp_path,
        "a",
        want_sonnet_variant="haiku_prompt_with_sonnet_model",
    )
    assert found == p_old

    # Same setup, but request the opus-prompt variant — picks the new file.
    found_opus = p3d._latest_three_way_artifact(
        tmp_path,
        "a",
        want_sonnet_variant="opus_prompt_with_sonnet_model",
    )
    assert found_opus == p_new


def test_variant_filter_returns_none_when_no_match(tmp_path: Path) -> None:
    """No matching variant on disk → return None, not the wrong-variant newest."""
    cmp_dir = tmp_path / "store" / "processed" / "meetings" / "a" / "comparisons"
    _write_three_way(
        cmp_dir, slug="only_haiku_prompt",
        sonnet_variant="haiku_prompt_with_sonnet_model",
        sonnet_f1=0.50,
    )
    found = p3d._latest_three_way_artifact(
        tmp_path,
        "a",
        want_sonnet_variant="opus_prompt_with_sonnet_model",
    )
    assert found is None
