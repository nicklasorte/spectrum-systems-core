# Artifact Manifest

Every artifact type the pipeline writes. Maintained as a living contract.

CLAUDE.md enforcement: every PR that adds, removes, or changes an artifact
type — including renaming its on-disk path, changing its schema, or
flipping its git-tracked status — must update this file. The
`scripts/_gitignore_audit.py` script reads this manifest and asserts every
"Git-tracked: YES" path is NOT gitignored in the repo that owns it.

## Data-lake separation

All pipeline artifacts live in the **`nicklasorte/data-lake`** repository,
not in `spectrum-systems-core`. Workflows clone `nicklasorte/data-lake`
into `./data-lake` at run time via the `DATA_LAKE_TOKEN` PAT (see
`.github/actions/clone-data-lake`). The directory `data-lake/` is
gitignored at the spectrum-systems-core repo root so the artifacts can
never be re-committed into spectrum-systems-core.

Path templates below carry the `data-lake/` prefix as a documentation
convenience — it is the path the local clone occupies in every workflow.
For artifacts in this manifest, "Git-tracked: YES" means **tracked inside
`nicklasorte/data-lake`**. The audit script strips the `data-lake/`
prefix and runs `git check-ignore` inside the data-lake clone when one is
present locally.

Judgment records under `docs/decisions/` are an exception: they live in
spectrum-systems-core (they document architectural reasoning that
belongs alongside the constitution).

Templates use the placeholders `<artifact_id>` (UUID), `<source_id>`
(transcript slug), `<run_id>`, `<failure_id>`, and `<source_artifact_id>`.
The audit substitutes synthetic strings into these placeholders before
calling `git check-ignore`.

## Artifact Types

### meeting_extraction
- **Writer:** `extraction/typed_extraction_runner.py` via
  `extraction/extraction_merger.py::ExtractionMerger.write_to`
- **Path template:** `data-lake/store/artifacts/extractions/<source_artifact_id>_meeting_extraction.json`
- **Schema:** `src/spectrum_systems_core/schemas/meeting_extraction.schema.json`
- **Optional top-level fields:**
  - `prompt_version` (Phase P2-B): SHA-256 digest of the canonical
    prompt template (`extraction/_prompt_blocks.py::compute_prompt_version`).
    Format `sha256:<12 hex chars>`. Optional so artifacts written
    before Phase P2-B still validate.
  - `extraction_mode` (Phase P3-A): `"two_stage"` (default) or
    `"single_pass"`. Stamped from the `EXTRACTION_MODE` env var so
    a regression triage can confirm which prompt path produced the
    artifact. Operator rollback path: set `EXTRACTION_MODE=single_pass`
    to revert to the pre-P3 single-prompt extraction.
  - `glossary_version` (Phase P3-A): integer version of the
    versioned glossary artifact that was injected into prompts
    during this run. Distinct from `prompt_version` so a glossary
    edit (no prompt template change) is still traceable.
  - `off_topic_rate` (Phase P3-A): float in `[0,1]`, fraction of
    classifier output that came back as `off_topic`.
  - `extraction_path_breakdown` (Phase P3-A): map of classification
    label to count.
  - `source_turn_orphan_rate` (Phase P3-A): float in `[0,1]`,
    fraction of extracted items whose `source_turn_ids` reference
    chunk ids not present in `chunks.jsonl`.
  - `source_turn_diversity_rate` (Phase P3-A): float in `[0,1]`,
    unique-turn / total-turn ratio computed across all extracted
    items. A low value means the model is over-citing a tiny
    cluster of chunks.
  - `stakeholders_populated_rate`, `rationale_populated_rate`,
    `claim_type_populated_rate` (Phase P3-A): per-field
    population rates surfaced into the post-extraction eval_summary
    so prompt tuning needs are visible. Rates < 0.8 emit a warn
    finding (`low_field_population_rate`) but never halt the run.
- **Git-tracked:** YES — required by `select_few_shot_examples.py`,
  `_few_shot_preflight.py`, the validate-and-baseline gate, and the
  evals runner.
- **Readers:** `scripts/select_few_shot_examples.py`,
  `scripts/_few_shot_preflight.py`,
  `scripts/_artifact_validator.py`,
  `evals.m4.runner`.

### meeting_minutes
- **Writer:** governed loop — `workflows/meeting_minutes.py`
  (deterministic regex), `workflows/meeting_minutes_llm.py`
  (live-LLM, default-off flag), routed by `workflows/dispatch.py`.
- **Path template:** `data-lake/store/processed/meetings/<meeting_id>/meeting_minutes__<slug>.json`
- **Schema:** `src/spectrum_systems_core/schemas/meeting_minutes.schema.json`
  — a flat content projection (`artifact_type` + string
  `schema_version` + content fields), the same shape pattern
  `meeting_extraction.schema.json` uses. NOT wired into the governed
  loop's write path: `meeting_minutes` structural validation still
  runs through `evals/runner.py` (required-field eval) and, on the
  LLM path, `evals/llm_extraction.py` (strict-schema eval). The
  schema file is the type-checking contract used by
  `tests/test_meeting_minutes_schema.py` and is available to any
  future `validate_artifact(..., "meeting_minutes")` caller.
