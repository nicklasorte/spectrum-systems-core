# Phase 2.D Decision Sub-Pass — Inventory

**Date**: 2026-05-23
**Branch**: `claude/phase-2d-inventory-B1r9d`
**Measured F1 at inventory time**: 36.5% (Phase 2.C cascade, 2026-05-23)
**Hard gate**: F1 ≥ 39.5% before Phase 2.D execution begins
**Status**: docs-only. No code, schemas, prompts, or workflows changed.

Headline: **no decision sub-pass exists in the codebase.** The
Fernández taxonomy lives only as Markdown prose inside one extraction
prompt and as four header-string assertions in one verification
script. No schema carries a per-item taxonomy label, no module
re-classifies an already-extracted decision, no CLI flag enables a
sub-pass, no workflow dispatches one, no eval scores per-class F1,
and no rollback runbook exists for it. Phase 2.D is a build against
an empty slot, not a hookup against existing wiring (the way Phase
2.C was for the cascade).

This inventory was produced by re-running each step of the inventory
plan against the current branch. The prior inventory
(`docs/inventory/phase_2d_decision_subpass_inventory.md`, merged
2026-05-23 as PR #227) reaches the same conclusion; this file is the
Step-4 deliverable in the format requested by the Phase 2.D plan and
adds the explicit F1-gate pre-condition table.

## Pre-conditions (must all be true before Phase 2.D execution)

- [ ] Claims placeholder fix merged and F1 re-measured
- [ ] Cascade threshold tuning complete and F1 re-measured
- [ ] F1 ≥ 39.5% (hard gate cleared; current measured F1 = 36.5%)
- [ ] Taxonomy reconciliation resolved (`scope` vs `agreement` — see §"Taxonomy Conflict")

Until ALL four boxes are checked, Phase 2.D execution must not start.

## Component Status

Statuses use the four-way distinction from CLAUDE.md: `present` /
`present_never_measured` / `partial` / `missing` / `unknown`.

| Component | Status | Notes |
|---|---|---|
| Schema: `decision_type` enum (Fernández 4-class) | **missing** | The existing `decision_type` enum is OUTCOME-shaped, not discourse-function-shaped. `meeting_minutes.schema.json:38-111` and `meeting_extraction.schema.json:42-122` carry `decision_type` enums whose values describe outcome (`approved / rejected / deferred / noted / considered / action_required / open_question / to_be_determined` in `meeting_extraction.schema.json:53-58`); no field carries the `issue / proposal / resolution /{agreement\|scope}` discourse class. Both schemas declare `additionalProperties: false` on decision items, so a new field cannot be added by a producer without a schema bump. |
| Schema: `decision_classifier` / `decision_subpass` artifact type | **missing** | `grep "decision_classifier\|decision_subpass\|sub_pass" src/spectrum_systems_core/schemas/` returns zero matches. No schema file in `src/spectrum_systems_core/schemas/` declares a decision-classification artifact type. The cascade's `meeting_minutes_filtered` (separate artifact, not a mutation of the source) is the template a Phase 2.D artifact should mirror. |
| Prompt: dedicated decision-classifier prompt file | **missing** | `ls src/spectrum_systems_core/workflows/prompts/` → `cascade_filter_sonnet.md`, `meeting_minutes_llm.md`, `meeting_minutes_opus.md`. No `decision_classifier.md`, no `decision_subpass.md`, no `fernandez_classifier.md`. The Fernández guidance lives ONLY as a section inside `meeting_minutes_llm.md` (lines 505–569 in the current branch). |
| Prompt: Fernández taxonomy embedded in any prompt | **partial** (guidance only) | `meeting_minutes_llm.md:497-569` documents the 4 sub-types with header titles `Sub-type 1: Issue identification`, `Sub-type 2: Proposal / Direction`, `Sub-type 3: Resolution / Agreement`, `Sub-type 4: Scope / Boundary ruling`. Cites Fernández et al. SIGDIAL 2008 and Hsueh & Moore NAACL 2007 (line 498). The model is told to RECOGNIZE implicit decisions via these patterns but is NOT asked to emit a per-item Fernández label. |
| CLI flag: `--enable-decision-subpass` or equivalent | **missing** | `grep -n "subpass\|sub_pass\|decision-pass\|decision_pass\|fernandez" src/spectrum_systems_core/cli.py` returns 0 matches. No dispatch block analogous to the cascade flag (`cli.py:3414-3426`). |
| Pipeline integration: sub-pass invoked after extraction | **missing** | `grep "decision.*pass\|sub.*pass" src/spectrum_systems_core/workflows/` returns no matches in any workflow file. The cascade is the only existing post-extraction filtering pass (`cascade/executor.py`); it filters keep/drop and does not classify by Fernández. No code path reads a freshly-written `meeting_minutes` artifact, classifies its `decisions[]` items, and writes a side-by-side artifact. |
| Evaluation: per-Fernández-class F1 in `compare_opus_haiku.py` | **missing** | `grep "decision.*subpass\|subpass\|fernandez\|per.*class" scripts/compare_opus_haiku.py` returns 0 matches. The comparison engine measures decisions as one undifferentiated array (`_f1` at `scripts/compare_opus_haiku.py:952`); there is no `--by-decision-subtype` flag or per-class F1 surface. |
| Test coverage: decision sub-pass tests | **missing** | `find tests/ -name "*.py" | xargs grep -l "decision.*subpass\|subpass\|fernandez"` returns 0 files. Existing decision-related tests cover unrelated surfaces: `tests/test_decision_brief_workflow.py` (decision-brief workflow), `tests/test_control_decision.py` (loop control), `tests/governance/test_decision_divergence_detector.py` (governance). No fixture pairs a transcript snippet with an expected Fernández class. |
| Workflow: `run-decision-subpass.yml` or equivalent | **missing** | `ls .github/workflows/ | grep -iE "decision\|subpass"` returns nothing. No phone-safe dispatch exists. Phase 2.B / 2.C precedent (`run-cascade-filter.yml`) is the template. |
| Rollback runbook | **missing** | `ls docs/runbooks/` → `first_run.md`, `phase_2b_chunking_rollback.md`, `phase_2c_cascade_rollback.md`, `verification-cycle-recovery.md`. No `phase_2d_decision_subpass_rollback.md`. |
| Rollback contracts entry | **missing** | `docs/architecture/rollback_contracts.md` has 13 `^## Phase` sections (last is "Phase 2.C schema fixes …" at line 1615); no Phase 2.D entry. The only mention of Phase 2.D in the file is a forward reference (`rollback_contracts.md:1609` — "Proceed to Phase 2.D"). The Phase 6 / Phase 2.C entries are the template. |
| Decision sub-pass ever run end-to-end on a real transcript | **unknown (no by construction)** | No writer exists, so no `decision_subpass__*.json` can be in the lake. `processed/meetings/` in this repo is `.gitkeep` only; CLAUDE.md "Data-lake separation" pins all artifacts to external `nicklasorte/data-lake`. The honest framing: even if the lake were accessible, no such artifact could be there because the code path is missing. |

Reading: every line above is `missing` or `partial`. The single `partial`
entry (Fernández taxonomy in prompt text) is "built but never measured" —
the prompt instructs the model on the 4 sub-types, but no field on any
artifact records the model's classification, no eval scores it, and
`scripts/verify_trigger_taxonomy.py` only string-asserts that the
prompt headers exist. The taxonomy is currently unfalsifiable.

## Taxonomy Conflict

Two distinct enums are in play:

- **Codebase enum (actual)** — `issue / proposal / resolution / scope`.
  Pinned by `scripts/verify_trigger_taxonomy.py:31-36`
  (`_SUBTYPE_HEADERS`), which asserts the four sub-type headers exist
  in `meeting_minutes_llm.md`. Sub-type 3's header is
  "Resolution / Agreement" (one item labelled with both names); sub-type
  4's header is "Scope / Boundary ruling". The verification script's
  programmatic enum is `(issue, proposal, resolution, scope)` — `scope`,
  not `agreement`.
- **Source-document enum (task brief)** — `issue / proposal / resolution / agreement`.
  This is what the Phase 2.D plan in the task description specifies as
  the Fernández taxonomy.

**Resolution needed**: the canonical 4-class enum must be pinned
explicitly before the classifier prompt is built or any rejection gate
is wired. Two viable choices:

1. **Adopt `agreement`** — match the source document. This requires
   updating `meeting_minutes_llm.md:541` (rename "Resolution / Agreement"
   → "Agreement") and `scripts/verify_trigger_taxonomy.py:31-36`
   (rename the fourth entry from `("scope", "## Sub-type 4: …")` to
   `("agreement", "## Sub-type 3: …")` and revise the 4-class layout).
   Note: the existing sub-type 3 already carries the dual label
   "Resolution / Agreement" — the split is subtle (resolution = closure
   reached, agreement = consensus commitment) and may collapse into one
   class.
2. **Keep `scope`** — accept the codebase prior. This requires the
   Phase 2.D plan and any classifier prompt to use
   `issue / proposal / resolution / scope`, and abandons the
   distinction the source document draws between resolution and
   agreement.

**This PR does NOT change the enum.** Per the task brief's invariants:
"Do NOT change the enum in this PR — document the conflict and flag it
for human decision."

Note on the schema-versioning side of the choice: adding `agreement`
as an additive enum expansion to a future `decision_subtype` field
does not require a `schema_version` bump (per the additive-enum policy
in `meeting_minutes.schema.json:1618`); only narrowing/removing an
enum value would. The taxonomy decision is therefore a product /
linguistic question, not a schema-versioning one.

## Build Estimate

Based on the inventory, every layer required for Phase 2.D is
**missing**. The build is not "wire up an existing module"; it is
"design and add the module, the artifact, the prompt, the flag, the
workflow, the eval, the tests, and the rollback runbook." Phase 2.B
(chunking) and Phase 2.C (cascade) are the structural precedents.

Plausible PR sequence (mirroring the cascade build sequence):

1. **Taxonomy reconciliation PR** (docs + prompt + verifier only).
   Pin the canonical 4-class enum, update
   `meeting_minutes_llm.md:541-569` and
   `scripts/verify_trigger_taxonomy.py:31-36` to match, no model
   work. Trivial. Required before any later PR.
2. **Schema + stub PR.** Add a new `decision_classification` artifact
   type with a strict 4-class `decision_subtype` enum, a deterministic
   stub classifier behind `--enable-decision-subpass=stub`, and two
   rejection tests (invalid label → BLOCK, missing classification →
   BLOCK). Mirrors the cascade's separate-artifact pattern
   (`meeting_minutes_filtered` is the analogue, not a mutation of
   `meeting_minutes`). Medium scope.
3. **Real classifier prompt + Sonnet wiring PR.** Adds the Sonnet
   classifier prompt (separate file under `workflows/prompts/`), the
   per-call item cap (`MAX_ITEMS_PER_DECISION_SUBPASS_CALL = 30` —
   ported, not inherited, from cascade), the cost estimator
   extension, and the dispatch workflow. Medium-large scope.
4. **Measurement + rollback PR.** Adds the per-class F1 slice to
   `scripts/compare_opus_haiku.py`, the `phase_2d_decision_subpass_rollback.md`
   runbook, and the `rollback_contracts.md` entry, then runs the
   corpus comparison and gates Phase 2.D on a non-overlapping
   confidence interval vs baseline. Medium scope, depends on PRs 1–3
   landing AND the 39.5% F1 gate being cleared.

Total: ~4 PRs, each independently revertable. The first PR is a
pre-condition for the others; PRs 2 and 3 are the real build; PR 4 is
the measurement gate and cannot start until F1 ≥ 39.5% is achieved on
the cascade.

## Reference

For deeper analysis — exact code path traces, the 5 "additional reads"
questions and answers, the unmeasured-but-built prompt section, and
the "smallest possible Phase 2.D PR" discussion — see the prior
inventory at `docs/inventory/phase_2d_decision_subpass_inventory.md`
(merged as PR #227). The findings there remain accurate on this
branch: a diff between PR #227's merge commit (`546ba98`) and the
current HEAD touches none of the decision-extraction code paths
(`meeting_minutes_llm.md` changes since then are unrelated — type
rules for `turn_id` fields and the `clarification` `position_type`
enum value).

## Invariants confirmed

- `artifact_type`, never `artifact_kind`, throughout this document.
- Every claim cites a file or grep result; "missing" means the search
  returned zero matches, not "present under another name."
- `present_never_measured` is reserved for the one `partial` row
  (Fernández taxonomy in prompt text).
- No code, schema, prompt, workflow, or runbook is modified by this PR.
- Phase 2.D execution does not start until the four pre-condition
  boxes above are all checked.
