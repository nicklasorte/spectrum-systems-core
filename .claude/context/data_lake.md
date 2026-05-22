# Phase context: data_lake

## What this phase does

The data-lake module is the boundary between this code repo and the
separate `nicklasorte/data-lake` storage repo. It loads raw transcripts
and metadata, writes promoted artifacts and the artifact index, renders
Markdown views, and emits the run-level harness-memory JSONL files. The
trust property is determinism: all outputs under `processed/` and
`indexes/` must be byte-identical across runs given the same inputs.
Stabilization (`_stable_artifact_id`, `_DETERMINISTIC_CREATED_AT`) is
applied to every artifact the pipeline writes.

## Entry points

- `src/spectrum_systems_core/data_lake/pipeline.py` — the only place
  data-lake I/O meets the core loop.
- `src/spectrum_systems_core/data_lake/loader.py` — raw transcript +
  metadata loader; rejects invalid `meeting_id` before any artifact runs.
- `src/spectrum_systems_core/data_lake/writer.py`,
  `serialize.py`, `index.py`, `markdown.py`, `markdown_views.py`,
  `run_history.py`, `experience.py`, `eval_history.py`.

## Artifact types produced or consumed

- Produces (promoted product): `meeting_minutes`, `decision_brief`,
  `agency_question_summary`, `meeting_action_log`.
- Produces (run-level, never indexed): `eval_result`,
  `control_decision`, `source_record`, manifest, debug, Markdown views
  (`meeting_index`, `agency_note`, `topic_note`, `run_note`), harness JSONL.
- Consumes: raw transcripts and metadata under `data-lake/raw/meetings/`.

## Known blockers

- None known at branch-open time.

## Last PR that touched this phase

- UNKNOWN (no recent numbered PR appears in `git log -- src/spectrum_systems_core/data_lake/`).
  Most recent named commit is `8687430 phase(5): Sonnet model wiring`.
