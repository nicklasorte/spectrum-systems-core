# spectrum-systems-core

Spectrum Systems Core is a governed AI pipeline that transforms federal
government meeting transcripts into structured, publication-ready spectrum
study artifacts. It is not a demo or MVP — it is a production pipeline with
LLM extraction via Claude Haiku, source attribution and grounding
verification, eval-gated promotion, and a self-improving correction loop
that compares Haiku output against Opus reference baselines and proposes
targeted, additive prompt improvements via draft PRs.

The constitution that binds every architectural decision is in
`docs/architecture/system_constitution.md`. The data-lake boundary contract
is in `docs/contracts/data_lake_contract.md`.

---

## Architecture: the governed loop

```
Raw transcript (.docx)
  → Speaker-turn chunker (speaker_turn_v1 / blank_line_v1 / recursive_512)
  → Context bundle
  → LLM extraction (Haiku) or deterministic regex extractor
  → Source attribution verification (source_turn_validity eval)
  → Eval gate (all required evals must pass)
  → Control decision (allow / block)
  → Promotion (status: promoted)
  → Data lake (nicklasorte/data-lake)
  → Opus reference baseline comparison (compare_opus_haiku.py)
  → Correction miner (correction_miner.py)
  → Draft PR with additive prompt improvement
```

Every decision flows through `control/decision.py::decide_control`. No
model output decides. Promotion requires `allow`. Missing or failed
required evals block — always. There is no fallback.

---

## Core modules

Active:

| Module | Purpose |
| --- | --- |
| `artifacts` | One envelope, in-memory store, status validation |
| `context` | Context bundle builder |
| `workflows` | Regex workflows + LLM workflow + dispatch router |
| `evals` | Required eval runner, LLM-specific evals, regulatory verb gate, grounding evals |
| `control` | Pure decision function: eval results in, `allow`/`block` out |
| `promotion` | Single path to `promoted` status, gated on `allow` |
| `data_lake` | Transcript pipeline, chunker, index, manifest, debug, harness memory, markdown views |
| `extraction` | Typed extraction runner (decisions, claims, action_items), chunk classifier, glossary manager, two-stage extractor |
| `schemas` | JSON Schema registry; write-time validation with `additionalProperties: false` |
| `config` | Feature flag reader, `llm_extraction` flag, taxonomy (regulatory verbs, claim types, decision outcomes) |
| `ai` | AI adapter (injectable LLM client, prompt registry, memory context builder, grounding eval) |
| `agency` | Agency profile builder, alias normalizer, eval, mitigation and objection predictors |
| `agenda` | Agenda item detector, pipeline integration |
| `glossary` | Per-chunk terminology injection, few-shot loader, versioned glossary builder |
| `governance` | Artifact validator, schema drift scanner, eval coverage scanner, cost trend reporter |
| `harness` | Run history, eval history, outcome memory, override store, workflow comparator |
| `health` | Preflight, smoke filter, index verifier, run summary, blocked-chunk text check |
| `ingestion` | DOCX extractor, PDF extractor + guard, source loader, minutes processor, ground truth linker |
| `obsidian_bridge` | Vault index writer, review gateway, review parser |
| `orchestration` | Pipeline orchestrator |
| `paper` | Paper claim extractor, evidence builder, revision workflow, publication formatter |
| `synthesis` | Theme synthesizer, keynote generator, bundle assembler, synthesis review gateway |
| `verification` | Pipeline state scanner, verification gate, next-phase handoff, post-hoc verifier |

---

## Workflows

### Deterministic (regex) workflows

Four workflows run a deterministic extractor through the same governed
loop (`workflows/_loop.py::run_governed_loop`). Each supplies only an
`artifact_type` string and a `extract(input_text) -> dict` function.

