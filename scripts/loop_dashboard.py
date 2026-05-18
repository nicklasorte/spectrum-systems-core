"""Phase Z.2 — loop observability dashboard.

Read-only. No writes. No model calls. Renders, in one plain-text
screen, the current state of the self-improvement loop for one
transcript by reading the most recently produced instrument of each
type from the data-lake.

The per-type table enumerates the canonical 23-type extraction
vocabulary (``create_opus_reference_baselines.extraction_types()`` —
the meeting_minutes-schema-derived list; reused, never re-defined, per
the CLAUDE.md taxonomy rule). The Phase Z spec text says "21 schema
types"; no 21-element canonical list exists in the codebase, so the
operator-confirmed decision is to enumerate the real 23-type list and
record the deviation in the PR. Types absent from the latest
comparison render with status ``X`` / "no ceiling items".

Red-team Pass 1 #2: when NO instrument exists for the transcript the
dashboard prints ``no cycle has run yet for {transcript_id}`` and
exits 0 — it never crashes on an empty data-lake.
"""
from __future__ import annotations

import argparse
import datetime
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
    iter_instruments,
    latest_instrument,
    produced_at_of,
)

import create_opus_reference_baselines as _crb  # noqa: E402

STALE_HOURS = 24


def _parse_iso(value: str) -> datetime.datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _staleness_tag(
    envelope: dict[str, Any], now: datetime.datetime
) -> str:
    """``[STALE: Nh]`` when produced_at is > 24h before ``now``."""
    dt = _parse_iso(produced_at_of(envelope))
    if dt is None:
        return ""
    delta_h = (now - dt).total_seconds() / 3600.0
    if delta_h > STALE_HOURS:
        return f" [STALE: {int(round(delta_h))}h]"
    return ""


def _payload(
    envelope: dict[str, Any] | None, expected_type: str
) -> dict[str, Any] | None:
    """Validated payload, or ``None`` (with a printed warning) when the
    artifact drifted from its schema. A read-only dashboard degrades —
    it never hard-crashes — but it also never reads fields off an
    artifact that failed its contract (CLAUDE.md read-path rule)."""
    if envelope is None:
        return None
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return None
    try:
        validate_artifact(payload, expected_type)
    except ArtifactValidationError as exc:
        print(f"  ! {expected_type} failed schema validation: {exc}")
        return None
    return payload


def _status_glyph(f1: float | None, ceiling_count: int) -> str:
    """Spec-mandated status symbols (Phase Z.2):
    U+2713 (>=0.70) / U+26A0 (0.50-0.69) / U+2717 (<0.50 or no ceiling)."""
    if ceiling_count <= 0 or f1 is None:
        return "✗"
    if f1 >= 0.70:
        return "✓"
    if f1 >= 0.50:
        return "⚠"
    return "✗"