- **Additive optional fields (all default `[]` / absent; legacy
  artifacts without them still validate):**
  - `action_items` items may be a legacy string OR a structured
    object carrying an optional `status`
    (`open` / `in_progress` / `completed`).
  - `open_questions` items may be a legacy string OR a structured
    Q&A-log object (`question_id`, `question_text`, `asked_by`,
    `category`, `initial_response`, `follow_up_action`, `resolved`).
  - `decisions` items may be a legacy string OR a structured object
    `{text (required), verb, stakeholders[], confidence (0.0-1.0,
    nullable)}`. `text`/`verb` match what `evals/regulatory_verb.py`
    reads, so an object-form decision is still verb-classified, not
    bypassed. `stakeholders` / `confidence` are the architecture-review
    fields.
  - New optional arrays: `commitments`, `risks`, `cross_references`,
    `attendees`, `topics`, `regulatory_references`,
    `technical_parameters`, `named_artifacts`, `scheduled_events`.
  - The live-LLM path now carries ALL of the above through to the
    promoted artifact: `workflows/meeting_minutes_llm._parse_llm_payload`
    preserves the structured object forms verbatim (never coerced to
    string, never dropped) and defaults every omitted new array to `[]`
    (never `null`, never absent). The LLM strict-schema eval validates
    the whole flat payload against this schema file BEFORE promotion,
    so a schema violation blocks the write rather than shipping a
    malformed artifact.
- **schema_version 1.2.0 additive optional fields** (all optional;
  every legacy 1.0.0 / 1.1.0 artifact validates unchanged — proven by
  `tests/test_meeting_minutes_schema.py` across all 6 golden
  transcripts on all three versions). "Within-source coverage" =
  whether the field's text is a verbatim/near-verbatim span of the
  transcript that the within-source eval could check.
  - `rationale` — on each `decisions` object item. Purpose: the
    stated reason WHY a decision was made (the explicit
    justification, distinct from background context). Within-source
    coverage: yes (verbatim speaker justification). Set by: extraction
    model.
  - `external_references` — on each `claims` object item (the
    `claims` array is itself new in 1.2.0). Purpose: documents,
    ITU articles, or CFR sections explicitly cited as evidence for
    the claim. Within-source coverage: no (proper-noun citations,
    not verbatim spans — same exclusion class as `named_artifacts`).
    Set by: extraction model.
  - `evidence_in_transcript` — on each `claims` object item.
    Purpose: `turn_id`s where evidence SUPPORTING the claim was
    presented, deliberately distinct from the PR #128
    grounding/`source_turns` (where the claim was STATED); the
    PR #128 grounding contract is unchanged. Within-source coverage:
    n/a (a `turn_id` list, not extracted text). Set by: extraction
    model.
  - `follow_up_required` — on each `action_items` object item.
    Purpose: true when a human must act before the next meeting,
    false for completed/informational items (producer default
    true). Within-source coverage: no (a status flag, not text).
    Set by: extraction model.
  - `word_level_timestamps` — top-level boolean. Purpose: whether
    the transcript's `chunks.jsonl` carries word-level timestamps;
    false for the current docx inputs, infrastructure for future
    diarized transcripts. Within-source coverage: n/a (ingestion
    signal). Set by: chunker (`data_lake/chunker.py`), never the
    extraction model; surfaces onto the artifact header on the
    grounded path.
  - `sentiment_indicators` — top-level array of
    `{turn_id, speaker, sentiment, text_preview}`; `sentiment` is one
    of `disagreement` / `concern` / `strong_endorsement` /
    `uncertainty` / `frustration`. Purpose: speaker turns flagged for
    notable, unambiguous sentiment (a high bar — never routine formal
    language). Within-source coverage: yes (`text_preview` is the
    leading slice of the flagged turn). Set by: extraction model.
  - `meeting_phases` — top-level array of
    `{phase_id, phase_name, start_turn_id, end_turn_id, summary}`;
    `phase_name` is one of `opening` / `working_session` / `q_and_a`
    / `wrap_up` / `other`. Purpose: ordered high-level segmentation of
    the meeting. Within-source coverage: no (structural segmentation;
    `summary` is a paraphrase). Set by: extraction model.
  - Carry-through: `claims`, `sentiment_indicators`, and
    `meeting_phases` are added to
    `workflows/meeting_minutes_llm._STRUCTURED_ARRAYS` so a
    model-emitted value reaches the artifact and is validated
    fail-closed by the strict-schema eval (an explicit `null` or a
    malformed item blocks promotion — never silently dropped). The
    Opus reference-baseline workflow derives its extraction types
    from this schema, so all three new arrays have
    `_PRIMARY_TEXT_FIELD` mappings (`claim_text` / `text_preview` /
    `phase_name`).
- **Git-tracked:** NO — `meeting_minutes` product artifacts live in
  the separate `nicklasorte/data-lake` repo under
  `processed/meetings/` (data-lake contract §6.1: only promoted
  artifacts written there). spectrum-systems-core carries only the
  schema source file under `src/`; the artifact payloads are never
  committed to this repo (root `.gitignore` carries `data-lake/`).
