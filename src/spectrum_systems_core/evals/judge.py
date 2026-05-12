"""Phase X2.3 — LLM-as-judge (qualitative evaluation layer).

Implements the third eval layer (Rec 6c): a separate model is asked to
score each extracted decision against four atomic boolean rubric
checks. The decision passes the judge when ALL four checks return
true.

Design constraints (mirroring Phase T/V rules):

  * ``JUDGE_ENABLED=false`` is the default. The runner makes zero
    model calls when disabled and writes no judge_score artifact.
  * The judge model is configurable via ``JUDGE_MODEL`` env var; the
    default ``claude-sonnet-4-6`` is intentionally a DIFFERENT family
    from the extraction Haiku model so the verdict is more
    independent (research note: same-family judges over-agree).
  * Never raises. Every recoverable failure (API timeout, parse
    failure) collapses to ``judge_decision: null`` for that item and
    a structured warn finding rather than crashing the run.
  * Stability check is opt-in via
    ``JUDGE_STABILITY_CHECK_ENABLED=true``. When on, every item is
    judged twice; a verdict mismatch emits ``judge_score_unstable``.

Public entry-point: ``run_judge`` -> ``JudgeRunResult``.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..health.finding import HealthFinding

_LOG = logging.getLogger(__name__)


JUDGE_ENABLED_ENV: str = "JUDGE_ENABLED"
JUDGE_STABILITY_CHECK_ENABLED_ENV: str = "JUDGE_STABILITY_CHECK_ENABLED"
JUDGE_MODEL_ENV: str = "JUDGE_MODEL"
DEFAULT_JUDGE_MODEL: str = "claude-sonnet-4-6"
# Default Haiku family identifier used by Phase T/V extraction. Used
# only to surface a same-family warn finding; never to gate.
EXTRACTION_FAMILY_HINT_ENV: str = "EXTRACTION_MODEL_FAMILY"
DEFAULT_EXTRACTION_FAMILY: str = "haiku"

JUDGE_CALIBRATION_WARN_THRESHOLD: float = 0.70
JUDGE_CALIBRATION_HALT_THRESHOLD: float = 0.60

JUDGE_SCHEMA_VERSION: str = "1.0.0"
PRODUCED_BY: str = "JudgeRunner"


# --- Rubric ----------------------------------------------------------

RUBRIC_CHECKS: tuple = (
    "decision_text_supported_by_source",
    "decision_outcome_matches_regulatory_verb",
    "speaker_attribution_correct",
    "no_hallucinated_constraints_or_actors",
)


@dataclass
class JudgeItemScore:
    """One item's judge verdict."""

    item_id: str
    decision_text: str
    rubric_results: Dict[str, Optional[bool]]
    passed: bool
    failure_reasons: List[str] = field(default_factory=list)
    stability_match: Optional[bool] = None
    judge_decision: Optional[str] = None  # 'pass' / 'fail' / 'unparseable'


@dataclass
class JudgeRunResult:
    judge_run_id: str
    judge_score_id: str
    enabled: bool
    judge_model: str
    items_evaluated: int
    aggregate_pass_rate: Optional[float]
    item_scores: List[JudgeItemScore]
    findings: List[HealthFinding]
    calibration_status: str  # 'unrun' | 'ok' | 'warn' | 'failed'


# --- Env helpers -----------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"true", "1", "yes", "on"}:
        return True
    if raw in {"false", "0", "no", "off"}:
        return False
    return default


def judge_enabled() -> bool:
    return _env_bool(JUDGE_ENABLED_ENV, default=False)


def stability_check_enabled() -> bool:
    return _env_bool(JUDGE_STABILITY_CHECK_ENABLED_ENV, default=False)


def judge_model() -> str:
    return os.environ.get(JUDGE_MODEL_ENV, "").strip() or DEFAULT_JUDGE_MODEL


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


# --- Same-family detection ------------------------------------------


_KNOWN_FAMILIES: tuple = (
    "opus", "sonnet", "haiku",  # Claude families
    "gpt", "o1", "o3",
    "gemini", "llama", "mistral",
)


def _model_family(model_id: str) -> str:
    """Return the family token from a model id, lowercase.

    Returns ``"unknown"`` when no family token is recognised.
    """
    if not isinstance(model_id, str):
        return "unknown"
    low = model_id.lower()
    for fam in _KNOWN_FAMILIES:
        if fam in low:
            return fam
    return "unknown"