def render(transcript_id: str, store: Path | None) -> int:
    now = datetime.datetime.now(datetime.timezone.utc)
    lines: list[str] = []

    if store is None:
        print(
            "error: DATA_LAKE_PATH not set or does not exist; "
            "dashboard is read-only and has nothing to show"
        )
        return 0

    cycle_env = latest_instrument(
        store, transcript_id, "improvement_cycle_result"
    )
    dec18_env = latest_instrument(store, transcript_id, "dec18_run_report")
    cmp_env = latest_instrument(
        store, transcript_id, "extraction_alignment_comparison"
    )
    fn_env = latest_instrument(store, transcript_id, "false_negative_set")
    cand_envs = iter_instruments(
        store, transcript_id, "correction_candidate"
    )
    eval_envs = iter_instruments(
        store, transcript_id, "candidate_evaluation"
    )

    if not any(
        [cycle_env, dec18_env, cmp_env, fn_env, cand_envs, eval_envs]
    ):
        print(f"no cycle has run yet for {transcript_id}")
        return 0

    cycle = _payload(cycle_env, "improvement_cycle_result")
    dec18 = _payload(dec18_env, "dec18_run_report")
    cmp_p = _payload(cmp_env, "extraction_alignment_comparison")
    fn_p = _payload(fn_env, "false_negative_set")

    lines.append("=" * 72)
    lines.append(f"LOOP DASHBOARD — {transcript_id}")
    lines.append(f"rendered_at: {now.strftime('%Y-%m-%dT%H:%M:%S+00:00')}")
    lines.append("=" * 72)

    # Artifact recency / staleness ----------------------------------------
    lines.append("")
    lines.append("ARTIFACTS")
    for label, env in (
        ("improvement_cycle_result", cycle_env),
        ("dec18_run_report", dec18_env),
        ("extraction_alignment_comparison", cmp_env),
        ("false_negative_set", fn_env),
    ):
        if env is None:
            lines.append(f"  {label}: (none)")
        else:
            lines.append(
                f"  {label}: produced_at="
                f"{produced_at_of(env) or '<unknown>'}"
                f"{_staleness_tag(env, now)}"
            )
    lines.append(
        f"  correction_candidate: {len(cand_envs)} found"
    )
    lines.append(
        f"  candidate_evaluation: {len(eval_envs)} found"
    )

    # Ceiling item count ---------------------------------------------------
    per_type_metrics: dict[str, Any] = {}
    if cmp_p is not None:
        ptm = cmp_p.get("per_type_metrics")
        if isinstance(ptm, dict):
            per_type_metrics = ptm
    ceiling_item_count = 0
    if dec18 is not None and isinstance(
        dec18.get("ceiling_item_count"), int
    ):
        ceiling_item_count = dec18["ceiling_item_count"]
    else:
        ceiling_item_count = sum(
            int(m.get("ceiling_count", 0))
            for m in per_type_metrics.values()
            if isinstance(m, dict)
        )
    lines.append("")
    lines.append(f"CEILING ITEM COUNT: {ceiling_item_count}")

    # Total F1 + per-type table -------------------------------------------
    total_f1: float | None = None
    if cmp_p is not None:
        tm = cmp_p.get("total_metrics")
        if isinstance(tm, dict) and isinstance(
            tm.get("f1"), (int, float)
        ):
            total_f1 = float(tm["f1"])
    lines.append("")
    lines.append(
        "TOTAL F1: "
        + ("n/a" if total_f1 is None else f"{total_f1:.4f}")
    )
    lines.append("")
    lines.append("PER-TYPE F1")
    header = (
        f"  {'schema_type':<34} {'ceil':>5} {'haiku':>6} "
        f"{'f1':>7} status"
    )
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    schema_types = _crb.extraction_types()
    for st in schema_types:
        m = per_type_metrics.get(st)
        if isinstance(m, dict):
            cc = int(m.get("ceiling_count", 0))
            hc = int(m.get("haiku_count", 0))
            f1 = m.get("f1")
            f1f = float(f1) if isinstance(f1, (int, float)) else None
        else:
            cc, hc, f1f = 0, 0, None
        f1s = "  n/a " if f1f is None else f"{f1f:6.4f}"
        lines.append(
            f"  {st:<34} {cc:>5} {hc:>6} {f1s:>7} "
            f"{_status_glyph(f1f, cc)}"
        )
    lines.append(f"  ({len(schema_types)} schema types enumerated)")

    # False negatives by type ---------------------------------------------
    lines.append("")
    lines.append("FALSE NEGATIVES BY TYPE")
    if fn_p is None:
        lines.append("  (no false_negative_set)")
    else:
        fns = fn_p.get("false_negatives", [])
        by_type: dict[str, int] = {}
        for fn in fns:
            st = fn.get("schema_type", "unknown")
            by_type[st] = by_type.get(st, 0) + 1
        lines.append(f"  total: {len(fns)}")
        for st in sorted(by_type):
            lines.append(f"    {st:<34} {by_type[st]}")

    # Correction candidates -----------------------------------------------
    lines.append("")
    lines.append(f"CORRECTION CANDIDATES: {len(cand_envs)}")
    eval_by_cand: dict[str, float] = {}
    for ev in eval_envs:
        ep = _payload(ev, "candidate_evaluation")
        if ep is None:
            continue
        cid = ep.get("candidate_id")
        delta = ep.get("target_delta_f1")
        if isinstance(cid, str) and isinstance(delta, (int, float)):
            eval_by_cand[cid] = float(delta)
    for env in cand_envs:
        cp = _payload(env, "correction_candidate")
        if cp is None:
            continue
        cid = cp.get("candidate_id") or cp.get("artifact_id") or "<?>"
        cluster = (
            cp.get("schema_type")
            or cp.get("cluster_schema_type")
            or cp.get("candidate_source")
            or "<unknown>"
        )
        addition = (
            cp.get("proposed_prompt_addition")
            or cp.get("prompt_addition")
            or cp.get("proposed_addition")
            or ""
        )
        preview = str(addition).replace("\n", " ")[:80]
        delta_txt = (
            f" delta_f1={eval_by_cand[cid]:+.4f}"
            if cid in eval_by_cand
            else " (not evaluated)"
        )
        tag = _staleness_tag(env, now)
        lines.append(
            f"  - [{cluster}] {cid}: \"{preview}\"{delta_txt}{tag}"
        )

    # Overall cycle status -------------------------------------------------
    lines.append("")
    if cycle is not None:
        lines.append(
            f"OVERALL CYCLE STATUS: {cycle.get('overall_status', '?')}"
            f" (blocking_phase={cycle.get('blocking_phase')})"
        )
    elif dec18 is not None:
        lines.append(
            "OVERALL CYCLE STATUS: dec18_run_report "
            f"control_decision={dec18.get('control_decision')}"
            f" halt_reason={dec18.get('halt_reason')}"
        )
    else:
        lines.append("OVERALL CYCLE STATUS: (no cycle / report artifact)")
    lines.append("=" * 72)

    print("\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase Z.2 read-only loop observability dashboard."
    )
    parser.add_argument(
        "--transcript",
        required=True,
        help="Transcript id to render (e.g. "
        "m-2025-12-18-7ghz-downlink-tig-kickoff).",
    )
    parser.add_argument(
        "--lake",
        default=None,
        help="Data-lake root (defaults to $DATA_LAKE_PATH).",
    )
    args = parser.parse_args(argv)
    store = data_lake_store_root(args.lake)
    return render(args.transcript, store)


if __name__ == "__main__":
    raise SystemExit(main())
