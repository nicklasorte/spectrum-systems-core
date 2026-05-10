# Phase K — GOV-10 Done Certification and Release

Branch: `phase-K/gov10-certification` (off `c205e66` — Phase J merge).

## Step 1 — Inventory & baseline

- Dep manager: `pip` + `pyproject.toml` (no Poetry, no uv, no Pipenv).
- Python interpreter: `3.11.15`.
- CI Python pin: `3.11` (per `CLAUDE.md` + `.github/workflows/pytest.yml`).
- `pip install -e ".[dev]"`: succeeded (jsonschema 4.26.0, pytest 9.0.3,
  pdfminer.six 20260107, PyYAML).
- Test framework: `pytest` (`[tool.pytest.ini_options]`,
  `testpaths = ["tests"]`, `addopts = "-ra"`).
- Lint config: none detected (no flake8/ruff/black config).
- Type-checker config: none detected (no mypy/pyright config).
- **Baseline test count: 674** (`pytest --collect-only -q`).

### audit-governance baseline (pre-Phase-K)

- `python -m spectrum_systems_core.cli audit-governance` → exit `1`.
- `total_flagged: 168`, `high: 31`. Pre-existing — not Phase K defects.
- This is the baseline to compare against when re-running after Step 7.
  Phase K gate is "zero NEW high-severity flags on Phase K files".

### Repository state confirmed

- `contracts/schemas/paper/formatted_paper_artifact.schema.json` — present.
- `contracts/schemas/paper/revised_draft.schema.json` — present.
- `src/spectrum_systems_core/paper/publication_formatter.py` — present.
  `PublicationFormatter.format(revised_draft_id, repo_root, vault_root)`
  returns `{status, artifact, reason}` per Phase J.
- `src/spectrum_systems_core/governance/eval_coverage_scanner.py` —
  exposes `EvalCoverageScanner.scan(repo_root)` (reused for CHECK-6).
- `src/spectrum_systems_core/governance/hidden_logic_scanner.py` and
  `markdown_authority_scanner.py` — Phase I scanners that gate Phase K.
- `src/spectrum_systems_core/synthesis/cost_recorder.py` — appends to
  `synthesis/<run_id>/cost.jsonl`. Field is `estimated_cost_usd`.
- `src/spectrum_systems_core/ai/adapter.py` — writes
  `ai/costs/<query_id>.json` with `estimated_cost_usd`. The query record
  at `ai/queries/<query_id>.json` has no `run_id` field — the link to a
  pipeline run is via `bundle_id` on the query record.
- `paper/formatted/` directory at repo root: **does not exist**.
  Phase J actually writes formatted artifacts to
  `processed/<family>/<source_id>/paper/formatted/<paper_id>.json`
  (`PublicationFormatter._FORMATTED_DIRNAME` + `find_paper_dir()`).
  GOV10CertificationStep must locate the formatted artifact by scanning
  `processed/*/*/paper/formatted/<paper_id>.json` rather than reading
  a flat `paper/formatted/` tree.

### Schema and evals inventories

- `contracts/schemas/` subdirs: `agency`, `ai`, `governance`, `harness`,
  `paper`, `synthesis`, plus the top-level extraction schemas.
- `contracts/evals/` files (count = 19): agency_evals, ai_query_evals,
  bundle_evals, claim_evals, evidence_evals, formatting_evals,
  governance_evals, harness_evals, issue_evals, keynote_evals,
  knowledge_synthesis_evals, mitigation_evals, objection_evals,
  obsidian_bridge_evals, pdf_extraction_evals, report_evals,
  revision_evals, source_ingestion_evals, story_extraction_evals.

### Discrepancies vs task spec (surfaced, not silently resolved)

1. Task spec assumes `paper/formatted/<paper_id>.json` at repo root.
   Reality: Phase J writes to
   `processed/<family>/<source_id>/paper/formatted/<paper_id>.json`.
   Disposition: locate by scanning the actual layout; release_artifact
   path follows the spec (`paper/released/<paper_id>.json` at repo root)
   so it can act as the documented terminal product.