- **Readers:** `data_lake/pipeline.py` (promotion + index),
  `evals/runner.py`, `evals/llm_extraction.py` (LLM path only).

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
- **Phase Z.4:** carries an optional `spurious_add_count` (integer ≥ 0)
  — the count of merged items the post-hoc verifier marked
  unsupported/contradicted, surfaced from the existing verification
  summary. Additive: path and Git-tracked status are unchanged;
  pre-Z.4 artifacts and blocked runs that never ran the verifier
  remain schema-valid because the property is optional.

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
  single-transcript debug run — SELF-REFERENTIAL, derived from
  pipeline output),
  `scripts/create_human_gt_pairs.py` (extracts NON-circular pairs
  directly from the human-authored minutes `.docx`; emitted as the
  `human_minutes_gt_pairs` JSONL below, not as per-pair files here).
- **Path template:** `data-lake/store/artifacts/ground_truth/<artifact_id>.json`
- **Schema:** `contracts/schemas/ingestion/ground_truth_pair.schema.json`
  — extended with optional `extraction_type`, `human_authored`,
  `verified`, `verified_by` fields and a new `provenance.produced_by`
  value `HumanMinutesGTPairs`. All additions are optional so existing
  GroundTruthLinker / GenerateGTPairs pairs continue to validate.
- **Git-tracked:** YES — the eval-ground-truth CLI reads every pair
  in this directory.
- **Readers:** `evals.m4.runner` via `eval-ground-truth` CLI,
  `scripts/annotate_rubric.py`,
  `scripts/review_gt_pairs.py` (Phase P1).

### human_minutes_gt_pairs
- **Writer:** `scripts/create_human_gt_pairs.py` (the
  create-human-gt-pairs workflow). One schema-valid
  `ground_truth_pair` envelope per JSONL line, extracted by
  `claude-sonnet-4-20250514` from the human-authored meeting minutes
  `.docx`. Carries `human_authored: true`, `verified: true`,
  `verified_by`, and `provenance.produced_by == "HumanMinutesGTPairs"`.
  These pairs are NON-circular: the pipeline's `meeting_extraction`
  output is never read as input — only the minutes `.docx` and
  `source_record.json` (the ingestion identity record).
- **Path template:** `data-lake/store/processed/meetings/<source_id>/ground_truth/human_minutes_gt_pairs.jsonl`
- **Schema:** `contracts/schemas/ingestion/ground_truth_pair.schema.json`
  (one envelope per line; same schema as `ground_truth_pair`).
- **Git-tracked:** NO — the path lives under `processed/`, which the
  `nicklasorte/data-lake` repo bulk-ignores via `**/processed/**`
  (the same rule that shadows everything except the explicitly
  un-ignored `source_record.json`). The create-human-gt-pairs and
  rubric workflows stage this exact path via `push-data-lake`, but
  for the commit to actually land the **data-lake repo** must carry
  the GENERAL negation
  `!**/processed/**/ground_truth/human_minutes_gt_pairs.jsonl`
  (covering every `<source_id>`, not just Dec 18) mirroring the
  existing `!**/processed/**/source_record.json` precedent. The
  `create-human-gt-pairs-batch` workflow ENSURES this exact general
  line in the data-lake clone's `.gitignore` (idempotent — only
  appends when absent) and commits it in the same single push, so the
  batch path is self-healing; a missing or Dec-18-specific negation in
  the data-lake repo no longer makes the `git add` a silent no-op for
  later transcripts. Per-artifact gitignore
  enforcement inside the data-lake repo is that repo's
  responsibility (see spectrum-systems-core `.gitignore` comment);
  the spectrum-systems-core `_gitignore_audit.py` only audits
  `Git-tracked: YES` entries, so this entry does not gate CI.
- **Readers:** `scripts/annotate_rubric.py` (via the new `--gt-file`
  seam), `evals.m4.runner` via `eval-ground-truth` once the data-lake
  negation is in place.

### opus_reference_minutes
- **Writer:** `scripts/create_opus_reference_baselines.py` (the
  create-opus-reference-baselines workflow). One JSONL line per
  extracted item. The Opus model string is NEVER hardcoded: the
  workflow resolves it from `ai/registry/model_registry.json` (the
  `opus_reference_baseline` key) at run time and the script stamps it
  into every line as `model_id`, so a past baseline keeps its exact
  model even after the registry is rolled forward. Each line carries
  `human_authored: false`, `model_authored: true`, `verified: false`,
  `status: "reference_only"`, and
  `provenance.produced_by == "opus_reference_baseline_workflow"`. These
  are NOT ground truth and NOT product artifacts — they are a stronger
  model's read of the SAME raw transcript, produced with the SAME
  canonical extraction prompt (`workflows/prompts/meeting_minutes_llm.md`)
  the Haiku pipeline uses, for human/eval comparison only. The script
  reads ONLY the raw transcript `.docx` and `source_record.json` (the
  ingestion identity record) — never any existing extraction artifact.
