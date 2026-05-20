"""Phase V: domain grounding.

Submodules:
- ``glossary_builder``: load + content-hash the versioned glossary artifact.
- ``term_injector``: per-chunk lexical matcher and prompt-block formatter.
- ``few_shot_loader``: load few-shot examples artifact (verified-only).
- ``chunk_position``: proportional position labels + attention-direction block.
- ``loader``: Phase 2P NTIA/DoD glossary loader, matcher, and chunk-context
  block builder (flag-gated infrastructure, parallel to ``glossary_builder``).
"""
from __future__ import annotations

from .chunk_position import (
    ATTENTION_DIRECTION_BLOCK,
    POSITION_LABELS,
    assign_chunk_positions,
    attention_block_for_position,
    compute_chunk_position,
)
from .few_shot_loader import (
    FEW_SHOT_ARTIFACT_FILENAME,
    FewShotLoadResult,
    build_few_shot_block,
    load_few_shot_examples,
)
from .glossary_builder import (
    GLOSSARY_SCHEMA_VERSION,
    REQUIRED_TERM_FIELDS,
    compute_glossary_content_hash,
    load_versioned_glossary,
)
from .loader import (
    DEFAULT_MAX_TERMS,
    Glossary,
    GlossaryEntry,
    GlossaryError,
    build_chunk_context,
    compute_allowed_sources_hash,
    compute_glossary_hash,
    format_terminology_block,
    load_glossary,
    validate_entry,
)
from .term_injector import (
    MAX_DEFINITION_CHARS,
    build_terminology_block,
    find_matching_terms,
    summarize_injections,
)

__all__ = [
    "ATTENTION_DIRECTION_BLOCK",
    "DEFAULT_MAX_TERMS",
    "FEW_SHOT_ARTIFACT_FILENAME",
    "FewShotLoadResult",
    "GLOSSARY_SCHEMA_VERSION",
    "Glossary",
    "GlossaryEntry",
    "GlossaryError",
    "MAX_DEFINITION_CHARS",
    "POSITION_LABELS",
    "REQUIRED_TERM_FIELDS",
    "assign_chunk_positions",
    "attention_block_for_position",
    "build_chunk_context",
    "build_few_shot_block",
    "build_terminology_block",
    "compute_allowed_sources_hash",
    "compute_chunk_position",
    "compute_glossary_content_hash",
    "compute_glossary_hash",
    "find_matching_terms",
    "format_terminology_block",
    "load_few_shot_examples",
    "load_glossary",
    "load_versioned_glossary",
    "summarize_injections",
    "validate_entry",
]