| Workflow | `artifact_type` | Required payload fields |
| --- | --- | --- |
| `meeting_minutes` | `meeting_minutes` | `title`, `summary`, `decisions`, `action_items`, `open_questions` |
| `decision_brief` | `decision_brief` | `title`, `context`, `options`, `recommendation`, `rationale` |
| `agency_question_summary` | `agency_question_summary` | `title`, `agency`, `question`, `summary`, `citations` |
| `meeting_action_log` | `meeting_action_log` | `title`, `meeting_ref`, `actions`, `open_count` |

### LLM workflow

`meeting_minutes_llm` — live Haiku extraction behind the `llm_extraction`
feature flag (default off). When enabled, this workflow:

1. Chunks the transcript into speaker turns (deterministic, same input →
   same turn_ids).
2. Sends the raw transcript + turn block to Haiku with the canonical
   extraction system prompt (`workflows/prompts/meeting_minutes_llm.md`).
3. Parses the JSON response and runs 6 evals — the 2 standard required
   evals plus 4 LLM-specific evals — through the same `decide_control`
   gate.
4. Emits schema version `1.1.0` (grounded, with `source_turns`) or
   `1.0.0` (ungrounded fallback for whitespace-only transcripts).

Fail-closed: a transport error, malformed JSON, non-object response, or
missing required array produces a payload the strict-schema eval blocks.
There is no fallback to the regex extractor.

### Dispatch

`run_meeting_minutes_dispatch` routes to `meeting_minutes_llm` when
`llm_extraction_enabled()` returns `True`, and to the regex
`meeting_minutes` workflow otherwise.

---

## meeting_minutes content arrays (schema versions 1.0.0, 1.1.0, 1.2.0)

The `meeting_minutes.schema.json` defines 15 content arrays. Three are
required (legacy); twelve are optional structured arrays added
incrementally.

| Array | Status | Primary text field | Notes |
| --- | --- | --- | --- |
| `decisions` | Required | string or `text` (object form) | Object form adds `verb`, `stakeholders`, `confidence`, `rationale` (1.2.0+) |
| `action_items` | Required | string or `action` (object form) | Object form adds `status`, `owner`, `due`, `follow_up_required` (1.2.0+) |
| `open_questions` | Required | string or `question_text` (object form) | Q&A-log object form adds `question_id`, `category`, `resolved` |
| `commitments` | Optional | `commitment_text` | Individual "I will do X" statements distinct from group action_items |
| `risks` | Optional | `risk_text` | Speaker-flagged potential problems with optional `severity` (`low`/`medium`/`high`) |
| `cross_references` | Optional | `ref_text` | References to other meetings or documents (`meeting`/`document`/`report`/`artifact`) |
| `attendees` | Optional | `name` | Roster with required `agency` and optional `role` |
| `topics` | Optional | `title` | Agenda-item segmentation |
| `regulatory_references` | Optional | `reference_text` | OB3, statutory citations, rule references |
| `technical_parameters` | Optional | `value` | Exact numeric values (frequencies, bands, thresholds) stated verbatim |
| `named_artifacts` | Optional | `name` | Documents, folders, reports mentioned by name |
| `scheduled_events` | Optional | `title` | Future meetings/events with dates and purposes |
| `claims` | Optional (1.2.0) | `claim_text` | Factual/analytical assertions; omitted by legacy 1.0.0/1.1.0 artifacts (additivity); adds `external_references`, `evidence_in_transcript` |
| `sentiment_indicators` | Optional (1.2.0) | `text_preview` | Speaker turns flagged for notable sentiment; exactly 5 approved values |
| `meeting_phases` | Optional (1.2.0) | `phase_name` | High-level meeting phases in sequence; exactly 5 approved values |

Additivity: every 1.2.0 field is optional, so a legacy 1.0.0 or 1.1.0
artifact validates against the 1.2.0 schema unchanged. The strict-schema
eval validates the full payload against `meeting_minutes.schema.json`
before promotion.

---

## Typed extraction (meeting_extraction artifact)

