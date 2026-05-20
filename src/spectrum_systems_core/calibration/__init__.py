"""Tolerance-budget + calibration-mode plumbing (Phase 2).

The promotion threshold the correction miner uses is no longer a
hardcoded 0.05; it is composed from three values:

  threshold = baseline_f1 + per_source_variance_budget + current_promotion_buffer

This package owns the calibration data layer:

* ``budget.get_variance_budget(source_id)`` returns the per-source
  variance budget when at least 3 non-legacy runs have been observed,
  otherwise the global median.
* ``budget.get_promotion_threshold(source_id, baseline_f1)`` composes
  the threshold a candidate must exceed to be promoted.
* ``budget.is_in_calibration_mode(source_id, data_lake_path)`` returns
  True when fewer than 1 non-legacy comparison artifact exists for
  the source. In calibration mode the miner can WRITE candidates but
  cannot promote them.

The buffer is bounded (0.02 <= current_promotion_buffer <= 0.10) and
the bound is enforced by the JSON Schema at write time, NOT by
budget.py — a malformed file fails closed at validation, not silently
during the miner's promotion decision.
"""
from .budget import (
    BudgetValidationError,
    CalibrationMode,
    DEFAULT_BUDGET_PATH,
    DEFAULT_GLOBAL_MEDIAN_BUDGET,
    DEFAULT_MAX_PROMOTION_BUFFER,
    DEFAULT_MIN_PROMOTION_BUFFER,
    PER_SOURCE_RUN_THRESHOLD,
    get_promotion_threshold,
    get_variance_budget,
    is_in_calibration_mode,
    load_budget,
)

__all__ = [
    "BudgetValidationError",
    "CalibrationMode",
    "DEFAULT_BUDGET_PATH",
    "DEFAULT_GLOBAL_MEDIAN_BUDGET",
    "DEFAULT_MAX_PROMOTION_BUFFER",
    "DEFAULT_MIN_PROMOTION_BUFFER",
    "PER_SOURCE_RUN_THRESHOLD",
    "get_promotion_threshold",
    "get_variance_budget",
    "is_in_calibration_mode",
    "load_budget",
]
