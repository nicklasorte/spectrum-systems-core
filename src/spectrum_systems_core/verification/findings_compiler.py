"""Phase O.6 — compile-findings.

Reads the most recent ``pipeline_state_record`` and the most recent
``eval_summary`` (if any), then synthesizes a single
``verification_findings`` artifact. The artifact is intentionally
human-reviewable: every finding is a (severity, area, title,
description, remediation) tuple with a list of affected artifact ids.

Sanity bounds are the same ones encoded in ``review-baseline-candidate``:

* regulatory_verb_fallback_rate < 0.30
* human_dedup_rate              < 0.20
* off_topic_rate                < 0.30

These three rates are computed from ``meeting_extraction`` artifacts the
caller passes in. The compiler never reaches into the data lake on its
own — the caller decides which artifacts are in scope.

Never raises.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import jsonschema

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0.0"
DEFAULT_PRODUCED_BY = "compile-findings"

# Sanity bounds. Documented in code so the rationale ships with the
# implementation rather than only the spec doc:
# * 0.30 verb-fallback rate ≈ one in three chunks classified by the
#   regex fallback. Above that, the routing is largely heuristic and
#   the eval numbers do not reflect model behavior.
# * 0.20 dedup rate is the operational ceiling Phase M.4 picked for
#   ground-truth pairs — beyond it, human review queue is unsustainable.
# * 0.30 off-topic rate echoes the verb-fallback ceiling: above it the
#   chunker is dragging non-pipeline turns into the classifier.
SANITY_BOUNDS = {
    "regulatory_verb_fallback_rate": 0.30,
    "human_dedup_rate": 0.20,
    "off_topic_rate": 0.30,
}


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _contracts_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "contracts"
        if (candidate / "schemas").is_dir():
            return candidate
    return Path(__file__).resolve().parents[3] / "contracts"


def _safe_div(num: float, den: float) -> Optional[float]:
    """Return num / den, or None when den == 0 (divide-by-zero guard)."""
    if den is None or den == 0:
        return None
    try:
        return float(num) / float(den)
    except (TypeError, ValueError):
        return None


def compute_extraction_rates(
    meeting_extractions: Iterable[Dict[str, Any]],
) -> Dict[str, Optional[float]]:
    """Aggregate the three sanity rates across meeting_extraction artifacts.

    Returns a dict with float values when the denominator is non-zero
    and ``None`` when the denominator is zero (no chunks classified,
    no items extracted). The ``None`` propagation is deliberate — the
    review-baseline-candidate command renders ``None`` as ``REVIEW
    (no data)`` rather than treating "0.0 of 0" as a passing rate.
    """
    total_chunks = 0
    total_items = 0
    fallback = 0
    off_topic = 0
    dedup = 0

    for me in meeting_extractions:
        if not isinstance(me, dict):
            continue
        try:
            total_chunks += int(me.get("total_chunks_classified", 0) or 0)
            off_topic += int(me.get("off_topic_count", 0) or 0)
            fallback += int(me.get("regulatory_verb_fallback_count", 0) or 0)
            dedup += int(me.get("requires_human_dedup_count", 0) or 0)
        except (TypeError, ValueError):
            continue
        for key in ("decisions", "claims", "action_items"):
            seq = me.get(key)
            if isinstance(seq, list):
                total_items += len(seq)

    return {
        "regulatory_verb_fallback_rate": _safe_div(fallback, total_chunks),
        "off_topic_rate": _safe_div(off_topic, total_chunks),
        "human_dedup_rate": _safe_div(dedup, total_items),
    }


def _load_meeting_extractions_from_sdl(
    sdl_root: Optional[Path],
) -> List[Dict[str, Any]]:
    if sdl_root is None or not sdl_root.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    candidates = list((sdl_root / "extractions").glob("*.json")) if (
        sdl_root / "extractions"
    ).is_dir() else []
    # Also look at the SDL fallback layout (flat sdl_root/*.json).
    candidates.extend(p for p in sdl_root.glob("*.json") if p.is_file())
    for path in candidates:
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(obj, dict)
            and obj.get("artifact_type") == "meeting_extraction"
        ):
            out.append(obj)
    return out


def _load_latest_eval_summary(
    sdl_root: Optional[Path],
) -> Optional[Dict[str, Any]]:
    """Return the most recent ``eval_summary`` artifact, or None."""
    if sdl_root is None or not sdl_root.is_dir():
        return None
    evals_dir = sdl_root / "evals"
    if not evals_dir.is_dir():
        return None
    best: Optional[Tuple[str, Dict[str, Any]]] = None
    for path in evals_dir.glob("eval_summary_*.json"):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("artifact_type") != "eval_summary":
            continue
        ts = obj.get("created_at") or ""
        if best is None or ts > best[0]:
            best = (ts, obj)
    return best[1] if best else None


def _load_latest_pipeline_state_record(
    sdl_root: Optional[Path],
) -> Optional[Dict[str, Any]]:
    if sdl_root is None or not sdl_root.is_dir():
        return None
    target = sdl_root / "verifications"
    if not target.is_dir():
        return None
    best: Optional[Tuple[str, Dict[str, Any]]] = None
    for path in target.glob("*.json"):
        if path.name.endswith(".invalid.json"):
            continue
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("artifact_type") != "pipeline_state_record":
            continue
        ts = obj.get("created_at") or ""
        if best is None or ts > best[0]:
            best = (ts, obj)
    return best[1] if best else None


def _findings_from_pipeline_state(
    record: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not isinstance(record, dict):
        return []
    findings: List[Dict[str, Any]] = []

    failures = record.get("validation_failures_by_type") or {}
    for artifact_type, count in sorted(failures.items()):
        if int(count or 0) <= 0:
            continue
        findings.append(
            {
                "severity": "sev_1",
                "area": "schema",
                "title": f"schema_validation_failures_for_{artifact_type}",
                "description": (
                    f"{count} artifact(s) of type '{artifact_type}' failed "
                    "schema validation. Inspect the .invalid.json sidecars."
                ),
                "affected_artifacts": [],
                "proposed_remediation": (
                    "Run the relevant pipeline stage with --force on the "
                    "affected source_ids, or fix the artifact body and "
                    "re-validate."
                ),
                "github_issue_url": None,
            }
        )

    kind_only = int(record.get("artifacts_with_artifact_kind_only", 0) or 0)
    if kind_only > 0:
        findings.append(
            {
                "severity": "sev_2",
                "area": "migration",
                "title": "legacy_artifact_kind_only_artifacts_present",
                "description": (
                    f"{kind_only} artifact(s) carry only 'artifact_kind' "
                    "(legacy). New schemas require 'artifact_type'."
                ),
                "affected_artifacts": [],
                "proposed_remediation": (
                    "Run migrate-artifact-kind workflow with dry-run=true "
                    "first, then confirm=true."
                ),
                "github_issue_url": None,
            }
        )

    expected = record.get("expected_artifacts") or {}
    confirmed = int(expected.get("confirmed_pair_count", 0) or 0)
    extractions = int(expected.get("meeting_extraction_count", 0) or 0)
    alignments = int(expected.get("alignment_result_count", 0) or 0)
    eval_results = int(expected.get("eval_result_count", 0) or 0)

    if confirmed > 0 and extractions < confirmed:
        findings.append(
            {
                "severity": "sev_1",
                "area": "pipeline",
                "title": "meeting_extraction_count_below_confirmed_pair_count",
                "description": (
                    f"{extractions} meeting_extraction artifact(s) for "
                    f"{confirmed} confirmed ground_truth_pair(s)."
                ),
                "affected_artifacts": [],
                "proposed_remediation": (
                    "Run the pipeline with force_only_missing=true to fill "
                    "the gap."
                ),
                "github_issue_url": None,
            }
        )

    if extractions > 0 and alignments < extractions:
        findings.append(
            {
                "severity": "sev_2",
                "area": "eval",
                "title": "alignment_result_count_below_meeting_extraction_count",
                "description": (
                    f"{alignments} alignment_result artifact(s) for "
                    f"{extractions} meeting_extraction(s)."
                ),
                "affected_artifacts": [],
                "proposed_remediation": "Run eval-ground-truth.",
                "github_issue_url": None,
            }
        )

    if eval_results >= 1 and not bool(
        expected.get("baseline_eval_summary_present", False)
    ):
        findings.append(
            {
                "severity": "sev_2",
                "area": "eval",
                "title": "baseline_eval_summary_missing",
                "description": (
                    "eval_result artifacts exist but no baseline_eval_summary "
                    "has been installed yet."
                ),
                "affected_artifacts": [],
                "proposed_remediation": (
                    "After human review, run "
                    "'eval-ground-truth --set-baseline'."
                ),
                "github_issue_url": None,
            }
        )

    return findings


def _findings_from_eval_summary(
    summary: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not isinstance(summary, dict):
        return []
    findings: List[Dict[str, Any]] = []
    if bool(summary.get("partial_run_warning", False)):
        detail = summary.get("partial_run_detail") or {}
        missing = []
        if isinstance(detail, dict):
            missing = list(detail.get("missing_source_ids") or [])
        findings.append(
            {
                "severity": "sev_1",
                "area": "eval",
                "title": "eval_summary_partial_run_warning",
                "description": (
                    "eval_summary was produced with partial_run_warning=True; "
                    "extraction count was below the confirmed_pair_count."
                ),
                "affected_artifacts": list(missing),
                "proposed_remediation": (
                    "Re-run the pipeline for the missing source_ids before "
                    "rebaselining."
                ),
                "github_issue_url": None,
            }
        )
    return findings


def _findings_from_rates(
    rates: Dict[str, Optional[float]],
) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    for key, value in rates.items():
        bound = SANITY_BOUNDS.get(key)
        if bound is None or value is None:
            continue
        if value >= bound:
            findings.append(
                {
                    "severity": "sev_2",
                    "area": "eval",
                    "title": f"sanity_bound_exceeded_{key}",
                    "description": (
                        f"{key} = {value:.3f}, exceeds sanity bound "
                        f"{bound:.2f}."
                    ),
                    "affected_artifacts": [],
                    "proposed_remediation": (
                        f"Review the upstream stage that drives {key} before "
                        "setting baseline."
                    ),
                    "github_issue_url": None,
                }
            )
    return findings


def _findings_from_orchestration_failures(
    sdl_root: Optional[Path],
) -> List[Dict[str, Any]]:
    """Surface failed source_ids from the latest orchestration_run_record."""
    if sdl_root is None or not sdl_root.is_dir():
        return []
    target = sdl_root / "orchestration"
    if not target.is_dir():
        return []
    best: Optional[Tuple[str, Dict[str, Any]]] = None
    for path in target.glob("*.json"):
        if path.name.endswith(".invalid.json"):
            continue
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(obj, dict):
            continue
        ts = obj.get("completed_at") or obj.get("started_at") or ""
        if best is None or ts > best[0]:
            best = (ts, obj)
    if best is None:
        return []
    failed = best[1].get("source_ids_failed") or []
    if not isinstance(failed, list) or not failed:
        return []
    return [
        {
            "severity": "sev_1",
            "area": "pipeline",
            "title": "orchestration_source_ids_failed",
            "description": (
                f"{len(failed)} source_id(s) failed in the latest "
                "orchestration run."
            ),
            "affected_artifacts": [str(s) for s in failed],
            "proposed_remediation": (
                "Inspect orchestration_run_record.results entries with "
                "status=failure; rerun with the specific_source_id flag."
            ),
            "github_issue_url": None,
        }
    ]


def compile_findings(
    *,
    cycle_id: str,
    pipeline_state_record: Optional[Dict[str, Any]] = None,
    eval_summary: Optional[Dict[str, Any]] = None,
    meeting_extractions: Optional[List[Dict[str, Any]]] = None,
    sdl_root: Optional[Path] = None,
    produced_by: str = DEFAULT_PRODUCED_BY,
) -> Dict[str, Any]:
    """Build a verification_findings dict ready for write_verification_findings.

    Either pass artifacts directly, or pass ``sdl_root`` and let the
    compiler discover the latest pipeline_state_record + eval_summary on
    disk. Explicit args win over disk discovery.
    """
    if pipeline_state_record is None and sdl_root is not None:
        pipeline_state_record = _load_latest_pipeline_state_record(sdl_root)
    if eval_summary is None and sdl_root is not None:
        eval_summary = _load_latest_eval_summary(sdl_root)
    if meeting_extractions is None and sdl_root is not None:
        meeting_extractions = _load_meeting_extractions_from_sdl(sdl_root)
    meeting_extractions = meeting_extractions or []

    rates = compute_extraction_rates(meeting_extractions)

    findings: List[Dict[str, Any]] = []
    findings.extend(_findings_from_pipeline_state(pipeline_state_record))
    findings.extend(_findings_from_eval_summary(eval_summary))
    findings.extend(_findings_from_rates(rates))
    findings.extend(_findings_from_orchestration_failures(sdl_root))

    # next_required_actions: copy from the pipeline_state_record so the
    # findings artifact is self-contained (a reader doesn't have to
    # cross-reference two artifacts).
    next_actions: List[str] = []
    if isinstance(pipeline_state_record, dict):
        actions = pipeline_state_record.get("next_required_actions") or []
        if isinstance(actions, list):
            next_actions = [str(a) for a in actions if isinstance(a, str)]

    metrics_snapshot = _build_metrics_snapshot(eval_summary, rates)

    return {
        "verification_findings_id": str(uuid.uuid4()),
        "artifact_type": "verification_findings",
        "schema_version": SCHEMA_VERSION,
        "created_at": _now_iso(),
        "cycle_id": cycle_id,
        "pipeline_state_record_id": (
            pipeline_state_record.get("pipeline_state_record_id")
            if isinstance(pipeline_state_record, dict)
            else None
        ),
        "findings": findings,
        "metrics_snapshot": metrics_snapshot,
        "next_required_actions": next_actions,
        "provenance": {"produced_by": produced_by},
    }


def _build_metrics_snapshot(
    eval_summary: Optional[Dict[str, Any]],
    rates: Dict[str, Optional[float]],
) -> Dict[str, Any]:
    """Aggregate metrics into the snapshot used by verification_findings.

    Missing eval_summary => every eval-side field is None (schema allows
    null). Rates come from meeting_extractions and are None when their
    denominator is zero.
    """
    if isinstance(eval_summary, dict):
        ac = eval_summary.get("aggregate_coverage")
        ap = eval_summary.get("aggregate_precision")
        items = eval_summary.get("total_items_requiring_review")
        prw = eval_summary.get("partial_run_warning")
    else:
        ac = ap = items = prw = None
    return {
        "aggregate_coverage": _safe_number(ac),
        "aggregate_precision": _safe_number(ap),
        "items_requiring_review_total": _safe_int(items),
        "partial_run_warning": _safe_bool(prw),
        "regulatory_verb_fallback_rate": rates.get(
            "regulatory_verb_fallback_rate"
        ),
        "human_dedup_rate": rates.get("human_dedup_rate"),
    }


def _safe_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    return None


def write_verification_findings(
    record: Dict[str, Any], *, sdl_root: Path
) -> Optional[Path]:
    """Validate against schema, write under ``$SDL_ROOT/verifications/``."""
    schema_path = (
        _contracts_root()
        / "schemas"
        / "verification"
        / "verification_findings.schema.json"
    )
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        schema = None

    target_dir = sdl_root / "verifications"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    target = target_dir / f"{record['verification_findings_id']}.json"
    if schema is not None:
        try:
            jsonschema.Draft202012Validator(schema).validate(record)
        except jsonschema.ValidationError:
            invalid = target.with_suffix(".invalid.json")
            try:
                invalid.write_text(
                    json.dumps(record, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            except OSError:
                pass
            return None
    try:
        target.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return None
    return target


def format_findings_markdown(record: Dict[str, Any]) -> str:
    """Render verification_findings as a Markdown report for Actions UI."""
    lines: List[str] = []
    lines.append("## compile-findings")
    lines.append("")
    lines.append(f"- Cycle: `{record.get('cycle_id', '')}`")
    lines.append(
        f"- Pipeline state record id: "
        f"`{record.get('pipeline_state_record_id', '')}`"
    )
    findings = record.get("findings") or []
    lines.append(f"- Findings: **{len(findings)}**")
    lines.append("")

    if findings:
        lines.append("### Findings")
        lines.append("")
        lines.append("| Severity | Area | Title |")
        lines.append("|----------|------|-------|")
        for f in findings:
            lines.append(
                f"| {f.get('severity', '')} | {f.get('area', '')} | "
                f"{f.get('title', '')} |"
            )
        lines.append("")

    metrics = record.get("metrics_snapshot") or {}
    if metrics:
        lines.append("### Metrics snapshot")
        lines.append("")
        for k, v in sorted(metrics.items()):
            lines.append(f"- `{k}`: {v}")
        lines.append("")

    actions = record.get("next_required_actions") or []
    if actions:
        lines.append("### Next required actions")
        lines.append("")
        for a in actions:
            lines.append(f"- [ ] {a}")
        lines.append("")
    return "\n".join(lines) + "\n"