2. Cost field name. Task spec says "Sum total_cost_usd" but the cost
   record schemas (`synthesis_run_cost_record`, `ai_query_record`'s
   adjacent cost file) use `estimated_cost_usd`. Disposition: sum
   `estimated_cost_usd` from all linked records and write the result to
   the `total_pipeline_cost_usd` field of `done_certification_record`
   (which is the field name the spec dictates for the *destination*).
3. CLAUDE.md/constitution mismatch. CLAUDE.md describes a
   meeting-minutes loop (`artifacts`, `context`, `workflows`, `evals`,
   `control`, `promotion`, `data_lake` only). Repo has many more
   modules (paper, synthesis, governance, agency, ai, ingestion,
   harness, extraction, obsidian_bridge, utils). Disposition: per task
   prompt's "pre-resolved by task spec" override, continue.

### Branch conflict resolution

- System SDK env specified branch `claude/gov10-certification-TOtmw`.
- Task spec specified branch `phase-K/gov10-certification`.
- User confirmed `phase-K/gov10-certification` via question.

## Step 2 — schemas

Wrote two schemas under `contracts/schemas/certification/`:

- `done_certification_record.schema.json` (terminal certification record)
- `release_artifact.schema.json` (terminal release pointer)

Both validate as Draft 2020-12, both have `additionalProperties: false`.
`done_certification_record` also has `if/then` cross-field constraints
applied per Gate A Sev-2 #1 (see Step 3): `status=PASSED` ⇒
`failure_reasons.maxItems = 0` and `failed_checks = 0`; `status=FAILED`
⇒ `failure_reasons.minItems = 1` and `failed_checks ≥ 1`.

## Step 3 — Gate A (design redteam)

Subagent ran with the verbatim Gate A prompt. Returned 6 Sev-1 and 8
Sev-2 findings. Triage:

