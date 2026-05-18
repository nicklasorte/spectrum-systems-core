"""Phase Z.3 — F1 regression gate (logic for .github/workflows/f1-regression.yml).

Protects a merged prompt change from silently regressing Dec 18 F1.
On ``push`` to ``main`` the workflow runs this script; ALL the logic
lives here (the YAML only invokes it).

Sequence:
  1. Load the most recently promoted ``extraction_alignment_comparison``
     for Dec 18 from the data-lake (the BASELINE).
  2. None on disk -> print ``baseline_not_yet_established`` and exit 0
     (the first prompt change cannot regress a baseline that does not
     exist yet).
  3. Re-execute the comparator against the SAME frozen ceiling and the
     most recently promoted Haiku extraction (a re-run of the
     measurement, never a re-run of Haiku) to get the NEW comparison.
  4. delta_total_f1 = new_total - baseline_total; per-type deltas too.
  5. delta_total_f1 <= -0.02 OR any per-type delta <= -0.05 -> exit 1
     with a structured message naming every failing condition.
  6. Log the active prompt_addition_id so the operator knows which
     prompt version is under test.

Rollback: ``F1_REGRESSION_GATE_ENABLED`` (default ``true``). When set
to a falsey token the gate exits 0 unconditionally but STILL logs the
verdict it would have reached, so a disabled gate is auditable, never
silent.

Test seams (deterministic, file-backed — the gate logic itself is
never mocked; it runs on REAL comparison artifacts produced by the
REAL ``compare_extractions``): ``Z3_BASELINE_COMPARISON_JSON`` and
``Z3_NEW_COMPARISON_JSON`` point at comparison envelope files used
verbatim as baseline / new, bypassing only the data-lake lookup and
the comparator re-run.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _artifact_validator import (  # noqa: E402
    ArtifactValidationError,
    validate_artifact,
)
from _phase_z_lake import (  # noqa: E402
    data_lake_store_root,
    latest_instrument,
)

TRANSCRIPT_ID = "m-2025-12-18-7ghz-downlink-tig-kickoff"
TOTAL_F1_REGRESSION_THRESHOLD = -0.02
PER_TYPE_F1_REGRESSION_THRESHOLD = -0.05
GATE_ENV = "F1_REGRESSION_GATE_ENABLED"

_ACTIVE_PROMPT_ADDITION = (
    _SCRIPTS_DIR.parent / "config" / "prompt_additions" / "active.json"
)


def _gate_enabled() -> bool:
    raw = (os.environ.get(GATE_ENV) or "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _active_prompt_addition_id() -> str:
    try:
        data = json.loads(
            _ACTIVE_PROMPT_ADDITION.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return "none"
    if isinstance(data, dict):
        return str(data.get("prompt_addition_id") or "none")
    return "none"


def _total_f1(payload: dict[str, Any]) -> float:
    tm = payload.get("total_metrics")
    if isinstance(tm, dict) and isinstance(tm.get("f1"), (int, float)):
        return float(tm["f1"])
    raise ValueError("comparison payload missing total_metrics.f1")


def _per_type_f1(payload: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    ptm = payload.get("per_type_metrics")
    if isinstance(ptm, dict):
        for st, m in ptm.items():
            if isinstance(m, dict) and isinstance(
                m.get("f1"), (int, float)
            ):
                out[st] = float(m["f1"])
    return out


def evaluate_f1_regression(
    *, baseline: dict[str, Any], new: dict[str, Any]
) -> tuple[bool, list[str]]:
    """The REAL gate. Returns ``(regressed, reasons)``.

    Pure: same inputs -> same verdict. ``reasons`` is empty iff no
    condition tripped. Missing per-type f1 on either side is treated
    as ``0.0`` (fail-closed — a dropped type IS a regression, never a
    pass-by-absence).
    """
    reasons: list[str] = []
    b_total = _total_f1(baseline)
    n_total = _total_f1(new)
    delta_total = n_total - b_total
    if delta_total <= TOTAL_F1_REGRESSION_THRESHOLD:
        reasons.append(f"total_f1_regression: delta={delta_total:.2f}")

    b_pt = _per_type_f1(baseline)
    n_pt = _per_type_f1(new)
    for st in sorted(set(b_pt) | set(n_pt)):
        delta = n_pt.get(st, 0.0) - b_pt.get(st, 0.0)
        if delta <= PER_TYPE_F1_REGRESSION_THRESHOLD:
            reasons.append(f"per_type_regression: {st}: delta={delta:.2f}")
    return (bool(reasons), reasons)


def _load_comparison_from_seam(env_var: str) -> dict[str, Any] | None:
    p = (os.environ.get(env_var) or "").strip()
    if not p:
        return None
    env = json.loads(Path(p).read_text(encoding="utf-8"))
    payload = env.get("payload") if isinstance(env, dict) else None
    if not isinstance(payload, dict):
        raise ValueError(f"{env_var} envelope has no payload object")
    return payload


def _resolve_baseline_and_new() -> (
    tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]
):
    """Return ``(baseline_payload, new_payload, skip_reason)``.

    ``skip_reason`` non-None means exit 0 without gating (no baseline
    established yet).
    """
    seam_b = _load_comparison_from_seam("Z3_BASELINE_COMPARISON_JSON")
    seam_n = _load_comparison_from_seam("Z3_NEW_COMPARISON_JSON")
    if seam_b is not None and seam_n is not None:
        return seam_b, seam_n, None

    store = data_lake_store_root()
    if store is None:
        return None, None, "baseline_not_yet_established"
    base_env = latest_instrument(
        store, TRANSCRIPT_ID, "extraction_alignment_comparison"
    )
    if base_env is None:
        return None, None, "baseline_not_yet_established"
    baseline = base_env.get("payload")
    if not isinstance(baseline, dict):
        return None, None, "baseline_not_yet_established"

    # Production re-run of the comparator against the SAME frozen
    # ceiling + most recent promoted Haiku extraction.
    from spectrum_systems_core.artifacts import Artifact
    from spectrum_systems_core.evals.extraction_comparison import (
        compare_extractions,
        contract_version,
    )

    ceil_env = latest_instrument(store, TRANSCRIPT_ID, "opus_ceiling")
    haiku_env = latest_instrument(
        store, TRANSCRIPT_ID, "meeting_extraction"
    )
    if ceil_env is None or haiku_env is None:
        # Cannot re-run the comparator without both inputs; the
        # baseline alone cannot regress against nothing.
        return None, None, "baseline_not_yet_established"

    def _art(e: dict[str, Any]) -> Artifact:
        return Artifact(
            artifact_type=e["artifact_type"],
            schema_version=e.get("schema_version", 1),
            status=e.get("status", "draft"),
            payload=e.get("payload", {}),
            trace_id=e.get("trace_id", ""),
            input_refs=e.get("input_refs", []),
            artifact_id=e.get("artifact_id", ""),
            created_at=e.get("created_at", ""),
            content_hash=e.get("content_hash", ""),
        )

    new_cmp = compare_extractions(
        ceiling_artifact=_art(ceil_env),
        haiku_artifact=_art(haiku_env),
        alignment_contract_version=contract_version(),
    )
    return baseline, new_cmp.payload, None


def main(argv: list[str] | None = None) -> int:
    addition_id = _active_prompt_addition_id()
    print(f"[z3] active prompt_addition_id={addition_id}", file=sys.stderr)

    try:
        baseline, new, skip = _resolve_baseline_and_new()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[z3] error resolving comparisons: {exc}", file=sys.stderr)
        return 1

    if skip is not None or baseline is None or new is None:
        print("baseline_not_yet_established — skipping F1 regression check")
        return 0

    for payload in (baseline, new):
        try:
            validate_artifact(payload, "extraction_alignment_comparison")
        except ArtifactValidationError as exc:
            print(
                f"[z3] comparison failed schema validation: {exc}",
                file=sys.stderr,
            )
            return 1

    regressed, reasons = evaluate_f1_regression(
        baseline=baseline, new=new
    )

    if not _gate_enabled():
        verdict = "WOULD FAIL" if regressed else "WOULD PASS"
        print(
            f"[z3] {GATE_ENV} is off; gate disabled. {verdict}; "
            f"reasons={reasons}",
            file=sys.stderr,
        )
        print(
            f"F1_REGRESSION_GATE_ENABLED=false — gate disabled "
            f"(would have: {verdict}; reasons={reasons})"
        )
        return 0

    if regressed:
        print("F1 REGRESSION DETECTED:")
        for r in reasons:
            print(f"  - {r}")
        return 1

    print(
        f"F1 regression check passed (prompt_addition_id={addition_id})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
