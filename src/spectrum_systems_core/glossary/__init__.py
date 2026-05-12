"""Phase V: domain grounding.

Submodules:
- ``glossary_builder``: load + content-hash the versioned glossary artifact.
- ``term_injector``: per-chunk lexical matcher and prompt-block formatter.
- ``few_shot_loader``: load few-shot examples artifact (verified-only).
- ``chunk_position``: proportional position labels + attention-direction block.
"""
from __future__ import annotations

from .glossary_builder import (
    GLOSSARY_SCHEMA_VERSION,
    REQUIRED_TERM_FIELDS,
    compute_glossary_content_hash,
    load_versioned_glossary,
)
from .term_injector import (
    MAX_DEFINITION_CHARS,
    build_terminology_block,
    find_matching_terms,
    summarize_injections,
)
from .few_shot_loader import (
    FEW_SHOT_ARTIFACT_FILENAME,
    FewShotLoadResult,
    build_few_shot_block,
    load_few_shot_examples,
)
from .chunk_position import (
    ATTENTION_DIRECTION_BLOCK,
    POSITION_LABELS,
    assign_chunk_positions,
    attention_block_for_position,
    compute_chunk_position,
)

__all__ = [
    "ATTENTION_DIRECTION_BLOCK",
    "FEW_SHOT_ARTIFACT_FILENAME",
    "FewShotLoadResult",
    "GLOSSARY_SCHEMA_VERSION",
    "MAX_DEFINITION_CHARS",
    "POSITION_LABELS",
    "REQUIRED_TERM_FIELDS",
    "assign_chunk_positions",
    "attention_block_for_position",
    "build_few_shot_block",
    "build_terminology_block",
    "compute_chunk_position",
    "compute_glossary_content_hash",
    "find_matching_terms",
    "load_few_shot_examples",
    "load_versioned_glossary",
    "summarize_injections",
]
