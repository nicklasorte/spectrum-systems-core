# Operator Reference

Runtime configuration, env vars, phase wiring notes, and feature flags.
This document is a reference for operators running the pipeline.
It is NOT a governance document — CLAUDE.md is authoritative for
session rules.

---

## Phase O — Operator env vars (pipeline debug observability)

- `RAW_RESPONSE_LOG_ENABLED=true` (default `false`) — turn on the
  per-chunk raw API response logger. Writes a `raw_api_response_log`
  artifact under
  `<sdl_root>/debug/raw_responses/<source_id>/<chunk_id>_<call_type>.json`.
  Zero-overhead when disabled (the enable flag is read once at module
  import).
- `RAW_RESPONSE_LOG_MAX_CHARS=2000` (default `2000`) — truncation
  budget for `raw_response_preview`. Larger payloads are classified as
  `response_type: truncated`.

`pipeline_run_summary` artifacts land under
`<data_lake>/store/artifacts/pipeline_runs/<pipeline_run_id>.json`
after the post-pipeline job. They are read by
`python -m spectrum_systems_core.health.run_diff` (workflow:
`.github/workflows/diff-pipeline-runs.yml`, workflow_dispatch only).

`blocked_chunk` envelopes (schema 2.0.0) are written alongside the
existing typed failure artifacts; the
`spectrum_systems_core.health.blocked_chunk_text_check` scanner
reports any legacy v1.0.0 envelope still on disk as an info-severity
health finding (`blocked_artifact_missing_chunk_text`).

`eval_summary` artifacts now carry a `pair_breakdown` and (when ≥2
distinct source_ids are present) `per_source_metrics`. Pairs whose
ground_truth record lacks a `source_id` field emit
`eval_pair_missing_source_id` info findings.

---

## PR Smoke Test

Every PR triggers an automatic extraction smoke test on the
Feb 19 Downlink transcript via GitHub Actions.

The smoke test:
- Runs extract-typed on source-id 7-ghz-downlink-tig-meeting---transcript-2-19-26
- Asserts decisions >= 1 OR claims >= 1 OR action_items >= 1
- Fails the PR if zero extractions produced
- Fails the PR if off_topic_rate > 0.80

If the smoke test fails on your PR:
1. Check the "Run extraction smoke test" step logs
2. Look for: "off_topic=N/N" — means classifier broken
3. Look for: "No meeting_extraction artifact" — means extractor crashed
4. Fix the root cause before requesting review
5. Push a new commit — smoke test re-runs automatically

---

## Phase T — Operator env vars (extraction quality)

- `BINDING_VALIDATOR_HALT_ENABLED=true` (default `false`) — promote
  `taxonomy_regulatory_verb_missing` health findings from `warn` to `halt`.
  Off by default so existing pipelines keep their fail-OPEN behaviour.
- `MAX_CHUNK_CHARS=2500` (default `2500`) — upper bound applied after the
  Phase R merge pass. Chunks exceeding this budget are split at the nearest
  speaker-turn boundary; if no boundary exists, split mid-turn and emit a
  `chunk_split_mid_turn_detected` info finding. Set to a very large value
  (e.g. `999999`) to disable the split pass without reverting code.
- `LOW_CONFIDENCE_GATE_ENABLED=true` (default `true`) — gate that scans
  extraction artifacts for high low-confidence-item rates and writes
  `correction_candidate` artifacts under `<sdl_root>/correction_candidates/`.
  Set to `false` to disable the correction-mining seed.
- `LOW_CONF_CONFIDENCE_THRESHOLD=0.6` (default `0.6`) — confidence below this
  counts as low-confidence for the gate.
- `LOW_CONF_RATE_LIMIT=0.30` (default `0.30`) — rate at which the gate fires.
- `SPURIOUS_ADD_RATE_BASELINE_BLOCK=0.25` (default `0.25`) — runs with
  spurious-add-rate above this threshold block the regression-gate
  `--set-baseline` action and emit a `spurious_add_rate_elevated` warn
  finding. Does NOT halt the run.
- `ATOMIC_DECOMPOSITION_ENABLED=true` (default `false`) — run a second
  Haiku call per decision to produce `atomic_facts`. Cost-impacting; left
  off until T.1–T.6 stabilise.
- `CORRECTION_CANDIDATE_TTL_DAYS=30` (default `30`) — TTL for unresolved
  correction candidates. The preflight scanner emits an info finding when
  a candidate's `expires_at` falls in the past; no auto-deletion.

