"""Phase 5 — operator-facing three-way comparison readout.

Reads the latest three-way ``comparison_result`` artifact for a
source and prints F1, recall, precision for Haiku and Sonnet against
the Opus ceiling (item count). The delta of Sonnet vs Haiku is the
key measurement; the script labels each row with its
``prompt_variant`` so the apples-to-apples vs. unconstrained-
capability distinction is unambiguous from the output alone.

Usage::

    python scripts/print_three_way_delta.py \
        --source-id <source_id> \
        [--variant haiku-prompt|opus-prompt] \
        [--data-lake <path>]

When ``--data-lake`` is omitted, ``DATA_LAKE_PATH`` from the
environment is used (matching the convention every other CLI in
this repo follows).

Exit codes:

* ``0``  -- artifact found, formatted, printed.
* ``1``  -- artifact not found / unreadable; no fallback.
* ``2``  -- bad arguments.

This script is observe-only. It NEVER writes to the data lake.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional


_VARIANT_LABEL = {
    "haiku-prompt": "haiku_prompt_with_sonnet_model",
    "opus-prompt": "opus_prompt_with_sonnet_model",
}


def _meeting_dir(data_lake: Path, source_id: str) -> Path:
    return data_lake / "store" / "processed" / "meetings" / source_id


def _latest_three_way_artifact(
    data_lake: Path, source_id: str
) -> Optional[Path]:
    """Return the newest three-way comparison artifact path or None."""
    cmp_dir = _meeting_dir(data_lake, source_id) / "comparisons"
    if not cmp_dir.is_dir():
        return None
    candidates = sorted(cmp_dir.glob("three_way_*.json"))
    if not candidates:
        return None
    # Newest by name (timestamp prefix is sortable lexicographically).
    return candidates[-1]


def _format_pct(x: Any) -> str:
    if isinstance(x, (int, float)):
        return f"{float(x) * 100:.1f}%"
    return "—"


def _format_int(x: Any) -> str:
    if isinstance(x, int):
        return str(x)
    return "—"


def render(artifact: Dict[str, Any], variant_label: Optional[str]) -> str:
    """Format the three-way readout. Pure function; tested directly."""
    haiku_summary = artifact.get("haiku_summary", {}) or {}
    sonnet_summary = artifact.get("sonnet_summary", {}) or {}
    haiku_variant = artifact.get("haiku_prompt_variant", "production_haiku")
    sonnet_variant = artifact.get("sonnet_prompt_variant", "production_haiku")

    # Opus ceiling = total_opus_items from either summary (both
    # diffs run against the same Opus baseline).
    opus_count = (
        haiku_summary.get("total_opus_items")
        if isinstance(haiku_summary.get("total_opus_items"), int)
        else sonnet_summary.get("total_opus_items")
    )

    haiku_f1 = haiku_summary.get("haiku_f1_vs_opus")
    haiku_recall = haiku_summary.get("haiku_recall_vs_opus")
    haiku_precision = haiku_summary.get("haiku_precision_vs_opus")

    # The Sonnet summary reuses the ``haiku_*`` field names by
    # construction — only the TOP-LEVEL key (``sonnet_summary``) is
    # different. See `build_three_way_comparison_artifact` docstring.
    sonnet_f1 = sonnet_summary.get("haiku_f1_vs_opus")
    sonnet_recall = sonnet_summary.get("haiku_recall_vs_opus")
    sonnet_precision = sonnet_summary.get("haiku_precision_vs_opus")

    delta_f1 = (
        (sonnet_f1 - haiku_f1)
        if isinstance(sonnet_f1, (int, float))
        and isinstance(haiku_f1, (int, float))
        else None
    )

    lines = []
    lines.append(f"source_id: {artifact.get('source_id', '?')}")
    lines.append(f"compared_at: {artifact.get('compared_at', '?')}")
    if variant_label:
        lines.append(f"requested variant: {variant_label}")
    lines.append("")
    lines.append(
        "candidate              | variant                          | items |   F1   | recall | precision"
    )
    lines.append("-" * 95)
    lines.append(
        "haiku (production)     | "
        f"{haiku_variant:<32s} | "
        f"{_format_int(haiku_summary.get('total_haiku_items')):>5s} | "
        f"{_format_pct(haiku_f1):>6s} | "
        f"{_format_pct(haiku_recall):>6s} | "
        f"{_format_pct(haiku_precision):>6s}"
    )
    lines.append(
        "sonnet                 | "
        f"{sonnet_variant:<32s} | "
        f"{_format_int(sonnet_summary.get('total_haiku_items')):>5s} | "
        f"{_format_pct(sonnet_f1):>6s} | "
        f"{_format_pct(sonnet_recall):>6s} | "
        f"{_format_pct(sonnet_precision):>6s}"
    )
    lines.append(
        "opus (ceiling)         | "
        f"{'opus_baseline':<32s} | "
        f"{_format_int(opus_count):>5s} |    —   |    —   |    —"
    )
    lines.append("")
    if delta_f1 is not None:
        sign = "+" if delta_f1 >= 0 else ""
        lines.append(
            f"delta (sonnet - haiku) F1: {sign}{delta_f1 * 100:.1f} pts"
        )
        if delta_f1 < 0.10:
            lines.append("interpretation: Sonnet < +10pts of Haiku — model swap is unlikely to justify cascade")
        elif delta_f1 < 0.30:
            lines.append("interpretation: Sonnet 10-30pts ahead — cascade with Sonnet as filter is the right next investment")
        else:
            lines.append("interpretation: Sonnet > +30pts ahead — consider switching primary to Sonnet before building cascade")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-id", required=True)
    parser.add_argument(
        "--variant",
        choices=["haiku-prompt", "opus-prompt"],
        default=None,
        help=(
            "Operator-facing label for the Sonnet variant just measured. "
            "Optional — the printed table reads `sonnet_prompt_variant` "
            "off the artifact regardless. Only the header banner uses "
            "this argument."
        ),
    )
    parser.add_argument(
        "--data-lake",
        default=os.environ.get("DATA_LAKE_PATH", ""),
        help=(
            "Path to the data lake root. Defaults to $DATA_LAKE_PATH."
        ),
    )
    args = parser.parse_args(argv)

    if not args.data_lake:
        print(
            "ERROR: --data-lake not provided and DATA_LAKE_PATH not set",
            file=sys.stderr,
        )
        return 2
    data_lake = Path(args.data_lake)
    if not data_lake.is_dir():
        print(
            f"ERROR: data lake path does not exist: {data_lake}",
            file=sys.stderr,
        )
        return 1

    artifact_path = _latest_three_way_artifact(data_lake, args.source_id)
    if artifact_path is None:
        print(
            f"ERROR: no three-way comparison artifact found for "
            f"source_id={args.source_id} under "
            f"{_meeting_dir(data_lake, args.source_id) / 'comparisons'}",
            file=sys.stderr,
        )
        return 1

    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"ERROR: cannot read artifact at {artifact_path}: {exc}",
            file=sys.stderr,
        )
        return 1

    variant_label = (
        _VARIANT_LABEL.get(args.variant) if args.variant else None
    )
    print(render(artifact, variant_label))
    print("")
    print(f"artifact: {artifact_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
