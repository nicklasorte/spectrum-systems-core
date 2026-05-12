# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -e ".[dev]"          # install package + dev extras (pytest)
python -m pytest                 # run full suite
python -m pytest tests/test_artifact_model.py            # one file
python -m pytest tests/test_artifact_model.py::test_name # one test
python -m pytest -k grounding    # filter by name
```

CI runs only `python -m pytest` on Python 3.11 (`.github/workflows/pytest.yml`). There are no linters, formatters, type-checkers, or coverage gates configured — do not add them without a concrete need (`docs/development/ci.md`).

## Constitutional governance

`docs/architecture/system_constitution.md` is **binding** for this repo. Other docs defer to it. Re-read it before any architectural change. Key rules that affect day-to-day edits:

- The system has exactly one loop: **Produce → Evaluate → Decide → Promote**. Every module must serve it or be deferred.
- Top-level module names are fixed: `artifacts`, `context`, `workflows`, `evals`, `control`, `promotion`, `data_lake`. Adding a new top-level module requires amending the constitution.
- `failure_learning` and `ai_adapter` are reserved names but deliberately not implemented. So are live model calls, autonomous agents, dashboards, vector indexes, embeddings, semantic search, certification gates, and remote persistence. Do not add them.
- Reject AEX/PQX/EVL/TPA/CDE/SEL terminology from the predecessor `spectrum-systems` repo. Plain module names only.
- Prefer one artifact envelope over many families; one control model over many authority systems.

## Core loop architecture

The loop lives in `src/spectrum_systems_core/workflows/_loop.py::run_governed_loop`. Each workflow file (`meeting_minutes.py`, `decision_brief.py`, `agency_question_summary.py`, `meeting_action_log.py`) only supplies an `artifact_type` string and a deterministic `extract(input_text) -> dict` function. The loop does the rest:

1. `context.build_context_bundle` → context artifact
2. `artifacts.new_artifact` with the extracted payload (status `draft`)
3. `evals.run_required_evals` → list of `eval_result` artifacts
4. `control.decide_control` → `control_decision` artifact (`allow` / `block`)
5. `promotion.promote_if_allowed` → status becomes `promoted` only on `allow`, else `rejected`

### Adding a new artifact type

Two edits, no new modules:

1. Add `run_<type>_workflow` in `workflows/<type>.py` that calls `run_governed_loop` with a deterministic `extract` function. Re-export it from `workflows/__init__.py`.
2. Add the required-field tuple and entry in `evals/runner.py::REQUIRED_FIELDS_BY_TYPE`. The `non_empty_payload` eval already runs for every type.

If the type also needs to flow through the data lake pipeline, extend `_CONTENT_SIGNAL_KEYS_BY_TYPE` in `data_lake/pipeline.py` and `data_lake/extract.py`'s grounded payload builder.

## Artifact envelope (one schema for everything)

`artifacts/model.py::Artifact` has these fields and they are shared by every artifact type (target artifacts, `context_bundle`, `eval_result`, `control_decision`, `manifest`, etc.): `artifact_id`, `artifact_type`, `schema_version`, `status`, `created_at`, `trace_id`, `input_refs`, `content_hash`, `payload`. Statuses are restricted to `{draft, evaluated, promoted, rejected}` (`artifacts/validation.py`). State changes are new envelopes or status updates — never edit `payload` in place.

## Control is fail-closed

`control/decision.py` is the only place decisions come from. Rules:

- No eval results → `block` (`missing_required_evals`).
- Any eval result with `status == "fail"` → `block` (`failed:<eval_type>`).
- All required evals pass → `allow`.
- `warn` and `freeze` exist in `ALLOWED_DECISIONS` but are **reserved** and not used. Do not emit them.
- Model output never decides; only the control function does.

`promotion/promoter.py::promote_if_allowed` is the single path to `promoted` — promotion anywhere else is a constitution violation.

## Data lake boundary (`data_lake/`)

`docs/contracts/data_lake_contract.md` is binding for everything under `data_lake/`. The lake is just a directory tree; core is a pure processor.

- Core **reads only** `raw/meetings/<meeting_id>/{transcript.txt,metadata.json}`.
- Core **writes only** under `processed/meetings/<meeting_id>/` and `indexes/meetings/`.
- Core **never deletes** anything. Append-only from core's perspective.
- Only `status == "promoted"` artifacts may be written under `processed/`. `eval_result` and `control_decision` artifacts stay inside manifests/debug reports — they are run records, not products.
- `meeting_id` must match `^[a-z0-9][a-z0-9_-]{0,127}$` and equal both the directory name and the `metadata.json` `meeting_id` field. The loader rejects the meeting otherwise.
- Processed artifact filenames are `<artifact_type>__<slug>.json`. The double-underscore is the separator and must not appear elsewhere in the two segments.

### Determinism is the trust property

All outputs under `processed/` and `indexes/` must be byte-identical across runs given the same inputs. Use `data_lake/serialize.py::canonical_json` (sorted keys, stable separators, single trailing newline). The pipeline (`data_lake/pipeline.py`) achieves this by replacing UUIDs and wall-clock `created_at` with `_stable_artifact_id` (hash of kind+trace_id+payload) and `_DETERMINISTIC_CREATED_AT = "1970-01-01T00:00:00+00:00"` for every artifact it produces. If you add an artifact to the pipeline, run it through `_stabilize` or you will break the determinism invariant.

The `artifact_index.jsonl` is built only from promoted processed artifacts and is sorted by `(meeting_id, artifact_type, artifact_id)` before write.

### Pipeline-only evals

Beyond the type-required-field evals from `evals/runner.py`, the pipeline adds three more in `data_lake/pipeline.py`: `source_grounding`, `transcript_evidence` (blocks transcript-source runs that produced no grounded spans), and `content_signal` (blocks `notes`/`summary` runs whose content lists are all empty). All four pass through the same `decide_control`.

## Testing philosophy

From the constitution: tests defend trust properties, not ceremony. Add a test only if it falls into one of these categories:

- **Unit** — pure logic (artifact construction, hashing, decision rules).
- **Contract** — payloads conform to declared `schema_version` for their `artifact_type`.
- **Golden workflow** — known input → known artifact → known decision, end-to-end.
- **Fail-closed** — missing required evals block; failed required evals block; only `allow` leads to promotion.

Golden transcripts live in `tests/fixtures/golden_meetings/`.

## Phase planning protocol

Before starting a new phase planning cycle, the operator should run:

```
python -m spectrum_systems_core.cli next-phase-handoff
```

The output `prompt_opening` should be pasted into the new Claude
conversation as the seed for STEP 1 inventory. If the briefing's
`valid_until` is in the past, re-run `verify-pipeline-state` first, then
re-run `next-phase-handoff`.

## Operator env vars (Phase O — pipeline debug observability)

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

## Files worth reading before non-trivial changes

- `docs/architecture/system_constitution.md` — binding; precedence over everything else.
- `docs/contracts/data_lake_contract.md` — binding for `data_lake/`.
- `src/spectrum_systems_core/workflows/_loop.py` — the loop in ~70 lines.
- `src/spectrum_systems_core/data_lake/pipeline.py` — the only place data-lake I/O meets the core loop.
