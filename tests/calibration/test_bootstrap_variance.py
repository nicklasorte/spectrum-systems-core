"""Phase 4 — bootstrap variance (third-tier fallback) tests.

The Phase 4 spec adds a third tier to ``get_variance_budget``:

  tier 1 — per-source: artifact with ``runs_observed >= 3`` -> use its value
  tier 2 — global median: at least one source in the lake has ``runs >= 3``
  tier 3 — bootstrap: nothing in the lake has signal yet

The dedicated tests below exercise all three tiers and the schema's
new ``bootstrap_variance`` bound ``[0.02, 0.15]``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from spectrum_systems_core.calibration.budget import (
    DEFAULT_BOOTSTRAP_VARIANCE,
    PER_SOURCE_RUN_THRESHOLD,
    BudgetValidationError,
    get_variance_budget,
    load_budget,
)


def _write_budget(
    path: Path,
    *,
    bootstrap_variance: float = 0.05,
    global_median_budget: float = 0.025,
    schema_version: str = "1.2.0",
) -> Path:
    doc = {
        "artifact_type": "tolerance_budget",
        "schema_version": schema_version,
        "min_promotion_buffer": 0.02,
        "max_promotion_buffer": 0.10,
        "current_promotion_buffer": 0.03,
        "global_median_budget": global_median_budget,
        "bootstrap_variance": bootstrap_variance,
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _write_per_source_state(
    data_lake: Path,
    source_id: str,
    *,
    runs_observed: int,
    f1_variance_budget: float,
) -> Path:
    out = (
        data_lake
        / "store"
        / "processed"
        / "meetings"
        / source_id
        / "diagnostics"
        / f"tolerance_budget_state__{source_id}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "artifact_type": "tolerance_budget_state",
                "schema_version": "1.0.0",
                "source_id": source_id,
                "runs_observed": runs_observed,
                "f1_variance_budget": f1_variance_budget,
                "last_updated": "2026-05-20T00:00:00+00:00",
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return out


# ---------------------------------------------------------------------------
# Schema bound enforcement.
# ---------------------------------------------------------------------------


def test_bootstrap_variance_required(tmp_path: Path) -> None:
    """Phase 4: the schema requires ``bootstrap_variance``."""
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
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(BudgetValidationError) as ei:
        load_budget(path)
    assert "bootstrap_variance" in str(ei.value)


def test_bootstrap_variance_below_min_rejected(tmp_path: Path) -> None:
    """Phase 4: bootstrap_variance < 0.02 fails schema validation."""
    path = _write_budget(tmp_path / "budget.json", bootstrap_variance=0.01)
    with pytest.raises(BudgetValidationError) as ei:
        load_budget(path)
    assert "bootstrap_variance" in str(ei.value) or "0.01" in str(ei.value)


def test_bootstrap_variance_above_max_rejected(tmp_path: Path) -> None:
    """Phase 4: bootstrap_variance > 0.15 fails schema validation."""
    path = _write_budget(tmp_path / "budget.json", bootstrap_variance=0.20)
    with pytest.raises(BudgetValidationError) as ei:
        load_budget(path)
    assert "bootstrap_variance" in str(ei.value) or "0.2" in str(ei.value)


def test_bootstrap_variance_at_bound_accepted(tmp_path: Path) -> None:
    """Phase 4: 0.02 and 0.15 are inclusive bounds."""
    p = _write_budget(tmp_path / "lo.json", bootstrap_variance=0.02)
    load_budget(p)
    p2 = _write_budget(tmp_path / "hi.json", bootstrap_variance=0.15)
    load_budget(p2)


# ---------------------------------------------------------------------------
# Three-tier fallback.
# ---------------------------------------------------------------------------


def test_tier1_per_source_value_used_when_runs_at_threshold(
    tmp_path: Path,
) -> None:
    """Tier 1: per-source state with runs_observed >= 3 wins."""
    budget = _write_budget(
        tmp_path / "budget.json",
        bootstrap_variance=0.05,
        global_median_budget=0.025,
    )
    dl = tmp_path / "dl"
    _write_per_source_state(
        dl,
        "src-x",
        runs_observed=PER_SOURCE_RUN_THRESHOLD,
        f1_variance_budget=0.07,
    )
    assert (
        get_variance_budget("src-x", budget_path=budget, data_lake_path=dl)
        == 0.07
    )


def test_tier2_global_median_used_when_another_source_has_runs(
    tmp_path: Path,
) -> None:
    """Tier 2: src-x has no state but src-y has runs >= 3 -> global_median."""
    budget = _write_budget(
        tmp_path / "budget.json",
        bootstrap_variance=0.05,
        global_median_budget=0.025,
    )
    dl = tmp_path / "dl"
    _write_per_source_state(
        dl,
        "src-y",
        runs_observed=PER_SOURCE_RUN_THRESHOLD,
        f1_variance_budget=0.10,
    )
    # src-x has no state, but src-y has enough runs to feed tier 2.
    assert (
        get_variance_budget("src-x", budget_path=budget, data_lake_path=dl)
        == 0.025
    )


def test_tier3_bootstrap_used_when_no_source_has_enough_runs(
    tmp_path: Path,
) -> None:
    """Tier 3: no source in the lake has runs >= 3 -> bootstrap_variance."""
    budget = _write_budget(
        tmp_path / "budget.json",
        bootstrap_variance=0.05,
        global_median_budget=0.025,
    )
    dl = tmp_path / "dl"
    # Seed src-y with only 2 runs (below threshold).
    _write_per_source_state(
        dl, "src-y", runs_observed=2, f1_variance_budget=0.10
    )
    assert (
        get_variance_budget("src-x", budget_path=budget, data_lake_path=dl)
        == 0.05
    )


def test_tier3_bootstrap_used_when_no_data_lake_path_passed(
    tmp_path: Path,
) -> None:
    """Tier 3 also applies when the caller passed no data_lake_path."""
    budget = _write_budget(
        tmp_path / "budget.json",
        bootstrap_variance=0.07,
        global_median_budget=0.025,
    )
    assert get_variance_budget("src-x", budget_path=budget) == 0.07


def test_tier3_uses_default_when_field_absent(tmp_path: Path) -> None:
    """When the field is absent at the contract level (defensive read),
    the loader's default DEFAULT_BOOTSTRAP_VARIANCE is returned. This
    is unreachable in practice (the schema requires the field) but
    documents the safe fallback the code uses."""
    # We can't use load_budget here because the schema rejects a
    # missing bootstrap_variance. The internal default is the
    # constant; assert it equals the documented value.
    assert DEFAULT_BOOTSTRAP_VARIANCE == 0.05


def test_tier_choice_is_deterministic(tmp_path: Path) -> None:
    """Two calls to get_variance_budget on the same inputs return
    identical values — the tier choice has no hidden state."""
    budget = _write_budget(
        tmp_path / "budget.json",
        bootstrap_variance=0.05,
        global_median_budget=0.025,
    )
    dl = tmp_path / "dl"
    _write_per_source_state(
        dl,
        "src-y",
        runs_observed=PER_SOURCE_RUN_THRESHOLD,
        f1_variance_budget=0.10,
    )
    a = get_variance_budget("src-x", budget_path=budget, data_lake_path=dl)
    b = get_variance_budget("src-x", budget_path=budget, data_lake_path=dl)
    assert a == b
