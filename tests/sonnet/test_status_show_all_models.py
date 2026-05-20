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
    in_comparisons_subdir: bool = False,
) -> None:
    """Write a two-way comparison artifact.

    ``in_comparisons_subdir=True`` writes to ``comparisons/haiku_vs_opus_<slug>.json``
    — the PRODUCTION path that ``scripts/compare_opus_haiku._comparison_out_path``
    targets. The default (``False``) writes the legacy / fixture path
    (``comparison_result__<slug>.json`` in the meeting root) so existing
    tests do not break.
    """
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
    if in_comparisons_subdir:
        sub = meeting_dir / "comparisons"
        sub.mkdir(parents=True, exist_ok=True)
        out = sub / f"haiku_vs_opus_{slug}.json"
    else:
        out = meeting_dir / f"comparison_result__{slug}.json"
    out.write_text(json.dumps(art), encoding="utf-8")


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


def test_opus_item_count_uses_schema_backed_type_list(tmp_path: Path) -> None:
    """`_opus_item_count` reads array names from the schema, not a hard-coded tuple.

    Review-comment P2 (Codex): the hard-coded type tuple drifted from
    the meeting_minutes schema. The fix reads the array names from the
    schema itself (mirroring `compare_opus_haiku.extraction_types`).

    This test asserts the helper:
      1. counts items in a non-hardcoded array (`commitments` is in the
         schema today but was NOT in the legacy tuple); and
      2. matches the union of declared array types in the schema.
    """
    import json as _json

    from spectrum_systems_core.corpus.status import (
        _extraction_types_from_schema,
        _opus_item_count,
    )

    # The schema-derived type list MUST contain every Phase-5 type
    # that the comparison engine compares; cross-check against the
    # canonical extraction_types() helper.
    import sys as _sys
    _scripts = Path(__file__).resolve().parents[2] / "scripts"
    if str(_scripts) not in _sys.path:
        _sys.path.insert(0, str(_scripts))
    import compare_opus_haiku as _cmp

    status_types = _extraction_types_from_schema()
    cmp_types = _cmp.extraction_types()
    # Both readers must agree on the type set.
    assert set(status_types) == set(cmp_types), (
        f"status type list drifted from comparison engine: "
        f"status_only={set(status_types) - set(cmp_types)}, "
        f"cmp_only={set(cmp_types) - set(status_types)}"
    )

    # Now exercise the counter with a payload that lands items in
    # every schema array. Items don't need to be valid envelopes for
    # the count (the function just sums list lengths).
    md = tmp_path / "lake" / "processed" / "meetings" / "src"
    md.mkdir(parents=True, exist_ok=True)
    payload_arrays: dict = {t: [{"placeholder": True}] for t in status_types}
    (md / "meeting_minutes_opus__b.json").write_text(
        _json.dumps(
            {
                "artifact_id": "x",
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
                    "provenance": {"produced_by": "meeting_minutes_opus"},
                    **payload_arrays,
                },
            }
        ),
        encoding="utf-8",
    )
    # One item per type — total = len(status_types).
    assert _opus_item_count(md) == len(status_types)


def test_production_comparison_layout_drives_state_consistently(
    tmp_path: Path,
) -> None:
    """`comparisons/haiku_vs_opus_*.json` must drive both haiku_latest_f1
    AND state/recommendation so they cannot contradict.

    Review-comment P2 (Codex): the original `_has_comparison_result`
    only scanned the legacy `comparison_result__*.json` path while
    `_latest_f1_by_variant` looks under `comparisons/`. That meant a
    production lake could show `haiku_latest_f1=0.395` AND
    `state=baseline_complete` + `recommendation=run_comparison` —
    misleading operators into re-running comparisons that already
    existed. After this fix both helpers read the same artifacts.
    """
    lake = tmp_path / "lake"
    md = lake / "processed" / "meetings" / "a"
    _write_source_record(md, "a")
    _write_opus_baseline(md, item_count=10)
    # PRODUCTION-layout comparison artifact only — no
    # comparison_result__*.json in the meeting root.
    _write_two_way_cmp(
        md,
        f1=0.395,
        variant="production_haiku",
        slug="20260520T000000",
        in_comparisons_subdir=True,
    )

    manifest = _write_manifest(tmp_path, ["a"])
    report = build_corpus_status_report(
        lake_root=lake, manifest_path=manifest, show_all_models=True
    )
    row = next(r for r in report["rows"] if r["source_id"] == "a")
    # Haiku F1 is populated (production path).
    assert row["haiku_latest_f1"] == pytest.approx(0.395)
    # The state/recommendation now reflect the SAME artifact — must
    # be comparison_complete, not baseline_complete.
    assert row["state"] == "comparison_complete"
    assert row["recommendation"] == "none"
    # has_comparison_result must be True since a real artifact exists.
    assert row["has_comparison_result"] is True


