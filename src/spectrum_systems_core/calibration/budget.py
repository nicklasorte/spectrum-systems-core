"""Tolerance budget loader + calibration-mode decision.

The contract lives at ``docs/contracts/tolerance_budget.json``; the
schema at ``src/spectrum_systems_core/schemas/tolerance_budget.schema.json``.

Three knobs compose the promotion threshold a candidate must clear:

  threshold = baseline_f1 + variance_budget + current_promotion_buffer

Where ``variance_budget`` is:

* the per-source ``f1_variance_budget`` when at least
  :data:`PER_SOURCE_RUN_THRESHOLD` non-legacy comparison runs are on
  record (``runs_observed >= 3``); or
* the file's ``global_median_budget`` otherwise.

The buffer is bounded (``min_promotion_buffer`` <=
``current_promotion_buffer`` <= ``max_promotion_buffer``). The bound
is enforced by the JSON Schema at write time so a malformed
``tolerance_budget.json`` cannot silently slip through the loader.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BUDGET_PATH: Path = _REPO_ROOT / "docs" / "contracts" / "tolerance_budget.json"

# Numeric defaults the contract ships with. The schema enforces these
# bounds at write time; the loader does NOT re-check the bound (single
# source of truth = the schema).
DEFAULT_MIN_PROMOTION_BUFFER: float = 0.02
DEFAULT_MAX_PROMOTION_BUFFER: float = 0.10
DEFAULT_GLOBAL_MEDIAN_BUDGET: float = 0.025

# Per-source variance budget kicks in only when there is enough signal.
PER_SOURCE_RUN_THRESHOLD: int = 3


class BudgetValidationError(ValueError):
    """Raised when the on-disk budget file fails schema validation."""


@dataclass(frozen=True)
class CalibrationMode:
    """Result of :func:`is_in_calibration_mode`.

    Carries the boolean plus the count so a caller (and a test) can
    assert the EXACT runs_observed value that drove the decision.
    """

    active: bool
    runs_observed: int
    reason: str


def _load_budget_schema() -> Dict[str, Any]:
    from ..schemas import schema_path

    path = schema_path("tolerance_budget")
    return json.loads(path.read_text(encoding="utf-8"))


def load_budget(path: Path | str | None = None) -> Dict[str, Any]:
    """Read and schema-validate the budget file.

    Returns the parsed dict. Raises :class:`BudgetValidationError`
    (a ValueError subclass) on any schema violation. The function
    deliberately does NOT cache so a test can swap the file between
    calls.
    """
    p = Path(path) if path is not None else DEFAULT_BUDGET_PATH
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise BudgetValidationError(
            f"tolerance_budget.json not found at {p}"
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BudgetValidationError(
            f"tolerance_budget.json is not valid JSON: {exc}"
        ) from exc

    import jsonschema

    schema = _load_budget_schema()
    validator = jsonschema.Draft202012Validator(schema)
    try:
        validator.validate(data)
    except jsonschema.ValidationError as exc:
        raise BudgetValidationError(
            f"tolerance_budget.json failed schema validation: "
            f"{exc.message} at path={list(exc.absolute_path)}"
        ) from exc
    return data


def _per_source_entry(
    budget: Mapping[str, Any], source_id: str
) -> Optional[Dict[str, Any]]:
    per_source = budget.get("per_source_budgets") or {}
    entry = per_source.get(source_id)
    return entry if isinstance(entry, dict) else None


def get_variance_budget(
    source_id: str,
    *,
    budget_path: Path | str | None = None,
) -> float:
    """Per-source budget if runs_observed >= 3 else global_median_budget.

    A per-source entry whose ``runs_observed`` is below the threshold
    falls back to the global median. The bound is enforced at the
    schema layer; the loader fails closed.
    """
    budget = load_budget(budget_path)
    entry = _per_source_entry(budget, source_id)
    if (
        entry is not None
        and int(entry.get("runs_observed", 0)) >= PER_SOURCE_RUN_THRESHOLD
        and "f1_variance_budget" in entry
    ):
        return float(entry["f1_variance_budget"])
    return float(budget.get("global_median_budget", DEFAULT_GLOBAL_MEDIAN_BUDGET))


def get_promotion_threshold(
    source_id: str,
    baseline_f1: float,
    *,
    budget_path: Path | str | None = None,
) -> float:
    """Returns ``baseline_f1 + variance_budget + current_promotion_buffer``.

    The miner's ``should_promote`` checks ``candidate_f1 >= threshold``.
    Note this is GREATER-THAN-OR-EQUAL; the existing miner's strict-
    greater behaviour was the source of the 0.05-exact ambiguity. The
    bounded buffer plus the budget make the decision unambiguous:
    crossing the threshold is a clear, schema-bounded amount.
    """
    budget = load_budget(budget_path)
    entry = _per_source_entry(budget, source_id)
    if (
        entry is not None
        and int(entry.get("runs_observed", 0)) >= PER_SOURCE_RUN_THRESHOLD
        and "f1_variance_budget" in entry
    ):
        variance = float(entry["f1_variance_budget"])
    else:
        variance = float(budget.get("global_median_budget", DEFAULT_GLOBAL_MEDIAN_BUDGET))
    buffer = float(budget.get("current_promotion_buffer", DEFAULT_MIN_PROMOTION_BUFFER))
    return float(baseline_f1) + variance + buffer


def is_in_calibration_mode(
    source_id: str,
    data_lake_path: Path | str,
    *,
    budget_path: Path | str | None = None,
) -> CalibrationMode:
    """True when fewer than 1 non-legacy comparison artifact exists.

    In calibration mode the miner may still generate candidates and
    open PRs but MUST NOT set ``promoted: true``. The PR description
    must include ``calibration: this candidate is not yet promoted —
    pending baseline run``.

    Walks ``processed/meetings/<source_id>/`` for files matching
    ``comparison_result__*.json`` and inspects each for the
    ``legacy_eval`` flag stamped by :func:`governed_pipeline_run`.
    A file whose payload has ``legacy_eval: true`` is excluded from
    the run count.
    """
    # The loader is called for its side effect: a malformed budget
    # file fails closed BEFORE we make a calibration-mode decision so
    # a corrupted file cannot trick the miner into promoting in
    # calibration.
    load_budget(budget_path)

    meeting_dir = (
        Path(data_lake_path)
        / "store"
        / "processed"
        / "meetings"
        / source_id
    )
    non_legacy = 0
    if meeting_dir.is_dir():
        for path in sorted(meeting_dir.glob("comparison_result__*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            # comparison_result emits `legacy_eval` at the top level
            # (Phase 2 schema addition). We also fall back to a nested
            # `payload.legacy_eval` for forwards-compat with a future
            # comparison_result envelope shape.
            legacy_flag = data.get("legacy_eval")
            if legacy_flag is None:
                payload = data.get("payload") or {}
                legacy_flag = payload.get("legacy_eval")
            if legacy_flag is True:
                continue
            non_legacy += 1

    if non_legacy < 1:
        return CalibrationMode(
            active=True,
            runs_observed=non_legacy,
            reason=(
                f"calibration_active: {non_legacy} non-legacy comparisons "
                f"observed for {source_id!r}"
            ),
        )
    return CalibrationMode(
        active=False,
        runs_observed=non_legacy,
        reason=(
            f"calibration_complete: {non_legacy} non-legacy comparisons "
            f"observed for {source_id!r}"
        ),
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
