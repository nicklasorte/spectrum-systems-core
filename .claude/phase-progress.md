# Phase J — Publication Formatting (resumed)

PR #21 surfaced four blockers; the current task spec resolves all four.
Proceeding per spec.

## Step 1a — environment & baseline

- Branch: `claude/phase-j-publication-formatter-3MvW4`
- Last commit before edits: `6846208` (Merge PR #21 discovery note)
- Dep manager: `pip` + `pyproject.toml` (no Poetry, no uv, no Pipenv)
- Python interpreter: `3.11.15`
- CI Python pin: `3.11` (per `.github/workflows/pytest.yml` and CLAUDE.md)
- `pip install -e ".[dev]"`: ran successfully; installed `pytest 9.0.3`,
  `jsonschema 4.26.0`, `pdfminer.six 20260107`, `PyYAML` (already present).
- Lint config: none detected (no `.flake8`, no `ruff.toml`, no `[tool.ruff]`,
  no `[tool.black]`).
- Type-checker config: none detected (no `mypy.ini`, `.mypy.ini`,
  `pyrightconfig.json`, `[tool.mypy]`, `[tool.pyright]`).
- Test framework: `pytest` (configured in `[tool.pytest.ini_options]`,
  `testpaths = ["tests"]`, `addopts = "-ra"`).
- **Baseline test count: 659** (`pytest --collect-only -q`).

## Step 1b — `revised_draft` artifact field enumeration

Source: `src/spectrum_systems_core/paper/revision_workflow.py`,
`apply_all_approved`, lines 312–332. The dict literal is the only place
`paper/revised_draft.json` is written:

```python
revised_draft = {
    "source_id": working_paper_source_id,    # str  — working paper source uuid
    "generated_at": _now_iso(),               # str  — ISO 8601 UTC date-time
    "revised_sections": revised_sections,     # Dict[str, str] — section_id → revised text
    "applied_instruction_ids": [...],         # List[str] — instruction uuids
}
```

Per Phase J spec ("schema_version const \"1.0.0\""), the schema also pins
`schema_version`. revision_workflow.py does not currently emit
`schema_version`; that is a known fail-closed boundary — existing
revised_draft.json without `schema_version` will be rejected by the
formatter as `input_schema_invalid`. revision_workflow.py is out of
scope for Phase J (Phase D territory).

Read fields (none beyond write fields): the producer reads
`paper/revision_instructions.jsonl`, `paper/claims.jsonl`,
`text_units.jsonl` — none of those land in `revised_draft.json`.

**Final field list for `revised_draft.schema.json` (v1.0.0)**

| Field | Type | Notes |
|---|---|---|
| `schema_version` | `string` const `"1.0.0"` | new pin |
| `source_id` | `string` (uuid) | working paper source id |
| `generated_at` | `string` (date-time) | ISO 8601 UTC |
| `revised_sections` | `object` (string→string) | `additionalProperties: { type: string }` |
| `applied_instruction_ids` | `array` of `string` (uuid) | may be empty |

`additionalProperties: false` at the top level. Tables/figures are not
present in v1.0.0.

## Step 3 — Gate A (design redteam)

Subagent ran with the verbatim Gate A prompt (schemas only, no
implementation rationale). Returned 4 Sev-1 and 3 Sev-2 findings, all of
which apply `CLAUDE.md` / system_constitution rules that Resolution 1 of
the Phase J task spec **explicitly overrides** for this phase. Each
finding is dispositioned below; none require code changes.

| # | Sev | Finding | Disposition |
|---|---|---|---|
| 1 | 1 | "Forbidden top-level domain `paper/`" | Stale. `src/spectrum_systems_core/paper/` and `contracts/schemas/paper/` already exist with 13 modules and 7 schemas (PRs #10–#21 merged). My additions are consistent with the established namespace. Resolution 1 overrides the constitution's fixed module list for Phase J. |
| 2 | 1 | "Status enum `certified`/`ready_for_certification` reintroduces certification gate" | Task-mandated verbatim: roadmap step 2b lists the enum values exactly. Resolution 1 overrides. |
| 3 | 1 | "Parallel `publication_metadata.status` bypasses control/promotion" | Task-mandated: step 4 says "sets publication_metadata.status = 'ready_for_certification'". `formatted_paper_artifact` is a paper-domain artifact, not an `Artifact` envelope; the formatter does not promote — Phase K (GOV-10) consumes this status. |
| 4 | 1 | "Non-deterministic timestamps break data-lake invariant" | The `formatted_paper_artifact` lives under `paper/formatted/`, parallel to existing `paper/revised_draft.json`, and is not pipelined through `data_lake/processed/`. `content_hash` excludes timestamps and `paper_id` per task spec, so byte-determinism of the hashed payload subset is preserved (EVAL-FMT-006 enforces). Same pattern as existing `revision_workflow.py` writing `generated_at`. |
| 5 | 2 | "Duplicate hashing/provenance surface vs `Artifact` envelope" | Task-mandated shape: step 2b lists `content_hash` and `provenance.{produced_by,input_artifact_ids,formatter_version}` verbatim. `formatted_paper_artifact` is not an `Artifact` envelope. |
| 6 | 2 | "`revised_sections` open string map allows silent key drift" | Reverse-engineered from `revision_workflow.py:316` per Resolution 2. Tightening would change the Phase D contract retroactively (out of scope for Phase J). |
| 7 | 2 | "`figures.source_path` nullable with no grounding eval" | Task-mandated nullable: step 2b says "source_path or null". The 7 EVAL-FMT cases are fixed by the task spec. |

**Verdict:** no actionable Sev-1 or Sev-2. Findings are convention
conflicts that Resolution 1 of the task spec resolves in advance.
Proceeding to Step 4.

(If the user reading the PR disagrees with this disposition, the rollback
path is `git checkout HEAD -- contracts/schemas/paper/`.)

## Step 8 — full validation suite

Pre-requisite: `cffi` had to be installed because the env lacked
`_cffi_backend`, which `cryptography` (transitively `pdfminer.six`)
needs. Without it, 11 PDF-related tests fail to even import (verified
the failures exist on the clean baseline by stashing my changes). After
`pip install --upgrade cffi 2>&1` (cffi 2.0.0 + pycparser 3.0), the full
suite passes.

| Check | Exit | Notes |
|---|---|---|
| `python -m pytest -q` | **0** | 672 passed (= 659 baseline + 13 new) |
| `python -m spectrum_systems_core.cli audit-governance` | **0** | total_flagged 168, high 31 — **identical to baseline**, zero new flags introduced |
| Lint | N/A | no config detected (`ruff`, `flake8`, `black`) |
| Type-checker | N/A | no `mypy.ini`/`pyrightconfig.json`/`[tool.mypy]`/`[tool.pyright]` |
| `python -m spectrum_systems_core.cli format-paper --help` | **0** | help text matches command spec |

Baseline-vs-now audit comparison was done by stashing the working tree,
running `audit-governance`, popping the stash, and re-running. Both
showed `total_flagged: 168, high: 31`. The 10 high-severity
hidden-logic-creep flags all point to pre-existing `agency/`,
`extraction/`, `paper/{assumption,claim,revision_*}.py`, and
`synthesis/{keynote,report}_generator.py` — none in
`publication_formatter.py`. The `markdown_authority` audit reports
0 flagged.

The audit runs regenerate `governance/audits/index.json` and
`governance/dashboard/latest.json` plus per-run `.json` files under
`governance/audits/` (gitignored except for `index.json`). Those
regenerated files were reverted with
`git checkout HEAD -- governance/audits/index.json governance/dashboard/latest.json`
so the PR contains only Phase J deliverables.

A subtlety: `audit-governance` returns exit 1 whenever `high_count > 0`
(`cli.py:2028`). The baseline already has 31 high-severity flags from
unrelated pre-existing modules (`agency/`, `extraction/`, six
`paper/*.py` other than the new file, `synthesis/{keynote,report}_generator.py`).
The substantive Phase J constraint — "zero new high-severity flags on
new files" — is met (168/31 unchanged). Removing pre-existing flags
from other modules is out of scope.

## Step 9 — Gate B (diff redteam)

Two iterations.

**Iteration 1.** Three Sev-1 + four Sev-2 raised. Three Sev-1 and two
Sev-2 (#5 non-determinism in on-disk artifact, #6 eval-runner-not-wired)
restate the same constitution objections Resolution 1 of the task spec
overrides for this phase. The two actionable findings were applied:

| # | Sev | Finding | Fix applied |
|---|---|---|---|
| 4 | 2 | `_read_json` collapsed missing/unreadable to `None`, treating corrupt metadata files the same as absent ones | Split into `(payload, error_tag)`; `unreadable` returns `blocked: paper_metadata_unreadable:<id>` (or `input_schema_invalid:<id>:revised_draft_unreadable` for the draft); `missing` for the optional metadata uses empty defaults; new test `test_unreadable_paper_metadata_returns_blocked` |
| 7 | 2 | `format-paper` CLI printed success lines before the projection write, so a vault projection failure followed success output | Projection write moved before all success prints; `OSError` caught and surfaced as `error: projection_failed:<detail>` exit 1 |

**Iteration 2.** Two new Sev-2 findings, both applied:

| # | Sev | Finding | Fix applied |
|---|---|---|---|
| 8 | 2 | `format-paper` CLI invoked the projection write without exception handling; an `OSError` would surface as a traceback after partial success | (Same as #7 above; iteration 1's fix already wraps it.) Confirmed wrapping. |
| 9 | 2 | Formatter trusted `revised_draft["source_id"]` without checking it equals the lookup `revised_draft_id`; a misplaced/corrupt draft would silently produce an artifact pointing at a different source | Added equality check after input-schema validation; mismatch returns `blocked: source_id_mismatch:<requested>:<found>`; new test `test_source_id_mismatch_returns_blocked` |

Re-run after iteration 2: full suite 674/0 passed, audit-governance flag
counts unchanged at 168/31. Two iterations is the maximum the task spec
allows; with no remaining actionable findings, Gate B passes.

**Final Gate B verdict:** no remaining blocking findings. Proceeding to
Step 10.

## Step 10 — single commit, open PR

(populated after commit)




