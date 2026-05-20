"""Tolerance budget loader + calibration-mode decision.

The contract lives at ``docs/contracts/tolerance_budget.json``; the
schema at ``src/spectrum_systems_core/schemas/tolerance_budget.schema.json``.

Three knobs compose the promotion threshold a candidate must clear:

  threshold = baseline_f1 + variance_budget + current_promotion_buffer

Where ``variance_budget`` is:

* the per-source ``f1_variance_budget`` from the data-lake state
  artifact when at least :data:`PER_SOURCE_RUN_THRESHOLD` non-legacy
  comparison runs are on record (``runs_observed >= 3``); or
* the file's ``global_median_budget`` otherwise.

The buffer is bounded (``min_promotion_buffer`` <=
``current_promotion_buffer`` <= ``max_promotion_buffer``). The bound
is enforced by the JSON Schema at write time so a malformed
``tolerance_budget.json`` cannot silently slip through the loader.

Phase 3 split: the per-source state lives in a separate, per-meeting
diagnostic artifact under the data lake, NOT in the contracts file.
The reader (:func:`get_variance_budget`, :func:`get_promotion_threshold`)
loads the state via :func:`_read_per_source_state`; the writer
(:func:`update_per_source_state`) is invoked from
``pipeline.governed_pipeline_run`` after a non-legacy comparison is
built.
"""
from __future__ import annotations

import datetime
import json
import statistics
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
# Phase 4: third-tier fallback used when no source in the lake yet has
# accumulated >= 3 runs (i.e. global_median has no signal either). The
# schema bound on ``bootstrap_variance`` is [0.02, 0.15]; this default
# is what the contracts file ships with. The reader pulls the value
# from the file at run time so changing the bootstrap value is a
# contract edit, not a code edit.
DEFAULT_BOOTSTRAP_VARIANCE: float = 0.05

# Per-source variance budget kicks in only when there is enough signal.
PER_SOURCE_RUN_THRESHOLD: int = 3

# Per-source state artifact constants. The path and schema are part of
# the data-lake contract; the writer (`update_per_source_state`) and
# the reader (`_read_per_source_state`) reference these so a path drift
# is a single edit, not a silent disagreement.
PER_SOURCE_STATE_ARTIFACT_TYPE: str = "tolerance_budget_state"
PER_SOURCE_STATE_SCHEMA_VERSION: str = "1.0.0"

# Sliding window used by `update_per_source_state` to recompute the
# variance budget. The window is intentionally small so the budget
# tracks recent runs; older comparisons fall out of the window after
# enough new runs land.
_VARIANCE_WINDOW_SIZE: int = 10


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


def _load_per_source_state_schema() -> Dict[str, Any]:
    from ..schemas import schema_path

    path = schema_path(PER_SOURCE_STATE_ARTIFACT_TYPE)
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


def _per_source_state_path(data_lake_path: Path | str, source_id: str) -> Path:
    """Canonical on-disk path for the per-source state artifact."""
    return (
        Path(data_lake_path)
        / "store"
        / "processed"
        / "meetings"
        / source_id
        / "diagnostics"
        / f"tolerance_budget_state__{source_id}.json"
    )