- **Path template:** `data-lake/store/processed/meetings/<source_id>/reference_baselines/opus_reference_minutes.jsonl`
- **Schema:** per-item `item_data` conforms to the matching array-item
  shape in `src/spectrum_systems_core/schemas/meeting_minutes.schema.json`
  (all 13 array types from PR #123; extraction types are derived from
  that schema's array properties so there is no parallel list to drift).
  The JSONL line envelope itself is the reference-baseline record shape
  documented in the script docstring.
- **Git-tracked:** NO — same reasoning as `human_minutes_gt_pairs`: the
  path lives under `processed/`, which the `nicklasorte/data-lake` repo
  bulk-ignores via `**/processed/**`. The create-opus-reference-baselines
  workflow ENSURES the GENERAL negation
  `!**/processed/**/reference_baselines/opus_reference_minutes.jsonl`
  in the data-lake clone's `.gitignore` (idempotent — only appends when
  absent) and commits it in the same push, mirroring the existing
  `!**/processed/**/source_record.json` precedent.
  `scripts/create_opus_reference_baselines.py` ALSO refuses to leave
  behind the artifact if it is still shadowed after the write (it shells
  `git check-ignore` and halts with `gitignore_blocks_artifact`). Per-
  artifact gitignore enforcement inside the data-lake repo is that
  repo's responsibility; `_gitignore_audit.py` only audits
  `Git-tracked: YES` entries, so this entry does not gate CI.
- **Readers:** none in-loop (reference baselines are NEVER read back
  into the governed loop). Consumed by humans / future eval comparison
  only.

### comparison_result
- **Writer:** `scripts/compare_opus_haiku.py` (the compare-opus-haiku
  workflow — System 1 of the self-improvement loop). ONE JSON object
  per file, the full `comparison_result` envelope. ZERO model calls:
  the diff is pure case-insensitive, whitespace-normalized substring
  matching (the same rule as the `extraction_within_source_required`
  eval), so the artifact is replay-stable for the same Opus baseline +
  Haiku artifact inputs. The script reads ONLY the Opus reference
  baseline, the promoted Haiku `meeting_minutes` artifact (whose
  `payload.provenance.produced_by` MUST be `meeting_minutes_llm` — a
  regex-extractor artifact is rejected fail-closed), and (optionally)
  the human GT pairs. It never reads a model.
- **Path template:** `data-lake/store/processed/meetings/<source_id>/comparisons/haiku_vs_opus_<run_id>.json`
- **Schema:** `src/spectrum_systems_core/schemas/comparison_result.schema.json`
  (validated by the script BEFORE the write — a malformed
  comparison_result is never written).
- **Git-tracked:** NO — same reasoning as `opus_reference_minutes`:
  the path lives under `processed/`, which the `nicklasorte/data-lake`
  repo bulk-ignores via `**/processed/**`. The compare-opus-haiku
  workflow ENSURES the general negations
  `!**/processed/**/comparisons/*.json` and
  `!**/processed/**/eval_history.jsonl` in the data-lake clone's
  `.gitignore` (idempotent — only appends when absent) and commits
  them in the same push, mirroring the existing
  `!**/processed/**/source_record.json` precedent. Per-artifact
  gitignore enforcement inside the data-lake repo is that repo's
  responsibility; `_gitignore_audit.py` only audits `Git-tracked: YES`
  entries, so this entry does not gate CI.
- **Readers:** `scripts/correction_miner.py` (System 2 — reads every
  `comparison_result` for a source to mine systematic failure
  patterns). Never read back into the governed loop.