def test_two_way_comparisons_in_comparisons_subdir_are_picked_up(
    tmp_path: Path,
) -> None:
    """Production two-way artifacts live at `comparisons/haiku_vs_opus_*.json`.

    Review-comment P1 (Codex): the rollup originally only scanned
    `comparison_result__*.json` in the meeting root, but the real
    comparison pipeline writes to `comparisons/haiku_vs_opus_*.json`
    (see `scripts/compare_opus_haiku._comparison_out_path`). Without
    this fix, every operator running the normal workflow would see
    `null` Haiku F1s in `--show-all-models`.
    """
    lake = tmp_path / "lake"
    md = lake / "processed" / "meetings" / "a"
    _write_source_record(md, "a")
    _write_opus_baseline(md, item_count=10)
    # Write the two-way artifact at the PRODUCTION path.
    _write_two_way_cmp(
        md, f1=0.395, variant="production_haiku",
        slug="20260520T000000",
        in_comparisons_subdir=True,
    )

    manifest = _write_manifest(tmp_path, ["a"])
    report = build_corpus_status_report(
        lake_root=lake, manifest_path=manifest, show_all_models=True
    )
    row = next(r for r in report["rows"] if r["source_id"] == "a")
    # The Haiku F1 is read from the comparisons/ subdir.
    assert row["haiku_latest_f1"] == pytest.approx(0.395)


def test_opus_item_count_picks_newest_baseline_by_mtime(tmp_path: Path) -> None:
    """When multiple Opus baselines exist, the NEWEST by mtime wins.

    Review-comment P2 (Codex): the previous implementation sorted
    baselines lexicographically by filename — but the slug segment is
    content-hash-based, so a newer baseline with a lower-sorting
    filename would be ignored. The mtime rule mirrors the F1 selection.
    """
    import os
    import time

    lake = tmp_path / "lake"
    md = lake / "processed" / "meetings" / "a"
    md.mkdir(parents=True, exist_ok=True)
    _write_source_record(md, "a")

    # Older Opus baseline with a lexicographically LATER filename
    # (`zzz` sorts AFTER `aaa`).
    _write_opus_baseline(md, item_count=5)
    older = md / "meeting_minutes_opus__baseline.json"
    older.rename(md / "meeting_minutes_opus__zzz_old.json")
    older_path = md / "meeting_minutes_opus__zzz_old.json"
    older_time = time.time() - 3600
    os.utime(older_path, (older_time, older_time))

    # Newer Opus baseline with a lexicographically EARLIER filename
    # (`aaa` sorts BEFORE `zzz`).
    _write_opus_baseline(md, item_count=42)
    newer = md / "meeting_minutes_opus__baseline.json"
    newer.rename(md / "meeting_minutes_opus__aaa_new.json")

    manifest = _write_manifest(tmp_path, ["a"])
    report = build_corpus_status_report(
        lake_root=lake, manifest_path=manifest, show_all_models=True
    )
    row = next(r for r in report["rows"] if r["source_id"] == "a")
    # The newer baseline (42 items) wins; lexicographic sort would
    # have picked the older (5-item) one.
    assert row["opus_item_count"] == 42


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


def test_human_readable_table_renders_model_columns(tmp_path: Path) -> None:
    """`--show-all-models` MUST surface in the non-JSON table too.

    Review-comment P2 (Codex): the previous `_format_status_table`
    rendered only source_id/state/recommendation, so the flag had no
    visible effect unless `--json` was also passed.
    """
    from spectrum_systems_core.data_lake.cli import _format_status_table

    lake = tmp_path / "lake"
    md = lake / "processed" / "meetings" / "a"
    _write_source_record(md, "a")
    _write_opus_baseline(md, item_count=12)
    _write_two_way_cmp(md, f1=0.395)
    manifest = _write_manifest(tmp_path, ["a"])

    # Legacy / default path: no model columns.
    legacy_report = build_corpus_status_report(
        lake_root=lake, manifest_path=manifest, show_all_models=False
    )
    legacy_table = _format_status_table(legacy_report)
    assert "haiku_f1" not in legacy_table
    assert "sonnet_f1" not in legacy_table
    assert "opus_items" not in legacy_table

    # Phase 5 path: model columns rendered.
    p5_report = build_corpus_status_report(
        lake_root=lake, manifest_path=manifest, show_all_models=True
    )
    p5_table = _format_status_table(p5_report)
    assert "haiku_f1" in p5_table
    assert "sonnet_f1" in p5_table
    assert "opus_items" in p5_table
    # The actual F1 value appears (39.5% from f1=0.395).
    assert "39.5%" in p5_table
    # Opus item count appears.
    assert "12" in p5_table


