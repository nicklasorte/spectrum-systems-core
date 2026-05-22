# Phase context: comparison

## What this phase does

Comparison runs the three extraction points (regex baseline, schema-shaped
Haiku, unconstrained Opus ceiling) against one transcript and writes
measurement-instrument artifacts plus a Markdown report. It is run-level
diagnostic output, not promoted product: artifacts are written even on
partial failure so blocked runs explain themselves, and they never enter
the artifact index. A separate Phase Y comparator artifact uses the name
`extraction_alignment_comparison` to avoid collision.

## Entry points

- `src/spectrum_systems_core/extraction/comparison_runner.py` — Phase AB.3
  three-point comparison runner; fail-closed on missing API key or
  `source_record`.
- `src/spectrum_systems_core/harness/workflow_comparator.py` — harness-side
  comparator used in higher-level loops.
- `.github/workflows/run-comparison.yml` — the workflow that drives a
  comparison run end-to-end against the data-lake.

## Artifact types produced or consumed

- Produces: `extraction_comparison`, `extraction_telemetry`,
  `extraction_unconstrained` (only when Opus succeeded),
  `extraction_alignment_comparison` (Phase Y).
- Consumes: `source_record`, the transcript file, and the per-extractor
  outputs above.

## Known blockers

- None known at branch-open time.

## Last PR that touched this phase

- PR #211 `feat(workflow): dedicated sonnet-unconstrained and comparison
  workflows` (commit `cf66097`). The most recent edit to
  `extraction/comparison_runner.py` itself merged via PR #164.
