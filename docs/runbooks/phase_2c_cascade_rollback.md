# Phase 2.C Cascade Filter First Dispatch — Rollback Runbook

Status: Operator-facing.
Scope: The first dispatch of the Stage 2 cascade filter against a real
transcript (the Dec 18 7 GHz Downlink TIG kickoff). The cascade module
itself was built in PR #203; Phase 2.C does NOT change cascade code.
Phase 2.C only adds a smoke test (PR-2) and turns the cascade on against
a real transcript via the existing `run-comparison.yml` workflow.

This runbook answers two questions:

1. How does an operator revert the dispatch?
2. What happens to artifacts already produced by the dispatch?

For the binding rollback contract entry (referenced by
`scripts/verify_rollback_contracts.py`), see
`docs/architecture/rollback_contracts.md` — section "Phase 2.C —
cascade filter first dispatch".

---

## 1. What changed

The Phase 2.C work is a hookup + smoke-test sequence, not a code change
to the cascade. The cascade filter module (`src/spectrum_systems_core/cascade/`)
and its production dispatch path (`cli.py::_dispatch_cascade_filter`,
the `--enable-cascade-filter` CLI flag, the `use_cascade_output` input on
`.github/workflows/run-comparison.yml`) were all built in PR #203 (see
the "Phase 6 — Stage 2 cascade filter" entry in
`docs/architecture/rollback_contracts.md`).

Phase 2.C adds:

- **PR 1 (docs-only)** — this runbook plus a corresponding entry in
  `docs/architecture/rollback_contracts.md`. No code, no schema, no
  workflow changes.
- **PR 2 (smoke test + fixture)** — a static fixture at
  `tests/cascade/fixtures/phase_2c_smoke_items.json` (3 items derived
  from the existing Haiku artifact `d019c5f793c4`) and a smoke test
  at `tests/cascade/test_cascade_smoke_real_items.py` that drives
  the production `run_cascade_filter` against the fixture. The smoke
  test is fully static — no data-lake read in CI. No cascade code is
  modified.
- **Post-merge dispatch (operator action, no code)** — dispatch
  `.github/workflows/run-comparison.yml` with `use_cascade_output=true`
  and `source_id=7-ghz-downlink-tig-meeting-kickoff---transcript-20251218`.
  The workflow auto-selects the cascade-filtered artifact (or cascades
  on the fly per the existing `compare_opus_haiku.py` logic) and
  compares against the `speaker_turn_v1` Opus baseline.

The only NEW artifacts on disk after the dispatch are:

- `<lake>/store/processed/meetings/<source_id>/meeting_minutes_filtered__<ts>.json`
  (the cascade-filtered `meeting_minutes_filtered` artifact, schema
  version 1.0.0).
- `<lake>/store/processed/meetings/<source_id>/diagnostics/cascade_filter_log__<ts>.json`
  (the per-decision diagnostic; 30-day TTL per
  `CASCADE_FILTER_LOG_TTL_DAYS`).
- A new `comparison_result` row in
  `<lake>/store/processed/meetings/<source_id>/` stamped with
  `prompt_variant=production_haiku_with_cascade_filter` (per
  `scripts/compare_opus_haiku.py:1700-1729`).

All three are append-only. None of them are promoted product artifacts;
none enter `indexes/meetings/artifact_index.jsonl`.

---

## 2. Revert

The cascade is append-only and opt-in (default OFF). "Revert" means
stop dispatching the cascade — there is no code to revert.

```bash
# Stop dispatching the cascade. The next run-comparison invocation
# leaves use_cascade_output=false (the default), so the comparison
# returns to the raw Haiku artifact path.
gh workflow run run-comparison.yml \
  -f source_id=7-ghz-downlink-tig-meeting-kickoff---transcript-20251218 \
  -f use_cascade_output=false
```

After "revert":

- No new `meeting_minutes_filtered__*.json` files are written for
  subsequent runs.
- The existing cascade-filtered artifact from the dispatch remains on
  disk in the data-lake — the data-lake is append-only from core's
  perspective (per `docs/contracts/data_lake_contract.md` §8). It is
  NEVER deleted.
- The existing `cascade_filter_log__*.json` diagnostic remains on
  disk; it expires under its own 30-day TTL when the operator runs
  the lake-side TTL sweep.
- Reverting PR 1 (this runbook) is a docs-only revert. Reverting PR 2
  removes the smoke test and fixture; no on-disk artifact is affected.

---

## 3. Gate-bug response: cascade dropping too aggressively

If the dispatch produces an `meeting_minutes_filtered` artifact whose
drop rate is so high that downstream F1 collapses (recall collapse),
the cascade artifact must NOT be deleted. Instead:

1. Mark the cascade artifact as superseded by adding a
   `superseded: true` field to its `filter_metadata` block (or by
   writing a sidecar `<filename>.superseded.json` if editing the
   artifact in place is not allowed by the data-lake's append-only
   contract — the lake's reader treats either as a "do not use this
   for the next promotion gate" signal).
2. Re-run `run-comparison.yml` with `use_cascade_output=false` so the
   comparison falls back to the base Haiku artifact, which remains
   canonical (the cascade output is downstream of, not a replacement
   for, the base Haiku artifact — see
   `docs/inventory/phase_2c_cascade_inventory.md` §"Sequencing
   implication").
3. Open a follow-up issue capturing: drop rate, items dropped per
   extraction_type, and the chunk indices with
   `chunks_with_invalid_filter_response > 0`. The diagnostic file
   `cascade_filter_log__<ts>.json` carries all three.
4. Do NOT change `cascade/executor.py` or any cascade schema as part
   of the gate-bug response. A code change requires a fresh
   architectural review and its own rollback-contract entry.

---

## 4. Artifact handling

- **Pre-Phase-2.C artifacts in the data lake (no cascade output):**
  unchanged. The dispatch is opt-in; the existing
  `meeting_minutes__*.json` files are not touched.
- **Phase-2.C cascade-filtered artifacts
  (`meeting_minutes_filtered__*.json`):** remain on disk. Append-only,
  never deleted. If marked `superseded`, downstream comparisons MUST
  skip them per the gate-bug response above.
- **Phase-2.C cascade diagnostic logs
  (`diagnostics/cascade_filter_log__*.json`):** retained for 30 days
  by their TTL. Operators may run the lake-side TTL sweep at any time.
- **Phase-2.C comparison results
  (`prompt_variant=production_haiku_with_cascade_filter`):** retained
  on disk as historical evidence of the run. Subsequent comparisons
  with `use_cascade_output=false` produce separate
  `comparison_result` rows stamped `prompt_variant=production_haiku`
  (the base variant). The two rows coexist; the comparison engine
  does not collapse them.

No mutation. No deletion. The append-only property of the data lake
applies.

---

## 5. Verification that the rollback is clean

```bash
python scripts/verify_rollback_contracts.py --pr <PR-1-number>
pytest tests/cascade/test_cascade_smoke_real_items.py
```

After PR 1 lands, the first command MUST pass — it asserts that this
runbook's companion entry in `rollback_contracts.md` references PR 1's
changed files and carries a whitelisted verification command.

After PR 2 lands, the second command MUST pass — it exercises the
smoke fixture against the production `run_cascade_filter`. If the
smoke test fails after PR 2 has landed, the cascade dispatch path is
broken; do NOT proceed to the operator dispatch step described in
§"What changed" until the test is green.

If the operator dispatch was already executed and is being rolled back
under §3 (gate-bug response), no test re-run is required by the
rollback itself — the cascade module is unchanged, so the test
behaviour is unchanged.
