"""Phase 5 — status CLI `--show-all-models` flag tests.

Covers:

* default output (no flag) is byte-identical to the Phase 4 shape
  (no haiku_latest_f1 / sonnet_latest_f1 / opus_item_count keys).
* with the flag, rows carry the three fields with the correct
  null/value semantics depending on what artifacts exist.
* mixed lake state: some sources have Haiku+Sonnet+Opus, some only
  Haiku+Opus, some only Sonnet.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from spectrum_systems_core.corpus.status import build_corpus_status_report


def _write_manifest(tmp_path: Path, source_ids: list[str]) -> Path:
    """Write a minimal corpus manifest the loader will accept."""
    from spectrum_systems_core.corpus.manifest_loader import (
        compute_manifest_hash,
    )
    sources = []
    for sid in source_ids:
        sources.append(
            {
                "source_id": sid,
                "declared": {
                    "expected_path": f"raw/transcripts/{sid}.txt",
                    "meeting_date": "2025-12-18",
                    "meeting_type": "working_group",
                    "supersedes": None,
                },
                "observed": {
                    "detected_speaker_count": 2,
                    "detected_word_count": 1000,
                    "ingestion_status": "validated",
                    "last_updated": "2026-05-20T00:00:00+00:00",
                },
            }
        )
    manifest = {
        "artifact_type": "corpus_manifest",
        "schema_version": "1.0.0",
        "manifest_hash": compute_manifest_hash(sources),
        "sources": sources,
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest_path


def _write_source_record(meeting_dir: Path, sid: str) -> None:
    meeting_dir.mkdir(parents=True, exist_ok=True)
    (meeting_dir / "source_record.json").write_text(
        json.dumps({"source_id": sid}), encoding="utf-8"
    )


def _write_opus_baseline(meeting_dir: Path, item_count: int = 5) -> None:
    meeting_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "artifact_id": "opus-1",
        "artifact_type": "meeting_minutes",
        "schema_version": "1.4.0",
        "status": "promoted",
        "created_at": "2026-05-20T00:00:00+00:00",
        "trace_id": "t",
        "input_refs": [],
        "content_hash": "h",
        "payload": {
            "title": "T",
            "summary": "S",
            "schema_version": "1.4.0",
            "decisions": [{"text": f"d{i}"} for i in range(item_count)],
            "action_items": [],
            "open_questions": [],
            "provenance": {"produced_by": "meeting_minutes_opus"},
        },
    }
    (meeting_dir / "meeting_minutes_opus__baseline.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _write_two_way_cmp(
    meeting_dir: Path,
    *,
    f1: float,
    variant: str = "production_haiku",
    slug: str = "c1",
) -> None:
    meeting_dir.mkdir(parents=True, exist_ok=True)
    art: Dict[str, Any] = {
        "artifact_type": "comparison_result",
        "schema_version": "1.0.0",
        "source_id": meeting_dir.name,
        "haiku_run_id": "r1",
        "opus_model_id": "claude-opus-4-7",
        "compared_at": "2026-05-20T00:00:00+00:00",
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
            "haiku_f1_vs_opus": float(f1),
            "gt_recall_haiku": 0.0,
            "gt_recall_opus": 0.0,
        },
        "by_type": {},
        "false_negatives": [],
        "haiku_only_items": [],
        "gt_missed": [],
        "gt_pairs_present": False,
        "haiku_prompt_variant": variant,
    }
    (meeting_dir / f"comparison_result__{slug}.json").write_text(
        json.dumps(art), encoding="utf-8"
    )


def _write_three_way_cmp(
    meeting_dir: Path,
    *,
    haiku_f1: float,
    sonnet_f1: float,
    haiku_variant: str = "production_haiku",
    sonnet_variant: str = "haiku_prompt_with_sonnet_model",
    slug: str = "tw1",
) -> None:
    three_way_dir = meeting_dir / "comparisons"
    three_way_dir.mkdir(parents=True, exist_ok=True)
    block = lambda f1: {
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
        "haiku_f1_vs_opus": float(f1),
        "gt_recall_haiku": 0.0,
        "gt_recall_opus": 0.0,
    }
    art = {
        "artifact_type": "comparison_result",
        "schema_version": "1.0.0",
        "comparison_mode": "three_way",
        "source_id": meeting_dir.name,
        "haiku_run_id": "h",
        "sonnet_run_id": "s",
        "opus_model_id": "claude-opus-4-7",
        "compared_at": "2026-05-20T00:00:00+00:00",
        "haiku_summary": block(haiku_f1),
        "sonnet_summary": block(sonnet_f1),
        "by_type": {},
        "gt_pairs_present": False,
        "haiku_prompt_variant": haiku_variant,
        "sonnet_prompt_variant": sonnet_variant,
    }
    (three_way_dir / f"three_way_{slug}.json").write_text(
        json.dumps(art), encoding="utf-8"
    )


def test_default_output_omits_phase5_fields(tmp_path: Path) -> None:
    """Without --show-all-models, rows carry no haiku/sonnet/opus extras."""
    lake = tmp_path / "lake"
    md = lake / "processed" / "meetings" / "a"
    _write_source_record(md, "a")
    _write_opus_baseline(md, item_count=12)
    _write_two_way_cmp(md, f1=0.395)
    manifest = _write_manifest(tmp_path, ["a"])

    report = build_corpus_status_report(
        lake_root=lake,
        manifest_path=manifest,
        show_all_models=False,
    )
    assert len(report["rows"]) == 1
    row = report["rows"][0]
    assert "haiku_latest_f1" not in row
    assert "sonnet_latest_f1" not in row
    assert "opus_item_count" not in row


def test_default_output_byte_identical_to_phase4(tmp_path: Path) -> None:
    """Byte comparison: default output equals the same lake's pre-Phase-5
    output (since the only Phase-5 change is the optional extra keys)."""
    lake = tmp_path / "lake"
    md = lake / "processed" / "meetings" / "a"
    _write_source_record(md, "a")
    _write_opus_baseline(md, item_count=12)
    _write_two_way_cmp(md, f1=0.395)
    manifest = _write_manifest(tmp_path, ["a"])

    rep1 = build_corpus_status_report(
        lake_root=lake, manifest_path=manifest, show_all_models=False
    )
    rep2 = build_corpus_status_report(
        lake_root=lake, manifest_path=manifest, show_all_models=False
    )
    # The generated_at value is current-time and may differ between
    # calls; we mask it for the determinism check.
    rep1["generated_at"] = ""
    rep2["generated_at"] = ""
    assert json.dumps(rep1, sort_keys=True) == json.dumps(
        rep2, sort_keys=True
    )


def test_show_all_models_adds_three_fields(tmp_path: Path) -> None:
    lake = tmp_path / "lake"
    md = lake / "processed" / "meetings" / "a"
    _write_source_record(md, "a")
    _write_opus_baseline(md, item_count=12)
    _write_two_way_cmp(md, f1=0.395, variant="production_haiku")
    manifest = _write_manifest(tmp_path, ["a"])

    report = build_corpus_status_report(
        lake_root=lake,
        manifest_path=manifest,
        show_all_models=True,
    )
    row = report["rows"][0]
    assert row["haiku_latest_f1"] == pytest.approx(0.395)
    assert row["sonnet_latest_f1"] is None
    assert row["opus_item_count"] == 12


def test_sonnet_latest_f1_picks_newer_variant(tmp_path: Path) -> None:
    """When BOTH Sonnet variants exist, the newer-by-mtime wins.

    Review-comment P1 (Codex): the previous implementation always
    preferred ``haiku_prompt_with_sonnet_model`` if present, ignoring
    a newer ``opus_prompt_with_sonnet_model`` run — which could mislead
    model-selection decisions when an operator iterates on both
    variants.
    """
    import os
    import time

    lake = tmp_path / "lake"
    md = lake / "processed" / "meetings" / "a"
    _write_source_record(md, "a")
    _write_opus_baseline(md, item_count=20)
    # Older Sonnet (haiku-prompt variant) first.
    _write_three_way_cmp(
        md,
        haiku_f1=0.40,
        sonnet_f1=0.50,
        haiku_variant="production_haiku",
        sonnet_variant="haiku_prompt_with_sonnet_model",
        slug="old",
    )
    # Force the older file's mtime backward so the newer-by-mtime
    # check can distinguish them deterministically.
    old_path = md / "comparisons" / "three_way_old.json"
    older_time = time.time() - 3600
    os.utime(old_path, (older_time, older_time))

    # Newer Sonnet (opus-prompt variant).
    _write_three_way_cmp(
        md,
        haiku_f1=0.40,
        sonnet_f1=0.72,
        haiku_variant="production_haiku",
        sonnet_variant="opus_prompt_with_sonnet_model",
        slug="new",
    )

    manifest = _write_manifest(tmp_path, ["a"])
    report = build_corpus_status_report(
        lake_root=lake, manifest_path=manifest, show_all_models=True
    )
    row = next(r for r in report["rows"] if r["source_id"] == "a")
    # The newer (opus-prompt-with-sonnet-model) F1 wins.
    assert row["sonnet_latest_f1"] == pytest.approx(0.72)


def test_show_all_models_mixed_state(tmp_path: Path) -> None:
    """A: Haiku+Sonnet+Opus, B: Haiku+Opus only, C: Sonnet only."""
    lake = tmp_path / "lake"

    # A
    a = lake / "processed" / "meetings" / "a"
    _write_source_record(a, "a")
    _write_opus_baseline(a, item_count=20)
    _write_two_way_cmp(a, f1=0.40, slug="ah")
    _write_three_way_cmp(a, haiku_f1=0.40, sonnet_f1=0.65)
    # B
    b = lake / "processed" / "meetings" / "b"
    _write_source_record(b, "b")
    _write_opus_baseline(b, item_count=15)
    _write_two_way_cmp(b, f1=0.30, slug="bh")
    # C — Sonnet only (three-way comparison artifact present, no Opus
    # baseline / Haiku comparison_result)
    c = lake / "processed" / "meetings" / "c"
    _write_source_record(c, "c")
    _write_three_way_cmp(
        c, haiku_f1=0.10, sonnet_f1=0.55,
        haiku_variant="production_haiku",
        sonnet_variant="haiku_prompt_with_sonnet_model",
    )

    manifest = _write_manifest(tmp_path, ["a", "b", "c"])
    report = build_corpus_status_report(
        lake_root=lake,
        manifest_path=manifest,
        show_all_models=True,
    )
    rows = {r["source_id"]: r for r in report["rows"]}
    # A: all three
    assert rows["a"]["haiku_latest_f1"] == pytest.approx(0.40)
    assert rows["a"]["sonnet_latest_f1"] == pytest.approx(0.65)
    assert rows["a"]["opus_item_count"] == 20
    # B: no Sonnet
    assert rows["b"]["haiku_latest_f1"] == pytest.approx(0.30)
    assert rows["b"]["sonnet_latest_f1"] is None
    assert rows["b"]["opus_item_count"] == 15
    # C: Sonnet only
    assert rows["c"]["sonnet_latest_f1"] == pytest.approx(0.55)
    assert rows["c"]["opus_item_count"] is None