### gt_pair_review
- **Writer:** `scripts/review_gt_pairs.py` (Phase P1 — human-in-the-loop
  confirmation of a pair's `expected_decision_outcome`).
- **Path template:** `data-lake/store/artifacts/ground_truth/<pair_id>_review.json`
- **Schema:** `src/spectrum_systems_core/schemas/gt_pair_review.schema.json`
- **Git-tracked:** YES — the Phase P1 eval gate refuses to score a
  ground_truth_pair until a sibling review artifact with
  `outcome_confirmed: true` is present. Storing the review under the
  same `ground_truth/` directory as the pair keeps the two artifacts
  co-located on disk; the `_review.json` filename suffix is the only
  thing that distinguishes the two from a directory glob.
- **Readers:** `evals.m4.runner` (the Phase P1 alignment gate),
  `scripts/review_gt_pairs.py` (idempotency check on re-run).

### eval_summary (incl. baseline)
- **Writer:** `evals/m4/runner.py` via `eval-ground-truth` CLI,
  `validate-and-baseline.yml`.
- **Path templates:**
  - `data-lake/store/artifacts/evals/baseline_eval_summary.json`
    (the development or production baseline)
  - `data-lake/store/artifacts/evals/eval_summary_<run_id>.json`
    (per-run summary)
- **Schema:** `src/spectrum_systems_core/schemas/eval_summary.schema.json`
  (mirror copy: `contracts/schemas/eval/eval_summary.schema.json`).
- **Optional top-level fields (Phase P2):**
  - `prompt_version` — mirrors the field from the
    `meeting_extraction` artifact this summary scored. Enables
    regression triage when a coverage drop coincides with a prompt
    edit.
  - `calibration_data` — list of per-decision
    `(decision_index, confidence, aligned, outcome, source_id)`
    records. Seeds future Expected-Calibration-Error computation
    once the corpus crosses ~30 decisions.
  - `calibration_note` — human-readable summary of the calibration
    sample size.
  - `judge_human_agreement_rate` — fraction of judged decisions
    whose verdict matches the human-confirmed ground-truth outcome.
    Null when no `judge_score` artifact is on disk for this run.
  - `judge_pass_rate` — aggregate fraction of decisions that
    passed the judge rubric (mirrors `judge_score.aggregate_pass_rate`).
  - `judge_evaluated_count` — number of decisions the judge
    scored.
  - `judge_calibration_note` — human-readable explanation when
    one of the judge metrics is null.
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
  `scripts/update_glossary.py` (Phase P3-A operator script),
  `glossary.glossary_builder`.
- **Path template:** `data-lake/store/artifacts/glossary/spectrum_glossary_v1.json`
- **Additional version-pinned templates (Phase P3-A):**
  - `data-lake/store/artifacts/glossary/spectrum_glossary_v<N>.json`
    where `<N>` is the integer `glossary_version`. The runner
    resolves the active version via the `GLOSSARY_VERSION` env var
    (`latest` reads the highest-numbered file; `<N>` pins to that
    file so a regression can be bisected against a prior glossary).
- **Schema:** `src/spectrum_systems_core/schemas/spectrum_glossary.schema.json`
- **Git-tracked:** YES — the term-injector reads this versioned
  artifact on every extraction run.
- **Readers:** `glossary.glossary_builder.load_versioned_glossary`,
  `glossary.term_injector`,
  `extraction.typed_extraction_runner` (stamps `glossary_version`
  on every `meeting_extraction` artifact).

### chunk_classifications
- **Writer:** `extraction/typed_extraction_runner.py`
  (Phase P3-A aggregate write, one file per source_artifact_id).
- **Path template:** `data-lake/store/artifacts/extractions/<source_artifact_id>_chunk_classifications.json`
- **Schema:** `src/spectrum_systems_core/schemas/chunk_classifications.schema.json`
- **Git-tracked:** YES — required for extraction-path auditing and
  for diagnosing off-topic rate regressions when an eval_summary
  reports a drop in coverage.
- **Readers:** diagnostic scripts; `evals.m4.runner` reads
  `extraction_path_breakdown` and `off_topic_rate` from the
  companion `meeting_extraction` field set instead of re-deriving
  from this aggregate. The aggregate is forensic provenance.
- **Skipped when:** `EXTRACTION_MODE=single_pass` — the aggregate
  is a record of the routing step, so single-pass runs produce no
  classifications artifact (operator can grep the absence to
  confirm rollback took effect).

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

### extraction_comparison (Phase AB instrument)
- **Writer:** `extraction/comparison_runner.py::run_compare_extraction`
  (the `compare-extraction` CLI). Written directly via
  `serialize.canonical_json` — NOT through `write_promoted_artifact`,
  because it is a run-level measurement record (like `manifest__` /
  `debug__`), not a promoted product artifact.
- **Path template:** `data-lake/store/processed/meetings/<meeting_id>/extraction_comparison__<slug>.json`
  (slug == `<meeting_id>`; re-runs overwrite, never accumulate).
- **`<meeting_id>` source:** either a lake meeting directory name
  (`--meeting-id`, requires a chunked `source_record`) OR the
  slugified stem of a flat transcript file passed via
  `--transcript-file` (`comparison_runner.slugify`: lowercase, each
  run of non-`[a-z0-9]` → one hyphen, ends trimmed). The flat-file
  mode requires no `source_record`; the Haiku point sees the raw
  transcript with no turn ids. Path template and Git-tracked status
  are unchanged by the source mode.
- **Schema:** envelope `schema_version: 1` (integer — the system
  constitution §6 binds the envelope to an integer; this is
  unchanged by Phase AC). `payload`: `meeting_id`,
  `transcript_artifact_id`, `extractor_status`
  (`{regex,haiku,opus}` each `ok|failed:<reason>`), `regex_output`
  / `haiku_output` (`{decisions,actions,questions}` lists of
  `{text,...}`), `opus_output_ref` (the `extraction_unconstrained`
  `artifact_id`, or `null` when Opus failed). Envelope `status` is
  `promoted` only when all three extractors succeeded, else
  `rejected` — but the file is written either way (it exists to
  explain the run).
- **Payload schema version (Phase AC.1):** the payload now carries a
  string `schema_version` semantic-version marker, distinct from the
  integer envelope `schema_version` (same flat-content-projection
  pattern `meeting_minutes.schema.json` uses). Two documented
  generations:
  - **`1.0.0`** (legacy, pre-Phase-AC): payload has NO
    `schema_version` key. Readers treat its absence as `1.0.0`. The
    raw extractor outputs are the only payload content; gap /
    per-entity metrics are derived on demand and not stored here.
  - **`1.1.0`** (Phase AC.1): payload carries
    `schema_version: "1.1.0"`. The raw extractor outputs are
    UNCHANGED (so a 1.0.0 reader of a 1.1.0 artifact still works,
    and the new code reading an old 1.0.0 artifact falls back to the
    aggregate-only view — backward compatible both directions). The
    per-entity F1 drill-down is produced by
    `evals.extraction_gap.compute_gap_metrics` /
    `compute_per_entity_metrics` against a `comparison_gold`
    independent gold set and is persisted at the CORPUS level (see
    `corpus_comparison`), never inlined into this single-meeting
    record (a meeting has no guaranteed gold set, so storing a
    fabricated 0.0 here would read like a real measurement).
- **Git-tracked:** NO — lives under `processed/`, which the
  `nicklasorte/data-lake` repo bulk-ignores via `**/processed/**`
  (same reasoning as `meeting_minutes` and `human_minutes_gt_pairs`).
  It is a measurement instrument / run-level record, not a promoted
  product artifact, and does not enter
  `indexes/meetings/artifact_index.jsonl`. The Phase AB prompt
  requested `Git-tracked: YES`; that was overridden because the
  binding data-lake contract (§6.1) and the manifest's own
  `processed/` precedent make a `Git-tracked: YES` claim on a
  `processed/` path internally contradictory and a post-merge audit
  trap (the audit only SKIPs it in CI because no data-lake clone is
  present there).
- **Readers:** `evals/extraction_gap.py::compute_gap_metrics`
  (regex/haiku outputs + the Opus-ref text it is given),
  `data_lake/markdown_views.py` (view render). Telemetry cost /
  latency values are real measurements and therefore NOT
  byte-deterministic across runs; the artifact's structural identity
  (`artifact_id`, `created_at`) IS stabilised the same way the
  pipeline stabilises its artifacts.

### extraction_telemetry (Phase AB instrument)
- **Writer:** `extraction/comparison_runner.py::run_compare_extraction`
  (sibling of `extraction_comparison`, same write path).
- **Path template:** `data-lake/store/processed/meetings/<meeting_id>/extraction_telemetry__<slug>.json`
  (slug == `<meeting_id>`).
- **Schema:** `schema_version: 1`. `payload`: `meeting_id`,
  `comparison_artifact_id`, and per-extractor `regex` / `haiku` /
  `opus` blocks carrying `cost_usd`, `latency_ms`, and (haiku/opus)
  `model`. Envelope `status` mirrors the comparison artifact.
- **Git-tracked:** NO — same reasoning as `extraction_comparison`
  (run-level measurement record under `processed/`, not a promoted
  product; cost/latency are non-deterministic real measurements).
- **Readers:** `data_lake/markdown_views.py` (cost table render);
  human operators / future cost-vs-quality analysis.

### extraction_unconstrained (Phase AB instrument)
- **Writer:** `extraction/comparison_runner.py::run_compare_extraction`
  (written only when the Opus extractor succeeded).
- **Path template:** `data-lake/store/processed/meetings/<meeting_id>/extraction_unconstrained__<slug>.json`
  (slug == `<meeting_id>`).
- **Schema:** `schema_version: 1`. `payload`: `meeting_id`,
  `raw_output` (str, OPAQUE), `model`, `prompt`, `cost_usd`,
  `latency_ms`.
- **PIPELINE INVARIANT (non-negotiable):** `payload.raw_output` is
  NEVER parsed or used by any eval, the control function, the
  promotion gate, or the governed loop. The ONLY code permitted to
  parse it is `evals/extraction_gap.py::parse_opus_output` (the
  explicitly approximate, deterministic, non-LLM gap parser). A
  source-level guard test
  (`tests/integration/test_opus_output_never_parsed.py`) asserts the
  token `raw_output` appears in no `evals/*.py` file except
  `extraction_gap.py`, and runs every registered eval against an
  `extraction_unconstrained` artifact whose `raw_output` carries a
  sentinel, asserting the sentinel never leaks into any eval_result.
- **Git-tracked:** NO — same reasoning as `extraction_comparison`
  (run-level measurement record under `processed/`, not a promoted
  product). Captured only for human comparison and the gap metric.
- **Readers:** `evals/extraction_gap.py` (the single approximate
  parser); `data_lake/markdown_views.py` renders the raw text
  verbatim in a fenced block (display only — quoting opaque text in
  a non-canonical view is not "parsing"; markdown is neither an eval
  nor the control gate).

### corpus_comparison (Phase AC instrument)
- **Writer:** `extraction/corpus_runner.py::run_compare_corpus`
  (the `compare-corpus` CLI). Written directly via
  `serialize.canonical_json` (the same write path as
  `extraction_comparison`) — NOT through `write_promoted_artifact`,
  because it is a corpus-level measurement record, not a promoted
  product artifact.
- **Path template:** `data-lake/store/processed/corpus/<corpus_id>/corpus_comparison__<corpus_id>.json`
  where `<corpus_id>` is `corpus-<16 hex>`, a deterministic hash of
  the sorted meeting ids + the transcripts dir (re-runs over the same
  corpus reuse the id and overwrite — never accumulate; same
  precedent as `extraction_comparison` slug==meeting_id). Lives under
  `processed/corpus/`, a sibling of `processed/meetings/`, so a corpus
  run never collides with a single meeting.
- **Schema:** envelope `schema_version: 1` (integer; constitution §6).
  `payload` carries a string `schema_version: "1.0.0"` plus
  `corpus_id`, `transcripts_dir`, `meeting_ids`,
  `discovery_findings` (`skipped_non_txt:<name>` for non-`.txt`
  inputs), `per_meeting` (`{<meeting_id>: {comparison_artifact_id,
  extractor_status {haiku,opus} each `ok|failed:<reason>|retry:<cmd>`,
  per_entity_f1 (`{decisions,actions,questions}` each `{haiku,opus}`)
  OR null when no gold, per_entity_metrics (full diagnostic incl
  `partial_items`) OR null, gold_present, findings}}`), `aggregate`
  (`per_entity_f1` unweighted mean of gold-backed successful meetings,
  `per_entity_f1_n_averaged` how many fed each mean,
  `total_cost_usd`/`total_latency_ms` summed across meetings,
  `meetings_processed`, `meetings_failed`), and `corpus_status`
  (`complete` all ok / `degraded` ≥1 extractor failure or empty
  transcript / `rejected` <50% of meetings succeeded for either
  extractor). Envelope `status` is `promoted` only when
  `corpus_status == "complete"`, else `rejected` — but the file is
  written whenever ≥1 transcript was attempted (it exists to explain
  the corpus run, exactly like `extraction_comparison`).
- **Git-tracked:** NO — same reasoning as `extraction_comparison` /
  `extraction_telemetry` / `extraction_unconstrained`: it lives under
  `processed/`, which the `nicklasorte/data-lake` repo bulk-ignores
  via `**/processed/**`; it is a run-level measurement instrument, not
  a promoted product artifact, and does not enter
  `indexes/meetings/artifact_index.jsonl`. The Phase AC prompt's
  red-team Pass 3 checklist requested `Git-tracked: YES`; that is
  overridden here for the SAME documented reason the manifest already
  overrode the identical Phase AB request for `extraction_comparison`
  (above): the binding data-lake contract §6.1 and the manifest's own
  `processed/` precedent make a `Git-tracked: YES` claim on a
  `processed/` path internally contradictory and a post-merge audit
  trap. Aggregate cost / latency are real (non-deterministic)
  measurements; the artifact's structural identity (`artifact_id`,
  `created_at`) IS stabilised the same way the pipeline stabilises
  its artifacts, and the Markdown projection is byte-deterministic
  from the stored payload.