def test_human_readable_table_handles_nulls(tmp_path: Path) -> None:
    """Rows with null Haiku/Sonnet/Opus fields render as '-' placeholders."""
    from spectrum_systems_core.data_lake.cli import _format_status_table

    lake = tmp_path / "lake"
    md = lake / "processed" / "meetings" / "a"
    _write_source_record(md, "a")
    # No Opus baseline, no comparison — all three fields will be None.
    manifest = _write_manifest(tmp_path, ["a"])

    report = build_corpus_status_report(
        lake_root=lake, manifest_path=manifest, show_all_models=True
    )
    table = _format_status_table(report)
    assert "haiku_f1" in table
    # Both Sonnet + Opus + Haiku columns render '-' for missing data.
    # Use a loose check: at least one '-' placeholder appears.
    assert "-" in table


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


def test_legacy_three_way_without_sonnet_variant_surfaces_sonnet_f1(
    tmp_path: Path,
) -> None:
    """Legacy three-way artifacts (no sonnet_prompt_variant stamp) must
    still surface their Sonnet F1 in --show-all-models.

    Review-comment P2 (Codex): the default for a missing
    sonnet_prompt_variant was `production_haiku`, which meant
    _newest_sonnet_f1 skipped every legacy three-way artifact and
    reported `null` on backward-compat lakes — silently misleading
    operators about historical Sonnet measurements.
    """
    lake = tmp_path / "lake"
    md = lake / "processed" / "meetings" / "a"
    _write_source_record(md, "a")
    _write_opus_baseline(md, item_count=10)

    # Construct a LEGACY three-way artifact: no prompt_variant fields
    # on the envelope (the pre-Phase-5 shape).
    three_way_dir = md / "comparisons"
    three_way_dir.mkdir(parents=True, exist_ok=True)
    legacy_block = lambda f1: {
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
        "gt_recall_haiku": 0,
        "gt_recall_opus": 0,
    }
    legacy = {
        "artifact_type": "comparison_result",
        "schema_version": "1.0.0",
        "comparison_mode": "three_way",
        "source_id": "a",
        "haiku_run_id": "h",
        "sonnet_run_id": "s",
        "opus_model_id": "claude-opus-4-7",
        "compared_at": "2025-12-01T00:00:00+00:00",
        "haiku_summary": legacy_block(0.40),
        "sonnet_summary": legacy_block(0.55),
        "by_type": {},
        "gt_pairs_present": False,
        # No haiku_prompt_variant, no sonnet_prompt_variant.
    }
    (three_way_dir / "three_way_legacy.json").write_text(
        json.dumps(legacy), encoding="utf-8"
    )

    manifest = _write_manifest(tmp_path, ["a"])
    report = build_corpus_status_report(
        lake_root=lake, manifest_path=manifest, show_all_models=True
    )
    row = next(r for r in report["rows"] if r["source_id"] == "a")
    # Sonnet F1 IS reported even though the variant stamp is absent.
    assert row["sonnet_latest_f1"] == pytest.approx(0.55)


def test_mtime_tie_breaks_by_compared_at(tmp_path: Path) -> None:
    """When two artifacts share an mtime (e.g. fresh git clone), the
    artifact's own `compared_at` breaks the tie deterministically.

    Review-comment P2 (Codex): a pure-mtime sort is non-deterministic
    after `git clone` because every file inherits the checkout time.
    """
    import os
    import time

    lake = tmp_path / "lake"
    md = lake / "processed" / "meetings" / "a"
    _write_source_record(md, "a")
    _write_opus_baseline(md, item_count=10)

    # Two comparisons in the comparisons/ subdir with different
    # compared_at; we'll forcibly tie their mtimes below.
    _write_two_way_cmp(
        md, f1=0.40, variant="production_haiku",
        slug="earlier_ca", in_comparisons_subdir=True,
    )
    _write_two_way_cmp(
        md, f1=0.55, variant="production_haiku",
        slug="later_ca", in_comparisons_subdir=True,
    )
    # Patch each artifact's compared_at so the later wins by payload
    # timestamp.
    for slug, ca, f1 in (
        ("earlier_ca", "2025-12-01T00:00:00+00:00", 0.40),
        ("later_ca", "2026-05-20T00:00:00+00:00", 0.55),
    ):
        p = md / "comparisons" / f"haiku_vs_opus_{slug}.json"
        doc = json.loads(p.read_text(encoding="utf-8"))
        doc["compared_at"] = ca
        doc["summary"]["haiku_f1_vs_opus"] = f1
        p.write_text(json.dumps(doc), encoding="utf-8")
    # Force identical mtimes (post-`git clone` scenario).
    shared_mtime = time.time()
    for slug in ("earlier_ca", "later_ca"):
        p = md / "comparisons" / f"haiku_vs_opus_{slug}.json"
        os.utime(p, (shared_mtime, shared_mtime))

    manifest = _write_manifest(tmp_path, ["a"])
    report = build_corpus_status_report(
        lake_root=lake, manifest_path=manifest, show_all_models=True
    )
    row = next(r for r in report["rows"] if r["source_id"] == "a")
    # The artifact with the later compared_at wins.
    assert row["haiku_latest_f1"] == pytest.approx(0.55)