The typed extraction pipeline (`extraction/typed_extraction_runner.py`) is
separate from the `meeting_minutes` governed loop. It runs after chunking,
classifies each chunk (`decision` / `claim` / `action_item` / `off_topic`),
routes classified chunks to three specialized Haiku extractors
(`DecisionExtractor`, `ClaimExtractor`, `ActionItemExtractor`), and writes
a `meeting_extraction` artifact to the data lake. Key fields:

- `decisions`, `claims`, `action_items` — typed extracted items with
  `source_turn_ids`, `source_turn_validation` (`verified`/`invalid`/`missing`),
  and `confidence` (0.0–1.0) on every item.
- `few_shot_injected`, `few_shot_version`, `few_shot_example_count` —
  few-shot injection provenance.
- `glossary_version` — versioned spectrum-domain glossary injected into
  every chunk prompt.
- `extraction_path_breakdown` — classifier output counts by label.
- `source_turn_orphan_rate`, `source_turn_diversity_rate` — grounding
  quality signals.
- `stakeholders_populated_rate`, `rationale_populated_rate`,
  `claim_type_populated_rate` — field population rates (warn when < 0.8).

`extraction_mode: two_stage` is the default; `single_pass` is the rollback
path.

---

## Evals

### Standard (all artifact types)

| Eval type | What it gates |
| --- | --- |
| `non_empty_payload` | Payload must not be all-null / all-empty |
| `required_meeting_minutes_fields` | Required fields + per-item `source_turns` (schema 1.1.0) |
| `required_decision_brief_fields` | Required fields present |
| `required_agency_question_summary_fields` | Required fields + non-empty `agency`, `question` |
| `required_meeting_action_log_fields` | Required fields present |
| `regulatory_verb` | Every decision's governing verb must appear in the taxonomy (`DECISION_VERBS`); ambiguous verbs warn but pass; unclassifiable verbs block |

### Pipeline evals (data_lake/pipeline.py)

| Eval type | What it gates |
| --- | --- |
| `source_grounding` | Transcript-source runs: at least one grounding span must be non-empty |
| `transcript_evidence` | Transcript-source runs: at least one content item found in the transcript |
| `content_signal` | Notes/summary runs: content arrays must not all be empty |

### LLM-specific evals (evals/llm_extraction.py)

| Eval type | Blocks? | What it gates |
| --- | --- | --- |
| `llm_extraction_strict_schema` | Yes | Full JSON Schema validation (`meeting_minutes.schema.json`) |
| `llm_extraction_nonempty_required` | Yes | Three legacy arrays non-empty + at least one fact-bearing proxy array (`technical_parameters`, `named_artifacts`, `scheduled_events`) non-empty on a content-bearing transcript |
| `extraction_within_source_required` | Yes | Every extracted string must appear verbatim (case-insensitive, normalized whitespace) in the source transcript |
| `extraction_vs_human_minutes_coverage` | No (observe-only) | Numeric coverage of pipeline output against human-authored GT pairs; emits `coverage_percent` but never blocks |

### Phase Y grounding evals

| Eval type | What it gates |
| --- | --- |
| `source_turn_validity` | Every `source_turns` entry in the grounding array must resolve to a chunk present in the on-disk `source_record.json` |
| `grounding_coverage` | Every content item must have at least one `source_turns` entry |
| `extraction_precision` | Source grounding overlap via LCS similarity against source turns |

---

## Model assignments

All model strings are resolved from `ai/registry/model_registry.json`. No
model string is hardcoded in code or workflow YAML — a CI gate
(`tests/ci/test_no_model_strings_in_workflow_yaml.py`) enforces this.

| Task | Registry key | Current model |
| --- | --- | --- |
| Extraction (Haiku) | `extraction` | `claude-haiku-4-5-20251001` |
| Generation / revision (Sonnet) | `generation` | `claude-sonnet-4-6` |
| Complex reasoning / correction mining (Opus) | `complex_reasoning` | `claude-opus-4-7` |
| Opus reference baselines | `opus_reference_baseline` | `claude-opus-4-6` |

