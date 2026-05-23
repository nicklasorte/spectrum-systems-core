"""Phase 6 — Stage 2 cascade filter.

The cascade reads a Haiku-produced `meeting_minutes` artifact and asks
Sonnet to keep/drop each item. Output is a `meeting_minutes_filtered`
artifact whose `filtered_items` is a strict subset of the source's
payload arrays (the cascade NEVER invents or mutates items).

The module is intentionally narrow:

- `executor.run_cascade_filter` is the single execution path.
- A failing per-chunk filter response triggers CONSERVATIVE pass-through
  (every item in that chunk is kept) — see `executor.py` for the rationale.
- The filter prompt lives at
  `workflows/prompts/cascade_filter_sonnet.md` and is loaded once per run.

Everything in this module is additive: a Phase 6 run with the cascade
disabled (the default) is byte-identical to a pre-Phase-6 run.
"""
from __future__ import annotations

from .executor import (
    CASCADE_FILTER_LOG_ARTIFACT_TYPE,
    CASCADE_FILTER_LOG_SCHEMA_VERSION,
    CASCADE_FILTER_LOG_TTL_DAYS,
    CASCADE_FILTER_PROMPT_PATH,
    DEFAULT_CASCADE_FILTER_MODEL,
    FILTER_RESPONSE_INVALID_PASSTHROUGH,
    FILTERED_ARTIFACT_TYPE,
    FILTERED_SCHEMA_VERSION,
    MAX_ITEMS_PER_FILTER_CALL,
    CascadeError,
    CascadeFilterResult,
    FilterDecision,
    cascade_filter_prompt_content,
    cascade_filter_prompt_content_hash,
    extraction_array_keys,
    items_in_artifact_count,
    run_cascade_filter,
    write_cascade_filter_log,
    write_filtered_artifact,
)

__all__ = [
    "CASCADE_FILTER_LOG_ARTIFACT_TYPE",
    "CASCADE_FILTER_LOG_SCHEMA_VERSION",
    "CASCADE_FILTER_LOG_TTL_DAYS",
    "CASCADE_FILTER_PROMPT_PATH",
    "DEFAULT_CASCADE_FILTER_MODEL",
    "FILTER_RESPONSE_INVALID_PASSTHROUGH",
    "FILTERED_ARTIFACT_TYPE",
    "FILTERED_SCHEMA_VERSION",
    "MAX_ITEMS_PER_FILTER_CALL",
    "CascadeError",
    "CascadeFilterResult",
    "FilterDecision",
    "cascade_filter_prompt_content",
    "cascade_filter_prompt_content_hash",
    "extraction_array_keys",
    "items_in_artifact_count",
    "run_cascade_filter",
    "write_cascade_filter_log",
    "write_filtered_artifact",
]
