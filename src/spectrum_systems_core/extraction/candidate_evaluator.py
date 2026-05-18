"""Phase Y.6 — multi-transcript candidate evaluator.

Scores a correction candidate's prompt addition against BOTH the
transcript it was mined from (``target``) and a config-pinned
``holdout`` it has never seen, using the FROZEN Opus ceilings for
both. The ceilings are LOADED, never regenerated — regenerating a
ceiling under the candidate prompt would move the yardstick and make
every candidate look like an improvement. This module never imports or
calls ``extract_ceiling``; that omission is the fairness invariant.

Holdout pinning is out-of-band: ``config/phase_y.yaml`` ->
``phase_y_holdout_transcript_id``. No holdout configured -> fail closed
with ``holdout_not_configured`` and NO artifact (a runtime-chosen
holdout could be the target itself).

``auto_pr_eligible`` / ``eligibility_reason`` are stamped via
``auto_pr_eligibility.evaluate_eligibility`` on the very payload that
is written, so the artifact's verdict can never drift from the
workflow's verdict.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

import yaml

from ..artifacts import Artifact, new_artifact
from ..evals.extraction_comparison import (
    compare_extractions,
    contract_version,
)
from .auto_pr_eligibility import evaluate_eligibility

ARTIFACT_TYPE = "candidate_evaluation"
SCHEMA_VERSION = "1.0.0"
PER_TYPE_REGRESSION_DROP = 0.02

_CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "phase_y.yaml"
)

CeilingLoader = Callable[[str], Artifact]
BaselineLoader = Callable[[str], Artifact]
HaikuRunner = Callable[[str, str], Artifact]
Comparator = Callable[[Artifact, Artifact, str], Artifact]


class CandidateEvaluatorError(RuntimeError):
    def __init__(self, message: str, *, reason_code: str):
        super().__init__(message)
        self.reason_code = reason_code


def _resolve_holdout(
    holdout_transcript_id: str | None, config_path: Path
) -> str:
    if holdout_transcript_id:
        return holdout_transcript_id
    try:
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise CandidateEvaluatorError(
            f"phase_y config unreadable: {exc}",
            reason_code="holdout_not_configured",
        ) from exc
    pinned = (cfg.get("phase_y_holdout_transcript_id") or "").strip()
    if not pinned:
        raise CandidateEvaluatorError(
            "phase_y_holdout_transcript_id is not configured",
            reason_code="holdout_not_configured",
        )
    return pinned


def _default_comparator(
    ceiling: Artifact, haiku: Artifact, version: str
) -> Artifact:
    return compare_extractions(
        ceiling_artifact=ceiling,
        haiku_artifact=haiku,
        alignment_contract_version=version,
    )


def _f1s(comparison: Artifact) -> tuple[float, dict[str, float]]:
    payload = comparison.payload
    total = float(payload["total_metrics"]["f1"])
    per_type = {
        st: float(m["f1"])
        for st, m in payload["per_type_metrics"].items()
    }
    return total, per_type


def _regressions(
    transcript_id: str,
    baseline_per_type: dict[str, float],
    candidate_per_type: dict[str, float],
) -> list[dict]:
    out: list[dict] = []
    for stype in sorted(
        set(baseline_per_type) | set(candidate_per_type)
    ):
        base = baseline_per_type.get(stype, 0.0)
        cand = candidate_per_type.get(stype, 0.0)
        delta = cand - base
        if base - cand >= PER_TYPE_REGRESSION_DROP:
            out.append(
                {
                    "transcript_id": transcript_id,
                    "schema_type": stype,
                    "baseline_f1": base,
                    "candidate_f1": cand,
                    "delta": delta,
                }
            )
    return out


def evaluate_candidate(
    *,
    candidate_id: str,
    candidate_prompt: str,
    target_transcript_id: str,
    ceiling_loader: CeilingLoader,
    baseline_loader: BaselineLoader,
    haiku_runner: HaikuRunner,
    holdout_transcript_id: str | None = None,
    config_path: Path | None = None,
    alignment_contract_version: str | None = None,
    comparator: Comparator | None = None,
) -> Artifact:
    """Produce the ``candidate_evaluation`` artifact. Fail-closed on an
    unconfigured holdout."""
    holdout = _resolve_holdout(
        holdout_transcript_id, config_path or _CONFIG_PATH
    )
    if holdout == target_transcript_id:
        raise CandidateEvaluatorError(
            "holdout equals target — refusing to evaluate a candidate "
            "against the transcript it was mined from",
            reason_code="holdout_equals_target",
        )
    version = alignment_contract_version or contract_version()
    cmp = comparator or _default_comparator

    deltas: dict[str, dict] = {}
    per_type_regressions: list[dict] = []
    for role, tid in (
        ("target", target_transcript_id),
        ("holdout", holdout),
    ):
        ceiling = ceiling_loader(tid)  # FROZEN — never regenerated
        baseline_haiku = baseline_loader(tid)
        candidate_haiku = haiku_runner(tid, candidate_prompt)
        base_total, base_pt = _f1s(cmp(ceiling, baseline_haiku, version))
        cand_total, cand_pt = _f1s(cmp(ceiling, candidate_haiku, version))
        deltas[role] = {
            "baseline": base_total,
            "candidate": cand_total,
            "delta": cand_total - base_total,
        }
        per_type_regressions.extend(_regressions(tid, base_pt, cand_pt))

    per_type_regressions.sort(
        key=lambda r: (r["transcript_id"], r["schema_type"])
    )
    payload = {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "target_transcript_id": target_transcript_id,
        "holdout_transcript_id": holdout,
        "baseline_target_f1": deltas["target"]["baseline"],
        "candidate_target_f1": deltas["target"]["candidate"],
        "target_delta_f1": deltas["target"]["delta"],
        "baseline_holdout_f1": deltas["holdout"]["baseline"],
        "candidate_holdout_f1": deltas["holdout"]["candidate"],
        "holdout_delta_f1": deltas["holdout"]["delta"],
        "per_type_regressions": per_type_regressions,
    }
    verdict = evaluate_eligibility(payload)
    payload["auto_pr_eligible"] = verdict.eligible
    payload["eligibility_reason"] = verdict.reasons
    return new_artifact(
        artifact_type=ARTIFACT_TYPE,
        payload=payload,
        trace_id=f"candeval-{uuid.uuid4().hex[:16]}",
        status="draft",
        input_refs=[candidate_id],
    )


__all__ = [
    "ARTIFACT_TYPE",
    "SCHEMA_VERSION",
    "PER_TYPE_REGRESSION_DROP",
    "CandidateEvaluatorError",
    "evaluate_candidate",
]
