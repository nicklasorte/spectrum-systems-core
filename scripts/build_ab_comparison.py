"""Phase 5 — build an ab_comparison artifact from per-variant runs.

Reads the latest ``comparison_result`` artifact for each variant's
run_id, pulls F1 / precision / recall, looks up the source
``meeting_minutes`` artifact to count items and grounding statistics,
and emits a single ``ab_comparison`` JSON file via
:func:`spectrum_systems_core.comparison.ab_comparison.build_ab_comparison_artifact`.

ZERO model calls. ZERO eval logic. Pure read-aggregate-write.

Usage:

    python scripts/build_ab_comparison.py \\
        --data-lake <path> \\
        --source-id <id> \\
        --run-id-baseline <run_id> \\
        --run-id-variant-a <run_id> \\
        --run-id-variant-b <run_id> \\
        --run-id-variant-c <run_id> \\
        --out <out.json>

Any --run-id-* flag may be empty (passed as ""). An empty/missing
variant input lands as ``null`` in the artifact; the winner picker
skips it.

The script honours the CLAUDE.md integration-test rule: every artifact
read goes through ``_artifact_validator.validate_artifact`` before any
field access.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Make scripts/_artifact_validator importable when run as a script.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _artifact_validator import (  # noqa: E402
    ArtifactValidationError,
    validate_artifact,
)

from spectrum_systems_core.comparison.ab_comparison import (  # noqa: E402
    build_ab_comparison_artifact,
)


def _meeting_dir(data_lake: Path, source_id: str) -> Path:
    """Resolve the per-meeting directory under the data lake.

    Mirrors compare_opus_haiku._meeting_dir to stay consistent with
    the rest of the codebase.
    """
    return data_lake / "store" / "processed" / "meetings" / source_id


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Corrupt artifact on disk — surface to the operator rather
        # than silently treating it as missing.
        sys.stderr.write(f"WARN: corrupt JSON at {path}; skipping\n")
        return None


def _find_meeting_minutes_for_run_id(
    meeting_dir: Path, run_id: str
) -> dict[str, Any] | None:
    """Find the meeting_minutes artifact whose trace_id matches run_id.

    Scans ``meeting_minutes__*.json`` directly under ``meeting_dir``.
    Returns the parsed artifact dict, or None if no match.
    """
    if not meeting_dir.is_dir():
        return None
    for candidate in sorted(meeting_dir.glob("meeting_minutes__*.json")):
        artifact = _read_json(candidate)
        if artifact is None:
            continue
        # meeting_minutes.schema.json describes the FLAT shape
        # ``{"artifact_type": "meeting_minutes", **payload}`` — the
        # on-disk envelope (with artifact_id / content_hash / ...) is
        # NOT directly validatable. Mirror the compare_opus_haiku
        # pattern: validate the flat form, then read trace_id /
        # artifact_id off the envelope.
        payload = artifact.get("payload")
        if not isinstance(payload, dict):
            continue
        flat = {"artifact_type": "meeting_minutes", **payload}
        try:
            validate_artifact(
                flat, "meeting_minutes", artifact_path=str(candidate)
            )
        except ArtifactValidationError:
            continue
        # trace_id is the run-level identifier in the envelope; some
        # callers also stamp run_id under provenance. Match either.
        if artifact.get("trace_id") == run_id:
            return artifact
        provenance = payload.get("provenance", {})
        if provenance.get("run_id") == run_id:
            return artifact
    return None


def _find_comparison_for_artifact(
    meeting_dir: Path, target_artifact_id: str | None, run_id: str
) -> dict[str, Any] | None:
    """Find the most recent comparison_result for the artifact / run_id.

    The comparison artifact references the haiku side via
    ``haiku_artifact_id`` / ``haiku_artifact_path`` / a nested
    ``haiku_run_id``. Match by artifact_id first (most specific),
    falling back to a substring search on the path / trace_id.
    """
    comparisons_dir = meeting_dir / "comparisons"
    if not comparisons_dir.is_dir():
        return None
    matches: list[tuple[str, dict[str, Any]]] = []
    for candidate in sorted(comparisons_dir.glob("*.json")):
        # Skip our own output kind so a stale ab_comparison can't
        # masquerade as the per-variant comparison.
        if candidate.name.startswith("ab_comparison__"):
            continue
        artifact = _read_json(candidate)
        if artifact is None:
            continue
        actual_type = artifact.get("artifact_type")
        if actual_type != "comparison_result":
            continue
        if _comparison_matches(artifact, target_artifact_id, run_id):
            matches.append((candidate.name, artifact))
    if not matches:
        return None
    # Most recent by filename — the existing comparisons are timestamp-
    # suffixed so lexicographic sort == chronological.
    matches.sort(key=lambda pair: pair[0])
    return matches[-1][1]


def _comparison_matches(
    comparison: dict[str, Any],
    target_artifact_id: str | None,
    run_id: str,
) -> bool:
    """Return True if ``comparison`` references the target run / artifact.

    Lenient — matches on any of the standard reference fields the
    comparison_result writer stamps.
    """
    if target_artifact_id:
        for k in ("haiku_artifact_id", "candidate_artifact_id"):
            if comparison.get(k) == target_artifact_id:
                return True
    # Fall back to trace_id / haiku_run_id / artifact path substring.
    for k in ("haiku_run_id", "candidate_run_id", "trace_id"):
        if comparison.get(k) == run_id:
            return True
    for k in ("haiku_artifact_path", "candidate_artifact_path"):
        v = comparison.get(k)
        if isinstance(v, str) and run_id and run_id in v:
            return True
    return False


def _extract_summary(comparison: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the summary block out of a comparison_result.

    Two shapes:
      * two-way: ``summary`` directly carries ``haiku_*`` metrics.
      * three-way: ``haiku_summary`` carries ``haiku_*``; the Sonnet
        side is irrelevant here.
    Returns whichever shape was present, normalised to the ``haiku_*``
    key set, or ``None`` if no summary is present.
    """
    s = comparison.get("summary")
    if isinstance(s, dict) and "haiku_f1_vs_opus" in s:
        return s
    hs = comparison.get("haiku_summary")
    if isinstance(hs, dict) and "haiku_f1_vs_opus" in hs:
        return hs
    return None


