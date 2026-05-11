# Verification cycle recovery runbook

This runbook covers the operational verification cycle introduced by
Phases O and P. It is a **decision tree**: start at the top, follow the
branch that matches the failure mode you are seeing, and stop when you
have a remediation. Sections cross-reference each other for compound
failures.

If a CLI command points you here with a section number (for example,
`check-preflight` printing "See docs/runbooks/verification-cycle-recovery.md
section 1 for recovery"), skip directly to that section.

## Quick triage

| Symptom                                                                          | Go to     |
| -------------------------------------------------------------------------------- | --------- |
| `check-preflight` exits 1 with `Migration incomplete`                            | Section 1 |
| migrate-artifact-kind workflow failed mid-run                                    | Section 2 |
| `run-pipeline` workflow exceeded 6h or was cancelled                             | Section 3 |
| `run-pipeline` exited 0 but `source_ids_failed` is non-empty                     | Section 4 |
| `eval_summary.partial_run_warning == True`                                       | Section 5 |
| `review-baseline-candidate` printed `REVIEW` for one or more metrics             | Section 6 |
| pipeline timed out AND `validation_failures_by_type` is non-empty                | Section 7 |

---

## Section 1 — Pre-flight check blocks because migration incomplete

### Symptoms

- `check-preflight` exits 1 with message
  `"Migration incomplete. Run migrate-artifact-kind workflow with confirm=true ..."`.
- `verify-pipeline-state` reports `artifacts_with_artifact_kind_only > 0`.

### Decision

1. Did the migrate-artifact-kind workflow's **dry-run step (P.2)**
   succeed previously?
   - **Yes** → run the workflow again with `confirm: "yes"` (P.3).
     The migration script is idempotent — re-running is safe.
     Once the confirm run completes, re-run `verify-pipeline-state`;
     `artifacts_with_artifact_kind_only` must read 0 before proceeding.
   - **No** → the dry-run itself is failing. Jump to **Section 2**.

### Commands

```
# Locally, against your data-lake checkout:
python -m spectrum_systems_core.cli verify-pipeline-state \
  --data-lake "$DATA_LAKE_PATH" \
  --no-write-artifact

# Inspect the kind_only count, then trigger the workflow:
# Actions → migrate-artifact-kind → Run workflow:
#   confirm: yes
#   data_lake_ref: main
```

### Remediation prompt template

```
The check-preflight CLI is blocking pipeline runs because
artifacts_with_artifact_kind_only is > 0 in the data lake. The
migrate-artifact-kind dry-run completed cleanly. Trigger the
migrate-artifact-kind workflow with confirm=yes against branch
<branch-name>, watch for the commit on the data-lake repo, then
re-run verify-pipeline-state to confirm the count drops to zero.
Stop and ask the user if any artifact fails to migrate.
```

---

## Section 2 — Migration workflow fails mid-run (P.2 or P.3)

### Symptoms

- migrate-artifact-kind workflow shows `failed`.
- `verify-pipeline-state` reports a mix of `artifact_kind`-only and
  `artifact_type`-only / both-fields artifacts.

### Decision

The migration script is **idempotent** — it never deletes or rewrites
already-migrated records. Re-run the workflow with the **same inputs**
that failed.

If the same step fails a second time, inspect the workflow log for the
specific artifact ID that triggered the failure. Most failures come from
unreadable JSON; treat that artifact as the root cause and fix it before
re-running. Do not run the pipeline until
`artifacts_with_artifact_kind_only == 0`.

### Commands

```
# Re-trigger Actions → migrate-artifact-kind with the same inputs.
# After completion:
python -m spectrum_systems_core.cli verify-pipeline-state \
  --data-lake "$DATA_LAKE_PATH" \
  --no-write-artifact
```

### Remediation prompt template

```
migrate-artifact-kind failed at <step>. The migration is idempotent.
Re-run the workflow with confirm=yes and the same data_lake_ref. After
it completes, run verify-pipeline-state and confirm
artifacts_with_artifact_kind_only is 0. If the same step fails twice,
extract the artifact ID from the workflow log and ask the user how to
fix it before proceeding.
```

---

## Section 3 — Force-run timeout in run-pipeline (P.4)

### Symptoms

- `run-pipeline` workflow exceeded 6h or was cancelled.
- `verify-pipeline-state` shows partial `meeting_extraction_count`
  (`< confirmed_pair_count`).

### Decision

Re-run the workflow with `force_only_missing: "true"`. Already-completed
`source_id`s have a `meeting_extraction` artifact on disk and will be
skipped. The run continues from where the previous attempt stopped.

If `meeting_extraction_count` does not increase between consecutive runs,
a specific `source_id` is failing on every attempt. Jump to **Section 4**.

### Commands

```
# Actions → run-pipeline → Run workflow:
#   dry_run: false
#   force: true
#   force_only_missing: true
```

### Remediation prompt template

```
The previous run-pipeline workflow run timed out. meeting_extraction_count
is <N> of <expected>. Trigger run-pipeline with force=true and
force_only_missing=true. After the run completes, verify
meeting_extraction_count has increased. If it has not, the same source_id
is failing repeatedly — see runbook section 4.
```

---

## Section 4 — Force-run partial completion with no timeout

### Symptoms

- `run-pipeline` exited 0.
- The latest `orchestration_run_record.source_ids_failed` list is
  non-empty.
- `verify-pipeline-state` shows `meeting_extraction_count` below
  `confirmed_pair_count`.
- `check-preflight` may also report
  `No source_records in data-lake. Run ingestion first.` for the very
  first run (no source_records exist yet); resolve ingestion first then
  re-run the pipeline.

### Decision

For each failing `source_id`:

1. Read the `orchestration_run_record.results[]` entry for that
   `source_id`.
2. Check whether the upstream artifacts exist:
   - `store/processed/meetings/<source_id>/chunks.jsonl` present?
   - chunk classifier output present?
   - typed extractor logs?
3. The remediation depends on which stage failed; consult the workflow
   log for the specific traceback. Re-run with
   `specific_source_id: <id>` to retry only that meeting.

### Commands

```
# Locate the latest orchestration_run_record:
python -m spectrum_systems_core.cli verify-pipeline-state \
  --data-lake "$DATA_LAKE_PATH" --no-write-artifact

# Re-run for one source_id:
# Actions → run-pipeline → Run workflow:
#   specific_source_id: <id>
```

### Remediation prompt template

```
run-pipeline completed with source_ids_failed = [<list>]. For each id,
read the orchestration_run_record.results entry, identify which pipeline
stage failed (chunks.jsonl, classifier, extractor), and decide whether
to retry with specific_source_id=<id> or to fix the upstream artifact
first. Stop and ask the user before deleting or rewriting anything.
```

---

## Section 5 — Eval workflow runs against partial pipeline output

### Symptoms

- The latest `eval_summary` has `partial_run_warning: true`.
- `review-baseline-candidate` prints `partial_run_warning` REVIEW.
- `eval-ground-truth --set-baseline` returns exit code 1 with
  `partial_run_warning_blocks_set_baseline`.

### Decision

Do **not** proceed to `--set-baseline`. The eval was measured against an
incomplete pipeline; baselining it would lock in a regression-detection
floor that is below real performance.

Re-run the pipeline to complete the missing extractions first. The
`partial_run_detail.missing_source_ids` array tells you which
`source_id`s the eval expected but did not see. Resolve those first,
then re-run `eval-ground-truth` (without `--set-baseline`), then
`review-baseline-candidate`, then `--set-baseline`.

### Commands

```
# Trigger run-pipeline with force=true, force_only_missing=true,
# wait for completion, then:
python -m spectrum_systems_core.cli eval-ground-truth \
  --data-lake "$DATA_LAKE_PATH"
python -m spectrum_systems_core.cli review-baseline-candidate \
  --data-lake "$DATA_LAKE_PATH"
# Only after every sanity bound PASSes:
python -m spectrum_systems_core.cli eval-ground-truth \
  --set-baseline --data-lake "$DATA_LAKE_PATH"
```

### Remediation prompt template

```
The latest eval_summary has partial_run_warning=true. Read
partial_run_detail.missing_source_ids and run the pipeline with
force=true, force_only_missing=true to fill those gaps. After the
pipeline completes, re-run eval-ground-truth and verify
partial_run_warning is false before considering --set-baseline.
```

---

## Section 6 — Sanity bound REVIEW from review-baseline-candidate

### Symptoms

`review-baseline-candidate` prints one or more `[REVIEW]` lines.

### Decision (per metric)

- **`regulatory_verb_fallback_rate >= 0.30`**
  The classifier is reclassifying too much content via the regex
  fallback. Inspect ChunkClassifier logs for chunks that fell back. The
  classifier may be mis-classifying decisions as `off_topic`, or the
  fallback condition may simply be too broad. Tune the classifier
  prompt OR narrow the fallback before rebaselining.

- **`human_dedup_rate >= 0.20`**
  The typed extractors are over-producing overlapping items. Inspect
  `ExtractionMerger` output for the worst meetings (highest
  `requires_human_dedup_count`). Likely root cause: merge-key logic does
  not collapse near-duplicate items.

- **`off_topic_rate >= 0.30`**
  The classifier is rejecting too much content as `off_topic`. The
  classifier prompt likely needs tuning to admit borderline turns. Cross-
  check with `regulatory_verb_fallback_rate` — both metrics moving
  together points at the classifier, not the chunker.

- **`total_extracted_items < 50`** (Phase P floor)
  Extraction is under-producing across all 13 meetings combined.
  Likely root causes: `source_turn_validation` is rejecting valid items
  (check `turn_validation` rejection logs); the typed extractors are
  returning empty arrays despite valid input (test on a known-good
  transcript); or the classifier upstream is dropping everything into
  `off_topic` (see the bullet above). Fix root cause before
  rebaselining — never `--set-baseline` over a thin extraction.

### Commands

```
python -m spectrum_systems_core.cli review-baseline-candidate \
  --data-lake "$DATA_LAKE_PATH"
python -m spectrum_systems_core.cli compile-findings \
  --data-lake "$DATA_LAKE_PATH"
```

### Remediation prompt template

```
review-baseline-candidate flagged [<metric>] as REVIEW with value
<value>. Following runbook section 6 for that metric:
  1. Read the relevant pipeline-stage logs.
  2. Identify the specific artifact(s) driving the rate.
  3. Propose a fix that targets the root cause, not the metric.
Stop and ask the user before lowering any sanity bound; the bounds
exist for a reason.
```

---

## Section 7 — Compound failure: timeout AND schema validation failure

### Symptoms

- `run-pipeline` was cancelled or hit the 6h timeout (matches Section 3).
- `verify-pipeline-state` reports a non-empty
  `validation_failures_by_type` (matches Section 1's symptom area but
  on artifacts, not migration).

### Decision (compound)

The two failures must be resolved in order — fixing the timeout alone
will leave a broken artifact pinned at the head of the data-lake, and
fixing only the schema failures while the pipeline is in a partial
state can corrupt the next force-run.

1. **First, address validation failures.** A partial pipeline run cannot
   be trusted as the input for the next re-run if any of its outputs
   fail schema validation. Use `compile-findings` to list the failing
   artifact types and their counts. For each type, inspect the
   `.invalid.json` sidecar under `$SDL_ROOT/`. Fix the root cause: this
   is usually a code change in the producer, not a one-off edit of the
   artifact body.

   - Re-run `verify-pipeline-state` after each fix until
     `validation_failures_by_type` is empty.

2. **Then, address the timeout** (Section 3 procedure):
   re-run `run-pipeline` with `force_only_missing=true` so the previously
   completed (and now-validated) artifacts are skipped. The pipeline
   continues from the same point it was cancelled at, but now over a
   schema-clean baseline.

3. **Finally**, re-run `verify-pipeline-state` once more and confirm:
   - `validation_failures_by_type` is empty.
   - `meeting_extraction_count == confirmed_pair_count`.
   - `artifacts_with_artifact_kind_only == 0`.

Do not run `eval-ground-truth --set-baseline` until **all three** are
true. If at any point a schema fix re-introduces an `artifact_kind`-only
artifact, jump to **Section 1** before continuing.

### Commands

```
python -m spectrum_systems_core.cli verify-pipeline-state \
  --data-lake "$DATA_LAKE_PATH" --no-write-artifact
python -m spectrum_systems_core.cli compile-findings \
  --data-lake "$DATA_LAKE_PATH"
# After fixes:
python -m spectrum_systems_core.cli check-preflight \
  --data-lake "$DATA_LAKE_PATH"
# Re-trigger run-pipeline with force=true, force_only_missing=true.
```

### Remediation prompt template

```
The pipeline timed out AND validation_failures_by_type is non-empty.
Following runbook section 7 (compound failure), the order is:
  1. Use compile-findings to enumerate failing artifact types. For
     each, read the .invalid.json sidecars and find the producer.
     Patch the producer, not the artifacts.
  2. Re-run verify-pipeline-state until validation_failures_by_type
     is empty.
  3. Re-run run-pipeline with force=true, force_only_missing=true to
     complete the timed-out work over a clean baseline.
  4. Verify check-preflight passes before continuing to eval.
Stop and ask the user before editing any artifact body by hand.
```

---

## Cross-references

- The CLI commands cited above are defined in
  `src/spectrum_systems_core/cli.py`.
- The Phase O baseline guards live in `review-baseline-candidate`.
- The Phase P safety nets live in `check-preflight` and
  `next-phase-handoff`.
- See `CLAUDE.md` for the phase planning protocol that uses
  `next-phase-handoff` between cycles.
