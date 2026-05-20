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

Phase 6 additions:

* :func:`estimate_cascade_cost` — USD estimate for the Stage 2
  cascade pass that asks Sonnet to keep/drop each Haiku-extracted
  item. Sized to ~30-50 chunks for a 100-200 item extraction.
* :func:`estimate_extraction_cost_breakdown` — returns a structured
  ``(extraction_cost, cascade_filter_cost, total_cost)`` dict so the
  CLI can print both lines when ``--enable-cascade-filter`` is set.
  The legacy :func:`estimate_extraction_cost` keeps its single-Decimal
  signature (pre-Phase-6 callers are unaffected).
* :func:`load_cascade_confirmation_item_threshold` — read the
  schema-bounded ``[10, 500]`` threshold from
  ``data/cost_constants.json`` with a default fallback so a
  pre-Phase-6 constants file still works.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
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
DEFAULT_SONNET_OUTPUT_TOKENS: int = 4_000
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
    if "sonnet" in model_id:
        return DEFAULT_SONNET_OUTPUT_TOKENS
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


# ---------------------------------------------------------------------------
# Phase 6 — cascade-filter cost surface.
# ---------------------------------------------------------------------------

# Default per-chunk output token budget for the cascade filter response.
# Mirrors `cascade.executor.DEFAULT_PER_CHUNK_OUTPUT_TOKENS` — kept in
# sync via tests/cascade/test_cost_constants_sync.py.
DEFAULT_CASCADE_PER_CHUNK_OUTPUT_TOKENS: int = 800

# Conservative defaults for cascade sizing when the caller does not
# pin chunk counts. ~30-50 chunks for a 100-200 item extraction; we
# default to the midpoint (40) and let callers override.
DEFAULT_CASCADE_AVG_CHUNK_BYTES: int = 2_000
DEFAULT_CASCADE_CHUNK_COUNT: int = 40
DEFAULT_CASCADE_FILTER_MODEL: str = "claude-sonnet-4-6"

# Fallback for the cascade confirmation threshold when the constants
# file pre-dates Phase 6 (or when it was deliberately omitted). The
# default matches the value the schema documents (`50`).
_DEFAULT_CASCADE_CONFIRMATION_THRESHOLD: int = 50


def load_cascade_confirmation_item_threshold(
    constants_path: Path | str | None = None,
) -> int:
    """Read the cascade item-confirmation threshold from the constants file.

    Returns the integer threshold; falls back to
    :data:`_DEFAULT_CASCADE_CONFIRMATION_THRESHOLD` when the key is
    absent (pre-Phase-6 constants files). Raises
    :class:`CostConstantsError` on any schema violation in the loaded
    file — failing here is far cheaper than failing inside the cascade
    dispatch path.
    """
    constants = load_cost_constants(constants_path)
    raw = constants.get("cascade_confirmation_item_threshold")
    if raw is None:
        return _DEFAULT_CASCADE_CONFIRMATION_THRESHOLD
    return int(raw)


@dataclass(frozen=True)
class CostBreakdown:
    """Structured cost breakdown for an extraction with optional cascade.

    Fields:
      extraction_cost: USD cost of the Stage 1 (Haiku / Sonnet / Opus)
        extraction pass.
      cascade_filter_cost: USD cost of the Stage 2 cascade filter pass.
        ``Decimal("0")`` when the cascade is disabled — never ``None``,
        so a printer can sum without branching.
      total_cost: ``extraction_cost + cascade_filter_cost``.
    """

    extraction_cost: Decimal
    cascade_filter_cost: Decimal
    total_cost: Decimal

    def to_dict(self) -> Dict[str, str]:
        return {
            "extraction_cost": str(self.extraction_cost),
            "cascade_filter_cost": str(self.cascade_filter_cost),
            "total_cost": str(self.total_cost),
        }