def _count_items_in_minutes(artifact: dict[str, Any]) -> int:
    """Sum array lengths of the claim-shaped product types.

    Intended only as a coarse "total items" headline; the exact total
    used by the grounding gate lives in the gate artifact when present.
    """
    payload = artifact.get("payload", {})
    types = (
        "decisions",
        "action_items",
        "open_questions",
        "commitments",
        "claims",
        "risks",
        "cross_references",
        "regulatory_references",
        "issue_registry_entry",
        "position_statement",
        "dissent_or_objection",
        "precedent_reference",
        "external_stakeholder_input",
        "procedural_ruling",
    )
    total = 0
    for t in types:
        v = payload.get(t)
        if isinstance(v, list):
            total += len(v)
    return total


def _grounding_stats(artifact: dict[str, Any]) -> tuple[int, float]:
    """Return ``(grounded_items, gate_drop_rate)`` if recorded.

    The promotion gate writes its result into
    ``payload.gate_result`` in newer artifacts; pre-Phase-4
    artifacts may omit it. Returns ``(total_items, 0.0)`` as the
    fall-back so the row stays well-typed.
    """
    payload = artifact.get("payload", {})
    gate = payload.get("gate_result") or payload.get("grounding_gate") or {}
    if isinstance(gate, dict):
        grounded = gate.get("grounded_items")
        total = gate.get("total_items")
        if isinstance(grounded, int) and isinstance(total, int) and total > 0:
            drop = 1.0 - grounded / total
            return grounded, round(drop, 4)
    return _count_items_in_minutes(artifact), 0.0