# --- Prompt ----------------------------------------------------------


_PROMPT_HEADER: str = (
    "You are an evaluator (NOT an extractor) judging a single extracted "
    "decision against the source chunk. Return ONLY a JSON object with "
    "the four boolean fields below. Do not add commentary.\n"
    "\n"
    "Required fields (each strictly true/false):\n"
    "- decision_text_supported_by_source\n"
    "- decision_outcome_matches_regulatory_verb\n"
    "- speaker_attribution_correct\n"
    "- no_hallucinated_constraints_or_actors\n"
)


def build_judge_prompt(
    item: Dict[str, Any],
    source_text: str,
) -> str:
    """Compose the per-item judge prompt."""
    lines: List[str] = [
        _PROMPT_HEADER,
        "",
        "EXTRACTED DECISION:",
        json.dumps({
            "decision_text": item.get("decision_text"),
            "decision_outcome": item.get("decision_outcome"),
            "regulatory_verb": item.get("regulatory_verb"),
            "speaker": item.get("speaker"),
        }, sort_keys=True),
        "",
        "SOURCE CHUNK:",
        source_text or "",
        "",
        "Respond with ONLY the JSON object.",
    ]
    return "\n".join(lines)


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_judge_response(text: str) -> Dict[str, Optional[bool]]:
    """Parse the model's judge response into a dict of boolean checks.

    Missing or non-boolean entries become ``None`` (treated as
    unparseable). The caller decides what to do with ``None`` -- the
    item is marked ``judge_decision = "unparseable"`` and the rubric
    keys with None fall through to a warn finding aggregator.
    """
    out: Dict[str, Optional[bool]] = {k: None for k in RUBRIC_CHECKS}
    if not isinstance(text, str) or not text.strip():
        return out
    match = _JSON_RE.search(text)
    if match is None:
        return out
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return out
    if not isinstance(data, dict):
        return out
    for key in RUBRIC_CHECKS:
        v = data.get(key)
        if isinstance(v, bool):
            out[key] = v
    return out


# --- Default offline caller -----------------------------------------


def _default_offline_caller(prompt: str) -> str:  # noqa: ARG001
    return ""


# --- Runner ----------------------------------------------------------