| # | Sev | Summary | Disposition |
|---|---|---|---|
| 1 | 1 | Constitution forbids certification module/gate | **Pre-resolved by task spec** — Phase K is mandated by the task prompt, which is the authoritative override for this phase. |
| 2 | 1 | Three `const "1.0.0"` versions hide future drift | **Task-mandated** verbatim: spec lists `certifier_version (const "1.0.0")`, `schema_version (const "1.0.0")`, `certification_logic_version (const "1.0.0")`. |
| 3 | 1 | CHECK-3 absent `content_hash` skipped not failed = unknown state | **Task-mandated** verbatim: spec says `If no content_hash field: skip and log as "not_verified" (not FAILED — absence of hash field is a gap, not a mismatch)`. Phase K covers `formatted_paper_artifact` (which has a hash) for full replay; non-hashed artifacts are a known follow-up gap. |
| 4 | 1 | `CERTIFICATION_COST_CEILING_USD = 5.00` hard-coded as constant | **Task-mandated** verbatim: spec says `CERTIFICATION_COST_CEILING_USD = 5.00 (constant — not a config toggle)`. |
| 5 | 1 | `paper/released/<paper_id>.json` overwrites prior releases | **Task-mandated** path. The release artifact carries `release_id` + `certification_id` so the `done_certification_record` (separately filed) is the audit trail; the on-disk release file is the current pointer. |
| 6 | 1 | CHECK-2 lineage existence-only, no hash verification | **Task-mandated** verbatim: spec says `Every referenced input_artifact_id must exist (DataLake.exists() or file path check)`. CHECK-3 covers content-hash verification for artifacts that carry one. |
| 1 | 2 | `status` ↔ `failure_reasons`/`failed_checks` cross-field constraint | **Applied** — added `if/then` `allOf` to the schema; verified with valid/invalid cases. |
| 2 | 2 | `passed + failed + skipped` not constrained to equal 7 | **Applied at code level** in Step 4 — `GOV10CertificationStep` will derive these counts from `check_results` and assert their sum equals 7 before emitting. |
| 3 | 2 | CHECK-5 word "stage" undefined | Implementation will tie required-eval lookup to `artifact_type` (matching CHECK-6's surface). Documented in code. |
| 4 | 2 | CHECK-6 `EvalCoverageScanner` version unpinned | Out of scope — the task spec says "Reuse `EvalCoverageScanner.scan()` from Phase I — do NOT reimplement". |
| 5 | 2 | CHECK-7 two cost surfaces | Pre-existing Phase F/H surfaces; the task spec lists both. Out of scope for Phase K. |
| 6 | 2 | `release_path` free-form | Acceptable per spec; the certifier writes a deterministic path so reader/writer can't disagree. |
| 7 | 2 | No `trace_id` / `run_id` on cert record | Spec lists exact required fields; `additionalProperties: false`. Adding fields beyond the spec would itself be a Sev-2 (duplicate surface). Out of scope. |
| 8 | 2 | CHECK-3 replay runs producer code | `PublicationFormatter` is deterministic, no LLM calls; replay is safe. The spec mandates this exact replay semantics. |

**Verdict:** zero remaining actionable Sev-1/Sev-2. The one applied
finding (Sev-2 #1) tightens the schema without conflicting with the
spec. Proceeding to Step 4.

## Step 4 — implementation

`src/spectrum_systems_core/governance/gov10_certification.py` written.
Single class `GOV10CertificationStep` with one public method
`certify(paper_id, run_id, repo_root, vault_root=None) -> dict`.
Standard library + jsonschema + reuse of existing surfaces only:

- `PublicationFormatter` (CHECK-3 replay)
- `EvalCoverageScanner` (CHECK-6 reuse — no reimplementation)
- `SOURCE_FAMILIES` (chain location)
- `ObsidianProjection.write_certification_projection` (vault optional)

Gate A applied Sev-2 #2 (passed+failed+skipped sum invariant) at the code
level: `_emit` derives all three counts from `check_results` and refuses
to emit if their sum ≠ 7.

`certify()` import-time guard (`_verify_certify_signature_no_override()`)
raises if anyone adds an `override`/`bypass`/`force`/`human_override`
parameter — defensive against future drift.

`certify_impl` is wrapped in a top-level try/except that converts ANY
unhandled exception into a fail-closed envelope via
`_build_failure_envelope`. A test
(`test_certify_never_raises`) monkeypatches `Path.read_text` to throw
`OSError` for every read and asserts the function returns a dict.

## Step 5 — CLI + projection

- `cli.py certify-paper --paper-id <uuid> --run-id <uuid> [--vault <path>]`.
  PASSED ⇒ exit 0 with release path + cert_id + total cost. FAILED ⇒
  exit 1 with all failure_reasons listed.
- `ObsidianProjection.write_certification_projection(record, vault_root)`
  writes `vault/Certifications/<cert_id>.md` with the VIEW ONLY banner
  as line 1 (asserted by `test_view_only_banner_first_line_of_projection`).

## Step 6 — eval registry

`contracts/evals/certification_evals.json` written with **6** eval cases.
The task spec called for 5; the EvalCoverageScanner flagged
`release_artifact` as `uncovered_artifact_type` (high severity), which
violates the Step 8 gate ("zero new high-severity flags on Phase K
files"). EVAL-CERT-006 closes that gap and resolves the contradiction
between spec point 6 ("5 cases") and the Step 8 gate. All 6 ids are
uuid v4. All 6 are `required: true`.

## Step 7 — tests

`tests/governance/test_gov10_certification.py` written. 17 tests, all
the required cases from the task spec. All pass:

```
17 passed in 1.59s
```

To make CHECK-5 testable (so `test_missing_eval_result_fails_check5` is
meaningful), `_eval_result_present` was tightened to require an explicit
result file at `synthesis/<run_id>/evals/<eval_case_id>.json` for every
required eval that targets a chain artifact_type. Tests stub these
files; the failure test omits one. Documented as a Phase K convention.

## Step 8 — full validation

Pre-requisite: `cffi` not installed in the environment; same fix as
Phase J (`pip install --upgrade cffi`).

| Check | Exit | Notes |
|---|---|---|
| `python -m pytest -q` | **0** | 691 passed (= 674 baseline + 17 new) |
| `python -m spectrum_systems_core.cli audit-governance` | **1** | total_flagged 175, **high 31 — identical to baseline**. Exit 1 is baseline-driven (pre-existing pre-Phase-K flags). |
| `python -m spectrum_systems_core.cli certify-paper --help` | **0** | help text matches command spec |
| Lint | N/A | no config detected |
| Type-checker | N/A | no config detected |

**audit-governance high-count diff (the binding gate)**

- Baseline (pre-Phase-K): high = 31, broken down as 21
  `uncovered_artifact_type` + 10 `prompt_like_string_outside_registry`.
- Post-Phase-K (with EVAL-CERT-006): high = 31, same breakdown.
- Phase K NEW high flags scanning Phase K files: **0**.
- Initial post-Phase-K run (before EVAL-CERT-006) had +1 high flag for
  uncovered `release_artifact` type. EVAL-CERT-006 resolved it.

The audit runs regenerate `governance/audits/*.json`,
`governance/audits/index.json`, `governance/dashboard/latest.json`, and
`governance/markdown/dashboard.md` every invocation. Those regenerated
files will be reverted before commit so the PR contains only Phase K
deliverables.

## Step 9 — Gate B (diff redteam)

Two iterations.

**Iteration 1.** Subagent returned 1 Sev-1 + 4 Sev-2. All four were
genuinely actionable; CLAUDE.md/constitution objections did not appear
this round (the certification module is no longer "novel" — it's the
diff against the Phase J merge).

| # | Sev | Finding | Fix applied |
|---|---|---|---|
| 1 | 1 | `_emit` wrote the PASSED record before attempting `_write_release`; a release-write OSError left the PASSED record orphaned on disk under `governance/certifications/` while a new FAILED record was written under a different `certification_id`. | Split `_write_release` into `_build_release_artifact` (in-memory build + schema validation) + `_persist_release` (disk write). New order: build release in memory → write record → persist release; on persist failure, `_delete_record_safe` rolls back the just-written record, then return failure envelope. Build-failure path does not write the record at all. |
| 2 | 2 | CHECK-5 returned PASSED with `all_required_eval_results_present:0` if no required cases targeted any chain artifact_type — silent unknown-state pass. | Added explicit fail when `required_cases` is empty: `missing_eval_result:no_required_cases_for_chain`. |
| 3 | 2 | CHECK-4 read `schema.get("version")` (rare top-level field) and silently skipped comparison when absent, but the actual pin in this repo is `properties.schema_version.const`. | New helper `_expected_schema_version` reads top-level `version` first, then falls back to `properties.schema_version.const`. Mismatch now actually fails. |
| 4 | 2 | `_eval_result_present` returned True on file existence; an empty/non-pass marker counted as result-present. | Now reads the marker JSON and requires `payload.get("status") == "pass"`. |
| 5 | 2 | `_eval_result_present` accepted but never used the `chain` parameter. | Dropped the parameter; signature is now `(repo_root, run_id, case)`. |

Re-ran full suite + audit-governance after iteration 1 fixes:

- `pytest -q` → 0, 691 passed (= 674 baseline + 17 new).
- `audit-governance` → exit 1, total_flagged 175, **high 31 — unchanged
  from baseline**, zero new high-severity flags on Phase K files.

**Iteration 2.** Verifying the iteration-1 fixes didn't introduce new
issues. Subagent returned **no blocking findings**. One non-blocking
observation about a narrow double-fault path (release write fails AND
record unlink fails); the original Sev-1 path is closed and the
double-fault would surface as `unexpected_error:OSError` via the
top-level try/except — acceptable per the "treat unknown states as bugs"
principle since the failure is recorded, not swallowed silently.

**Final Gate B verdict:** no remaining Sev-1 or Sev-2. Proceeding to
Step 10.

## Step 10 — single commit, open PR

(populated after commit)