To migrate model versions: edit `ai/registry/model_registry.json` (one
file, one change) and re-run `seed-model-registry.yml`.

---

## Scripts

| Script | Purpose |
| --- | --- |
| `create_opus_reference_baselines.py` | Run Opus against raw transcript `.docx` files; write `reference_baselines/opus_reference_minutes.jsonl` per source. Reference baselines are `status: reference_only`, never promoted |
| `compare_opus_haiku.py` | System 1 of the self-improvement loop: deterministic (zero LLM calls) Haiku-vs-Opus diff by extraction type; writes `comparison_result` artifact + `eval_history.jsonl` row |
| `correction_miner.py` | System 2: reads `comparison_result` artifacts, mines miss patterns, asks Opus for additive prompt additions, re-evaluates each candidate with Haiku through the governed loop, opens a draft PR — only when F1 improvement vs Opus exceeds 0.05 |
| `create_human_gt_pairs.py` | Non-circular ground truth: extract decisions / action_items / claims from the human-authored minutes `.docx` via Sonnet; never reads pipeline output |
| `generate_gt_pairs.py` | Self-referential GT pairs from pipeline `meeting_extraction` output (circular; used for smoke coverage only) |
| `validate_data_lake.py` | Lightweight artifact integrity walk: malformed JSON, bad `verified` field types, missing glossary aggregate, bad audit_log enum values |
| `select_few_shot_examples.py` | Select high-quality examples for few-shot injection into extraction prompts |
| `verify_example.py` | Mark a few-shot example as verified |
| `seed_glossary.py` | Deterministic seed of spectrum-domain glossary terms to `store/artifacts/glossary/` |
| `seed_phase_v_flag.py` | Seed Phase V feature flag artifact |
| `seed_phase_w_flag.py` | Seed Phase W feature flag artifact |
| `annotate_rubric.py` | Annotate GT pairs with rubric scores |
| `review_gt_pairs.py` | Human-in-the-loop review of GT pairs |
| `confirm_pairs.py` | Confirm/approve GT pairs |
| `cleanup_data_lake.py` | Remove stale or orphaned artifacts |
| `cleanup_duplicate_pairs.py` | Deduplicate GT pairs |
| `reset_stale_baseline.py` | Reset a stale eval baseline |
| `update_glossary.py` | Update glossary terms |
| `wipe_eval_artifacts.py` | Wipe eval artifacts for a reset |
| `migrate_artifact_kind.py` | Migrate legacy `artifact_kind` field → `artifact_type` |
| `_artifact_validator.py` | Callable by any script before reading artifact fields; validates `artifact_type` + schema shape |
| `_env_validate.py` | Assert data-lake clone, `ANTHROPIC_API_KEY`, and gitignore invariant before extraction starts |
| `_few_shot_preflight.py` | Preflight checks for few-shot examples; resolves `source_id` → `source_artifact_id` |
| `_gitignore_audit.py` | Assert every `Git-tracked: YES` path in the artifact manifest is not shadowed by a `.gitignore` rule |
| `_match_minutes_transcripts.py` | Match human-authored minutes `.docx` files to transcript `source_ids` by date token |
| `_pr_triage.py` | Classify PR failures using the taxonomy before any code change |
| `_validate_runbook.py` | Validate runbook references |

---

## GitHub Actions workflows

### CI (runs on every PR / push to main)

| Workflow | Trigger | Purpose |
| --- | --- | --- |
| `pytest.yml` | PR + push to main | Full test suite on Python 3.11; clones data-lake when `DATA_LAKE_TOKEN` is present |
| `smoke-test.yml` | PR + `workflow_dispatch` | Extraction smoke test: CI checks (no data-lake required) + `extract-typed` against a real transcript |
| `smoke-e2e.yml` | PR + push to main | Phase R e2e: gitignore audit, preflight/few-shot selector chain, runbook validator; budgeted < 2 min |

### Pipeline

