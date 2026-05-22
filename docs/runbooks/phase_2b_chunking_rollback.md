# Phase 2.B Chunk Overlap — Rollback Runbook

Status: Operator-facing.
Scope: The chunk-overlap change introduced in Phase 2.B (CHUNK_OVERLAP_TURNS
env var; `chunking_strategy_version` provenance field; the two governance
gates that key on those fields).

This runbook answers two questions:

1. How does an operator revert the change?
2. What happens to artifacts already produced under the change?

For the binding rollback contract entry (referenced by
`scripts/verify_rollback_contracts.py`), see
`docs/architecture/rollback_contracts.md`.

---

## 1. What changed

- `CHUNK_OVERLAP_TURNS` environment variable, read by both chunkers
  (`src/spectrum_systems_core/extraction/chunker.py` and
  `src/spectrum_systems_core/data_lake/chunker.py`). Default `0`. When
  `> 0`, each speaker-turn chunk at position `i` has its `text`
  prepended with the text of the prior `min(N, i)` turns; the
  recipient chunk records the prepended turn IDs and the prepended
  count.
- Per-chunk metadata fields on both chunker outputs:
  `overlap_turns_prepended: int`, `overlap_clamped: bool`,
  `prepended_overlap_turn_ids: list[str]`. All three are absent when
  `CHUNK_OVERLAP_TURNS=0` (preserving byte-identical pre-Phase-2.B
  output for the default-off path).
- New optional `chunking_strategy_version: str` field on the
  `meeting_minutes` artifact's `provenance` block. Stamped at
  extraction time. Values:
  - `"speaker_turn_v1"` — current behaviour, zero overlap. Treated
    as the default for any pre-Phase-2.B artifact that does NOT
    carry the field.
  - `"speaker_turn_v1_overlap{N}"` — overlap of `N` turns.
- New gate function `verify_no_overlap_only_attribution` in
  `src/spectrum_systems_core/promotion/gate.py`. Emits reason code
  `failed:extracted_from_overlap_context` when an extracted item's
  `source_turn_ids` reference only overlap-tagged turn IDs.
- New gate in `scripts/compare_opus_haiku.py` that halts with
  reason code `chunking_strategy_mismatch` when the two artifacts
  being compared declare different `chunking_strategy_version`
  values (with `None`/absent treated as `"speaker_turn_v1"`).
- New optional aggregate fields on `chunk_merge_summary.json` /
  `chunk_split_summary.json`:
  `overlap_turns_prepended_total: int`,
  `overlap_clamped_count: int`.

---

## 2. Revert

```bash
git revert <merge-sha>
```

This restores zero overlap and removes the `chunking_strategy_version`
field writer in production. The schema retains the field declaration
(it is documented optional and additive), so artifacts that were
produced under overlap and carry the field on disk remain valid against
the reverted schema.

After the revert:

- New extractions are byte-identical to pre-Phase-2.B output.
- The two new gates disappear; their reason codes
  (`failed:extracted_from_overlap_context`,
  `chunking_strategy_mismatch`) are no longer emitted.
- Existing artifacts in the data lake that carry
  `chunking_strategy_version` remain readable. The reader (comparison
  engine, status CLI) treats a missing or stale value as
  `"speaker_turn_v1"` and proceeds; no halt fires post-revert.

---

## 3. Artifact handling

- **Old artifacts (no `chunking_strategy_version`):** read as
  `"speaker_turn_v1"`. No error. No gate trip.
- **Phase-2.B artifacts (`chunking_strategy_version: "speaker_turn_v1"`
  explicit):** identical to old artifacts in behaviour.
- **Phase-2.B artifacts under overlap
  (`chunking_strategy_version: "speaker_turn_v1_overlap{N}"`):**
  remain on disk. Append-only — the data lake never deletes. The
  comparison engine post-revert treats these as
  `"speaker_turn_v1"` (the default) and may compare them against
  pre-Phase-2.B baselines without halting. Operators should be
  aware that this is technically a cross-strategy comparison and
  may report different F1 than the matched-baseline run.
- No mutation, no deletion.

---

## 4. Fixture regeneration

After a revert, the ground-truth pair fixtures under
`tests/fixtures/eval/ground_truth/` continue to be valid: every
pair pins its own `fixture_chunking_strategy` and those values are
not affected by the revert.

If an operator wants to regenerate fixtures against the post-revert
chunker explicitly:

```bash
python scripts/regenerate_gold_fixtures.py --strategy speaker_turn_v1
```

This script must exist before this rollback path is exercised; if
not present, the operator should regenerate fixtures by running the
extraction CLI against the canonical transcripts and comparing the
new `source_turn_ids` list against the in-fixture list, updating
the fixtures manually for the differences.

Old fixtures are archived under `tests/fixtures/eval/_archive/`
(when the archive directory exists), not deleted.

---

## 5. Verification that the rollback is clean

```bash
pytest tests/extraction/test_overlap_attribution_gate.py
pytest tests/comparison/test_chunking_strategy_version_gate.py
pytest tests/data_lake/test_chunker.py
pytest tests/extraction/test_chunker.py
```

After revert, `tests/extraction/test_overlap_attribution_gate.py`
and `tests/comparison/test_chunking_strategy_version_gate.py` are
expected to disappear. If they remain present and any test fails,
the revert is incomplete — fix forward.

The chunker tests (`tests/data_lake/test_chunker.py`,
`tests/extraction/test_chunker.py`) are expected to PASS post-revert
because they pin behaviour under the default `CHUNK_OVERLAP_TURNS=0`
which the revert restores byte-identically.