---

## Phase V — Operator env vars (domain grounding)

- `MAX_GLOSSARY_TERMS_PER_CHUNK=10` (default `10`) — cap on the
  number of glossary terms injected per chunk by
  `glossary.term_injector.find_matching_terms`. Set to `0` to disable
  injection without reverting code. Each definition is truncated to
  the term's `short_definition` (<= 200 chars) so 10 terms add ~2 KB
  to the prompt at most.
- `FEW_SHOT_REQUIRED=true` (default `false`) — promote
  `few_shot_artifact_missing` from `info` to `halt`. Off by default
  so first-run environments without
  `data-lake/store/artifacts/evals/few_shot/decision_examples_v1.json`
  do not block; the loader emits the same `info` finding so the
  operator still sees it in health output.
- `POSITION_AWARE_PROMPTING_ENABLED=true` (default `true`) — when
  true, `chunk_position == "middle"` chunks receive an
  ATTENTION DIRECTION prompt block. `chunk_position` is computed
  proportionally from the CURRENT chunk-list length on every run, so
  re-runs with a different chunk count get fresh positions.
- `BINDING_TUPLE_ENABLED=true` (default `false`) — run a second Haiku
  call per decision to extract
  `(actor, action_verb, object_description, band_or_spectrum_ref,
  constraint_or_condition)`. Cost: roughly `$0.0005 per decision`.
  When `false`, `binding_tuple` is `null` and zero model calls are
  made — `binding_tuple_incomplete` cannot fire in that mode by
  design.
- `GENERALIZATION_CHECK_ENABLED=true` (default `true`) — when true,
  the post-extraction scanner emits `scope_overgeneralization` warn
  findings when a source chunk carries a specific band reference
  (`r"\d(\.\d+)?\s*(MHz|GHz|kHz)"`) and the extracted text contains
  an entry from `OVERGENERALIZATION_MARKERS`. Set to `false` to
  disable.

### Glossary governance (Phase V.1)

- The versioned glossary lives at
  `data-lake/store/artifacts/glossary/spectrum_glossary_v1.json` with
  `artifact_type: spectrum_glossary` and a `content_hash` computed via
  `glossary.glossary_builder.compute_glossary_content_hash` (sorted
  keys, compact JSON, sha256). Any term-list edit must bump
  `glossary_version` AND recompute `content_hash`.
- The legacy `working_paper.json` referenced by the predecessor
  `spectrum-systems` repo is superseded; the marker
  `working_paper.retired.json` is kept alongside the versioned
  artifact so future tooling does not look for it.
- `OVERGENERALIZATION_MARKERS` (in
  `src/spectrum_systems_core/config/taxonomy.py`) is governed under
  the same rule as `REGULATORY_VERBS`: updates require a PR. Tests
  assert the list is non-empty so it cannot be accidentally emptied.

### Few-shot examples (Phase V.3)

- The artifact at
  `data-lake/store/artifacts/evals/few_shot/decision_examples_v1.json`
  ships with `verified: false` placeholders. Examples are NEVER
  injected into a prompt while `verified: false` — the loader filters
  to verified examples only. Promote an example to live by setting
  `"verified": true` (and ideally populating `verified_by`).
- The artifact uses `artifact_type: decision_few_shot_examples` so
  it does NOT collide with the legacy `few_shot_examples` artifact
  consumed by `evals.m4.few_shot.FewShotLoader`.

### Findings introduced by Phase V

- `few_shot_artifact_missing` (info default; halt when
  `FEW_SHOT_REQUIRED=true`).
- `few_shot_no_verified_examples` (info).
- `binding_tuple_parse_failed` (warn) — only fires when
  `BINDING_TUPLE_ENABLED=true`.
- `binding_tuple_incomplete` (warn) — null `actor` on
  `approval` / `rejection`; only fires when `BINDING_TUPLE_ENABLED=true`.
- `scope_overgeneralization` (warn) — source has a numeric MHz/GHz/kHz
  reference and the extracted text uses an
  `OVERGENERALIZATION_MARKERS` entry.

---

## Phase W (integration wiring) — Connecting Phase T/V into the runner

