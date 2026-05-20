"""Phase 4 — Anthropic API cost estimator.

Estimates the USD cost of one extraction against a Claude model
given the input transcript's byte length and a default output-token
budget per model family. Used by ``baseline-opus --all --confirm-cost``
to print the operator-facing cost estimate before any API call.

Approach:

* Read per-model pricing from ``data/cost_constants.json`` (schema:
  ``cost_constants.schema.json``).
* Estimate input tokens as ``byte_length / 4`` (a deliberately
  conservative under-estimate; the real ratio averages 3.5 for
  English text — using 4 keeps the estimate from spiking on edge
  cases).
* Use a fixed default output-token budget per model family
  (:data:`DEFAULT_HAIKU_OUTPUT_TOKENS`, :data:`DEFAULT_OPUS_OUTPUT_TOKENS`).
* Multiply by the per-million-token price and return a
  :class:`decimal.Decimal` rounded to 6 decimal places (cents
  precision is too coarse for a 100KB transcript through Haiku).

The estimator is pure. Two calls with the same arguments return the
same Decimal. No env-var reads, no caching of disk reads in
module-level state — the constants are reloaded on every call so a
test can swap the file between calls.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional

import jsonschema

from ..schemas import schema_path


_REPO_ROOT: Path = Path(__file__).resolve().parents[3]
DEFAULT_COST_CONSTANTS_PATH: Path = _REPO_ROOT / "data" / "cost_constants.json"

# Default output-token budgets per family. Sized to the canonical
# meeting_minutes_llm.md / meeting_minutes_opus.md prompts: Haiku
# extractions average ~1.5K tokens; the Opus baseline allows more
# slack. The values are documented here (and in the rollback contract)
# so an operator can override them by passing explicit ``output_tokens``.
DEFAULT_HAIKU_OUTPUT_TOKENS: int = 2_000
DEFAULT_OPUS_OUTPUT_TOKENS: int = 10_000

# Conservative bytes-per-token ratio. English averages ~3.5; we use 4
# so the estimate never undershoots by enough to surprise the operator
# at the confirmation prompt.
_BYTES_PER_TOKEN: int = 4

# Precision for the returned Decimal. Six decimal places puts the
# Haiku estimate for a small transcript into a visible range without
# noise from floating-point arithmetic.
_OUTPUT_PLACES: int = 6


class CostConstantsError(ValueError):
    """Raised on a malformed ``cost_constants.json`` file."""


def _load_cost_schema() -> Dict[str, Any]:
    return json.loads(schema_path("cost_constants").read_text(encoding="utf-8"))


def load_cost_constants(
    path: Path | str | None = None,
) -> Dict[str, Any]:
    """Read and schema-validate ``cost_constants.json``.

    Raises :class:`CostConstantsError` on any schema violation. The
    function deliberately does NOT cache so a test can swap the file
    between calls.
    """
    p = Path(path) if path is not None else DEFAULT_COST_CONSTANTS_PATH
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CostConstantsError(
            f"cost_constants.json not found at {p}"
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CostConstantsError(
            f"cost_constants.json is not valid JSON: {exc}"
        ) from exc

    validator = jsonschema.Draft202012Validator(_load_cost_schema())
    try:
        validator.validate(data)
    except jsonschema.ValidationError as exc:
        raise CostConstantsError(
            f"cost_constants.json failed schema validation: "
            f"{exc.message} at path={list(exc.absolute_path)}"
        ) from exc
    return data


def _default_output_tokens(model_id: str) -> int:
    if "opus" in model_id:
        return DEFAULT_OPUS_OUTPUT_TOKENS
    return DEFAULT_HAIKU_OUTPUT_TOKENS


def estimate_extraction_cost(
    transcript_byte_length: int,
    model_id: str,
    *,
    output_tokens: Optional[int] = None,
    constants_path: Path | str | None = None,
) -> Decimal:
    """Estimate the USD cost of one extraction.

    Inputs:

    * ``transcript_byte_length`` — bytes of the input transcript. A
      ``ValueError`` is raised on a negative value (the estimator is
      called from operator-facing paths and a negative byte length is
      almost certainly a bug at the call site).
    * ``model_id`` — the Anthropic model identifier (must appear in
      ``data/cost_constants.json::constants``).
    * ``output_tokens`` — override the default per-family budget.
    * ``constants_path`` — override the on-disk constants file
      location (used by tests).

    Returns the estimate as a :class:`decimal.Decimal` quantized to
    six decimal places. The Decimal is the right type here because
    cent-precision arithmetic on floats produces visible rounding
    errors in the operator's summary line.
    """
    if not isinstance(transcript_byte_length, int) or transcript_byte_length < 0:
        raise ValueError(
            f"transcript_byte_length must be a non-negative int, "
            f"got {transcript_byte_length!r}"
        )

    constants = load_cost_constants(constants_path)
    if model_id not in constants["constants"]:
        raise CostConstantsError(
            f"no cost constants for model_id={model_id!r}; the "
            f"constants file knows about "
            f"{sorted(constants['constants'].keys())}"
        )

    pricing = constants["constants"][model_id]
    input_per_m = Decimal(str(pricing["input_per_million_tokens"]))
    output_per_m = Decimal(str(pricing["output_per_million_tokens"]))

    input_tokens = transcript_byte_length // _BYTES_PER_TOKEN
    out_tokens = (
        output_tokens
        if output_tokens is not None
        else _default_output_tokens(model_id)
    )
    if out_tokens < 0:
        raise ValueError(
            f"output_tokens must be non-negative, got {out_tokens!r}"
        )

    million = Decimal(1_000_000)
    cost = (
        Decimal(input_tokens) * input_per_m / million
        + Decimal(out_tokens) * output_per_m / million
    )
    quantize_unit = Decimal(10) ** (-_OUTPUT_PLACES)
    return cost.quantize(quantize_unit)


__all__ = [
    "CostConstantsError",
    "DEFAULT_COST_CONSTANTS_PATH",
    "DEFAULT_HAIKU_OUTPUT_TOKENS",
    "DEFAULT_OPUS_OUTPUT_TOKENS",
    "estimate_extraction_cost",
    "load_cost_constants",
]
