"""Phase AA.7 — outer-loop harness search driver.

``spectrum-core harness-search --transcript <id> --iterations N``.

The driver is the ONLY place a proposed diff is validated and the ONLY
place a candidate is written (the proposer never self-validates —
AA.4). Everything external is reached through an injected seam so the
loop is fully testable without a data lake or a live model:

* ``preflight`` — runs the four AA.7 pre-flight checks; returns
  ``(ok, detail)``. A failed pre-flight emits a valid
  ``harness_search_result`` with ``iterations_completed: 0`` and
  ``halt_reason: preflight_failed`` (it is a clean stop, not a crash —
  this is the expected sandbox result when the data-lake is absent).
* ``propose`` — ``iteration -> ProposerProposal``.
* ``validate_diff`` — the real allowlist validator seam.
* ``evaluate_code`` — ``harness_code_candidate -> evaluation artifact``.
* ``route_prompt`` — Type-A fast-track router (existing Phase Y).
* ``trigger_pr`` / ``update_frontier`` — side-effect seams.

Convergence: if best F1 has not improved by >= 0.01 for 3 consecutive
completed iterations the loop halts with ``convergence_halt`` — a
stabilised loop, not a failure.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jsonschema

from ..artifacts import Artifact, new_artifact
from ._schema import load_harness_schema
from .harness_mutation_validator import ValidationResult, validate_diff
from .proposer import (
    ProposerContext,
    ProposerError,
    ProposerProposal,
    build_harness_code_candidate,
)

ARTIFACT_TYPE = "harness_search_result"
SCHEMA_VERSION = "1.0.0"
CONVERGENCE_F1_EPSILON = 0.01
CONVERGENCE_FLAT_LIMIT = 3

Clock = Callable[[], str]
# () -> (ok, halt_detail). halt_detail is one of the AA.7 pre-flight
# reason strings when ok is False.
Preflight = Callable[[], tuple[bool, str | None]]
ProposeFn = Callable[[int], ProposerProposal]
ContextFn = Callable[[int], ProposerContext]
EvaluateCodeFn = Callable[[Artifact], Artifact]
RoutePromptFn = Callable[[ProposerProposal], str]
TriggerPrFn = Callable[[str], None]
UpdateFrontierFn = Callable[[], list[dict]]
ValidateFn = Callable[[str], ValidationResult]


@dataclass(frozen=True)
class FinalizeOutcome:
    """Result of the driver's validate-then-build step for a Type-B
    proposal. ``candidate`` is ``None`` exactly when the diff was
    rejected — in that case NOTHING is written and ``finding`` is
    ``proposer_rejected_invalid_diff``."""
    candidate: Artifact | None
    validation: ValidationResult
    finding: str | None


def finalize_code_proposal(
    proposal: ProposerProposal,
    context: ProposerContext,
    *,
    validate_fn: ValidateFn | None = None,
    clock: Clock | None = None,
) -> FinalizeOutcome:
    """Validate a Type-B proposal's diff, then (only if valid) build
    the ``harness_code_candidate`` embedding the validator's result.

    This is the single chokepoint that enforces "the proposer never
    self-validates; the driver validates before any write". When the
    diff is invalid the candidate is NOT built and NOT written.
    """
    if proposal.candidate_type != "B":
        raise ValueError("finalize_code_proposal requires a Type-B proposal")
    vfn = validate_fn or validate_diff
    result = vfn(proposal.proposed_diff or "")
    if not result.valid:
        return FinalizeOutcome(
            candidate=None,
            validation=result,
            finding="proposer_rejected_invalid_diff",
        )
    candidate = build_harness_code_candidate(
        proposal,
        context,
        allowlist_validation_result={
            "valid": result.valid,
            "reason": result.reason,
            "rejected_paths": list(result.rejected_paths),
            "touched_paths": list(result.touched_paths),
        },
        clock=clock,
    )
    return FinalizeOutcome(
        candidate=candidate, validation=result, finding=None
    )


def _target_f1(evaluation: Artifact | dict | None) -> float | None:
    if evaluation is None:
        return None
    payload = (
        evaluation.payload
        if isinstance(evaluation, Artifact)
        else evaluation
    )
    val = payload.get("candidate_target_f1")
    return float(val) if isinstance(val, (int, float)) else None


def run_harness_search(
    *,
    transcript_id: str,
    iterations: int,
    preflight: Preflight,
    propose: ProposeFn,
    context_for: ContextFn,
    evaluate_code: EvaluateCodeFn,
    route_prompt: RoutePromptFn,
    trigger_pr: TriggerPrFn,
    update_frontier: UpdateFrontierFn,
    validate_fn: ValidateFn | None = None,
    search_id: str | None = None,
    clock: Clock | None = None,
) -> Artifact:
    """Run the outer loop. Always returns a valid
    ``harness_search_result`` artifact — pre-flight halt, convergence,
    proposer error, and exhaustion are all *results*, never crashes."""
    sid = search_id or f"search-{uuid.uuid4().hex[:16]}"

    def _emit(
        *,
        iterations_completed: int,
        halt_reason: str,
        per_iteration: list[dict],
        best_f1: float | None,
        best_candidate_id: str | None,
        frontier_final: list[dict],
        preflight_halt_detail: str | None = None,
        convergence_detail: dict | None = None,
    ) -> Artifact:
        payload: dict[str, Any] = {
            "artifact_type": ARTIFACT_TYPE,
            "schema_version": SCHEMA_VERSION,
            "search_id": sid,
            "transcript_id": transcript_id,
            "iterations_requested": int(iterations),
            "iterations_completed": int(iterations_completed),
            "halt_reason": halt_reason,
            "preflight_halt_detail": preflight_halt_detail,
            "convergence_detail": convergence_detail,
            "best_f1_achieved": best_f1,
            "best_candidate_id": best_candidate_id,
            "per_iteration": per_iteration,
            "pareto_frontier_final": frontier_final,
        }
        schema = load_harness_schema(ARTIFACT_TYPE)
        jsonschema.validate(payload, schema)
        return new_artifact(
            artifact_type=ARTIFACT_TYPE,
            payload=payload,
            trace_id=f"hsearch-{sid[:16]}",
            status="draft",
        )

    # ---- pre-flight (all-or-nothing; no iteration runs) ----
    ok, detail = preflight()
    if not ok:
        return _emit(
            iterations_completed=0,
            halt_reason="preflight_failed",
            per_iteration=[],
            best_f1=None,
            best_candidate_id=None,
            frontier_final=[],
            preflight_halt_detail=detail or "preflight_failed",
        )

    per_iteration: list[dict] = []
    best_f1: float | None = None
    best_candidate_id: str | None = None
    flat_streak = 0
    completed = 0
    frontier_final: list[dict] = []

    for i in range(int(iterations)):
        try:
            proposal = propose(i)
        except ProposerError as exc:
            return _emit(
                iterations_completed=completed,
                halt_reason="proposer_error",
                per_iteration=per_iteration,
                best_f1=best_f1,
                best_candidate_id=best_candidate_id,
                frontier_final=frontier_final,
                preflight_halt_detail=(
                    f"{exc.reason_code}: {exc}"
                ),
            )

        entry: dict[str, Any] = {
            "iteration": i,
            "candidate_type": "rejected",
            "candidate_id": None,
            "delta_f1": None,
            "pr_opened": False,
            "outcome": "unset",
        }

        if proposal.candidate_type == "A":
            cand_id = route_prompt(proposal)
            entry.update(
                candidate_type="prompt",
                candidate_id=cand_id,
                outcome="routed_to_fast_track",
            )
        elif proposal.candidate_type == "B":
            ctx = context_for(i)
            outcome = finalize_code_proposal(
                proposal, ctx, validate_fn=validate_fn, clock=clock
            )
            if outcome.candidate is None:
                entry.update(
                    candidate_type="rejected",
                    candidate_id=None,
                    outcome="proposer_rejected_invalid_diff",
                )
            else:
                cand_payload = outcome.candidate.payload
                cand_id = cand_payload["candidate_id"]
                entry.update(candidate_type="code", candidate_id=cand_id)
                try:
                    evaluation = evaluate_code(outcome.candidate)
                except Exception as exc:  # noqa: BLE001
                    # Red-Team Pass-2 #1: the driver "never crashes".
                    # A real evaluator failure (diff_apply_failed,
                    # holdout_equals_target, ...) is logged as this
                    # iteration's outcome and the loop continues — it
                    # never propagates out of run_harness_search.
                    entry["outcome"] = (
                        f"code_evaluation_failed:"
                        f"{type(exc).__name__}"
                    )
                    per_iteration.append(entry)
                    completed += 1
                    flat_streak += 1
                    if flat_streak >= CONVERGENCE_FLAT_LIMIT:
                        return _emit(
                            iterations_completed=completed,
                            halt_reason="convergence_halt",
                            per_iteration=per_iteration,
                            best_f1=best_f1,
                            best_candidate_id=best_candidate_id,
                            frontier_final=frontier_final,
                            convergence_detail={
                                "consecutive_flat_iterations":
                                    CONVERGENCE_FLAT_LIMIT,
                                "current_best_f1": best_f1,
                            },
                        )
                    continue
                ev_payload = (
                    evaluation.payload
                    if isinstance(evaluation, Artifact)
                    else evaluation
                )
                eligible = bool(ev_payload.get("auto_pr_eligible"))
                delta = ev_payload.get("target_delta_f1")
                entry.update(
                    delta_f1=(
                        float(delta)
                        if isinstance(delta, (int, float))
                        else None
                    ),
                )
                if eligible:
                    trigger_pr(cand_id)
                    entry["pr_opened"] = True
                    entry["outcome"] = "pr_triggered"
                else:
                    entry["outcome"] = "evaluated_not_eligible"
                cand_f1 = _target_f1(evaluation)
                if cand_f1 is not None and (
                    best_f1 is None or cand_f1 > best_f1
                ):
                    best_f1 = cand_f1
                    best_candidate_id = cand_id
                frontier_final = update_frontier()
        else:  # "none"
            entry.update(
                candidate_type="rejected",
                candidate_id=None,
                outcome="frontier_exhausted",
            )

        per_iteration.append(entry)
        completed += 1

        # An iteration is "flat" when it produced no measured F1 gain of
        # >= 0.01 (a rejected/prompt iteration has delta_f1 None and is
        # flat by definition — it moved nothing measurable). 3 flat
        # iterations in a row means the loop has stabilised.
        iter_delta = entry["delta_f1"]
        flat = (
            iter_delta is None
            or float(iter_delta) < CONVERGENCE_F1_EPSILON
        )
        flat_streak = flat_streak + 1 if flat else 0
        if flat_streak >= CONVERGENCE_FLAT_LIMIT:
            return _emit(
                iterations_completed=completed,
                halt_reason="convergence_halt",
                per_iteration=per_iteration,
                best_f1=best_f1,
                best_candidate_id=best_candidate_id,
                frontier_final=frontier_final,
                convergence_detail={
                    "consecutive_flat_iterations": CONVERGENCE_FLAT_LIMIT,
                    "current_best_f1": best_f1,
                },
            )

    return _emit(
        iterations_completed=completed,
        halt_reason="iterations_exhausted",
        per_iteration=per_iteration,
        best_f1=best_f1,
        best_candidate_id=best_candidate_id,
        frontier_final=frontier_final,
    )


__all__ = [
    "ARTIFACT_TYPE",
    "SCHEMA_VERSION",
    "CONVERGENCE_F1_EPSILON",
    "CONVERGENCE_FLAT_LIMIT",
    "FinalizeOutcome",
    "finalize_code_proposal",
    "run_harness_search",
]
