"""Phase 4 — cost estimation helpers.

Exposes :func:`estimate_extraction_cost` from
:mod:`spectrum_systems_core.cost.estimator`. The module is pure: no
LLM calls, no network access, no env-var reads.

Cost constants live in ``data/cost_constants.json`` (schema:
``cost_constants.schema.json``). The values are placeholders pending
operator verification against current Anthropic API pricing — the
PR description documents this.
"""
from __future__ import annotations

from .estimator import (
    DEFAULT_HAIKU_OUTPUT_TOKENS,
    DEFAULT_OPUS_OUTPUT_TOKENS,
    CostConstantsError,
    estimate_extraction_cost,
    load_cost_constants,
)

__all__ = [
    "CostConstantsError",
    "DEFAULT_HAIKU_OUTPUT_TOKENS",
    "DEFAULT_OPUS_OUTPUT_TOKENS",
    "estimate_extraction_cost",
    "load_cost_constants",
]