Phase W is a **wiring-only** phase: it adds no new logic. PRs #68
(Phase T) and #69 (Phase V) built the glossary injector, the
chunk-position attention block, the V.3 few-shot loader, and the
generalization checker, but those modules were never called by the
live extraction runner. Phase W routes them through
`extraction/typed_extraction_runner.py` so every run produces
measurable output for the wired features.

Note on naming: the existing `apply_phase_w_if_enabled` symbol in
`spectrum_systems_core.agenda` is for **agenda detection** (a
predecessor "Phase W" name). Phase W (integration wiring) does NOT
touch that code path; the two phases share a letter but are
otherwise independent.

### What the wiring does, in one place

1. **Glossary injection (W.1).** The runner loads
   `<sdl_root>/glossary/spectrum_glossary_v1.json` once per run via
   `glossary.glossary_builder.load_versioned_glossary` and matches
   each chunk's text against the term list with
   `glossary.term_injector.find_matching_terms`. Matched terms feed
   `build_terminology_block`, which is concatenated into the
   `glossary_block` string passed to each typed extractor.
   `glossary_terms_injected` (a list of `term_id` strings — never
   None) is recorded per-chunk in
   `result["chunk_extraction_records"]`. Term IDs are stable UUIDs,
   so a future glossary edit does NOT invalidate historical
   comparison.
2. **Chunk position + attention block (W.2).** The chunker calls
   `glossary.chunk_position.assign_chunk_positions` AFTER all merge
   AND split passes — the position is proportional to the FINAL
   chunk count, so reordering this call would label positions on
   the wrong chunk list. The runner reads `chunk_position` per
   chunk and prepends the `ATTENTION DIRECTION` block when any
   chunk in an extractor group is `middle` AND
   `POSITION_AWARE_PROMPTING_ENABLED=true` (default).
3. **Few-shot examples (W.3).** The runner calls
   `glossary.few_shot_loader.load_few_shot_examples(sdl_root)` once
   per run for decision extraction only (claims / action items have
   no examples shipped). The loader returns
   `FewShotLoadResult(examples, finding_code, severity,
   remediation)`; the runner promotes any non-None `finding_code`
   into a `HealthFinding` and surfaces it under
   `result["phase_w_findings"]`. Unverified examples never reach
   the prompt.
4. **Generalization checker (W.4).** After the merger runs, the
   runner calls
   `extraction.generalization_checker.scan_items` on decisions and
   claims with `source_text` set to the **chunk** text (never the
   full transcript). The returned `HealthFinding` list is appended
   to `result["phase_w_findings"]` and the count is reported as
   `scope_overgeneralization_count`. Gated by
   `GENERALIZATION_CHECK_ENABLED=true` (default).
5. **Orchestration counters (W.5).** The orchestration_result
   artifact carries three new optional fields:
   `glossary_injection_summary`,
   `binding_tuple_call_count`, and
   `scope_overgeneralization_count`. They are additive — the
   `schema_version` const stays `"1.0.0"` because all three are
   declared optional in `orchestration_result.schema.json`.

### `build_extraction_prompt` — the canonical prompt-builder

`typed_extraction_runner.build_extraction_prompt` is exported so the
W.6 integration smoke test can inspect block ordering without
invoking the LLM. Block order (omitted entirely when empty):

```
1. Role / extraction-type instruction
2. REGULATORY TAXONOMY BLOCK (Phase T.1)
3. TERMINOLOGY FOR THIS SECTION (Phase V.2)
4. ATTENTION DIRECTION (Phase V.4)
5. FEW-SHOT EXAMPLES (Phase V.3)
6. Legacy glossary block (suppressed when 3 is non-empty)
7. CHUNK content
```

The runner's group-level prompt is still built INSIDE each
extractor (`DecisionExtractor._build_prompt` etc.); the helper
mirrors the same order on a single chunk. The legacy `GlossaryManager`
block and the V.2 terminology block share the
`TERMINOLOGY FOR THIS SECTION` header, so the runner suppresses the
legacy block whenever the versioned glossary produces a non-empty
terminology block. This avoids duplicate headers in the same prompt.

### Chunk position computation order (chunker)

`extraction/chunker.py` calls passes in this fixed order; do not
reorder:

```
merge_short_chunks → split_oversized_chunks → merge_short_chunks (re-merge if split fired) → assign_chunk_positions
```

`assign_chunk_positions` MUST run AFTER all merge and split passes
because the proportional cut-offs depend on the final chunk count.
The `chunks.jsonl` write happens AFTER `assign_chunk_positions` so
the position lands on every chunk on disk.

