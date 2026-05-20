"""Phase 3 operator helper — print the latest comparison F1 + delta.

Not CI-triggered. Invoked by ``scripts/run_glossary_measurement.sh``
after the operator dispatches the extraction + comparison workflows;
the helper walks the data lake for the latest
``comparison_result__*.json`` belonging to ``--source-id`` and prints:

* F1 / recall / precision and the delta against ``--baseline-f1``;
* the extraction artifact's ``extraction_config.glossary_version_hash``
  and ``glossary_tokens_added`` so the operator can confirm the
  glossary was actually injected (an empty hash is the headline signal
  that the wiring did not engage);
* the ``tainted_glossary_drift`` flag when set.

The script reads JSON from disk only; it never calls an LLM and never
mutates the data lake. ``DATA_LAKE_PATH`` is consulted when
``--data-lake`` is not passed (matching the production CLI's contract).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# scripts/ is not a package; allow direct ``python scripts/...`` usage
# from a fresh checkout without ``pip install -e .``.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from spectrum_systems_core.pipeline.governed_run import (  # noqa: E402
    validate_glossary_metadata_consistency,
    PipelineRunError,
)


def _meeting_dir(data_lake: Path, source_id: str) -> Path:
    return data_lake / "store" / "processed" / "meetings" / source_id


def _latest_comparison(meeting_dir: Path) -> Optional[Path]:
    candidates = sorted(meeting_dir.glob("comparison_result__*.json"))
    return candidates[-1] if candidates else None


def _latest_extraction(meeting_dir: Path) -> Optional[Path]:
    candidates = sorted(meeting_dir.glob("meeting_minutes__*.json"))
    return candidates[-1] if candidates else None


def _safe_load(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot read {path}: {exc}", file=sys.stderr)
        sys.exit(2)


def _extract_config(meeting_minutes: Dict[str, Any]) -> Dict[str, Any]:
    payload = meeting_minutes.get("payload") or {}
    provenance = payload.get("provenance") or {}
    return provenance.get("extraction_config") or {}


def _format_float(value: object) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):.4f}"
    return "n/a"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-id", required=True)
    parser.add_argument(
        "--baseline-f1",
        type=float,
        required=True,
        help="The F1 score (0.0-1.0) to compare against. Phase 3 uses 0.395.",
    )
    parser.add_argument(
        "--data-lake",
        default=os.environ.get("DATA_LAKE_PATH", ""),
        help=(
            "Path to the data lake root. Falls back to the "
            "DATA_LAKE_PATH environment variable. Required either way."
        ),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not args.data_lake:
        print(
            "ERROR: --data-lake not provided and DATA_LAKE_PATH not set",
            file=sys.stderr,
        )
        return 2
    data_lake = Path(args.data_lake)
    meeting_dir = _meeting_dir(data_lake, args.source_id)
    if not meeting_dir.is_dir():
        print(
            f"ERROR: meeting directory not found: {meeting_dir}",
            file=sys.stderr,
        )
        return 2

    cmp_path = _latest_comparison(meeting_dir)
    if cmp_path is None:
        print(
            f"ERROR: no comparison_result__*.json under {meeting_dir}",
            file=sys.stderr,
        )
        return 2

    extraction_path = _latest_extraction(meeting_dir)
    cmp_doc = _safe_load(cmp_path)
    extraction_doc = _safe_load(extraction_path) if extraction_path else {}

    summary = cmp_doc.get("summary") or {}
    f1 = summary.get("haiku_f1_vs_opus")
    recall = summary.get("haiku_recall_vs_opus")
    precision = summary.get("haiku_precision_vs_opus")
    delta: Optional[float] = None
    if isinstance(f1, (int, float)):
        delta = float(f1) - float(args.baseline_f1)

    ec = _extract_config(extraction_doc)
    # Sanity-check the present-together invariant. Print a warning when
    # it is violated rather than crash — the helper is operator-facing.
    try:
        validate_glossary_metadata_consistency(ec)
        consistency_ok = True
        consistency_msg = "ok"
    except PipelineRunError as exc:
        consistency_ok = False
        consistency_msg = exc.reason_code
    glossary_version_hash = ec.get("glossary_version_hash") or "(unset)"
    glossary_tokens_added = ec.get("glossary_tokens_added")
    tainted = cmp_doc.get("tainted_glossary_drift")
    if tainted is None:
        tainted = ec.get("tainted_glossary_drift")

    print(f"=== Glossary measurement for {args.source_id} ===")
    print(f"comparison_artifact: {cmp_path}")
    if extraction_path is not None:
        print(f"extraction_artifact: {extraction_path}")
    print(f"F1:        {_format_float(f1)} (baseline {args.baseline_f1:.4f})")
    print(f"Recall:    {_format_float(recall)}")
    print(f"Precision: {_format_float(precision)}")
    if delta is not None:
        print(f"Delta F1:  {delta:+.4f}")
    else:
        print("Delta F1:  n/a (no F1 in comparison summary)")
    print()
    print("=== Glossary provenance ===")
    print(f"glossary_version_hash: {glossary_version_hash}")
    print(f"glossary_tokens_added: {glossary_tokens_added}")
    print(f"tainted_glossary_drift: {tainted}")
    print(f"extraction_config consistency: {consistency_msg}")
    if not consistency_ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
