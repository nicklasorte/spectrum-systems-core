"""Phase AA.5 — governed-track code candidate evaluator.

Mirrors ``extraction/candidate_evaluator.py`` (Phase Y.6) but for
``harness_code_candidate`` artifacts. The fairness invariant is
identical: the FROZEN Opus ceiling is LOADED, never regenerated, for
both the target and the config-pinned holdout.

Defense-in-depth (Red-Team Pass-1 #6, Pass-2 #4): step 2 re-runs the
REAL allowlist validator on the candidate's ACTUAL ``proposed_diff``
field — never the cached ``allowlist_validation_result``. If the diff
was tampered with between proposal and evaluation so it now touches a
forbidden file, the recheck halts with ``allowlist_recheck_failed``
BEFORE any harness is patched or any extraction runs. No evaluation
artifact is emitted on that halt, so a tampered diff can never reach
the auto-PR gate.

The diff is applied to a TEMP copy of the harness, never the working
tree (the ``apply_diff`` seam defaults to a temp-dir ``git apply``).
The patched extraction is produced through the injected
``patched_runner`` seam, so this module never makes a live model call
itself — the same seam pattern Phase Y uses.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jsonschema

from ..artifacts import Artifact, new_artifact
from ..evals.extraction_comparison import compare_extractions, contract_version
from ..extraction.candidate_evaluator import (
    _CONFIG_PATH,
    _f1s,
    _regressions,
    _resolve_holdout,
)
from ._schema import load_harness_schema
from .code_pr_eligibility import evaluate_code_eligibility
from .harness_mutation_validator import validate_diff

ARTIFACT_TYPE = "harness_code_candidate_evaluation"
SCHEMA_VERSION = "1.0.0"

# Allowlisted harness files staged into the temp patch root. Mirrors
# the AA.3 allowlist; the snapshot copy keeps relative layout so a
# unified diff with ``a/src/...`` paths applies cleanly.
_PATCH_SOURCE_FILES: tuple[str, ...] = (
    "src/spectrum_systems_core/extraction/typed_extraction_runner.py",
    "src/spectrum_systems_core/extraction/chunker.py",
    "src/spectrum_systems_core/context/bundle_builder.py",
    "src/spectrum_systems_core/workflows/prompts",
)

CeilingLoader = Callable[[str], Artifact]
BaselineLoader = Callable[[str], Artifact]
# (transcript_id, patched_harness_dir) -> extraction artifact.
PatchedRunner = Callable[[str, Path], Artifact]
Comparator = Callable[[Artifact, Artifact, str], Artifact]
# (diff_text, dest_dir) -> None. Raises on apply failure.
ApplyDiff = Callable[[str, Path], None]


class CodeCandidateEvaluatorError(RuntimeError):
    def __init__(self, message: str, *, reason_code: str):
        super().__init__(message)
        self.reason_code = reason_code


def _default_comparator(
    ceiling: Artifact, candidate: Artifact, version: str
) -> Artifact:
    return compare_extractions(
        ceiling_artifact=ceiling,
        haiku_artifact=candidate,
        alignment_contract_version=version,
    )


def _default_apply_diff(diff_text: str, dest_dir: Path) -> None:
    """Stage allowlisted files into ``dest_dir`` and ``git apply`` the
    diff there. Fail-closed: any apply failure raises so a candidate
    whose diff does not apply cleanly is never silently evaluated as a
    no-op improvement."""
    repo_root = Path(__file__).resolve().parents[3]
    for rel in _PATCH_SOURCE_FILES:
        src = repo_root / rel
        dst = dest_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        elif src.is_file():
            shutil.copy2(src, dst)
    patch_file = dest_dir / "_candidate.diff"
    patch_file.write_text(diff_text, encoding="utf-8")
    result = subprocess.run(
        ["git", "apply", "--unsafe-paths", "-p1",
         f"--directory={dest_dir}", str(patch_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise CodeCandidateEvaluatorError(
            f"git apply failed: {result.stderr.strip()}",
            reason_code="diff_apply_failed",
        )


def _payload_of(candidate: Artifact | dict) -> dict:
    if isinstance(candidate, Artifact):
        return candidate.payload
    if isinstance(candidate, dict):
        # Accept either a raw envelope or just the payload.
        return candidate.get("payload", candidate)
    raise CodeCandidateEvaluatorError(
        f"unsupported candidate type {type(candidate).__name__}",
        reason_code="malformed_candidate",
    )


def evaluate_code_candidate(
    *,
    candidate: Artifact | dict,
    target_transcript_id: str,
    ceiling_loader: CeilingLoader,
    baseline_loader: BaselineLoader,
    patched_runner: PatchedRunner,
    holdout_transcript_id: str | None = None,
    config_path: Path | None = None,
    alignment_contract_version: str | None = None,
    comparator: Comparator | None = None,
    apply_diff: ApplyDiff | None = None,
    contract_path: Path | str | None = None,
) -> Artifact:
    """Evaluate a ``harness_code_candidate``. Emit
    ``harness_code_candidate_evaluation`` — or halt before evaluation
    on a failed defense-in-depth allowlist recheck."""
    payload = _payload_of(candidate)
    candidate_id = payload.get("candidate_id")
    diff_text = payload.get("proposed_diff")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise CodeCandidateEvaluatorError(
            "candidate missing candidate_id",
            reason_code="malformed_candidate",
        )
    if not isinstance(diff_text, str) or not diff_text.strip():
        raise CodeCandidateEvaluatorError(
            "candidate missing proposed_diff",
            reason_code="malformed_candidate",
        )

    # Step 2 — defense-in-depth recheck on the ACTUAL proposed_diff,
    # never the cached allowlist_validation_result.
    recheck = validate_diff(diff_text, contract_path=contract_path)
    if not recheck.valid:
        raise CodeCandidateEvaluatorError(
            f"allowlist recheck rejected diff: reason={recheck.reason} "
            f"rejected={recheck.rejected_paths}",
            reason_code="allowlist_recheck_failed",
        )

    holdout = _resolve_holdout(
        holdout_transcript_id, config_path or _CONFIG_PATH
    )
    if holdout == target_transcript_id:
        raise CodeCandidateEvaluatorError(
            "holdout equals target — refusing to evaluate a code "
            "candidate against its own target transcript",
            reason_code="holdout_equals_target",
        )

    version = alignment_contract_version or contract_version()
    cmp = comparator or _default_comparator
    apply_fn = apply_diff or _default_apply_diff

    deltas: dict[str, dict] = {}
    per_type_regressions: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="harness-cand-") as td:
        patch_root = Path(td)
        apply_fn(diff_text, patch_root)
        for role, tid in (
            ("target", target_transcript_id),
            ("holdout", holdout),
        ):
            ceiling = ceiling_loader(tid)  # FROZEN — never regenerated
            baseline = baseline_loader(tid)
            patched = patched_runner(tid, patch_root)
            base_total, base_pt = _f1s(cmp(ceiling, baseline, version))
            cand_total, cand_pt = _f1s(cmp(ceiling, patched, version))
            deltas[role] = {
                "baseline": base_total,
                "candidate": cand_total,
                "delta": cand_total - base_total,
            }
            per_type_regressions.extend(
                _regressions(tid, base_pt, cand_pt)
            )

    per_type_regressions.sort(
        key=lambda r: (r["transcript_id"], r["schema_type"])
    )
    eval_payload: dict[str, Any] = {
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
        "allowlist_recheck_passed": True,
    }
    verdict = evaluate_code_eligibility(eval_payload)
    eval_payload["auto_pr_eligible"] = verdict.eligible
    eval_payload["eligibility_reason"] = verdict.reason

    schema = load_harness_schema("harness_code_candidate_evaluation")
    jsonschema.validate(eval_payload, schema)
    return new_artifact(
        artifact_type=ARTIFACT_TYPE,
        payload=eval_payload,
        trace_id=f"codeeval-{uuid.uuid4().hex[:16]}",
        status="draft",
        input_refs=[candidate_id],
    )


__all__ = [
    "ARTIFACT_TYPE",
    "SCHEMA_VERSION",
    "CodeCandidateEvaluatorError",
    "evaluate_code_candidate",
]