def estimate_cascade_cost(
    haiku_items_count: int,
    *,
    avg_chunk_text_bytes: int = DEFAULT_CASCADE_AVG_CHUNK_BYTES,
    chunk_count: Optional[int] = None,
    per_chunk_output_tokens: int = DEFAULT_CASCADE_PER_CHUNK_OUTPUT_TOKENS,
    filter_model: str = DEFAULT_CASCADE_FILTER_MODEL,
    constants_path: Path | str | None = None,
) -> Decimal:
    """Estimate the USD cost of one cascade filter pass.

    Inputs:
      haiku_items_count: number of items in the upstream Haiku artifact.
        Used to derive a default chunk_count when one is not supplied
        (we assume the cascade groups roughly 4 items per chunk on
        average, matching the empirical Dec 18 baseline).
      avg_chunk_text_bytes: rough estimate of the transcript context
        attached to each per-chunk filter call (chunk text + per-item
        grounding context window).
      chunk_count: optional explicit override. When None we derive it
        from ``haiku_items_count`` clipped to [1, 200] so the estimate
        does not balloon on a runaway extraction.
      per_chunk_output_tokens: max_tokens budget per filter call.
      filter_model: pricing model id (must exist in
        ``data/cost_constants.json``).
      constants_path: test override for the constants file.

    Returns the estimate as a :class:`decimal.Decimal` quantized to
    six decimal places. Zero items in produces zero cost (no chunks
    need filtering).
    """
    if not isinstance(haiku_items_count, int) or haiku_items_count < 0:
        raise ValueError(
            f"haiku_items_count must be non-negative int, got "
            f"{haiku_items_count!r}"
        )
    if haiku_items_count == 0:
        return Decimal("0").quantize(Decimal(10) ** (-_OUTPUT_PLACES))

    if chunk_count is None:
        # Empirical: a 100-item extraction over a typical kickoff
        # transcript groups into ~25-30 chunks. Use a midpoint of
        # 4 items/chunk and clip so a 1000-item runaway does not
        # claim 250 chunks worth of filter cost.
        chunk_count = max(1, min(200, (haiku_items_count + 3) // 4))
    if chunk_count < 0:
        raise ValueError(f"chunk_count must be non-negative, got {chunk_count!r}")

    constants = load_cost_constants(constants_path)
    if filter_model not in constants["constants"]:
        raise CostConstantsError(
            f"no cost constants for filter_model={filter_model!r}; the "
            f"constants file knows about "
            f"{sorted(constants['constants'].keys())}"
        )
    pricing = constants["constants"][filter_model]
    input_per_m = Decimal(str(pricing["input_per_million_tokens"]))
    output_per_m = Decimal(str(pricing["output_per_million_tokens"]))

    per_chunk_input_tokens = avg_chunk_text_bytes // _BYTES_PER_TOKEN
    total_input_tokens = per_chunk_input_tokens * chunk_count
    total_output_tokens = per_chunk_output_tokens * chunk_count

    million = Decimal(1_000_000)
    cost = (
        Decimal(total_input_tokens) * input_per_m / million
        + Decimal(total_output_tokens) * output_per_m / million
    )
    quantize_unit = Decimal(10) ** (-_OUTPUT_PLACES)
    return cost.quantize(quantize_unit)


def estimate_extraction_cost_breakdown(
    transcript_byte_length: int,
    model_id: str,
    *,
    output_tokens: Optional[int] = None,
    constants_path: Path | str | None = None,
    enable_cascade: bool = False,
    haiku_items_count: int = 0,
    cascade_filter_model: str = DEFAULT_CASCADE_FILTER_MODEL,
    cascade_chunk_count: Optional[int] = None,
) -> CostBreakdown:
    """Estimate a full extraction (+ optional cascade) cost.

    Returns a :class:`CostBreakdown` so the CLI can print both lines
    when the cascade is enabled. When ``enable_cascade`` is False
    the cascade field is :class:`Decimal` zero and ``total_cost``
    equals ``extraction_cost`` (byte-identical to the pre-Phase-6
    single-Decimal output).
    """
    extraction = estimate_extraction_cost(
        transcript_byte_length,
        model_id,
        output_tokens=output_tokens,
        constants_path=constants_path,
    )
    cascade = Decimal("0").quantize(Decimal(10) ** (-_OUTPUT_PLACES))
    if enable_cascade:
        cascade = estimate_cascade_cost(
            haiku_items_count,
            chunk_count=cascade_chunk_count,
            filter_model=cascade_filter_model,
            constants_path=constants_path,
        )
    total = (extraction + cascade).quantize(Decimal(10) ** (-_OUTPUT_PLACES))
    return CostBreakdown(
        extraction_cost=extraction,
        cascade_filter_cost=cascade,
        total_cost=total,
    )


__all__ = [
    "CostBreakdown",
    "CostConstantsError",
    "DEFAULT_CASCADE_AVG_CHUNK_BYTES",
    "DEFAULT_CASCADE_CHUNK_COUNT",
    "DEFAULT_CASCADE_FILTER_MODEL",
    "DEFAULT_CASCADE_PER_CHUNK_OUTPUT_TOKENS",
    "DEFAULT_COST_CONSTANTS_PATH",
    "DEFAULT_HAIKU_OUTPUT_TOKENS",
    "DEFAULT_OPUS_OUTPUT_TOKENS",
    "DEFAULT_SONNET_OUTPUT_TOKENS",
    "estimate_cascade_cost",
    "estimate_extraction_cost",
    "estimate_extraction_cost_breakdown",
    "load_cascade_confirmation_item_threshold",
    "load_cost_constants",
]
