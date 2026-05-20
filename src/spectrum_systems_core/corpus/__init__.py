"""Phase 4 — corpus manifest loader and ingestion helpers.

The corpus manifest at ``data/corpus/manifest.json`` is the single
source of truth for the 13-transcript corpus. This package exposes:

* :mod:`spectrum_systems_core.corpus.manifest_loader` — schema-validated
  load + hash verification + custom uniqueness and cross-reference
  checks. Also writes back manifest updates from the ingest CLI.
* :mod:`spectrum_systems_core.corpus.ingest` — the ``ingest-corpus``
  subcommand implementation: pre-flight gate, source_record write,
  manifest update.

Both modules are pure (no LLM, no network) so the corpus subsystem
can be exercised end-to-end in the test suite without external
dependencies.
"""
from __future__ import annotations
