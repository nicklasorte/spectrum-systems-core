# Artifact Manifest

Every artifact type the pipeline writes. Maintained as a living contract.

CLAUDE.md enforcement: every PR that adds, removes, or changes an artifact
type — including renaming its on-disk path, changing its schema, or
flipping its git-tracked status — must update this file. The
`scripts/_gitignore_audit.py` script reads this manifest and asserts every
"Git-tracked: YES" path is NOT gitignored.

The single absolute root referenced in the templates below is
`data-lake/` (set via `DATA_LAKE_PATH`). Templates use the placeholders
`<artifact_id>` (UUID), `<source_id>` (transcript slug), `<run_id>`,
`<failure_id>`, and `<source_artifact_id>`. The audit substitutes
synthetic strings into these placeholders before calling
`git check-ignore`.

## Artifact Types

### meeting_extraction
- **Writer:** `extraction/typed_extraction_runner.py` via
  `extraction/extraction_merger.py::ExtractionMerger.write_to`
- **Path template:** `data-lake/store/artifacts/extractions/<source_artifact_id>_meeting_extraction.json`
- **Schema:** `src/spectrum_systems_core/schemas/meeting_extraction.schema.json`
- **Git-tracked:** YES — required by `select_few_shot_examples.py`,
  `_few_shot_preflight.py`, the validate-and-baseline gate, and the
  evals runner.
- **Readers:** `scripts/select_few_shot_examples.py`,
  `scripts/_few_shot_preflight.py`,
  `scripts/_artifact_validator.py`,
  `evals.m4.runner`.

### source_record
- **Writer:** `extraction/chunker.py` (per-source metadata file)
- **Path template:** `data-lake/store/processed/meetings/<source_id>/source_record.json`
- **Schema:** `src/spectrum_systems_core/schemas/source_record.schema.json`
- **Git-tracked:** YES — required for the slug → UUID resolution that
  the few-shot preflight and selection script depend on. The
  root `.gitignore` carries an explicit `!**/processed/**/source_record.json`
  negation that keeps this file un-ignored even though the rest of
  `processed/` is bulk runtime data.
- **Readers:** `scripts/select_few_shot_examples.py`,
  `scripts/_few_shot_preflight.py`.

### orchestration_result
- **Writer:** `extraction/typed_extraction_runner.py`
  (`_orchestration_result_path`).
- **Path template:** `data-lake/store/artifacts/orchestration/<run_id>_extraction.json`
- **Schema:** `src/spectrum_systems_core/schemas/orchestration_result.schema.json`
- **Git-tracked:** YES — diagnostic readers and post-pipeline jobs
  (`run_diff`, validate-and-baseline) depend on it being present in
  the workspace.
- **Readers:** `spectrum_systems_core.health.run_diff`,
  validate-and-baseline workflow, manual diagnostic scripts.

### decision_few_shot_examples
- **Writer:** `scripts/select_few_shot_examples.py`,
  `scripts/verify_example.py`.
- **Path template:** `data-lake/store/artifacts/evals/few_shot/decision_examples_v1.json`
- **Schema:** `src/spectrum_systems_core/schemas/decision_few_shot_examples.schema.json`
- **Git-tracked:** YES — read by every extraction run when
  `glossary.few_shot_loader.load_few_shot_examples` is called.
- **Readers:** `glossary.few_shot_loader`,
  `scripts/verify_example.py`,
  `scripts/select_few_shot_examples.py`.

### ground_truth_pair
- **Writer:** `scripts/annotate_rubric.py`,
  `scripts/confirm_pairs.py`,
  `scripts/confirm_rubric_annotations.py` (when present),
  `scripts/generate_gt_pairs.py` (Phase X2 follow-up: synthesizes
  decision-derived pairs from a `meeting_extraction` so the
  annotate-gt-rubric mobile workflow has input to operate on after a
  single-transcript debug run).
- **Path template:** `data-lake/store/artifacts/ground_truth/<artifact_id>.json`
- **Schema:** `contracts/schemas/ingestion/ground_truth_pair.schema.json`
- **Git-tracked:** YES — the eval-ground-truth CLI reads every pair
  in this directory.
- **Readers:** `evals.m4.runner` via `eval-ground-truth` CLI,
  `scripts/annotate_rubric.py`.

### eval_summary (incl. baseline)
- **Writer:** `evals/m4/runner.py` via `eval-ground-truth` CLI,
  `validate-and-baseline.yml`.
- **Path templates:**
  - `data-lake/store/artifacts/evals/baseline_eval_summary.json`
    (the development or production baseline)
  - `data-lake/store/artifacts/evals/eval_summary_<run_id>.json`
    (per-run summary)
- **Schema:** `src/spectrum_systems_core/schemas/eval_summary.schema.json`
- **Git-tracked:** YES — the baseline file IS the regression gate;
  per-run summaries are kept for diff context.
- **Readers:** `evals.m4.runner` (regression check),
  `validate-and-baseline.yml` (gate decision step).

### gate_decision
- **Writer:** `evals/m4/runner.py` via `eval-ground-truth` CLI.
- **Path template:** `data-lake/store/artifacts/evals/gate_decision_<run_id>.json`
- **Schema:** none (small JSON record).
- **Git-tracked:** YES — committed alongside the eval_summary so the
  audit trail of pass/fail decisions stays in the repo.
- **Readers:** `validate-and-baseline.yml`, manual auditors.

### spectrum_glossary
- **Writer:** `scripts/seed_glossary.py`,
  `glossary.glossary_builder`.
- **Path template:** `data-lake/store/artifacts/glossary/spectrum_glossary_v1.json`
- **Schema:** `src/spectrum_systems_core/schemas/spectrum_glossary.schema.json`
- **Git-tracked:** YES — the term-injector reads this versioned
  artifact on every extraction run.
