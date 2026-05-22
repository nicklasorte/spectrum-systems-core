# Phase context: governance

## What this phase does

Governance is the Phase I system-wide audit layer. Per SSC-VISION-001
it provides visibility, not action: scanners surface complexity for
human review and never mutate anything outside `governance/`. The single
exception is `apply_compression` (FINDING-I-006). Audit failures never
block the synthesis pipeline. Zero LLM calls in any scanner.

## Entry points

- `src/spectrum_systems_core/governance/dashboard.py` —
  `GovernanceDashboard` aggregator over the scanners.
- `src/spectrum_systems_core/governance/compression_scanner.py` and
  `apply_compression.py` — the only mutating path.
- `src/spectrum_systems_core/governance/{eval_coverage_scanner,
  schema_drift_scanner, hidden_logic_scanner,
  decision_divergence_detector, markdown_authority_scanner,
  exception_accumulation_tracker, cost_trend_reporter,
  gov10_certification}.py`.

## Artifact types produced or consumed

- Produces: `eval_gate_coverage`, `revised_draft`, `override_artifact`,
  `formatted_paper_artifact`, `run_history_entry`. Audits also report on
  `uncovered_artifact_type` and `target_artifact_type` rather than
  defining new ones.
- Consumes: promoted artifacts from the data lake and the artifact
  manifest.

## Known blockers

- None known at branch-open time.

## Last PR that touched this phase

- PR #214 `feat(grounding): opt-in source_quote length threshold`
  (`3ea9805`) — `promotion/gate.py`, governance-adjacent. Most recent
  edit inside `src/spectrum_systems_core/governance/` merged via PR #164.