| Workflow | Trigger | Purpose |
| --- | --- | --- |
| `run-pipeline.yml` | `workflow_dispatch` + `repository_dispatch: transcript-added` | Full parallel pipeline: discover → up to 13 parallel per-transcript jobs (run-pipeline → extract-typed → push artifacts) → post-pipeline (link-ground-truth, run summary) |
| `debug-single-transcript.yml` | `workflow_dispatch` | Debug one transcript: full pipeline + typed extraction; ~4 min vs. full 13-way matrix |
| `eval-ground-truth.yml` | After `run-pipeline` completes + `workflow_dispatch` | Evaluate pipeline output against GT pairs; write `alignment_result`, `eval_summary`, `gate_decision` artifacts |
| `validate-and-baseline.yml` | Push to main touching extraction/glossary/evals paths + `workflow_dispatch` | Single-transcript E2E: run-pipeline → extract-typed → verify 5 Phase W wiring signals → set development baseline → push to data-lake → trigger `compare-opus-haiku.yml` |
| `verify-pipeline-state.yml` | `workflow_dispatch` | Scan data-lake; write verification artifacts; optionally compile findings |

### Self-improvement loop

| Workflow | Trigger | Purpose |
| --- | --- | --- |
| `create-opus-reference-baselines.yml` | `workflow_dispatch` | Run Opus against all or one transcript; write `reference_baselines/opus_reference_minutes.jsonl`; `dry_run=true` by default |
| `compare-opus-haiku.yml` | `workflow_dispatch` + triggered by `validate-and-baseline` | Deterministic Haiku-vs-Opus diff (zero model calls); write `comparison_result` artifact; auto-dispatch `run-correction-miner.yml` when F1 < 0.70 |
| `run-correction-miner.yml` | `workflow_dispatch` + triggered by `compare-opus-haiku` when F1 < 0.70 | Mine miss patterns → generate additive prompt candidates (Opus) → evaluate each with Haiku → open draft PR if F1 improves by > 0.05; `dry_run=true` by default |

### Ground truth and evaluation

| Workflow | Trigger | Purpose |
| --- | --- | --- |
| `create-human-gt-pairs.yml` | `workflow_dispatch` | Non-circular GT for one transcript: extract from human minutes `.docx` via Sonnet; `dry_run=true` by default |
| `create-human-gt-pairs-batch.yml` | `workflow_dispatch` | Batch non-circular GT for all matched transcript/minutes pairs; `skip_existing=true` by default |
| `annotate-gt-rubric.yml` | `workflow_dispatch` | Annotate GT pairs with rubric scores |
| `annotate-human-gt-dec18.yml` | `workflow_dispatch` | Annotate human GT for the Dec 18 transcript |
| `confirm-pairs.yml` | `workflow_dispatch` | Confirm / approve GT pairs |
| `confirm-rubric-annotations.yml` | `workflow_dispatch` | Confirm rubric annotations |
| `review-gt-pairs.yml` (via `review_gt_pairs.py`) | `workflow_dispatch` | Human-in-the-loop review of GT pairs |
| `eval-single-transcript.yml` | `workflow_dispatch` | Run eval against GT pairs for a single transcript |
| `diff-pipeline-runs.yml` | `workflow_dispatch` | Compare two pipeline runs |

### Glossary and few-shot

| Workflow | Trigger | Purpose |
| --- | --- | --- |
| `seed-glossary.yml` | `workflow_dispatch` | Seed spectrum-domain glossary terms to `store/artifacts/glossary/` |
| `select-few-shot-candidates.yml` | `workflow_dispatch` | Select few-shot candidates for extraction prompts |
| `verify-few-shot-example.yml` | `workflow_dispatch` | Mark a few-shot example as verified |
| `force-verify-few-shot-examples.yml` | `workflow_dispatch` | Force-verify all few-shot examples |
| `repair-few-shot-verified.yml` | `workflow_dispatch` | Repair `verified` field type on few-shot examples |

### Config and maintenance

