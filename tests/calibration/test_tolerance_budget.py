"""Tolerance budget + calibration mode tests (Phase 2 Step 2.3, 2.5, 2.8).

Each gate added by Phase 2 has a paired rejection test here:

* ``current_promotion_buffer < min_promotion_buffer`` rejection
* ``current_promotion_buffer > max_promotion_buffer`` rejection
* empty ``per_source_budgets`` is accepted
* ``get_variance_budget`` returns the per-source budget when
  runs_observed >= 3; otherwise the global median
* ``get_promotion_threshold`` composes the threshold correctly
* ``is_in_calibration_mode`` returns True when zero non-legacy
  comparisons exist; False after at least one is on disk
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from spectrum_systems_core.calibration.budget import (
    BudgetValidationError,
    DEFAULT_GLOBAL_MEDIAN_BUDGET,
    DEFAULT_MAX_PROMOTION_BUFFER,
    DEFAULT_MIN_PROMOTION_BUFFER,
    PER_SOURCE_RUN_THRESHOLD,
    get_promotion_threshold,
    get_variance_budget,
    is_in_calibration_mode,
    load_budget,
)


def _write_budget(path: Path, **overrides) -> Path:
    """Build a tolerance_budget.json fixture.

    Phase 3 removed ``per_source_budgets`` from this file (the state
    lives in a per-meeting diagnostic artifact under the data lake).
    Callers that pass ``per_source_budgets=`` in overrides are writing
    a Phase-2 shape; tests that need to exercise per-source data must
    instead seed the per-source state artifact via
    :func:`_write_per_source_state` below.
    """
    doc = {
        "artifact_type": "tolerance_budget",
        "schema_version": "1.1.0",
        "min_promotion_buffer": 0.02,
        "max_promotion_buffer": 0.10,
        "current_promotion_buffer": 0.03,
        "global_median_budget": 0.025,
    }
    doc.update(overrides)
    # Phase 3: the schema no longer accepts per_source_budgets here.
    doc.pop("per_source_budgets", None)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _write_per_source_state(
    data_lake_path: Path,
    source_id: str,
    *,
    runs_observed: int,
    f1_variance_budget: float,
    last_updated: str = "2026-05-20T00:00:00+00:00",
) -> Path:
    """Seed a Phase-3 per-source state artifact under the data lake."""
    state = {
        "artifact_type": "tolerance_budget_state",
        "schema_version": "1.0.0",
        "source_id": source_id,
        "runs_observed": runs_observed,
        "f1_variance_budget": f1_variance_budget,
        "last_updated": last_updated,
    }
    out = (
        data_lake_path
        / "store"
        / "processed"
        / "meetings"
        / source_id
        / "diagnostics"
        / f"tolerance_budget_state__{source_id}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return out


def test_default_contract_validates() -> None:
    """The shipped contract validates against its schema."""
    doc = load_budget()
    assert doc["schema_version"] in ("1.0.0", "1.1.0")
    assert doc["min_promotion_buffer"] == 0.02
    assert doc["max_promotion_buffer"] == 0.10
    assert (
        0.02
        <= doc["current_promotion_buffer"]
        <= 0.10
    )
    # Phase 3 split: per_source_budgets must not appear in the contract.
    assert "per_source_budgets" not in doc


def test_current_buffer_below_min_rejected(tmp_path: Path) -> None:
    """Step 2.3 paired rejection: buffer < 0.02 fails schema validation."""
    path = _write_budget(
        tmp_path / "budget.json",
        current_promotion_buffer=0.01,
    )
    with pytest.raises(BudgetValidationError) as ei:
        load_budget(path)
    assert "current_promotion_buffer" in str(ei.value) or "0.01" in str(ei.value)


def test_current_buffer_above_max_rejected(tmp_path: Path) -> None:
    """Step 2.3 paired rejection: buffer > 0.10 fails schema validation."""
    path = _write_budget(
        tmp_path / "budget.json",
        current_promotion_buffer=0.15,
    )
    with pytest.raises(BudgetValidationError) as ei:
        load_budget(path)
    assert "0.15" in str(ei.value) or "current_promotion_buffer" in str(
        ei.value
    )


def test_per_source_budgets_in_contracts_file_rejected(tmp_path: Path) -> None:
    """Phase 3 split: per_source_budgets is no longer accepted in the
    contracts file. A budget that still carries the legacy key fails
    schema validation under additionalProperties:false."""
    path = tmp_path / "budget.json"
    path.write_text(
        json.dumps(
            {
                "artifact_type": "tolerance_budget",
                "schema_version": "1.1.0",
                "min_promotion_buffer": 0.02,
                "max_promotion_buffer": 0.10,
                "current_promotion_buffer": 0.03,
                "global_median_budget": 0.025,
                "per_source_budgets": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(BudgetValidationError) as ei:
        load_budget(path)
    assert "per_source_budgets" in str(ei.value)


def test_get_variance_budget_uses_global_median_when_runs_observed_below_threshold(
    tmp_path: Path,
) -> None:
    """Phase 3: per-source budget reads from the data-lake state artifact
    and kicks in only at runs_observed >= 3."""
    path = _write_budget(
        tmp_path / "budget.json",
        global_median_budget=0.04,
    )
    dl = tmp_path / "dl"
    _write_per_source_state(
        dl, "src-x", runs_observed=2, f1_variance_budget=0.07
    )
    assert get_variance_budget(
        "src-x", budget_path=path, data_lake_path=dl
    ) == 0.04


def test_get_variance_budget_uses_per_source_at_threshold(
    tmp_path: Path,
) -> None:
    """Phase 3: at runs_observed == 3 the per-source budget is used."""
    path = _write_budget(
        tmp_path / "budget.json",
        global_median_budget=0.04,
    )
    dl = tmp_path / "dl"
    _write_per_source_state(
        dl,
        "src-x",
        runs_observed=PER_SOURCE_RUN_THRESHOLD,
        f1_variance_budget=0.07,
    )
    assert get_variance_budget(
        "src-x", budget_path=path, data_lake_path=dl
    ) == 0.07


def test_get_variance_budget_falls_back_when_state_artifact_missing(
    tmp_path: Path,
) -> None:
    """Phase 3 Pass 1: a missing state artifact returns global_median_budget,
    never raises. Tests the fallback path the rollback contract relies on."""
    path = _write_budget(
        tmp_path / "budget.json",
        global_median_budget=0.04,
    )
    dl = tmp_path / "dl"
    (dl / "store" / "processed" / "meetings" / "src-x").mkdir(parents=True)
    # No state artifact written. Reader must fall back to the global median.
    assert get_variance_budget(
        "src-x", budget_path=path, data_lake_path=dl
    ) == 0.04


def test_get_variance_budget_falls_back_on_corrupt_state_artifact(
    tmp_path: Path,
) -> None:
    """Phase 3 Pass 1: a corrupt state artifact is treated as missing.
    The state file is a diagnostic, not a gate; the budget loader must
    fail open on it (load_budget itself still fails closed on the
    contracts file)."""
    path = _write_budget(
        tmp_path / "budget.json",
        global_median_budget=0.04,
    )
    dl = tmp_path / "dl"
    state_path = (
        dl
        / "store"
        / "processed"
        / "meetings"
        / "src-x"
        / "diagnostics"
        / "tolerance_budget_state__src-x.json"
    )
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{not json", encoding="utf-8")
    assert get_variance_budget(
        "src-x", budget_path=path, data_lake_path=dl
    ) == 0.04


def test_get_promotion_threshold_computes_baseline_plus_budgets(
    tmp_path: Path,
) -> None:
    """Step 2.5 + Pass-2 mutation test: threshold = baseline + variance + buffer."""
    path = _write_budget(
        tmp_path / "budget.json",
        current_promotion_buffer=0.05,
        global_median_budget=0.04,
    )
    dl = tmp_path / "dl"
    _write_per_source_state(
        dl,
        "src-x",
        runs_observed=PER_SOURCE_RUN_THRESHOLD,
        f1_variance_budget=0.04,
    )
    threshold = get_promotion_threshold(
        "src-x", 0.38, budget_path=path, data_lake_path=dl
    )
    assert abs(threshold - 0.47) < 1e-9


def test_get_promotion_threshold_below_threshold_does_not_promote(
    tmp_path: Path,
) -> None:
    """Pass 2 mutation: candidate F1 0.46 must NOT meet a 0.47 threshold."""
    path = _write_budget(
        tmp_path / "budget.json",
        current_promotion_buffer=0.05,
        global_median_budget=0.04,
    )
    dl = tmp_path / "dl"
    _write_per_source_state(
        dl,
        "src-x",
        runs_observed=PER_SOURCE_RUN_THRESHOLD,
        f1_variance_budget=0.04,
    )
    threshold = get_promotion_threshold(
        "src-x", 0.38, budget_path=path, data_lake_path=dl
    )
    candidate_f1 = 0.46
    assert candidate_f1 < threshold, "0.46 must not clear 0.47 threshold"


def test_get_promotion_threshold_above_threshold_promotes(
    tmp_path: Path,
) -> None:
    """Pass 2 mutation: candidate F1 0.48 must meet a 0.47 threshold."""
    path = _write_budget(
        tmp_path / "budget.json",
        current_promotion_buffer=0.05,
        global_median_budget=0.04,
    )
    dl = tmp_path / "dl"
    _write_per_source_state(
        dl,
        "src-x",
        runs_observed=PER_SOURCE_RUN_THRESHOLD,
        f1_variance_budget=0.04,
    )
    threshold = get_promotion_threshold(
        "src-x", 0.38, budget_path=path, data_lake_path=dl
    )
    candidate_f1 = 0.48
    assert candidate_f1 >= threshold, "0.48 must clear 0.47 threshold"


def test_calibration_active_with_zero_comparisons(tmp_path: Path) -> None:
    """Step 2.8: calibration is active when no non-legacy comparison exists."""
    dl = tmp_path / "dl"
    (dl / "store" / "processed" / "meetings" / "src-x").mkdir(parents=True)
    mode = is_in_calibration_mode("src-x", dl)
    assert mode.active is True
    assert mode.runs_observed == 0


def test_calibration_inactive_after_one_non_legacy_comparison(
    tmp_path: Path,
) -> None:
    """Step 2.8: calibration ends after 1 non-legacy comparison lands."""
    dl = tmp_path / "dl"
    sid_dir = dl / "store" / "processed" / "meetings" / "src-x"
    sid_dir.mkdir(parents=True)
    (sid_dir / "comparison_result__abc.json").write_text(
        json.dumps(
            {
                "artifact_type": "comparison_result",
                "schema_version": "1.0.0",
                "source_id": "src-x",
                "legacy_eval": False,
                "summary": {"haiku_f1_vs_opus": 0.40},
            }
        ),
        encoding="utf-8",
    )
    mode = is_in_calibration_mode("src-x", dl)
    assert mode.active is False
    assert mode.runs_observed == 1


def test_calibration_still_active_when_only_legacy_comparisons_exist(
    tmp_path: Path,
) -> None:
    """Step 2.8: a legacy-only comparison set does NOT clear calibration."""
    dl = tmp_path / "dl"
    sid_dir = dl / "store" / "processed" / "meetings" / "src-x"
    sid_dir.mkdir(parents=True)
    (sid_dir / "comparison_result__legacy.json").write_text(
        json.dumps(
            {
                "artifact_type": "comparison_result",
                "schema_version": "1.0.0",
                "source_id": "src-x",
                "legacy_eval": True,
                "summary": {"haiku_f1_vs_opus": 0.40},
            }
        ),
        encoding="utf-8",
    )
    mode = is_in_calibration_mode("src-x", dl)
    assert mode.active is True
    assert mode.runs_observed == 0


def test_corrupted_budget_fails_closed_in_calibration_check(
    tmp_path: Path,
) -> None:
    """Defence-in-depth: a corrupt budget fails closed BEFORE the
    miner can make a promotion decision in calibration mode."""
    bad = tmp_path / "bad_budget.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(BudgetValidationError):
        is_in_calibration_mode("src-x", tmp_path, budget_path=bad)