def _read_per_source_state(
    source_id: str,
    data_lake_path: Path | str | None,
) -> Optional[Dict[str, Any]]:
    """Read + schema-validate the per-source state artifact.

    Returns ``None`` when the artifact does not exist, when the data
    lake path is not provided, or when the file fails schema validation
    (the fallback is to ``global_median_budget``; the budget module
    deliberately MUST NOT raise on a malformed state file because the
    state artifact is a diagnostic, not a gate).
    """
    if data_lake_path is None:
        return None
    path = _per_source_state_path(data_lake_path, source_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        import jsonschema

        validator = jsonschema.Draft202012Validator(
            _load_per_source_state_schema()
        )
        validator.validate(data)
    except Exception:  # noqa: BLE001 — fallback path is the safe default
        return None
    return data if isinstance(data, dict) else None


def _any_source_has_enough_runs(
    data_lake_path: Path | str | None,
) -> bool:
    """True iff at least one source on disk has runs_observed >= 3.

    Used by :func:`get_variance_budget` to decide whether the global
    median (tier 2) carries enough signal to be trusted, or whether
    the function should fall through to the bootstrap variance
    (tier 3). When the data lake is unavailable we return False so
    the caller falls through to bootstrap — the safer default for a
    cold-start corpus.
    """
    if data_lake_path is None:
        return False
    root = Path(data_lake_path) / "store" / "processed" / "meetings"
    if not root.is_dir():
        return False
    for source_dir in root.iterdir():
        if not source_dir.is_dir():
            continue
        diag = source_dir / "diagnostics"
        if not diag.is_dir():
            continue
        for state_path in diag.glob("tolerance_budget_state__*.json"):
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if int(state.get("runs_observed", 0)) >= PER_SOURCE_RUN_THRESHOLD:
                return True
    return False


def get_variance_budget(
    source_id: str,
    *,
    budget_path: Path | str | None = None,
    data_lake_path: Path | str | None = None,
) -> float:
    """Per-source budget if runs_observed >= 3, else global_median_budget,
    else bootstrap_variance (the Phase 4 third tier).

    Tier 1 — per-source: ``processed/meetings/<source_id>/diagnostics/
    tolerance_budget_state__<source_id>.json`` exists and reports
    ``runs_observed >= 3``. Return its ``f1_variance_budget``.

    Tier 2 — global median: at least one source in the lake has
    ``runs_observed >= 3`` (i.e. there is signal SOMEWHERE in the
    corpus). Return the contracts file's ``global_median_budget``.

    Tier 3 — bootstrap (Phase 4 addition): no source has yet reached
    the per-source threshold. Return the contracts file's
    ``bootstrap_variance``. This avoids the previous silent behaviour
    where a cold-start corpus would use ``global_median_budget``
    even though no source had contributed to that median.

    Callers in production pass ``data_lake_path``; legacy callers
    that do not (some calibration test fixtures) get tier 3
    (bootstrap) automatically because tier 1 and tier 2 both depend
    on lake artifacts.
    """
    budget = load_budget(budget_path)
    state = _read_per_source_state(source_id, data_lake_path)
    if (
        state is not None
        and int(state.get("runs_observed", 0)) >= PER_SOURCE_RUN_THRESHOLD
        and "f1_variance_budget" in state
    ):
        return float(state["f1_variance_budget"])
    if _any_source_has_enough_runs(data_lake_path):
        return float(
            budget.get("global_median_budget", DEFAULT_GLOBAL_MEDIAN_BUDGET)
        )
    return float(
        budget.get("bootstrap_variance", DEFAULT_BOOTSTRAP_VARIANCE)
    )


def get_promotion_threshold(
    source_id: str,
    baseline_f1: float,
    *,
    budget_path: Path | str | None = None,
    data_lake_path: Path | str | None = None,
) -> float:
    """Returns ``baseline_f1 + variance_budget + current_promotion_buffer``.

    The miner's ``should_promote`` checks ``candidate_f1 >= threshold``.
    Note this is GREATER-THAN-OR-EQUAL; the existing miner's strict-
    greater behaviour was the source of the 0.05-exact ambiguity. The
    bounded buffer plus the budget make the decision unambiguous:
    crossing the threshold is a clear, schema-bounded amount.
    """
    budget = load_budget(budget_path)
    state = _read_per_source_state(source_id, data_lake_path)
    if (
        state is not None
        and int(state.get("runs_observed", 0)) >= PER_SOURCE_RUN_THRESHOLD
        and "f1_variance_budget" in state
    ):
        variance = float(state["f1_variance_budget"])
    elif _any_source_has_enough_runs(data_lake_path):
        variance = float(
            budget.get("global_median_budget", DEFAULT_GLOBAL_MEDIAN_BUDGET)
        )
    else:
        variance = float(
            budget.get("bootstrap_variance", DEFAULT_BOOTSTRAP_VARIANCE)
        )
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


def _collect_recent_f1s(
    meeting_dir: Path,
    *,
    limit: int,
) -> list[float]:
    """Collect F1 values from the most recent N non-legacy, non-tainted
    comparison artifacts for one source.

    Used by :func:`update_per_source_state` to recompute the variance
    budget. Sort order is filename-ascending — the canonical layout
    timestamps comparison artifacts via the suffix so the lex order
    aligns with time order. Returns at most ``limit`` values; an
    unreadable file is skipped silently.
    """
    out: list[float] = []
    for path in sorted(meeting_dir.glob("comparison_result__*.json"), reverse=True):
        if len(out) >= limit:
            break
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("legacy_eval") is True:
            continue
        if data.get("tainted_glossary_drift") is True:
            continue
        summary = data.get("summary") or {}
        f1 = summary.get("haiku_f1_vs_opus")
        if isinstance(f1, (int, float)):
            out.append(float(f1))
    return out


def update_per_source_state(
    *,
    source_id: str,
    data_lake_path: Path | str,
    comparison_artifact: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    """Post-extraction hook: refresh the per-source state artifact.

    Called by ``pipeline.governed_pipeline_run`` after a non-legacy,
    non-tainted comparison artifact is built. The hook:

    * is idempotent on ``last_comparison_artifact_path`` — passing the
      same comparison artifact twice does NOT double-increment
      ``runs_observed``;
    * recomputes ``f1_variance_budget`` from the most recent
      :data:`_VARIANCE_WINDOW_SIZE` non-legacy, non-tainted F1 values
      on disk for this source;
    * writes the artifact atomically (write to ``.tmp`` then rename)
      so a crash mid-write cannot leave a half-written file the
      reader then has to skip.

    Returns the written state dict, or ``None`` when the hook decided
    not to write (e.g. when the comparison artifact is missing the
    required F1 field — a partial bundle should never advance the
    budget). Never raises: the caller wraps a broad ``except`` so a
    diagnostic write failure cannot take down production extraction.
    """
    summary = comparison_artifact.get("summary") if isinstance(
        comparison_artifact, Mapping
    ) else None
    if not isinstance(summary, Mapping):
        return None
    f1_now = summary.get("haiku_f1_vs_opus")
    if not isinstance(f1_now, (int, float)):
        return None

    # Idempotency token: the comparison artifact's compared_at + source
    # uniquely identifies one run. We use it (not a filesystem path —
    # the caller does not always know where the comparison will be
    # written) so a re-run with the same comparison content is a no-op.
    cmp_token = (
        f"{comparison_artifact.get('source_id', source_id)}|"
        f"{comparison_artifact.get('compared_at', '')}"
    )

    state_path = _per_source_state_path(data_lake_path, source_id)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    existing: Optional[Dict[str, Any]] = None
    if state_path.is_file():
        try:
            existing = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None

    if (
        isinstance(existing, dict)
        and existing.get("last_comparison_artifact_path") == cmp_token
    ):
        # Idempotency: the same comparison artifact has already been
        # ingested. Do nothing — runs_observed must not double-count.
        return existing

    meeting_dir = (
        Path(data_lake_path)
        / "store"
        / "processed"
        / "meetings"
        / source_id
    )
    recent = _collect_recent_f1s(meeting_dir, limit=_VARIANCE_WINDOW_SIZE)
    if len(recent) >= 2:
        # statistics.pstdev is a population-stdev — appropriate here
        # because we are characterising the spread of the observed
        # sample, not estimating a population parameter.
        variance_budget = float(statistics.pstdev(recent))
    else:
        # With <2 observations there is no spread to characterise; use
        # 0.0 so the reader (which requires runs_observed >= 3 before
        # using this value at all) keeps falling back to global_median.
        variance_budget = 0.0

    runs_observed = int(existing.get("runs_observed", 0)) if isinstance(
        existing, dict
    ) else 0
    runs_observed += 1

    state = {
        "artifact_type": PER_SOURCE_STATE_ARTIFACT_TYPE,
        "schema_version": PER_SOURCE_STATE_SCHEMA_VERSION,
        "source_id": source_id,
        "runs_observed": runs_observed,
        "f1_variance_budget": variance_budget,
        "last_updated": datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat(),
        "last_comparison_artifact_path": cmp_token,
    }

    # Atomic write: rename is atomic on POSIX so a partial file never
    # appears at the canonical path even if the process is killed
    # mid-write.
    tmp_path = state_path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(state, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(state_path)
    return state


__all__ = [
    "BudgetValidationError",
    "CalibrationMode",
    "DEFAULT_BOOTSTRAP_VARIANCE",
    "DEFAULT_BUDGET_PATH",
    "DEFAULT_GLOBAL_MEDIAN_BUDGET",
    "DEFAULT_MAX_PROMOTION_BUFFER",
    "DEFAULT_MIN_PROMOTION_BUFFER",
    "PER_SOURCE_RUN_THRESHOLD",
    "PER_SOURCE_STATE_ARTIFACT_TYPE",
    "PER_SOURCE_STATE_SCHEMA_VERSION",
    "get_promotion_threshold",
    "get_variance_budget",
    "is_in_calibration_mode",
    "load_budget",
    "update_per_source_state",
]