| Workflow | Trigger | Purpose |
| --- | --- | --- |
| `seed-model-registry.yml` | `workflow_dispatch` | Write `ai/registry/model_registry.json` to `store/artifacts/config/` in the data lake; no model string is hardcoded in any workflow YAML |
| `seed-feature-flags.yml` | `workflow_dispatch` | Seed feature flag artifacts |
| `validate-data-lake.yml` | Push to main touching validation surface + `workflow_dispatch` | Lightweight artifact integrity check before `validate-and-baseline` runs |
| `migrate-artifact-kind.yml` | `workflow_dispatch` | Migrate legacy `artifact_kind` field → `artifact_type` |
| `migrate-data-lake-artifacts.yml` | `workflow_dispatch` | Migrate data-lake artifacts |
| `cleanup-data-lake.yml` | `workflow_dispatch` | Remove stale or orphaned artifacts |
| `cleanup-duplicate-pairs.yml` | `workflow_dispatch` | Deduplicate GT pairs |
| `reset-stale-baseline.yml` | `workflow_dispatch` | Reset a stale eval baseline |
| `wipe-eval-artifacts.yml` | `workflow_dispatch` | Wipe eval artifacts for a reset |

---

## Data lake layout

The data lake is the `nicklasorte/data-lake` repository. This repo never
commits data. Workflows access it via the `.github/actions/clone-data-lake`
composite action using `DATA_LAKE_TOKEN`.

```
nicklasorte/data-lake/
  store/
    raw/
      transcripts/                         # source .docx files
      minutes/                             # human-authored minutes .docx
    artifacts/
      config/
        model_registry.json                # seeded by seed-model-registry.yml
        llm_extraction_enabled.json        # seeded by seed-feature-flags.yml
      evals/
        few_shot/
          decision_examples_v1.json        # verified few-shot examples
      glossary/
        spectrum_glossary_v1.json          # versioned glossary aggregate
      extractions/
        <meeting_extraction_id>.json       # typed extraction artifacts
      orchestration/
        <orchestration_result_id>.json     # orchestration result per run
      health/                              # health findings
    processed/
      meetings/
        <source_id>/
          source_record.json               # ingestion identity record
          stories/
            chunks.jsonl                   # speaker-turn chunks
          reference_baselines/
            opus_reference_minutes.jsonl   # Opus reference baseline (reference_only)
          ground_truth/
            human_minutes_gt_pairs.jsonl   # non-circular GT pairs from human minutes
          comparisons/
            haiku_vs_opus_<run_id>.json    # comparison_result artifact
          run_history.jsonl                # harness memory (not authoritative)
          experience_history.jsonl         # harness memory
          eval_history.jsonl               # per-eval rows including haiku_vs_opus_comparison
          markdown/                        # human-readable views (never canonical)
```

The `eval_history.jsonl` tracks every eval that ran in every workflow,
including `haiku_vs_opus_comparison` rows emitted by `compare_opus_haiku.py`.
The index at `indexes/meetings/artifact_index.jsonl` is built only from
`status: promoted` artifacts, sorted by `(meeting_id, artifact_type, artifact_id)`.

---

## Governance invariants

These are enforced at every session — not suggestions.

- **All code changes via PR.** No direct push to main.
- **Never commit data to spectrum-systems-core.** All pipeline artifacts,
  transcripts, and minutes live in `nicklasorte/data-lake`. The repo-root
  `.gitignore` carries `data-lake/`; `_gitignore_audit.py` asserts this
  before every PR.
- **Fail-closed.** Missing artifact = halt, not inference. A missing
  required eval blocks. A transport error produces a blocked artifact, not
  a partial result. `preflight_llm_config` halts before any artifact is
  produced when `ANTHROPIC_API_KEY` is absent and the LLM flag is on.
- **`artifact_type` + `schema_version` everywhere.** The legacy
  `artifact_kind` field is rejected. Scripts call
  `scripts/_artifact_validator.validate_artifact` before reading any field
  off a loaded artifact.