- **Readers:** `data_lake/markdown_views.py::render_corpus_comparison_markdown`
  (view render). No in-loop reader — corpus_comparison is a human /
  cost-vs-quality analysis instrument, never read back into the
  governed loop.

### cross_meeting_synthesis
- **Writer:** `scripts/create_cross_meeting_synthesis.py` (the
  create-cross-meeting-synthesis workflow). ONE JSON object per file.
  A SINGLE Opus pass over EVERY promoted `meeting_minutes` artifact in
  the data-lake (the STRUCTURED product artifacts — it NEVER opens a
  raw transcript or raw metadata; the meeting date is derived from the
  promoted artifact's own `source_id` slug). The Opus model string is
  NEVER hardcoded: the workflow resolves it from
  `ai/registry/model_registry.json` (the `complex_reasoning` key — the
  opus entry, which names exactly this cross-system-reasoning task) at
  run time and the script stamps it into the artifact as `model_id`,
  so a past synthesis keeps its exact model even after the registry is
  rolled forward. The cross-meeting analogue of `comparison_result` /
  `corpus_comparison`: an instrument artifact, NOT a promoted product,
  never read back into the governed loop, never in the artifact index.
  Halts fail-closed with `insufficient_corpus` when fewer than
  `max(--min-meetings, 2)` promoted `meeting_minutes` artifacts exist
  (a single meeting cannot be synthesized across).
- **Path template:** `data-lake/store/artifacts/synthesis/cross_meeting_synthesis_<datestamp>.json`
  (timestamped per run; a synthesis is written once and never
  overwritten — the Opus pass is not byte-stable, exactly like
  `opus_reference_minutes`).
- **Schema:** `src/spectrum_systems_core/schemas/cross_meeting_synthesis.schema.json`
  — a flat artifact (`artifact_type` const + string
  `schema_version: "1.0.0"` + the synthesis fields), the same shape
  pattern `comparison_result.schema.json` uses. The script validates
  its OWN output against this schema via
  `_artifact_validator.validate_artifact` before writing; it also
  validates every promoted `meeting_minutes` it reads (the flat
  `{"artifact_type": "meeting_minutes", **payload}` form) before
  reading any field (CLAUDE.md read-path co-requirement). Every
  `*_id` is re-stamped with a frozen-namespace UUID5, every `*_date`
  is overridden with the date the script derives from the cited
  `source_id`, `open_actions[].status` is recomputed from
  `closed_meeting` corpus membership, and `decision_threads[].open`
  is recomputed from decision status — so model id/date/status quality
  can never corrupt the artifact and a closure recorded in any meeting
  (the whole corpus is read in one pass) can never be mislabelled
  "open".
- **Git-tracked:** YES — it lives under `store/artifacts/`, the same
  committed tree as `meeting_extraction` /
  `decision_few_shot_examples` / `ground_truth_pair` (NOT under
  `processed/`, which the data-lake repo bulk-ignores). The
  create-cross-meeting-synthesis workflow idempotently ensures the
  data-lake `.gitignore` carries `!**/artifacts/**/` (re-include the
  directory chain) and `!**/artifacts/synthesis/*.json` before the
  push, and the script's own `gitignore_blocks_artifact` guard refuses
  to leave behind an artifact git cannot commit. When the data-lake
  clone is absent (e.g. the pytest CI job) `_gitignore_audit.py` skips
  this path cleanly, exactly as it does for every other `data-lake/`
  entry.
- **Readers:** none in-loop. The cross_meeting_synthesis is a
  human cross-meeting analysis instrument, never read back into the
  governed loop and never an eval input.

## Runtime / debug artifacts (intentionally NOT git-tracked)

These are recorded here for completeness so future authors do not
accidentally git-track them. They are produced by the pipeline as
debug or runtime state and live under `data-lake/` only when
`DATA_LAKE_PATH` is set; they are not part of the repo baseline.

### regulatory_verb_result (eval sub-type)
- Phase Z.2 eval, runs over decision-bearing artifacts
  (`meeting_minutes`, `decision_brief`) inside the core eval runner.
- Surfaced as the `payload.eval_type = "regulatory_verb"` value on
  the normal `eval_result` envelope; not a separate file on disk.
- Git-tracked: NO — eval results live only inside the run manifest
  and the per-meeting debug report (see contract §6.2). The
  promotion gate applies to product artifacts, not eval sub-types.

### extraction_precision_result (eval sub-type)
- Phase Z.3 eval, runs alongside `source_turn_validity` inside the
  transcript pipeline. Surfaced as
  `payload.eval_type = "extraction_precision"` on the normal
  `eval_result` envelope.
- Git-tracked: NO — same reasoning as
  `regulatory_verb_result`. Eval results are run-level provenance,
  not promoted product artifacts.

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

### llm_extraction eval sub-types
- Phase: live-LLM `meeting_minutes_llm` workflow. Four evals run only
  for that workflow (passed via `run_governed_loop(extra_evals=...)`),
  surfaced as the `payload.eval_type` value on the normal
  `eval_result` envelope — not separate files on disk:
  `llm_extraction_strict_schema`, `llm_extraction_nonempty_required`,
  `extraction_within_source_required`,
  `extraction_vs_human_minutes_coverage`.
- Git-tracked: NO — same reasoning as `regulatory_verb_result`. Eval
  results are run-level provenance, not promoted product artifacts.
  The regex `meeting_minutes` path never emits these (mutual
  exclusion at dispatch), so no existing artifact's shape changes.

### llm_extraction eval_history projection
- Path: `data-lake/store/processed/meetings/<source_id>/eval_history.jsonl`
- Writer: `workflows/llm_eval_history.py` (shape-identical to
  `data_lake/eval_history.py`; written only for the LLM workflow when
  a `lake_root` is supplied, for GT-coverage threshold auditability).
- Git-tracked: NO — harness memory, not authority (data-lake contract
  §6.4). Covered by the `**/processed/**` ignore.

## Gitignore Audit Rule

Every path listed above as **Git-tracked: YES** must satisfy:

```
git check-ignore -v <instantiated_path> → returncode != 0
```

That is: the path MUST NOT be ignored by any rule in the
`.gitignore` of the repo that owns it (spectrum-systems-core for
paths under `docs/decisions/`, nicklasorte/data-lake for paths
under `data-lake/`). The `scripts/_gitignore_audit.py` script
enforces this on every PR by parsing this file, instantiating each
path template with synthetic ids, and shelling out to
`git check-ignore` against the appropriate repo:

* Paths whose template begins with `data-lake/` are audited against
  the data-lake repo's gitignore (when a local clone is present
  under `./data-lake/`). When the clone is absent (e.g. forked PR
  without `DATA_LAKE_TOKEN`), the audit reports the data-lake
  paths as "SKIP" and exits 0 — the remaining checks still bind.
* All other paths are audited against the spectrum-systems-core
  gitignore as before.

The audit also asserts spectrum-systems-core's `.gitignore` carries
the `data-lake/` rule so the separate data-lake repo cannot
accidentally be re-committed into spectrum-systems-core.

If the audit fails on a `Git-tracked: YES` artifact, the fix is
either:

1. Add an explicit un-ignore (`!<path>`) to the **owning repo's**
   `.gitignore`, OR
2. Move the artifact to a different on-disk path that is not
   shadowed by a broader rule, OR
3. If the artifact is genuinely runtime-only, change the manifest
   entry to **Git-tracked: NO** and remove any workflow that
   `git add`s it.

The audit MUST pass before any PR that touches an artifact path or
a `.gitignore` rule can be merged.