### `glossary_not_preloaded` debug log (not a finding)

When `<sdl_root>/glossary/` is missing entirely, the runner emits a
single per-process debug log line and continues with empty
injection. This is NOT a `HealthFinding` because it is an
efficiency note, not a governance concern: a caller without a
glossary directory still gets a working extraction run with
`glossary_terms_injected: []` on every chunk.

### Findings introduced by Phase W

- `glossary_injection_field_absent` (info) — emitted when more than
  50% of records scanned for the
  `glossary_injection_summary` rollup lack the
  `glossary_terms_injected` field. Remediation: re-run extraction
  with `force=true` so the field lands on every record. Reserved
  in `ALL_FINDING_CODES`; not raised by the current runner because
  the field is always populated on writes since Phase W.

---

## Phase X2 — closing the 10 research recommendations

Phase X2 is the keystone phase that turns the eval infrastructure from
"measuring but not gating" into "measuring AND gating", plus the
remaining seams (agenda boundaries, few-shot verification, LLM judge,
rubric annotation, HITL review, validate-and-baseline workflow). The
work is in seven small modules; each is independently rollback-able.

### X2.1 — Heuristic agenda boundary detector

Module: `src/spectrum_systems_core/extraction/heuristic_agenda_detector.py`.
Pure-regex deterministic detector — makes ZERO model calls. Distinct
from `src/spectrum_systems_core/agenda/agenda_detector.py`, which is
the LLM-based predecessor "Phase W" detector and is unrelated.

- Detection rules (priority order): explicit prefix markers
  (`agenda item`, `discussion:`, `topic:`), numbered formats
  (`1. Title` / `2) Title`), all-caps headers ≤ 60 chars whose NEXT
  non-blank line looks like a speaker turn.
- When zero headers detected, callers MUST assign
  `agenda_item_id = "unclassified"` to every chunk via
  `assign_agenda_item_ids`. `agenda_item_id` is ALWAYS a non-empty
  string after this function returns — NEVER None — per the Phase X2
  amendment that prevents the slice-membership gap.
- Rollback: `AGENDA_DETECTION_ENABLED=false` makes
  `detect_agenda_items` return `[]` even on a transcript with valid
  headers, so the caller falls back to `"unclassified"`.
- Wiring scope: X2 SHIPS the module and the
  `data-lake/store/artifacts/evals/metadata_slices.json` predicate
  file. Live-pipeline integration (calling the detector from
  `extraction/chunker.py` and writing `agenda_items` to
  `source_record.json`) is intentionally NOT wired in this phase
  because it would touch the chunker contract; a follow-up phase
  performs the integration. The detector is callable from operator
  scripts in the meantime.

### X2.2 — Few-shot example selection + verification (human-only)

Scripts:

- `scripts/select_few_shot_examples.py` — reads the most recent
  `meeting_extraction` artifact for a `--source-id`, selects one
  candidate decision per outcome bucket (approval / deferral /
  action_required), writes them to
  `data-lake/store/artifacts/evals/few_shot/decision_examples_v1.json`
  with `verified: false`. Also writes a `REVIEW_CHECKLIST.md` next
  to the artifact so a human reviewer has a file-on-disk record of
  what to inspect.
- `scripts/verify_example.py` — sets `verified: true` on a single
  example after a human review. Refuses to run with
  `ANTHROPIC_API_KEY` set in the environment (unless `--force`) so
  no LLM agent can self-verify its own examples.

The `decision_examples_v1.json` schema gained optional `verified_at`,
`selected_at`, `selection_reason`, and an artifact-level `audit_log`
array that records every `selected` / `verified` / `force-verified`
action.

Reviewer policy: the reviewer MUST be a different person from the
operator who ran the extraction. The system stores `reviewer_id` on
the audit_log entry but cannot enforce identity uniqueness; the
policy is the enforcement.

### X2.3 — LLM-as-judge (qualitative eval layer)

Modules: `src/spectrum_systems_core/evals/judge.py`,
`src/spectrum_systems_core/evals/judge_calibration.py`.

- 4 atomic boolean rubric checks per decision:
  `decision_text_supported_by_source`,
  `decision_outcome_matches_regulatory_verb`,
  `speaker_attribution_correct`,
  `no_hallucinated_constraints_or_actors`.