- **No model strings in code or workflow YAML.** All model resolution goes
  through `ModelRegistry.get(task_type)` reading from
  `ai/registry/model_registry.json`. A CI gate
  (`tests/ci/test_no_model_strings_in_workflow_yaml.py`) enforces this in
  workflows.
- **Determinism.** Same inputs → byte-identical outputs. UUIDs and
  wall-clock timestamps are replaced with stable equivalents inside the
  pipeline (`_stable_artifact_id`, `_DETERMINISTIC_CREATED_AT`). JSON is
  serialized via `data_lake/serialize.py::canonical_json` (sorted keys,
  single trailing newline).
- **`context_items` access pattern.** The context bundle (v2.3.0) exposes
  items via the `context_items` field. Direct dict-key access on the old
  shape is rejected by the schema.
- **Correction mining is additive only.** `correction_miner.py` never
  rewrites the extraction prompt — it only appends. The current prompt is
  backed up before modification. Every candidate must improve F1 vs Opus
  by strictly more than 0.05 before a PR is opened.
- **Data lake is append-only from core's perspective.** Core never deletes.
  Two runs over the same raw inputs produce identical outputs.

---

## What is deferred

Items that are reserved by the constitution but not yet implemented:

- `failure_learning` module — the governed loop from failure record →
  eval_case_candidate → reviewed eval_case → regression suite (constitution
  §10); `data_lake/failure_seed.py` seeds the failure record, but the
  full promotion-into-regression-suite step is not built.
- Cross-chunk resolution — extractors run per-chunk; resolution of items
  that span chunk boundaries is not implemented.
- Prior meeting context injection — each run processes one meeting in
  isolation; meeting-to-meeting context is not threaded.
- Meeting graph builder / study document compiler — aggregating promoted
  artifacts across meetings into a compiled study document.
- Vector indexes, embeddings, semantic search.
- Remote persistence beyond the directory tree.

---

## Quickstart

Install:

```bash
pip install -e ".[dev]"
```

Run the full deterministic pipeline (all four regex workflows) against one meeting:

```bash
spectrum-core process-meeting \
  --lake /path/to/data-lake \
  --meeting-id <meeting_id>
```

`--workflow` is repeatable and scopes to a subset
(`meeting_minutes`, `meeting_action_log`, `agency_question_summary`,
`decision_brief`).

Enable LLM extraction (Haiku) for the `meeting_minutes` workflow:

```bash
# ANTHROPIC_API_KEY must be set. --llm routes meeting_minutes through
# the live-LLM extractor (in-process override; the code default stays False).
# Fail-closed: missing API key halts pre-run; never falls back to regex.
spectrum-core process-meeting \
  --lake /path/to/data-lake \
  --meeting-id <meeting_id> \
  --workflow meeting_minutes \
  --llm
```

Run typed extraction (decisions, claims, action_items with source_turn attribution)
for a single source:

```bash
spectrum-core extract-typed \
  --source-id <source_id> \
  --data-lake /path/to/data-lake
```

Run tests:

```bash
pytest
pytest tests/test_artifact_model.py           # one file
pytest -k regulatory_verb                     # filter by name
```

---

## Data lake separation

`spectrum-systems-core` is code only. Workflows access `nicklasorte/data-lake`
via the `.github/actions/clone-data-lake` composite action (uses
`DATA_LAKE_TOKEN` PAT). Pushes back go through `.github/actions/push-data-lake`.

Never commit data into this repo. Never push to `nicklasorte/data-lake`
from a pipeline workflow targeting this repo's `main`. Never reference
`DATA_LAKE_TOKEN` outside the secret context.

## Obsidian / Claude / MCP

The vault and integration boundaries are documented in
`docs/integrations/claude_mcp_obsidian.md`. Practical Dataview queries
live in `docs/integrations/obsidian_dataview_examples.md`. JSON stays
canonical regardless of which tool is reading the lake.
