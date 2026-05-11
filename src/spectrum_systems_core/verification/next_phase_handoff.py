"""Phase P — next-phase-handoff.

Build a ``next_phase_briefing`` artifact from the latest verification state.
The briefing is intentionally self-contained: a reader can pick it up cold,
without cross-referencing pipeline_state_record / eval_summary on disk, and
seed the next planning cycle from the ``prompt_opening`` string alone.

Freshness: the briefing carries a ``valid_until`` timestamp.
``next-phase-handoff`` defaults to 24 hours. If the briefing is consumed
after ``valid_until``, the consumer is expected to re-run
``verify-pipeline-state`` + ``next-phase-handoff`` before planning.

Never raises.
"""
from __future__ import annotations

import datetime
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import jsonschema

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0.0"
PRODUCED_BY = "next-phase-handoff"
DEFAULT_FRESHNESS_HOURS = 24


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def _iso(ts: datetime.datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _contracts_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "contracts"
        if (candidate / "schemas").is_dir():
            return candidate
    return Path(__file__).resolve().parents[3] / "contracts"


def _load_latest(
    target_dir: Path,
    artifact_type: str,
) -> Optional[Dict[str, Any]]:
    """Return the most recent JSON artifact of the given type under target_dir."""
    if not target_dir.is_dir():
        return None
    best: Optional[Tuple[str, Dict[str, Any]]] = None
    for path in target_dir.glob("*.json"):
        if path.name.endswith(".invalid.json"):
            continue
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("artifact_type") != artifact_type:
            continue
        ts = obj.get("created_at") or ""
        if best is None or ts > best[0]:
            best = (ts, obj)
    return best[1] if best else None


def _load_latest_pipeline_state_record(
    sdl_root: Path,
) -> Optional[Dict[str, Any]]:
    return _load_latest(sdl_root / "verifications", "pipeline_state_record")


def _load_latest_eval_summary(sdl_root: Path) -> Optional[Dict[str, Any]]:
    return _load_latest(sdl_root / "evals", "eval_summary")


def _load_latest_verification_findings(
    sdl_root: Path,
) -> Optional[Dict[str, Any]]:
    return _load_latest(sdl_root / "verifications", "verification_findings")


def _safe_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _build_inventory_snapshot(
    pipeline_state: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    expected = (pipeline_state or {}).get("expected_artifacts") or {}
    return {
        "source_records": _safe_int(expected.get("source_record_count")),
        "minutes_records": _safe_int(expected.get("minutes_record_count")),
        "confirmed_pairs": _safe_int(expected.get("confirmed_pair_count")),
        "meeting_extractions": _safe_int(
            expected.get("meeting_extraction_count")
        ),
        "eval_results": _safe_int(expected.get("eval_result_count")),
        "baseline_set": bool(
            expected.get("baseline_eval_summary_present", False)
        ),
        "glossary_terms": _safe_int(expected.get("glossary_term_count")),
    }


def _build_metrics_snapshot(
    eval_summary: Optional[Dict[str, Any]],
    meeting_extractions: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Null when no eval_summary is present (red-team scenario 3)."""
    if not isinstance(eval_summary, dict):
        return None
    # Lazy import to avoid circular dependency on findings_compiler.
    from .findings_compiler import compute_extraction_rates  # type: ignore

    rates = compute_extraction_rates(meeting_extractions or [])
    total_extracted_items = 0
    for me in meeting_extractions or []:
        if not isinstance(me, dict):
            continue
        for key in ("decisions", "claims", "action_items"):
            seq = me.get(key)
            if isinstance(seq, list):
                total_extracted_items += len(seq)
    return {
        "aggregate_coverage": _safe_float(
            eval_summary.get("aggregate_coverage")
        ),
        "aggregate_precision": _safe_float(
            eval_summary.get("aggregate_precision")
        ),
        "items_requiring_review_total": _safe_int_or_none(
            eval_summary.get("total_items_requiring_review")
        ),
        "regulatory_verb_fallback_rate": rates.get(
            "regulatory_verb_fallback_rate"
        ),
        "human_dedup_rate": rates.get("human_dedup_rate"),
        "off_topic_rate": rates.get("off_topic_rate"),
        "total_extracted_items": total_extracted_items,
    }


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int_or_none(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _build_outstanding_findings(
    findings_record: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not isinstance(findings_record, dict):
        return []
    out: List[Dict[str, Any]] = []
    for f in findings_record.get("findings") or []:
        if not isinstance(f, dict):
            continue
        sev = f.get("severity")
        area = f.get("area")
        title = f.get("title")
        if sev not in ("sev_1", "sev_2", "sev_3"):
            continue
        if area not in ("pipeline", "eval", "migration", "schema", "other"):
            continue
        if not isinstance(title, str) or not title:
            continue
        out.append({"severity": sev, "area": area, "title": title})
    return out


def _build_next_required_actions(
    pipeline_state: Optional[Dict[str, Any]],
    findings_record: Optional[Dict[str, Any]],
) -> List[str]:
    """Prefer pipeline_state.next_required_actions; fall back to findings."""
    if isinstance(pipeline_state, dict):
        actions = pipeline_state.get("next_required_actions") or []
        if isinstance(actions, list) and actions:
            return [str(a) for a in actions if isinstance(a, str) and a]
    if isinstance(findings_record, dict):
        actions = findings_record.get("next_required_actions") or []
        if isinstance(actions, list) and actions:
            return [str(a) for a in actions if isinstance(a, str) and a]
    return []


def _format_prompt_opening(record: Dict[str, Any]) -> str:
    """Render the copy-paste-ready prompt opening for the next conversation."""
    cycle_id = record.get("cycle_id", "")
    created_at = record.get("created_at", "")
    valid_until = record.get("valid_until", "")
    inventory = record.get("inventory_snapshot") or {}
    metrics = record.get("metrics_snapshot")
    findings = record.get("outstanding_findings") or []
    actions = record.get("next_required_actions") or []
    pipeline_state_record_id = (
        record.get("pipeline_state_record_id") or "<missing>"
    )
    eval_summary_id = record.get("eval_summary_id") or "<missing>"

    lines: List[str] = []
    lines.append(
        f"## CURRENT STATE — Briefing from {cycle_id}"
    )
    lines.append("")
    lines.append(
        f"This briefing was generated at {created_at} and is valid until "
        f"{valid_until}. If you are reading this past valid_until, re-run "
        "verify-pipeline-state and next-phase-handoff before planning."
    )
    lines.append("")
    lines.append(
        f"### Inventory (from pipeline_state_record {pipeline_state_record_id})"
    )
    lines.append("")
    lines.append(f"- source_records: {inventory.get('source_records', 0)}")
    lines.append(f"- minutes_records: {inventory.get('minutes_records', 0)}")
    lines.append(f"- confirmed_pairs: {inventory.get('confirmed_pairs', 0)}")
    lines.append(
        f"- meeting_extractions: {inventory.get('meeting_extractions', 0)}"
    )
    lines.append(f"- eval_results: {inventory.get('eval_results', 0)}")
    lines.append(
        f"- baseline_set: {bool(inventory.get('baseline_set', False))}"
    )
    lines.append(f"- glossary_terms: {inventory.get('glossary_terms', 0)}")
    lines.append("")
    lines.append(f"### Metrics (from eval_summary {eval_summary_id})")
    lines.append("")
    if metrics is None:
        lines.append(
            "_no eval_summary present yet — metrics_snapshot is null. "
            "Run eval-ground-truth before relying on coverage/precision._"
        )
    else:
        lines.append(
            f"- aggregate_coverage: {metrics.get('aggregate_coverage')}"
        )
        lines.append(
            f"- aggregate_precision: {metrics.get('aggregate_precision')}"
        )
        lines.append(
            "- items_requiring_review_total: "
            f"{metrics.get('items_requiring_review_total')}"
        )
        lines.append(
            "- regulatory_verb_fallback_rate: "
            f"{metrics.get('regulatory_verb_fallback_rate')}"
        )
        lines.append(
            f"- human_dedup_rate: {metrics.get('human_dedup_rate')}"
        )
        lines.append(
            f"- off_topic_rate: {metrics.get('off_topic_rate')}"
        )
        lines.append(
            "- total_extracted_items: "
            f"{metrics.get('total_extracted_items')}"
        )
    lines.append("")
    lines.append("### Outstanding findings")
    lines.append("")
    if findings:
        for i, f in enumerate(findings, 1):
            lines.append(
                f"{i}. [{f.get('severity', '')}] {f.get('title', '')} "
                f"({f.get('area', '')})"
            )
    else:
        lines.append("_no outstanding findings recorded._")
    lines.append("")
    lines.append("### Next required actions")
    lines.append("")
    if actions:
        for i, a in enumerate(actions, 1):
            lines.append(f"{i}. {a}")
    else:
        lines.append("_no next_required_actions recorded._")
    lines.append("")
    lines.append(
        "Use this briefing as the seed for STEP 1 inventory in the next "
        "phase planning."
    )
    lines.append("")
    return "\n".join(lines)


def build_next_phase_briefing(
    *,
    cycle_id: str,
    freshness_window_hours: int = DEFAULT_FRESHNESS_HOURS,
    pipeline_state_record: Optional[Dict[str, Any]] = None,
    eval_summary: Optional[Dict[str, Any]] = None,
    verification_findings: Optional[Dict[str, Any]] = None,
    meeting_extractions: Optional[List[Dict[str, Any]]] = None,
    sdl_root: Optional[Path] = None,
    now: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Build a next_phase_briefing dict (unwritten).

    Explicit args win over disk discovery. ``sdl_root`` is used only to
    discover artifacts that were not passed in.
    """
    if pipeline_state_record is None and sdl_root is not None:
        pipeline_state_record = _load_latest_pipeline_state_record(sdl_root)
    if eval_summary is None and sdl_root is not None:
        eval_summary = _load_latest_eval_summary(sdl_root)
    if verification_findings is None and sdl_root is not None:
        verification_findings = _load_latest_verification_findings(sdl_root)
    if meeting_extractions is None and sdl_root is not None:
        # Lazy import to avoid circular dependency at module load.
        from .findings_compiler import (  # type: ignore
            _load_meeting_extractions_from_sdl,
        )

        meeting_extractions = _load_meeting_extractions_from_sdl(sdl_root)
    meeting_extractions = meeting_extractions or []

    if freshness_window_hours < 1:
        freshness_window_hours = DEFAULT_FRESHNESS_HOURS

    created_at_dt = now or _now_utc()
    valid_until_dt = created_at_dt + datetime.timedelta(
        hours=freshness_window_hours
    )

    record: Dict[str, Any] = {
        "next_phase_briefing_id": str(uuid.uuid4()),
        "artifact_type": "next_phase_briefing",
        "schema_version": SCHEMA_VERSION,
        "created_at": _iso(created_at_dt),
        "cycle_id": cycle_id,
        "freshness_window_hours": int(freshness_window_hours),
        "valid_until": _iso(valid_until_dt),
        "pipeline_state_record_id": (
            pipeline_state_record.get("pipeline_state_record_id")
            if isinstance(pipeline_state_record, dict)
            else None
        ),
        "pipeline_state_recorded_at": (
            pipeline_state_record.get("created_at")
            if isinstance(pipeline_state_record, dict)
            else None
        ),
        "eval_summary_id": (
            eval_summary.get("eval_summary_id")
            if isinstance(eval_summary, dict)
            else None
        ),
        "eval_summary_recorded_at": (
            eval_summary.get("created_at")
            if isinstance(eval_summary, dict)
            else None
        ),
        "verification_findings_id": (
            verification_findings.get("verification_findings_id")
            if isinstance(verification_findings, dict)
            else None
        ),
        "inventory_snapshot": _build_inventory_snapshot(pipeline_state_record),
        "metrics_snapshot": _build_metrics_snapshot(
            eval_summary, meeting_extractions
        ),
        "outstanding_findings": _build_outstanding_findings(
            verification_findings
        ),
        "next_required_actions": _build_next_required_actions(
            pipeline_state_record, verification_findings
        ),
        "prompt_opening": "",  # filled below
        "provenance": {"produced_by": PRODUCED_BY},
    }
    record["prompt_opening"] = _format_prompt_opening(record)
    return record


def write_next_phase_briefing(
    record: Dict[str, Any], *, sdl_root: Path
) -> Optional[Path]:
    """Validate against schema, write under ``$SDL_ROOT/verifications/briefings/``."""
    schema_path = (
        _contracts_root()
        / "schemas"
        / "verification"
        / "next_phase_briefing.schema.json"
    )
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        schema = None

    target_dir = sdl_root / "verifications" / "briefings"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    target = target_dir / f"{record['next_phase_briefing_id']}.json"
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
