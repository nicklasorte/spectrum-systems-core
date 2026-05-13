# First-Run Runbook

## Purpose

Step-by-step sequence for the first end-to-end pipeline run on a new
transcript. Each step lists the workflow file, expected duration,
success signals, and the failure modes a new operator most often
hits. Designed for use from a phone — every step references a
workflow file in `.github/workflows/` that is dispatchable from the
GitHub Actions tab.

## Prerequisites

- Branch-protection bypass for `github-actions[bot]` is configured on
  `main` (so the workflows can push back artifact commits).
- `ANTHROPIC_API_KEY` is set in repo secrets.
- A transcript `.docx` or `.txt` file is checked in under
  `tests/fixtures/debug_transcripts/` and its slugified filename matches
  the `source_id` you intend to use (see
  `spectrum_systems_core.orchestration.pipeline_orchestrator._slugify`).

## Sequence

### Step 1: Debug single transcript

- **Workflow:** `.github/workflows/debug-single-transcript.yml`
- **Inputs:** none — the workflow's env block sets `SOURCE_ID`. To
  debug a different transcript, edit the env block and merge the
  edit before dispatching.
- **Expected duration:** ~2 minutes.
- **Success signals:**
  - Every step ends green.
  - The "Commit pipeline artifacts to data-lake" step shows
    "N files changed".
  - `data-lake/store/artifacts/extractions/` contains a new `.json`
    file.
  - `data-lake/store/processed/meetings/<source_id>/source_record.json`
    is committed.
- **Failure modes:**
  - `chunks_jsonl_not_found`: the transcript was not seeded — check
    the "Seed transcript into data-lake" step output.
  - `git push rejected`: branch protection is not configured — verify
    the bypass list includes `github-actions[bot]`.
  - `extract-typed FAIL`: open the typed-extraction step log and
    look for the finding code (e.g. `chunks_jsonl_not_found`).

### Step 2: Select few-shot candidates

- **Workflow:** `.github/workflows/select-few-shot-candidates.yml`
- **Inputs:** `source_id` (same as Step 1), `max_candidates`
  (default 3).
- **Expected duration:** ~30 seconds.
- **Success signals:**
  - The "Preflight" step exits 0 (no `[BLOCKED]` banner in the
    step summary).
  - The "Select few-shot candidates" step prints
    `diag: selected N candidates`.
  - The "Verify no placeholders remain" step prints
    `OK: N real examples`.
  - The commit step shows files changed under
    `store/artifacts/evals/few_shot/`.
- **Failure modes:**
  - `[BLOCKED] — extraction artifacts missing`: re-run Step 1 and
    confirm it pushed an extraction artifact to `main`.
  - `resolved source_artifact_id: None`: `source_record.json` is
    missing or got gitignored — run
    `python scripts/_gitignore_audit.py` to confirm the negation
    rule for `**/processed/**/source_record.json` is still in place.

### Step 3: Verify few-shot example

- **Workflow:** `.github/workflows/verify-few-shot-example.yml`
- **Inputs:** `example_id` (from `decision_examples_v1.json`),
  `reviewer_id`, `decision: approve`, plus optional notes.
- **Expected duration:** ~15 seconds.
- **Success signals:** the example is marked `verified: true` in
  the artifact and the `audit_log` gains a `verified` entry.

### Step 4: Annotate GT rubric

- **Workflow:** `.github/workflows/annotate-gt-rubric.yml`
- **Inputs:** `source_id`, `classification_mode: auto_classify_for_review`.
- **Expected duration:** ~30 seconds.
- **Success signals:** the step summary lists the proposed
  classifications for every ground-truth pair under the source.

### Step 5: Confirm rubric annotations

- **Workflow:** `.github/workflows/confirm-rubric-annotations.yml`
- **Inputs:** `source_id`, `confirm_auto_classified: yes`.

### Step 6: Validate and baseline

- **Workflow:** `.github/workflows/validate-and-baseline.yml`
- **Inputs:** `source_id`, `enable_judge: false`.
- **Success signals:**
  - Gate decision is `pass` (not `skip_no_baseline`).
  - The baseline artifact is committed to `data-lake/`.

## Runbook Maintenance

This runbook references workflow file paths under `.github/workflows/`.
If a workflow file is renamed, update this runbook in the same PR.
The `scripts/_validate_runbook.py` script enforces this:

```bash
python scripts/_validate_runbook.py
```

It parses this file, extracts every `.github/workflows/*.yml`
reference, and asserts each file exists. The script runs in CI on
every PR.
