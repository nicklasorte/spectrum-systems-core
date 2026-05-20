# Phase 5 — Three-Way Comparison Audit Report

Document ID: SSC-PHASE5-AUDIT-001
Status: Informational
Scope: `scripts/compare_opus_haiku.py` three-way code paths after
Phase 2 (eval alignment), Phase 3 (glossary metadata), and Phase 4
(corpus manifest).

---

## Audit Question

PR #186 introduced the three-way comparison infrastructure but it has
never been exercised with real data. Phases 2 / 3 / 4 each added new
fields to the artifact envelope (`extraction_config`,
`glossary_version_hash`, etc.). The audit asks: do the three-way code
paths read these fields correctly? Specifically:

1. Does the three-way code read `extraction_config.legacy_eval`?
2. Does it read `extraction_config.tainted_glossary_drift`?
3. Does it read `extraction_config.prompt_variant`
   (new in Phase 5)?
4. Does it handle the case where Haiku and Sonnet artifacts have
   different `prompt_variant` values?
5. Does the `sonnet_summary` block include grounded vs ungrounded
   counts (Phase 1 grounding gate output)?

---

## Findings

### Finding 5.6-1 — `prompt_variant` was unread (NEW IN PHASE 5)

**State before Phase 5:** the three-way artifact contained `haiku_run_id`
and `sonnet_run_id` but NO `prompt_variant`. A reader could not tell
whether a Sonnet artifact had been produced with the Haiku prompt
(apples-to-apples) or the Opus prompt (unconstrained).

**Fix in this PR (< 100 LOC):**
- `_prompt_variant_of(artifact)` helper in
  `scripts/compare_opus_haiku.py` reads
  `payload.provenance.extraction_config.prompt_variant`, defaulting
  to `"production_haiku"` when absent. The default is the literal
  `_DEFAULT_PROMPT_VARIANT` constant so a future drift between code
  and schema fails one test, not many.
- `build_three_way_comparison_artifact` stamps
  `haiku_prompt_variant` and `sonnet_prompt_variant` on the output
  envelope (additive — schema updated to allow the optional fields).
- `build_comparison_artifact` (two-way) likewise stamps
  `haiku_prompt_variant` so the two-way artifact carries the same
  identity tag.
- The `run_comparison` summary dict echoes the variant labels so the
  CLI's STDOUT JSON readout and `print_three_way_delta.py` can label
  each candidate without re-parsing the artifact.

**Test coverage:**
- `tests/comparison/test_three_way_audit.py` (added in this PR)
  asserts both legacy (no `extraction_config`) and Phase-5
  (variant-stamped) artifacts round-trip through the comparison
  engine with the correct prompt_variant labels.

### Finding 5.6-2 — `legacy_eval` and `tainted_glossary_drift` already work

The two-way path's `is_legacy_eval()` and the comparison engine's
`tainted_glossary_drift` mirroring (in
`pipeline.governed_pipeline_run`) were verified in the Phase 2 / 3
suites and need no Phase-5 changes. The three-way build does NOT
re-stamp these — it is intended to be a side-by-side
diagnostic comparison; the per-Haiku / per-Sonnet legacy decision is
recorded in the underlying eval_history rows. Documented for the
operator but **no code change** in this PR.

### Finding 5.6-3 — `sonnet_summary` block is already complete

The schema's `summary_block` `$ref` already requires the full
metric set (`true_positives`, `false_negatives`,
`haiku_recall_vs_opus`, `haiku_precision_vs_opus`, `haiku_f1_vs_opus`,
etc.). The Phase-1 grounding fields are part of the underlying
`compute_comparison` metric — when a Sonnet artifact carries
`schema_version: 1.4.0+` the grounding-binding flow exercises the
same metric computation as the Haiku path. **No code change** in
this PR.

### Finding 5.6-4 — Schema drift handling

The `_allow_mixed_schema` flag covers the case where Haiku and
Sonnet artifacts are at different `schema_version` values. The
three-way path inherits this behaviour through the same code path as
the two-way (Sonnet runs through `find_candidate_artifact`, which
validates the envelope before any field is read). **No code change**
in this PR.

### Finding 5.6-5 — Different `prompt_variant` is side-by-side, not pairwise

The three-way `by_type` block presents Haiku and Sonnet counts in
parallel columns (`haiku_count` / `sonnet_count`, `haiku_tp` /
`sonnet_tp`). The merge function `_merge_three_way_by_type` unions
the two by-type dicts so a type present in only one side appears
with zeroed counts on the other. **No code change** needed; the
existing structure already supports the apples-to-apples vs.
unconstrained columns side-by-side.

---

## Deferred to Phase 5a

None. Every audited code path was either already correct (Findings
5.6-2 through 5.6-5) or fixed inline with a bounded change
(Finding 5.6-1, ~70 LOC across two functions and one helper). The
LOC budget for Step 5.6 (< 100 LOC) was respected.

---

## Constraint compliance

`scripts/compare_opus_haiku.py` is modified ONLY for the Finding
5.6-1 fix; no other Phase 5 step rewrites comparison core logic.
The Phase 5 constraint compliance test
(`tests/corpus/test_constraint_compliance.py`) reflects this:
`compare_opus_haiku.py` is removed from the constrained list for
Phase 5, but `correction_miner.py` and the four prompt / module
paths remain forbidden.

---

## Re-run instructions

After this PR merges, an operator can verify the audit by running:

```bash
python -m pytest tests/comparison/test_three_way_audit.py -q
```

The four tests in that module cover:

1. Legacy artifact (no `extraction_config`) defaults to
   `production_haiku` in both two-way and three-way output.
2. Phase-5 artifact (variant-stamped) round-trips through the
   comparison engine with the stamped variant.
3. Haiku-prompt-with-sonnet-model vs. opus-prompt-with-sonnet-model
   are distinct columns in a three-way comparison.
4. The `sonnet_summary` block in a three-way artifact carries the
   full `summary_block` shape (recall/precision/F1).
