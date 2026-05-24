"""Phase 5 — tests for the A/B comparison artifact builder."""
from __future__ import annotations

import pytest

from spectrum_systems_core.comparison.ab_comparison import (
    ARTIFACT_TYPE_AB_COMPARISON,
    VARIANT_KEY_A,
    VARIANT_KEY_B,
    VARIANT_KEY_BASELINE,
    VARIANT_KEY_C,
    build_ab_comparison_artifact,
)


def _row(
    *,
    run_id: str,
    total_items: int = 100,
    grounded_items: int = 60,
    gate_drop_rate: float = 0.4,
    f1_vs_opus: float = 0.4,
    precision_vs_opus: float = 0.3,
    recall_vs_opus: float = 0.6,
    f1_vs_human: float | None = None,
) -> dict[str, object]:
    out: dict[str, object] = {
        "run_id": run_id,
        "total_items": total_items,
        "grounded_items": grounded_items,
        "gate_drop_rate": gate_drop_rate,
        "f1_vs_opus": f1_vs_opus,
        "precision_vs_opus": precision_vs_opus,
        "recall_vs_opus": recall_vs_opus,
    }
    if f1_vs_human is not None:
        out["f1_vs_human"] = f1_vs_human
    return out


def test_ab_artifact_has_all_variants() -> None:
    """The artifact MUST always carry all four variant keys.

    A missing key would force every downstream reader to special-case
    "is the key there at all?"; the contract is that every variant
    slot exists, even if its value is ``None`` for a non-run variant.
    """
    art = build_ab_comparison_artifact(
        source_id="src-1",
        baseline=_row(run_id="b"),
        variant_a=_row(run_id="a"),
        variant_b=_row(run_id="b2"),
        variant_c=_row(run_id="c"),
    )
    assert set(art["variants"].keys()) == {
        VARIANT_KEY_BASELINE,
        VARIANT_KEY_A,
        VARIANT_KEY_B,
        VARIANT_KEY_C,
    }


def test_ab_winner_computed_correctly() -> None:
    """Winner per metric MUST point at the highest-scoring variant.

    Constructs a row set where each variant wins a different metric so
    the winner block can be exhaustively checked. f1_vs_human is set
    only on variant_c so the picker correctly chooses it.
    """
    art = build_ab_comparison_artifact(
        source_id="src-1",
        baseline=_row(
            run_id="b",
            f1_vs_opus=0.30,
            precision_vs_opus=0.20,
            recall_vs_opus=0.90,
        ),
        variant_a=_row(
            run_id="a",
            f1_vs_opus=0.40,
            precision_vs_opus=0.30,
            recall_vs_opus=0.80,
        ),
        variant_b=_row(
            run_id="b2",
            f1_vs_opus=0.50,
            precision_vs_opus=0.60,
            recall_vs_opus=0.70,
        ),
        variant_c=_row(
            run_id="c",
            f1_vs_opus=0.45,
            precision_vs_opus=0.40,
            recall_vs_opus=0.60,
            f1_vs_human=0.55,
        ),
    )
    assert art["winner"]["by_f1_vs_opus"] == VARIANT_KEY_B
    assert art["winner"]["by_precision"] == VARIANT_KEY_B
    assert art["winner"]["by_recall"] == VARIANT_KEY_BASELINE
    assert art["winner"]["by_f1_vs_human"] == VARIANT_KEY_C


def test_ab_comparison_artifact_type_correct() -> None:
    """The artifact_type field is the named constant, no drift.

    The schema validator binds on this exact string; a typo would
    silently reject the artifact downstream.
    """
    art = build_ab_comparison_artifact(
        source_id="src-1",
        baseline=_row(run_id="b"),
        variant_a=None,
        variant_b=None,
        variant_c=None,
    )
    assert art["artifact_type"] == ARTIFACT_TYPE_AB_COMPARISON
    assert art["artifact_type"] == "ab_comparison"


def test_ab_comparison_handles_missing_variant_gracefully() -> None:
    """A failed-extraction variant lands as ``None`` rather than crashing.

    The measurement plan explicitly allows running variants
    independently, so the A/B artifact must combine whichever variants
    succeeded without erroring out on the ones that didn't.
    """
    art = build_ab_comparison_artifact(
        source_id="src-1",
        baseline=_row(run_id="b", f1_vs_opus=0.30),
        variant_a=None,
        variant_b=_row(run_id="b2", f1_vs_opus=0.40),
        variant_c=None,
    )
    assert art["variants"][VARIANT_KEY_A] is None
    assert art["variants"][VARIANT_KEY_C] is None
    # Winner picker MUST skip the None variants entirely.
    assert art["winner"]["by_f1_vs_opus"] == VARIANT_KEY_B
    # No variant carried f1_vs_human — picker returns None, not a guess.
    assert art["winner"]["by_f1_vs_human"] is None


def test_ab_comparison_rejects_partial_variant_row() -> None:
    """A row missing a required metric MUST fail at build time.

    Silent acceptance would let the winner picker rank against
    half-populated rows; we want the failure to surface at the
    artifact boundary.
    """
    with pytest.raises(ValueError, match="missing required fields"):
        build_ab_comparison_artifact(
            source_id="src-1",
            baseline={"run_id": "x"},  # everything else missing
            variant_a=None,
            variant_b=None,
            variant_c=None,
        )


def test_ab_comparison_rejects_empty_source_id() -> None:
    with pytest.raises(ValueError, match="source_id required"):
        build_ab_comparison_artifact(
            source_id="",
            baseline=None,
            variant_a=None,
            variant_b=None,
            variant_c=None,
        )


def test_winner_is_none_when_all_variants_missing_metric() -> None:
    """No variant has the metric → winner is None, not a fabricated pick."""
    art = build_ab_comparison_artifact(
        source_id="src-1",
        baseline=_row(run_id="b"),  # no f1_vs_human
        variant_a=_row(run_id="a"),
        variant_b=_row(run_id="b2"),
        variant_c=_row(run_id="c"),
    )
    assert art["winner"]["by_f1_vs_human"] is None
