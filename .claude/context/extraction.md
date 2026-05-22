# Phase context: extraction

## What this phase does

Extraction turns a raw meeting transcript into structured payloads that
feed the governed loop's `meeting_minutes` (and adjacent) artifacts. It
runs three layers: a deterministic labelled-prefix regex baseline, a
schema-shaped Haiku LLM extraction, and an unconstrained Opus extraction
that serves as the ceiling. Each extractor is fail-closed: missing
inputs or API keys exit before any artifact is written.

## Entry points

- `src/spectrum_systems_core/workflows/meeting_minutes_llm.py` —
  LLM-driven meeting_minutes workflow (cascade + batching).
- `src/spectrum_systems_core/extraction/typed_extraction_runner.py` —
  schema-typed extraction runner used by the cascade.
- `src/spectrum_systems_core/extraction/corpus_runner.py` —
  multi-transcript corpus driver.
- `src/spectrum_systems_core/extraction/binding_validator.py` —
  validates extracted bindings against `config/taxonomy.py`.

## Artifact types produced or consumed

- Produces: `meeting_minutes`, `chunk_classification`,
  `chunk_classifications`, `orchestration_result`.
- Consumes: `source_record`, raw transcript files under
  `raw/meetings/<meeting_id>/transcript.txt`.

## Known blockers

- None known at branch-open time. Recent activity has been around
  multi-batch aggregation correctness (PR #208, #212) and source-turn
  validity retries (PR #206).

## Last PR that touched this phase

- PR #213 `feat(workflow): dedicated haiku extraction workflow` (commit
  `ee58861`). Most recent named commit inside
  `src/spectrum_systems_core/extraction/` is `c2394d6 Phase Y`.