def _row_for_run_id(
    meeting_dir: Path, run_id: str
) -> dict[str, Any] | None:
    """Build one variant row by joining the minutes + comparison artifacts.

    Returns ``None`` (translated to ``null`` in the final artifact)
    when the run_id is empty or the minutes artifact can't be found.
    """
    if not run_id:
        return None
    minutes = _find_meeting_minutes_for_run_id(meeting_dir, run_id)
    if minutes is None:
        sys.stderr.write(
            f"WARN: no meeting_minutes artifact for run_id={run_id} "
            f"in {meeting_dir}; marking variant as null\n"
        )
        return None
    total = _count_items_in_minutes(minutes)
    grounded, drop_rate = _grounding_stats(minutes)
    artifact_id = minutes.get("artifact_id")
    comparison = _find_comparison_for_artifact(
        meeting_dir, target_artifact_id=artifact_id, run_id=run_id
    )
    f1 = precision = recall = float("nan")
    f1_vs_human = None
    if comparison is not None:
        s = _extract_summary(comparison)
        if s is not None:
            f1 = float(s.get("haiku_f1_vs_opus", "nan"))
            precision = float(s.get("haiku_precision_vs_opus", "nan"))
            recall = float(s.get("haiku_recall_vs_opus", "nan"))
        # Optional human-minutes metric — kept as None if absent so the
        # winner picker treats it as "not measured" rather than 0.0.
        h = comparison.get("haiku_summary_vs_human") or comparison.get(
            "summary_vs_human"
        )
        if isinstance(h, dict) and "f1" in h:
            try:
                f1_vs_human = float(h["f1"])
            except (TypeError, ValueError):
                f1_vs_human = None
    row: dict[str, Any] = {
        "run_id": run_id,
        "total_items": int(total),
        "grounded_items": int(grounded),
        "gate_drop_rate": float(drop_rate),
        "f1_vs_opus": float(f1) if f1 == f1 else 0.0,
        "precision_vs_opus": float(precision) if precision == precision else 0.0,
        "recall_vs_opus": float(recall) if recall == recall else 0.0,
    }
    if f1_vs_human is not None:
        row["f1_vs_human"] = f1_vs_human
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate per-variant comparison artifacts into one "
        "ab_comparison artifact"
    )
    parser.add_argument("--data-lake", required=True, help="Data-lake root path")
    parser.add_argument("--source-id", required=True, help="Transcript source id")
    parser.add_argument("--run-id-baseline", default="")
    parser.add_argument("--run-id-variant-a", default="")
    parser.add_argument("--run-id-variant-b", default="")
    parser.add_argument("--run-id-variant-c", default="")
    parser.add_argument(
        "--out",
        required=True,
        help="Where to write the ab_comparison artifact (JSON, UTF-8, "
        "sorted-key serialization for determinism)",
    )
    args = parser.parse_args(argv)

    data_lake = Path(args.data_lake)
    if not data_lake.is_dir():
        sys.stderr.write(
            f"ERR: --data-lake is not a directory: {data_lake}\n"
        )
        return 2

    meeting_dir = _meeting_dir(data_lake, args.source_id)
    if not meeting_dir.is_dir():
        sys.stderr.write(
            f"WARN: no meeting dir at {meeting_dir}; every variant row "
            "will be null\n"
        )

    baseline = _row_for_run_id(meeting_dir, args.run_id_baseline)
    variant_a = _row_for_run_id(meeting_dir, args.run_id_variant_a)
    variant_b = _row_for_run_id(meeting_dir, args.run_id_variant_b)
    variant_c = _row_for_run_id(meeting_dir, args.run_id_variant_c)

    artifact = build_ab_comparison_artifact(
        source_id=args.source_id,
        baseline=baseline,
        variant_a=variant_a,
        variant_b=variant_b,
        variant_c=variant_c,
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(artifact, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    sys.stdout.write(f"wrote {out_path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