- `JUDGE_ENABLED=false` (default) makes `run_judge` perform ZERO
  model calls; `aggregate_pass_rate` is `None`.
- `JUDGE_MODEL` defaults to `claude-sonnet-4-6` so the judge family
  differs from the extraction Haiku family. When the families match,
  a `judge_same_family` warn finding is emitted.
- `JUDGE_STABILITY_CHECK_ENABLED=true` re-runs the judge per item;
  verdict mismatches emit `judge_score_unstable` (warn).
- Calibration thresholds: agreement ≥ 0.70 → `ok`; 0.60–0.70 → warn
  (`judge_calibration_low`); <0.60 → halt (`judge_calibration_failed`).
- `agreement_rate_verb_discrimination` is computed only over GT
  pairs with `rubric_notes.verb_discrimination_example == true`;
  None when no such pairs exist.

### X2.4 — eval-ground-truth `--specific-source-id` + baseline_scope

CLI: `eval-ground-truth --specific-source-id <id> [--set-baseline]`.

- Filters ground_truth pairs to those whose resolved source_id
  matches the filter (`fixture_source_id` first, then
  `source_record.payload.source_id`).
- `eval_summary.baseline_scope` is set to `"single_transcript"` when
  the run installs a baseline AND `--specific-source-id` is provided;
  otherwise `"full_corpus"` on the baseline, `None` on non-baseline
  summaries.
- `gate_decision.baseline_type` mirrors as `"development"` /
  `"production"` for at-a-glance diagnosis.
- A `baseline_set` info finding is emitted on every successful
  `--set-baseline` run with `{coverage, precision, f1,
  baseline_scope, pairs_count, eval_summary_id}` in `context`.
- When `--specific-source-id` is provided AND `--set-baseline` is
  used AND the last `orchestration_result` for the source has
  `stage_status="failed"`, the runner refuses with
  `baseline_requires_successful_run` (halt) and exit_code=1; no
  eval_summary is written.

Two-baseline model: the single-transcript baseline is the
**development** baseline, useful for gating but not production-grade
regression detection. The **production** baseline is set after all
13 transcripts run with `--set-baseline` (no `--specific-source-id`).

### X2.5 — Regulatory verb rubric annotation

Schema: `contracts/schemas/ingestion/ground_truth_pair.schema.json`
gained optional `rubric_notes`, `target_type`, `decision_id`, and
`ground_truth_pass`. Existing pairs without `rubric_notes` continue
to validate (schema_version stays `1.0.0`; the new properties are
optional and `additionalProperties` was already false).

Script: `scripts/annotate_rubric.py`.

- `--apply-from <annotations.json>` non-interactively applies a
  precomputed annotations file (the path used by tests and CI).
- Default invocation prints candidate pair_ids + ground_truth_text
  excerpts for an operator to author the annotations file.

Judge calibration reads `rubric_notes.verb_discrimination_example`
to compute `agreement_rate_verb_discrimination` separately from
`agreement_rate_overall`; the per-rubric metric is the one that
directly validates Rec 9 ("approved ≠ considered ≠ deferred").

### X2.6 — Minimum-viable HITL review workflow

Schema: `src/spectrum_systems_core/schemas/human_review_artifact.schema.json`.

Script: `scripts/submit_review.py`.

- Looks up the `correction_candidate` by id, writes a
  `human_review_artifact` to
  `<data-lake>/store/artifacts/human_reviews/<source_id>/`, and
  updates `correction_candidate.review_status` to `reviewed` (or
  `reviewed_after_expiry` when the candidate's `expires_at` is in
  the past). Both review states are durable; the after-expiry
  branch is preserved for audit.
- `correction_candidate.schema.json` gained `review_status` and
  `review_artifact_id` (both optional).
- `orchestration_result.schema.json` gained
  `correction_candidates_pending` and
  `correction_candidates_reviewed` counters (both optional).
- `human_review_artifact_missing` (info) is reserved in
  `ALL_FINDING_CODES` for a future preflight scanner; not currently
  emitted because the candidate-level state is for operator triage,
  not the blocking gate.

Reviewer policy (same as X2.2): the reviewer must be a different
person from the operator who ran the extraction. The artifact
stores `reviewer_id`; identity uniqueness is not enforced
technically.

### X2.7 — validate-and-baseline GitHub Actions workflow

