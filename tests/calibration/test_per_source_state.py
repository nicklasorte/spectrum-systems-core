"""Phase 3 Step 3.5 — per-source variance budget state tests.

Phase 3 split the previously-inline per-source budget out of
``docs/contracts/tolerance_budget.json`` into a per-meeting diagnostic
artifact at
``processed/meetings/<source_id>/diagnostics/tolerance_budget_state__<source_id>.json``.

These tests cover the writer (:func:`update_per_source_state` — the
post-extraction hook) and the reader (the
:func:`get_variance_budget` integration that pulls from the data
lake).

Pass 2 item 2 (per-source state recomputation) and Pass 2 item 5
(idempotency) are the headline coverage; Pass 1 item 11 (per-source
state separation) is verified by the inverse test
``test_contracts_file_has_no_per_source_data``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


from spectrum_systems_core.calibration.budget import (
    PER_SOURCE_STATE_ARTIFACT_TYPE,
    PER_SOURCE_STATE_SCHEMA_VERSION,
    get_variance_budget,
    load_budget,
    update_per_source_state,
)


def _budget_file(tmp_path: Path) -> Path:
    """Write a minimal Phase-3 contracts file the loader accepts."""
    path = tmp_path / "budget.json"
    path.write_text(
        json.dumps(
            {
                "artifact_type": "tolerance_budget",
                "schema_version": "1.2.0",
                "min_promotion_buffer": 0.02,
                "max_promotion_buffer": 0.10,
                "current_promotion_buffer": 0.03,
                "global_median_budget": 0.025,
                "bootstrap_variance": 0.05,
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_comparison(
    data_lake: Path,
    source_id: str,
    slug: str,
    *,
    f1: float,
    legacy: bool = False,
    tainted: bool = False,
    compared_at: str = "2026-05-20T00:00:00+00:00",
) -> Dict[str, Any]:
    cmp_path = (
        data_lake
        / "store"
        / "processed"
        / "meetings"
        / source_id
        / f"comparison_result__{slug}.json"
    )
    cmp_path.parent.mkdir(parents=True, exist_ok=True)
    cmp_doc = {
        "artifact_type": "comparison_result",
        "schema_version": "1.0.0",
        "source_id": source_id,
        "compared_at": compared_at,
        "summary": {"haiku_f1_vs_opus": float(f1)},
        "legacy_eval": legacy,
    }
    if tainted:
        cmp_doc["tainted_glossary_drift"] = True
    cmp_path.write_text(json.dumps(cmp_doc), encoding="utf-8")
    return cmp_doc


def _state_path(data_lake: Path, source_id: str) -> Path:
    return (
        data_lake
        / "store"
        / "processed"
        / "meetings"
        / source_id
        / "diagnostics"
        / f"tolerance_budget_state__{source_id}.json"
    )


def test_contracts_file_has_no_per_source_data() -> None:
    """Pass 1 item 11: the shipped contracts file carries only bounds."""
    doc = load_budget()
    assert "per_source_budgets" not in doc
    # All expected immutable fields are present.
    for k in (
        "min_promotion_buffer",
        "max_promotion_buffer",
        "current_promotion_buffer",
        "global_median_budget",
    ):
        assert k in doc


def test_update_per_source_state_creates_artifact(tmp_path: Path) -> None:
    """First call seeds the artifact with runs_observed=1."""
    dl = tmp_path / "dl"
    source_id = "src-a"
    cmp_doc = _write_comparison(dl, source_id, "0001", f1=0.40)

    state = update_per_source_state(
        source_id=source_id,
        data_lake_path=dl,
        comparison_artifact=cmp_doc,
    )
    assert state is not None
    assert state["artifact_type"] == PER_SOURCE_STATE_ARTIFACT_TYPE
    assert state["schema_version"] == PER_SOURCE_STATE_SCHEMA_VERSION
    assert state["source_id"] == source_id
    assert state["runs_observed"] == 1
    # On a single observation the writer uses 0.0 as a placeholder —
    # the reader still requires runs_observed >= 3 before using the
    # value at all.
    assert state["f1_variance_budget"] == 0.0
    assert _state_path(dl, source_id).is_file()


def test_update_per_source_state_recomputes_after_three_runs(
    tmp_path: Path,
) -> None:
    """Pass 2 item 2: after three non-legacy comparisons the per-source
    state recomputes the variance from the actual F1 values."""
    dl = tmp_path / "dl"
    source_id = "src-b"
    # Three diverse F1 observations on disk.
    f1s = [0.38, 0.42, 0.40]
    docs = [
        _write_comparison(
            dl,
            source_id,
            f"{i:04d}",
            f1=f1,
            compared_at=f"2026-05-2{i}T00:00:00+00:00",
        )
        for i, f1 in enumerate(f1s, start=1)
    ]
    # Call the hook once per observation, mirroring production.
    state = None
    for d in docs:
        state = update_per_source_state(
            source_id=source_id,
            data_lake_path=dl,
            comparison_artifact=d,
        )
    assert state is not None
    assert state["runs_observed"] == 3
    # Variance is the population stdev of the observed F1s.
    import statistics

    expected = float(statistics.pstdev(f1s))
    assert abs(state["f1_variance_budget"] - expected) < 1e-9


def test_update_per_source_state_is_idempotent(tmp_path: Path) -> None:
    """Pass 2 item 5: re-running the hook with the same comparison
    artifact does NOT double-increment ``runs_observed``."""
    dl = tmp_path / "dl"
    source_id = "src-c"
    cmp_doc = _write_comparison(dl, source_id, "abc", f1=0.39)

    first = update_per_source_state(
        source_id=source_id,
        data_lake_path=dl,
        comparison_artifact=cmp_doc,
    )
    assert first is not None and first["runs_observed"] == 1
    second = update_per_source_state(
        source_id=source_id,
        data_lake_path=dl,
        comparison_artifact=cmp_doc,
    )
    # The second invocation returns the EXISTING state unchanged.
    assert second is not None
    assert second["runs_observed"] == 1


def test_update_per_source_state_excludes_legacy_runs(tmp_path: Path) -> None:
    """The recomputation must skip ``legacy_eval: true`` comparison
    artifacts so legacy data does not contaminate the per-source
    variance budget."""
    dl = tmp_path / "dl"
    source_id = "src-d"
    _write_comparison(dl, source_id, "legacy-1", f1=0.10, legacy=True)
    _write_comparison(dl, source_id, "legacy-2", f1=0.15, legacy=True)
    non_legacy_doc = _write_comparison(
        dl, source_id, "real", f1=0.40, compared_at="2026-05-20T01"
    )

    state = update_per_source_state(
        source_id=source_id,
        data_lake_path=dl,
        comparison_artifact=non_legacy_doc,
    )
    assert state is not None
    # Only one non-legacy run on disk -> runs_observed=1.
    assert state["runs_observed"] == 1


def test_update_per_source_state_excludes_tainted_runs(tmp_path: Path) -> None:
    """Same lifecycle as legacy_eval: a tainted_glossary_drift run does
    not enter the variance recomputation."""
    dl = tmp_path / "dl"
    source_id = "src-e"
    _write_comparison(dl, source_id, "tainted-1", f1=0.10, tainted=True)
    non_tainted_doc = _write_comparison(
        dl, source_id, "real", f1=0.40, compared_at="2026-05-20T02"
    )

    state = update_per_source_state(
        source_id=source_id,
        data_lake_path=dl,
        comparison_artifact=non_tainted_doc,
    )
    assert state is not None
    assert state["runs_observed"] == 1


def test_get_variance_budget_reads_from_data_lake_state(tmp_path: Path) -> None:
    """End-to-end Phase-3 contract: the reader pulls the per-source
    variance budget from the data-lake state artifact, NOT the
    contracts file."""
    budget = _budget_file(tmp_path)
    dl = tmp_path / "dl"
    source_id = "src-f"
    # Seed three observations with known F1 values.
    for i, f1 in enumerate([0.36, 0.40, 0.44], start=1):
        cmp_doc = _write_comparison(
            dl,
            source_id,
            f"{i:04d}",
            f1=f1,
            compared_at=f"2026-05-2{i}T00:00:00+00:00",
        )
        update_per_source_state(
            source_id=source_id,
            data_lake_path=dl,
            comparison_artifact=cmp_doc,
        )

    # Reader picks up the per-source budget once runs_observed >= 3.
    variance = get_variance_budget(
        source_id, budget_path=budget, data_lake_path=dl
    )
    # Same arithmetic the writer used.
    import statistics

    expected = float(statistics.pstdev([0.36, 0.40, 0.44]))
    assert abs(variance - expected) < 1e-9


def test_get_variance_budget_falls_back_when_only_two_runs(tmp_path: Path) -> None:
    """Below the PER_SOURCE_RUN_THRESHOLD the reader returns
    ``global_median_budget`` instead of the (tiny / unreliable)
    per-source value. Phase 4 tier 2: requires at least one OTHER
    source in the lake to have runs >= 3 — otherwise the reader
    falls through to `bootstrap_variance` (tier 3)."""
    budget = _budget_file(tmp_path)
    dl = tmp_path / "dl"
    source_id = "src-g"
    for i, f1 in enumerate([0.36, 0.40], start=1):
        cmp_doc = _write_comparison(
            dl, source_id, f"{i:04d}", f1=f1, compared_at=f"2026-05-2{i}"
        )
        update_per_source_state(
            source_id=source_id,
            data_lake_path=dl,
            comparison_artifact=cmp_doc,
        )

    # Phase 4: seed a second source whose run count clears tier 2.
    for i, f1 in enumerate([0.32, 0.36, 0.40], start=1):
        cmp_doc2 = _write_comparison(
            dl,
            "src-other",
            f"{i:04d}",
            f1=f1,
            compared_at=f"2026-04-1{i}",
        )
        update_per_source_state(
            source_id="src-other",
            data_lake_path=dl,
            comparison_artifact=cmp_doc2,
        )

    variance = get_variance_budget(
        source_id, budget_path=budget, data_lake_path=dl
    )
    assert variance == 0.025  # global_median_budget from the fixture
