"""Phase 5 — A/B comparison artifact builder.

Combines per-variant comparison results into a single ``ab_comparison``
record so the operator sees baseline + A + B + C side-by-side in one
artifact. Pure function over already-computed variant rows: this module
does NOT run extractions or comparisons itself.

The output shape is intentionally narrow:

  {
    "artifact_type": "ab_comparison",
    "source_id": "...",
    "variants": {
        "baseline": {"run_id": "...", "total_items": ...,
                     "f1_vs_opus": ..., ...} | null,
        "variant_a": {...} | null,
        "variant_b": {...} | null,
        "variant_c": {...} | null,
    },
    "winner": {
        "by_f1_vs_opus": "<variant_key>" | null,
        "by_f1_vs_human": "<variant_key>" | null,
        "by_precision": "<variant_key>" | null,
        "by_recall": "<variant_key>" | null,
    },
  }

A null variant row marks a variant whose extraction or comparison
didn't complete; the winner computation skips nulls rather than
treating them as zero.
"""
from __future__ import annotations

from typing import Any, Final, Mapping

ARTIFACT_TYPE_AB_COMPARISON: Final[str] = "ab_comparison"

VARIANT_KEY_BASELINE: Final[str] = "baseline"
VARIANT_KEY_A: Final[str] = "variant_a"
VARIANT_KEY_B: Final[str] = "variant_b"
VARIANT_KEY_C: Final[str] = "variant_c"

ALL_VARIANT_KEYS: Final[tuple[str, ...]] = (
    VARIANT_KEY_BASELINE,
    VARIANT_KEY_A,
    VARIANT_KEY_B,
    VARIANT_KEY_C,
)

REQUIRED_VARIANT_METRICS: Final[tuple[str, ...]] = (
    "run_id",
    "total_items",
    "grounded_items",
    "gate_drop_rate",
    "f1_vs_opus",
    "precision_vs_opus",
    "recall_vs_opus",
)

OPTIONAL_VARIANT_METRICS: Final[tuple[str, ...]] = ("f1_vs_human",)


def _validate_variant_row(
    variant_key: str, row: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """Validate one variant row.

    Returns a defensive copy of the row, or ``None`` if the row is
    ``None`` (the "variant did not run / could not be measured" case).
    Raises ``ValueError`` on a non-None row missing required fields —
    silent acceptance of a partial row would make the winner picker
    rank against missing data.
    """
    if row is None:
        return None
    if not isinstance(row, Mapping):
        raise ValueError(
            f"variant {variant_key!r} row must be a mapping or None, "
            f"got {type(row).__name__}"
        )
    missing = [k for k in REQUIRED_VARIANT_METRICS if k not in row]
    if missing:
        raise ValueError(
            f"variant {variant_key!r} row missing required fields: {missing}"
        )
    out: dict[str, Any] = {k: row[k] for k in REQUIRED_VARIANT_METRICS}
    for k in OPTIONAL_VARIANT_METRICS:
        if k in row:
            out[k] = row[k]
    return out


def _pick_winner(
    variants: Mapping[str, dict[str, Any] | None], metric: str
) -> str | None:
    """Return the variant key with the highest value for ``metric``.

    Skips ``None`` variants and variants where the metric is absent or
    not a finite number. Returns ``None`` if no variant has a valid
    value, so the operator can tell "this comparison didn't measure
    metric X" apart from "baseline won metric X".
    """
    best_key: str | None = None
    best_val: float | None = None
    for key in ALL_VARIANT_KEYS:
        row = variants.get(key)
        if row is None:
            continue
        v = row.get(metric)
        if not isinstance(v, (int, float)):
            continue
        if v != v:  # NaN
            continue
        if best_val is None or float(v) > best_val:
            best_val = float(v)
            best_key = key
    return best_key


def _pick_recall_winner(
    variants: Mapping[str, dict[str, Any] | None],
) -> str | None:
    """Recall winner.

    Kept as a dedicated function (rather than _pick_winner) so the call
    site in :func:`build_ab_comparison_artifact` reads as "by_recall"
    instead of an opaque string argument.
    """
    return _pick_winner(variants, "recall_vs_opus")


def build_ab_comparison_artifact(
    *,
    source_id: str,
    baseline: Mapping[str, Any] | None,
    variant_a: Mapping[str, Any] | None,
    variant_b: Mapping[str, Any] | None,
    variant_c: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Produce the ab_comparison artifact body.

    Pure function — no I/O. Caller is responsible for writing the
    result to the data lake under the comparison_results path.
    """
    if not source_id:
        raise ValueError("source_id required")

    variants: dict[str, dict[str, Any] | None] = {
        VARIANT_KEY_BASELINE: _validate_variant_row(VARIANT_KEY_BASELINE, baseline),
        VARIANT_KEY_A: _validate_variant_row(VARIANT_KEY_A, variant_a),
        VARIANT_KEY_B: _validate_variant_row(VARIANT_KEY_B, variant_b),
        VARIANT_KEY_C: _validate_variant_row(VARIANT_KEY_C, variant_c),
    }

    winner = {
        "by_f1_vs_opus": _pick_winner(variants, "f1_vs_opus"),
        "by_f1_vs_human": _pick_winner(variants, "f1_vs_human"),
        "by_precision": _pick_winner(variants, "precision_vs_opus"),
        "by_recall": _pick_recall_winner(variants),
    }

    return {
        "artifact_type": ARTIFACT_TYPE_AB_COMPARISON,
        "source_id": source_id,
        "variants": variants,
        "winner": winner,
    }


__all__ = [
    "ALL_VARIANT_KEYS",
    "ARTIFACT_TYPE_AB_COMPARISON",
    "OPTIONAL_VARIANT_METRICS",
    "REQUIRED_VARIANT_METRICS",
    "VARIANT_KEY_A",
    "VARIANT_KEY_B",
    "VARIANT_KEY_BASELINE",
    "VARIANT_KEY_C",
    "build_ab_comparison_artifact",
]