Workflow: `.github/workflows/validate-and-baseline.yml` (shipped in a
**follow-up PR** so the main Phase X2 PR does not trigger GitHub's
"approve workflows when a PR modifies .github/workflows/" gate —
which silently blocks `pytest` and `smoke-test` until a maintainer
clicks approve. The follow-up PR carries the workflow file alone so
its approval is a separate, scoped decision).

The tests in `tests/ci/test_validate_baseline_workflow.py` skip when
the workflow file is absent, so the test file is safe to land in
either PR.

- Triggers: push to main touching extraction / glossary / evals /
  agenda code, OR explicit workflow_dispatch with an optional
  `source_id` input (default: the Dec 18 kickoff transcript).
- Two jobs: `early-exit-check` (guard) and `validate-and-baseline`
  (the work). The work job depends on the guard.
- **Two-layer loop prevention**:
  1. The baseline commit message contains the skip-ci marker
     (GitHub's standard guard).
  2. The commit also contains `[baseline-commit]`, which the
     `early-exit-check` job inspects defensively in case GitHub's
     skip-ci behavior changes.
  3. The repository variable `SKIP_BASELINE_WORKFLOW=true` mutes
     the workflow without editing the YAML.
- Verifies 5 Phase W wiring signals before `--set-baseline` is
  called: `agenda_item_id_nonnull`,
  `few_shot_present_with_verified`,
  `glossary_terms_injected_present`, `binding_taxonomy_present`,
  `generalization_check_ran`. If ANY signal is missing the verify
  step exits 1 and the baseline step never runs (the workflow's
  default behavior aborts on the first failure).

### Operator env vars introduced by Phase X2

- `AGENDA_DETECTION_ENABLED=true` (default) — heuristic agenda
  detector returns the detected items. Set to `false` to roll back
  to pre-Phase-X2 behavior (every chunk gets `unclassified`).
- `JUDGE_ENABLED=false` (default) — the LLM-as-judge module makes
  zero model calls. Set to `true` to run the judge and emit
  `judge_score` artifacts.
- `JUDGE_MODEL=claude-sonnet-4-6` (default) — judge model id.
  Choose a model from a different family than the extraction
  model to keep the verdict independent.
- `JUDGE_STABILITY_CHECK_ENABLED=false` (default) — re-run the
  judge per item and emit `judge_score_unstable` on mismatches.
- `SKIP_BASELINE_WORKFLOW=true` (repository variable; default
  unset) — mute the validate-and-baseline workflow without
  editing the YAML.

### Findings introduced by Phase X2

- `agenda_detection_failed` (info) — emitted when callers wire
  the heuristic detector and detection found zero items.
- `judge_calibration_low` (warn) — agreement in 0.60–0.70 band.
- `judge_calibration_failed` (halt) — agreement < 0.60; gate
  refuses `--set-baseline` until the judge prompt is fixed.
- `judge_same_family` (warn) — judge model family matches the
  extraction model family.
- `judge_score_unstable` (warn) — per-item verdict differs across
  re-runs when `JUDGE_STABILITY_CHECK_ENABLED=true`.
- `baseline_set` (info) — `--set-baseline` succeeded; context
  carries coverage / precision / f1 / baseline_scope / pairs_count.
- `baseline_requires_successful_run` (halt) — `--set-baseline`
  refused because the last `orchestration_result` for the source
  had `stage_status="failed"`.
- `human_review_artifact_missing` (info) — reserved for a future
  preflight scan; not currently emitted by the runner.

### Post-merge human-only steps

After this PR lands the operator runs, in order:

1. `python scripts/select_few_shot_examples.py
     --source-id <dec18-source-id> --data-lake data-lake/`
   then review each candidate and run
   `python scripts/verify_example.py
     --example-id <uuid> --reviewer-id <your-name>
     --data-lake data-lake/`
2. `python scripts/annotate_rubric.py
     --source-id <dec18-source-id> --data-lake data-lake/
     --limit 20` then author the annotations file and re-run with
   `--apply-from annotations.json`.
3. Push any code change to trigger the validate-and-baseline
   workflow, OR run it via workflow_dispatch.
4. Confirm the step-summary shows 5 green signals and the
   `baseline_set` finding has been emitted with non-zero
   `pairs_count`.

These steps cannot be automated because each requires a human
judgment: the few-shot review is an extraction-correctness check,
the rubric annotation is a ground-truth labeling decision, and the
operator running steps 3 + 4 needs to confirm the baseline matches
expectations before promoting to production.

---

## Phase X2 follow-up — Codex fixes, agenda wiring, mobile workflows

### Codex bug fixes shipped with the follow-up

1. **`scripts/annotate_rubric.py` `--source-id` filter.** Production GT
   pairs only carry `source_artifact_id` (per
   `contracts/schemas/ingestion/ground_truth_pair.schema.json`);
   fixture pairs may additionally carry `fixture_source_id`. The
   filter accepts a match against ANY value in `_SOURCE_ID_FIELDS`.
   When the filter matches zero pairs the CLI prints a helpful error
   listing available identifiers and exits non-zero — never silently
   returns empty results.
2. **`gate_decision.baseline_type` preserved across runs.** Previously
   only set when `becoming_baseline` was true, so subsequent
   non-baseline runs wrote `null` and lost the dev/prod label. The
   runner now derives `baseline_type` from the existing baseline's
   `baseline_scope` (`single_transcript` → `development`,
   `full_corpus` → `production`) on every non-baseline run.
3. **Few-shot selection prioritises grounding over confidence.**
   `scripts/select_few_shot_examples.py::_select_candidates_from_decisions`
   sorts by grounding-first, then confidence-desc, then
   source_turn_ids. A grounded mid-confidence decision now beats an
   ungrounded high-confidence one in the same outcome bucket.

### Agenda detector wired into the chunker

Phase X2.1 shipped `extraction/heuristic_agenda_detector.py` but
deferred the live-pipeline integration. The follow-up wires it in. The
chunker call order is now mandatory and explicit:

```
merge_short_chunks
  -> split_oversized_chunks
  -> merge_short_chunks (re-merge if split fired)
  -> assign_chunk_positions
  -> assign_agenda_item_ids   # <-- Phase X2 follow-up; MUST be last
```

`agenda_item_id` is a non-empty string on every chunk after the
detector runs (either an `AI-NNN` id or the literal string
`"unclassified"`). When `AGENDA_DETECTION_ENABLED=false`, the
chunker skips the wiring entirely and the field is absent — that is
the documented rollback path back to pre-X2 behaviour. The chunker
also writes the detected `agenda_items` list to
`source_record.payload.agenda_items` so downstream readers can map
ids back to titles.

### `--force` flag on `verify_example.py`

`verify_example.py` refuses to run when `ANTHROPIC_API_KEY` is set in
the environment unless `--force` is also passed. The flag exists for
use in controlled GitHub Actions contexts (specifically the
`verify-few-shot-example.yml` mobile workflow) where the secret is
exported globally but the workflow itself is the human-approved
review action. **The `--force` flag is for use in controlled GitHub
Actions contexts only**; CLI invocations must not pass it.

### Mobile workflow_dispatch workflows (phone sequence)

The five mobile workflows let an operator drive the post-merge
human-only steps from a phone with no laptop. Run them in this
order — the next step always consumes an id printed by the prior
step's step-summary, so a copy-paste loop is sufficient:

1. **`select-few-shot-candidates.yml`** — auto-select up to N decision
   candidates for `source_id`, write them to
   `decision_examples_v1.json` with `verified: false`, and print each
   `example_id` plus decision text in the step summary.
2. **`verify-few-shot-example.yml`** — given an `example_id` plus
   `reviewer_id`, decision (`approve` / `reject`), and notes, set
   `verified: true` (or record a rejection in the audit log) and
   commit. Passes `--force` to `verify_example.py` because the secret
   is present in the Actions environment.
3. **`annotate-gt-rubric.yml`** — auto-classify the rubric for every
   GT pair under `source_id` using `OUTCOME_TO_VERBS` from
   `config/taxonomy.py` (no API key). Posts a proposals table.
4. **`confirm-rubric-annotations.yml`** — rubber-stamp the
   auto-classification with `reviewer_id`, or override a single pair
   via `override_pair_id` + `override_outcome`. Commits.
5. **`validate-and-baseline.yml`** — runs the extraction pipeline,
   verifies the 5 Phase W wiring signals, and calls
   `eval-ground-truth --set-baseline --specific-source-id`. Carries
   both the skip-ci marker and `[baseline-commit]` early-exit guards.

Secret policy: only `validate-and-baseline.yml` references
`secrets.ANTHROPIC_API_KEY` (it runs the actual extraction). The
other four workflows operate on existing artifacts and must NOT pull
the secret into their job env.
