"""Chunk-level outcome counters for the typed extraction pipeline.

Phase X-0 part D. The orchestrator previously marked an entire stage
`clm✓` whenever the runner returned without an exception, even if
half the chunks in that stage had been silently dropped by an API
failure. That `✓` was lying.

This module is the single source of truth for chunk-level outcome
counting. Every code path that fails to produce a successful
extraction must call ``record_block`` with the right ``block_reason``.
Every code path that produces a successful extraction must call
``record_success``. The orchestrator reads the totals at the end of
the run and decides the stage status (``✓`` / ``partial`` / ``failed``)
per the rules in CLAUDE.md.

Rule (X-0 part D):
  chunks_blocked == 0                                 -> ``✓``
  0 < chunks_blocked / chunks_attempted <= 0.5        -> ``partial``  (``clm⚠``)
  chunks_blocked / chunks_attempted >  0.5            -> ``failed``   (``clm✗``)

Block reasons mirror the failure artifact types we emit:

  rate_limit_exhausted  ->  api_rate_limit_exhausted
  empty_response        ->  extraction_empty_response
  parse_error           ->  typed_extraction_llm_json_parse_failed
  other                 ->  e.g. typed_extraction_empty_result (zero items),
                            unexpected schema-validation failure, etc.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# String constants for block reasons. Kept here so callers cannot drift
# from the schema or the orchestrator summary format.
BLOCK_REASON_RATE_LIMIT: str = "rate_limit_exhausted"
BLOCK_REASON_EMPTY_RESPONSE: str = "empty_response"
BLOCK_REASON_PARSE_ERROR: str = "parse_error"
BLOCK_REASON_OTHER: str = "other"

BLOCK_REASONS: tuple = (
    BLOCK_REASON_RATE_LIMIT,
    BLOCK_REASON_EMPTY_RESPONSE,
    BLOCK_REASON_PARSE_ERROR,
    BLOCK_REASON_OTHER,
)

# Stage outcome strings emitted by the orchestrator. The orchestrator
# already has a STAGE_STATUS_* set with `success` / `partial` / `failure`
# semantics; we describe the chunk-level rollup with its own labels so
# callers do not confuse one with the other.
STAGE_OK: str = "ok"
STAGE_PARTIAL: str = "partial"
STAGE_FAILED: str = "failed"


@dataclass
class ChunkCounters:
    """Mutable tally of chunk-level outcomes for one extraction stage."""

    chunks_attempted: int = 0
    chunks_succeeded: int = 0
    chunks_blocked: int = 0
    block_reasons: dict[str, int] = field(
        default_factory=lambda: {r: 0 for r in BLOCK_REASONS}
    )

    def record_attempt(self, n: int = 1) -> None:
        """Bump ``chunks_attempted`` by ``n``. Called once per chunk submitted."""
        if n > 0:
            self.chunks_attempted += int(n)

    def record_success(self, n: int = 1) -> None:
        """Bump ``chunks_succeeded`` by ``n``."""
        if n > 0:
            self.chunks_succeeded += int(n)

    def record_block(self, reason: str, n: int = 1) -> None:
        """Bump ``chunks_blocked`` and the specific ``block_reasons[reason]``.

        Unknown reasons are tallied under ``other`` so we never lose count
        if a new failure mode is added without updating BLOCK_REASONS.
        """
        if n <= 0:
            return
        key = reason if reason in BLOCK_REASONS else BLOCK_REASON_OTHER
        self.chunks_blocked += int(n)
        self.block_reasons[key] = int(self.block_reasons.get(key, 0)) + int(n)

    def stage_status(self) -> str:
        """Return ``ok`` / ``partial`` / ``failed`` per the X-0 rules.

        Zero attempts is ``ok`` (nothing was tried, nothing was blocked).
        The orchestrator can still surface a separate "stage skipped"
        signal upstream of this -- the counter has no opinion there.
        """
        if self.chunks_attempted <= 0 or self.chunks_blocked <= 0:
            return STAGE_OK
        ratio = self.chunks_blocked / self.chunks_attempted
        if ratio > 0.5:
            return STAGE_FAILED
        return STAGE_PARTIAL

    def as_dict(self) -> dict[str, object]:
        """Serialise the counter into the orchestrator-summary shape.

        Keys match ``orchestration_result.schema.json`` exactly so the
        result can be embedded into the artifact without further
        massaging.
        """
        return {
            "chunks_attempted": int(self.chunks_attempted),
            "chunks_succeeded": int(self.chunks_succeeded),
            "chunks_blocked": int(self.chunks_blocked),
            "block_reasons": {
                r: int(self.block_reasons.get(r, 0)) for r in BLOCK_REASONS
            },
        }
