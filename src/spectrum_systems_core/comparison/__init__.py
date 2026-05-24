"""Spectrum-systems-core comparison artifacts.

Module currently exposes the Phase 5 A/B comparison artifact builder.
"""
from .ab_comparison import (
    ALL_VARIANT_KEYS,
    ARTIFACT_TYPE_AB_COMPARISON,
    VARIANT_KEY_A,
    VARIANT_KEY_B,
    VARIANT_KEY_BASELINE,
    VARIANT_KEY_C,
    build_ab_comparison_artifact,
)

__all__ = [
    "ALL_VARIANT_KEYS",
    "ARTIFACT_TYPE_AB_COMPARISON",
    "VARIANT_KEY_A",
    "VARIANT_KEY_B",
    "VARIANT_KEY_BASELINE",
    "VARIANT_KEY_C",
    "build_ab_comparison_artifact",
]
