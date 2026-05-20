"""Phase 3P few-shot examples + negative patterns infrastructure.

This module is distinct from the legacy ``glossary.few_shot_loader``
(Phase V.3) which loads a different artifact at a different path.
Phase 3P uses ``data/few_shot/examples_v1.jsonl`` with a manifest hash
gate at ``data/few_shot/MANIFEST.json`` and ships a schema
(``few_shot_entry``) the legacy system does not have.

Public API:
- :class:`FewShotError` — fail-closed exception with a ``reason`` token.
- :class:`FewShotEntry` — typed loaded entry.
- :class:`FewShotRegistry` — the loaded set of entries plus version hash.
- :func:`load_few_shot_registry` — primary loader (manifest-gated).
- :func:`build_few_shot_block` — render the prompt section text.
- :func:`inject_or_strip_few_shot` — runtime gate for prompt content.
- :func:`FEW_SHOT_BEGIN_MARKER` / :func:`FEW_SHOT_END_MARKER` — the
  delimiter pair the loader uses to slice the section out of the
  canonical prompt file.
"""
from __future__ import annotations

from .loader import (
    FEW_SHOT_BEGIN_MARKER,
    FEW_SHOT_END_MARKER,
    FEW_SHOT_MANIFEST_PATH,
    FEW_SHOT_EXAMPLES_PATH,
    FEW_SHOT_REGISTRY_VERSION,
    FEW_SHOT_SCHEMA_VERSION,
    FewShotEntry,
    FewShotError,
    FewShotRegistry,
    build_few_shot_block,
    compute_examples_hash,
    count_missing_reason_rate,
    inject_or_strip_few_shot,
    load_few_shot_registry,
    validate_entry,
)

__all__ = [
    "FEW_SHOT_BEGIN_MARKER",
    "FEW_SHOT_END_MARKER",
    "FEW_SHOT_EXAMPLES_PATH",
    "FEW_SHOT_MANIFEST_PATH",
    "FEW_SHOT_REGISTRY_VERSION",
    "FEW_SHOT_SCHEMA_VERSION",
    "FewShotEntry",
    "FewShotError",
    "FewShotRegistry",
    "build_few_shot_block",
    "compute_examples_hash",
    "count_missing_reason_rate",
    "inject_or_strip_few_shot",
    "load_few_shot_registry",
    "validate_entry",
]