- **Readers:** `glossary.glossary_builder.load_versioned_glossary`,
  `glossary.term_injector`.

### metadata_slices (eval slice predicates)
- **Writer:** committed by hand / Phase X2 seed.
- **Path template:** `data-lake/store/artifacts/evals/metadata_slices.json`
- **Schema:** none (predicate file).
- **Git-tracked:** YES — required by per-slice eval reporting.
- **Readers:** `evals.m4.runner` slice computation.

### judgment_record
- **Writer:** Human-authored via SKL-J workflow (not the core loop).
- **Path template:** `docs/decisions/<datestamp>-<slug>.judgment_record.json`
- **Schema:** one JSON object per file. Required fields:
  `artifact_id`, `artifact_type`, `schema_version`, `created_at`,
  `judgment_type`, `question_under_judgment`, `selected_outcome`,
  `confidence`, `rationale`, `alternatives_rejected`, `assumptions`,
  `consequences`.
- **Git-tracked:** YES — judgment records are part of the repo's
  permanent reasoning record.
- **Loop involvement:** None — not produced by
  `Produce → Evaluate → Decide → Promote`. Stored in `docs/`
  alongside the constitution and contracts (same authority tier),
  not under `data-lake/`.
- **Purpose:** Captures architectural decisions made in chat
  sessions before implementation. Provides institutional memory
  queryable by future Claude Code sessions.
- **Companion:** `docs/decisions/<datestamp>-<slug>.md` —
  human-readable Markdown view. Not canonical; the `.judgment_record.json`
  is the source of truth.
- **Readers:** human reviewers; future Claude Code sessions
  instructed via CLAUDE.md to read `docs/decisions/` before
  architectural changes.
- **First PR:** #96.

## Runtime / debug artifacts (intentionally NOT git-tracked)

These are recorded here for completeness so future authors do not
accidentally git-track them. They are produced by the pipeline as
debug or runtime state and live under `data-lake/` only when
`DATA_LAKE_PATH` is set; they are not part of the repo baseline.

### feature_flag (config)
- Path: `data-lake/store/artifacts/config/<flag_name>.json`
- Writer: `scripts/seed_phase_v_flag.py`, `scripts/seed_phase_w_flag.py`
- Git-tracked: NO — gitignored. Seeded into the workspace by the
  seed-feature-flags workflow at run time.

### model_registry
- Path: `data-lake/store/artifacts/config/model_registry.json`
- Writer: `verification/model_registry.py`
- Git-tracked: NO — gitignored. Seeded by seed-model-registry workflow.

### agenda artifacts
- Path: `data-lake/store/artifacts/agenda/`
- Git-tracked: NO — gitignored.

### verifications
- Path: `data-lake/store/artifacts/verifications/`
- Git-tracked: NO — gitignored. Written by verify-pipeline-state.

### calibration_warning
- Path: `data-lake/store/artifacts/calibration/<run_id>_calibration_warning.json`
- Writer: `extraction/typed_extraction_runner.py`
- Git-tracked: NO — debug/warning record, not a contract product.

### classification cache
- Path: `data-lake/store/artifacts/cache/classifications/<source_id>_cache.json`
- Writer: `extraction/classification_cache.py`
- Git-tracked: NO — runtime cache.

### raw API response log
- Path: `data-lake/store/artifacts/orchestration/raw_responses/<source_id>/<chunk_id>_<call_type>.json`
- Writer: `extraction/_raw_response_log.py`
- Git-tracked: NO — debug-only, gated by `RAW_RESPONSE_LOG_ENABLED`.

### typed-extraction failure artifacts
- Path: `data-lake/store/artifacts/failures/<failure_id>.json`
- Writer: `extraction/_failure_artifacts.py`
- Git-tracked: NO — written under failures/ only when an extraction
  call fails; not part of the product contract.

### bulk processed pipeline data
- Paths under `data-lake/store/processed/<family>/<source_id>/`
  (`stories/chunks.jsonl`, `stories/text_units.jsonl`,
  `stories/candidates.jsonl`, etc.)
- Git-tracked: NO — bulk data. The `**/processed/**` ignore covers
  everything in this tree EXCEPT `source_record.json` (explicit
  un-ignore) and the directory entries themselves.

### raw transcripts
- Paths: `data-lake/store/raw/transcripts/`, `raw/`
- Git-tracked: NO — bulk source material. Workflows seed transcripts
  from `tests/fixtures/debug_transcripts/` at run time.

### governance outputs
- Paths under `data-lake/store/governance/` and `governance/{audits,candidates,drift,markdown}/`
- Git-tracked: NO — runtime governance state. The repo carries only
  `governance/audits/index.json` (explicit un-ignore in root
  `.gitignore`).

## Gitignore Audit Rule

Every path listed above as **Git-tracked: YES** must satisfy:

```
git check-ignore -v <instantiated_path> → returncode != 0
```

That is: the path MUST NOT be ignored by any rule in any
`.gitignore` reachable from the repo root. The
`scripts/_gitignore_audit.py` script enforces this on every PR by
parsing this file, instantiating each path template with synthetic
ids, and shelling out to `git check-ignore`.

If the audit fails on a `Git-tracked: YES` artifact, the fix is
either:

1. Add an explicit un-ignore (`!<path>`) to the appropriate
   `.gitignore`, OR
2. Move the artifact to a different on-disk path that is not
   shadowed by a broader rule, OR
3. If the artifact is genuinely runtime-only, change the manifest
   entry to **Git-tracked: NO** and remove any workflow that
   `git add`s it.

The audit MUST pass before any PR that touches an artifact path or
a `.gitignore` rule can be merged.
