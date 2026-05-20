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
    doc = {
        "artifact_type": "tolerance_budget",
        "schema_version": "1.0.0",
        "min_promotion_buffer": 0.02,
        "max_promotion_buffer": 0.10,
        "current_promotion_buffer": 0.03,
        "global_median_budget": 0.025,
        "per_source_budgets": {},
    }
    doc.update(overrides)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_default_contract_validates() -> None:
    """The shipped contract validates against its schema."""
    doc = load_budget()
    assert doc["schema_version"] == "1.0.0"
    assert doc["min_promotion_buffer"] == 0.02
    assert doc["max_promotion_buffer"] == 0.10
    assert (
        0.02
        <= doc["current_promotion_buffer"]
        <= 0.10
    )


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


def test_empty_per_source_budgets_accepted(tmp_path: Path) -> None:
    """A budget with empty per_source_budgets is valid on first boot."""
    path = _write_budget(tmp_path / "budget.json", per_source_budgets={})
    doc = load_budget(path)
    assert doc["per_source_budgets"] == {}


def test_get_variance_budget_uses_global_median_when_runs_observed_below_threshold(
    tmp_path: Path,
) -> None:
    """Step 2.3: per-source budget kicks in only at runs_observed >= 3."""
    path = _write_budget(
        tmp_path / "budget.json",
        global_median_budget=0.04,
        per_source_budgets={
            "src-x": {
                "f1_variance_budget": 0.07,
                "runs_observed": 2,
                "last_updated": "2026-05-20T00:00:00+00:00",
            }
        },
    )
    assert get_variance_budget("src-x", budget_path=path) == 0.04


def test_get_variance_budget_uses_per_source_at_threshold(
    tmp_path: Path,
) -> None:
    """Step 2.3: at runs_observed == 3 the per-source budget is used."""
    path = _write_budget(
        tmp_path / "budget.json",
        global_median_budget=0.04,
        per_source_budgets={
            "src-x": {
                "f1_variance_budget": 0.07,
                "runs_observed": PER_SOURCE_RUN_THRESHOLD,
                "last_updated": "2026-05-20T00:00:00+00:00",
            }
        },
    )
    assert get_variance_budget("src-x", budget_path=path) == 0.07


def test_get_promotion_threshold_computes_baseline_plus_budgets(
    tmp_path: Path,
) -> None:
    """Step 2.5 + Pass-2 mutation test: threshold = baseline + variance + buffer."""
    path = _write_budget(
        tmp_path / "budget.json",
        current_promotion_buffer=0.05,
        global_median_budget=0.04,
        per_source_budgets={
            "src-x": {
                "f1_variance_budget": 0.04,
                "runs_observed": PER_SOURCE_RUN_THRESHOLD,
                "last_updated": "2026-05-20T00:00:00+00:00",
            }
        },
    )
    threshold = get_promotion_threshold("src-x", 0.38, budget_path=path)
    assert abs(threshold - 0.47) < 1e-9


def test_get_promotion_threshold_below_threshold_does_not_promote(
    tmp_path: Path,
) -> None:
    """Pass 2 mutation: candidate F1 0.46 must NOT meet a 0.47 threshold."""
    path = _write_budget(
        tmp_path / "budget.json",
        current_promotion_buffer=0.05,
        global_median_budget=0.04,
        per_source_budgets={
            "src-x": {
                "f1_variance_budget": 0.04,
                "runs_observed": PER_SOURCE_RUN_THRESHOLD,
                "last_updated": "2026-05-20T00:00:00+00:00",
            }
        },
    )
    threshold = get_promotion_threshold("src-x", 0.38, budget_path=path)
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
        per_source_budgets={
            "src-x": {
                "f1_variance_budget": 0.04,
                "runs_observed": PER_SOURCE_RUN_THRESHOLD,
                "last_updated": "2026-05-20T00:00:00+00:00",
            }
        },
    )
    threshold = get_promotion_threshold("src-x", 0.38, budget_path=path)
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
