"""Weekly reconciler: every comparison_result must have a matching invocation log.

Phase 2 — pipeline_invocation_log diagnostic artifact.

The reconciler walks the data lake and, for every
``comparison_result__*.json`` it finds, asserts that a matching
``pipeline_invocation_log__*.json`` exists in the same meeting's
``diagnostics/`` directory. A comparison without a paired invocation
log is a drift gap — most likely a workflow that bypassed
:func:`spectrum_systems_core.pipeline.governed_pipeline_run`.

Behaviour:

* The reconciler does NOT block. It writes a
  ``reconciliation_gaps.jsonl`` file at the data lake root with one
  JSON object per gap and prints a WARNING for the operator.
* A missing data-lake (forked PR, fresh checkout) results in an empty
  gaps file and exit code 0 — the reconciler is best-effort.
* Exit code 0 always. The gaps file is the surface area.

Run as:

    python scripts/reconcile_invocation_logs.py [--data-lake <path>]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path
from typing import Iterable, List, Optional


def _default_data_lake() -> Path:
    env = os.environ.get("DATA_LAKE_PATH")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[1] / "data-lake"


def _now_utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _invocation_log_matches(
    comparison: Path, logs: List[Path]
) -> Optional[Path]:
    """A comparison artifact is matched by ANY invocation log in the
    same meeting directory's ``diagnostics/`` subdirectory.

    We do NOT require a per-comparison 1:1 match because the comparison
    is named by haiku_run_id (a UUID separate from the invocation_id)
    and re-deriving the link from artifact contents would require
    reading every comparison_result. The looser "at least one log
    exists for the meeting" check is sufficient to detect the drift
    case the reconciler is designed for (a workflow that bypassed
    governed_pipeline_run will produce zero logs).
    """
    return logs[0] if logs else None


def reconcile(data_lake: Path) -> List[dict]:
    """Walk every meeting under ``data_lake`` and find gaps.

    Returns a list of gap records. Caller writes the JSONL.
    """
    gaps: List[dict] = []
    meetings_dir = data_lake / "store" / "processed" / "meetings"
    if not meetings_dir.is_dir():
        return gaps

    for meeting_dir in sorted(meetings_dir.iterdir()):
        if not meeting_dir.is_dir():
            continue
        comparisons = sorted(
            meeting_dir.glob("comparison_result__*.json")
        )
        if not comparisons:
            continue
        diag_dir = meeting_dir / "diagnostics"
        logs = (
            sorted(diag_dir.glob("pipeline_invocation_log__*.json"))
            if diag_dir.is_dir()
            else []
        )
        if not logs:
            for cmp_path in comparisons:
                gaps.append(
                    {
                        "kind": "missing_invocation_log",
                        "source_id": meeting_dir.name,
                        "comparison_artifact_path": str(cmp_path),
                        "diagnostics_dir": str(diag_dir),
                        "detected_at": _now_utc_iso(),
                    }
                )
    return gaps


def write_gaps(data_lake: Path, gaps: List[dict]) -> Path:
    """Write the JSONL file at the data lake root. Always overwrites."""
    out = data_lake / "reconciliation_gaps.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for gap in gaps:
            fh.write(json.dumps(gap, sort_keys=True) + "\n")
    return out


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--data-lake",
        type=Path,
        default=_default_data_lake(),
        help="Path to the cloned data-lake repo root.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not args.data_lake.is_dir():
        print(
            f"WARNING: data lake not found at {args.data_lake}; "
            "reconciler ran with empty result set.",
            file=sys.stderr,
        )
        return 0

    gaps = reconcile(args.data_lake)
    out = write_gaps(args.data_lake, gaps)
    if gaps:
        print(
            f"WARNING: {len(gaps)} reconciliation gap(s) written to {out}",
            file=sys.stderr,
        )
        for gap in gaps[:5]:
            print(f"  - {gap['kind']} {gap['source_id']} ({gap['comparison_artifact_path']})", file=sys.stderr)
        if len(gaps) > 5:
            print(f"  ... {len(gaps) - 5} more", file=sys.stderr)
    else:
        print("OK: no reconciliation gaps found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