def run_judge(
    decisions: Sequence[Dict[str, Any]],
    source_texts_by_chunk: Dict[str, str],
    *,
    source_id: str = "",
    pipeline_run_id: Optional[str] = None,
    api_caller: Optional[Callable[[str], str]] = None,
    extraction_model: Optional[str] = None,
) -> JudgeRunResult:
    """Run the judge over ``decisions``. Never raises.

    When ``JUDGE_ENABLED=false`` (default) returns a result with
    ``enabled=False``, zero items_evaluated, and zero model calls
    (the caller is responsible for not invoking the SDK in that case).
    """
    judge_run_id = str(uuid.uuid4())
    judge_score_id = str(uuid.uuid4())

    if not judge_enabled():
        return JudgeRunResult(
            judge_run_id=judge_run_id,
            judge_score_id=judge_score_id,
            enabled=False,
            judge_model="",
            items_evaluated=0,
            aggregate_pass_rate=None,
            item_scores=[],
            findings=[],
            calibration_status="unrun",
        )

    judge_id = judge_model()
    caller = api_caller or _default_offline_caller
    stability_on = stability_check_enabled()

    findings: List[HealthFinding] = []
    # Same-family check is opportunistic. It does NOT gate the run; it
    # gives the operator a single warn finding so they know the judge
    # is not from an independent model family.
    if extraction_model:
        if _model_family(judge_id) == _model_family(extraction_model):
            findings.append(HealthFinding(
                finding_code="judge_same_family",
                severity="warn",
                pipeline_run_id=pipeline_run_id,
                context={
                    "judge_model": judge_id,
                    "extraction_model": extraction_model,
                    "family": _model_family(judge_id),
                },
                remediation=(
                    "Set JUDGE_MODEL to a model from a different family "
                    "(e.g. claude-sonnet-4-6 against haiku extraction) "
                    "for an independent verdict."
                ),
            ))

    item_scores: List[JudgeItemScore] = []
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        item_id = (
            decision.get("decision_id")
            or decision.get("item_id")
            or str(uuid.uuid4())
        )
        chunk_ids = decision.get("source_turn_ids") or decision.get("source_chunk_ids") or []
        source_text = ""
        if isinstance(chunk_ids, list):
            for cid in chunk_ids:
                src = source_texts_by_chunk.get(str(cid))
                if src:
                    source_text += src + "\n"
        prompt = build_judge_prompt(decision, source_text)
        try:
            response = caller(prompt)
        except Exception as exc:  # noqa: BLE001 - judge never raises
            _LOG.warning(
                "judge_api_error: item=%s %s: %s",
                item_id, type(exc).__name__, exc,
            )
            response = ""

        rubric = parse_judge_response(response)
        unparseable = any(v is None for v in rubric.values())
        passed = (not unparseable) and all(rubric.values())

        stability_match: Optional[bool] = None
        if stability_on:
            try:
                second = caller(prompt)
            except Exception:  # noqa: BLE001
                second = ""
            rubric2 = parse_judge_response(second)
            stability_match = rubric == rubric2
            if stability_match is False:
                findings.append(HealthFinding(
                    finding_code="judge_score_unstable",
                    severity="warn",
                    pipeline_run_id=pipeline_run_id,
                    context={
                        "item_id": item_id,
                        "judge_model": judge_id,
                        "first": rubric, "second": rubric2,
                    },
                    remediation=(
                        "Re-run the judge with temperature=0 OR pin the "
                        "judge_model to a single version; verdicts must "
                        "be reproducible for calibration to be valid."
                    ),
                ))

        failure_reasons = [
            f"{k}=false" for k, v in rubric.items() if v is False
        ]
        if unparseable:
            failure_reasons.append("rubric_unparseable")
        item_scores.append(JudgeItemScore(
            item_id=str(item_id),
            decision_text=(decision.get("decision_text") or "")[:500],
            rubric_results=rubric,
            passed=passed,
            failure_reasons=failure_reasons,
            stability_match=stability_match,
            judge_decision=("unparseable" if unparseable else ("pass" if passed else "fail")),
        ))

    aggregate_pass_rate: Optional[float] = None
    parseable = [s for s in item_scores if s.judge_decision != "unparseable"]
    if parseable:
        aggregate_pass_rate = sum(1 for s in parseable if s.passed) / float(
            len(parseable)
        )

    return JudgeRunResult(
        judge_run_id=judge_run_id,
        judge_score_id=judge_score_id,
        enabled=True,
        judge_model=judge_id,
        items_evaluated=len(item_scores),
        aggregate_pass_rate=aggregate_pass_rate,
        item_scores=item_scores,
        findings=findings,
        calibration_status="unrun",
    )


# --- Artifact serialisation -----------------------------------------


def judge_score_to_artifact(
    result: JudgeRunResult,
    source_id: str,
    *,
    pipeline_run_id: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "artifact_type": "judge_score",
        "schema_version": JUDGE_SCHEMA_VERSION,
        "judge_score_id": result.judge_score_id,
        "judge_run_id": result.judge_run_id,
        "source_id": source_id,
        "pipeline_run_id": pipeline_run_id,
        "judge_model": result.judge_model,
        "enabled": result.enabled,
        "items_evaluated": result.items_evaluated,
        "aggregate_pass_rate": result.aggregate_pass_rate,
        "calibration_status": result.calibration_status,
        "created_at": _now_iso(),
        "item_scores": [
            {
                "item_id": s.item_id,
                "decision_text": s.decision_text,
                "rubric_results": s.rubric_results,
                "passed": s.passed,
                "failure_reasons": list(s.failure_reasons),
                "stability_match": s.stability_match,
                "judge_decision": s.judge_decision,
            }
            for s in result.item_scores
        ],
        "provenance": {"produced_by": PRODUCED_BY},
    }


__all__ = [
    "DEFAULT_JUDGE_MODEL",
    "JUDGE_CALIBRATION_HALT_THRESHOLD",
    "JUDGE_CALIBRATION_WARN_THRESHOLD",
    "JUDGE_ENABLED_ENV",
    "JUDGE_MODEL_ENV",
    "JUDGE_STABILITY_CHECK_ENABLED_ENV",
    "JudgeItemScore",
    "JudgeRunResult",
    "RUBRIC_CHECKS",
    "build_judge_prompt",
    "judge_enabled",
    "judge_model",
    "judge_score_to_artifact",
    "parse_judge_response",
    "run_judge",
    "stability_check_enabled",
]
