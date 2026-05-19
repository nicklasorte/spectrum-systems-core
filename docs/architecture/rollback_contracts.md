# Rollback Contracts

Document ID: SSC-ROLLBACK-001
Status: Binding for any change with a versioned schema or governance gate
Scope: Spectrum Systems Core

This file documents the rollback path for every governance change that
introduces a new schema version, a new gate, or a new diagnostic artifact.
Every entry must answer two questions:

1. What does an operator do to revert the change?
2. What happens to artifacts already produced under the change?

Every Claude Code session that adds a new schema version or a new
gate MUST add a corresponding entry here BEFORE opening the PR.

---

## Phase 1 — verbatim span grounding (PR #XXX)

### What this change adds

- `meeting_minutes` schema bumped from 1.3.0 to 1.4.0.
- Every item-type sub-schema declares a `grounding_mode` discriminator
  with value `verbatim` or `turn_aggregate`.
- Verbatim items add optional `source_quote`, `quote_offset_normalized`,
  `quote_offset_original` fields. Turn-aggregate items add optional
  `source_turn_ids`.
- New module `src/spectrum_systems_core/grounding/` (normalization).
- New module `src/spectrum_systems_core/promotion/gate.py` with
  `verify_grounding()` and `grounding_rejection_report_payload()`.
- New artifact type `grounding_rejection_report` (diagnostic, never
  promoted, never indexed).
- New CLI flag `--allow-mixed-schema` on `scripts/compare_opus_haiku.py`
  (CLI-only — not env, not config).
- Comparison engine re-verifies grounding on 1.4.0 haiku artifacts and
  sets `tainted: true` when re-verification fails.

### To roll back

1. Revert the schema PR. Pre-1.4 artifacts in the data lake remain valid
   because they declare `schema_version: 1.3.x` (or earlier) and the
   pre-1.4 schema validates them unchanged.
2. The grounding gate becomes a no-op for 1.3.x artifacts automatically
   because callers only invoke it for 1.4.0 producers — pre-1.4
   workflows never enter the new code path.
3. Existing 1.4.0 artifacts in the data lake remain READABLE even after
   rollback: the comparison engine and downstream consumers tolerate the
   extra `grounding_mode` / `source_quote` / `quote_offset_*` /
   `source_turn_ids` keys because the comparator reads them only when
   present. If the strict schema validator runs against a 1.4.0 artifact
   under a reverted (pre-1.4) schema, it will fail
   `additionalProperties: false` — operators should NOT re-validate
   pre-existing 1.4.0 artifacts after rollback. The comparator does NOT
   re-validate stored artifacts on read, so the rollback does not
   spontaneously break any in-flight workflow.
4. The correction miner's hallucination-pattern handling becomes a no-op
   because no new `grounding_rejection_report` artifacts will be
   produced; any existing diagnostics on disk remain present as inert
   files (the miner's reader tolerates an empty set).
5. The `--allow-mixed-schema` CLI flag disappears with the revert.
   Any caller relying on it must drop the flag from its invocation.

### Data migration required for rollback

None. Pre-1.4 artifacts validate unchanged; 1.4.0 artifacts in the data
lake are read-only after rollback (their grounding fields become inert
metadata) but they do not need to be deleted, edited, or migrated.

### Verification that the rollback is clean

```bash
# 1. Pre-1.4 artifacts validate unchanged.
python -c "
import json
from spectrum_systems_core.validation import validate_artifact, _load_schema
_load_schema.cache_clear()
art = json.loads(open('<pre-1.4-artifact-path>').read())
validate_artifact(art, 'meeting_minutes')
print('OK')
"

# 2. Pre-1.4 workflows still produce a promoted artifact end-to-end.
python -m pytest tests/test_golden_transcripts.py -q
```

If either step fails, the rollback has not actually completed — fix
forward rather than re-revert.

---

## How to add a new entry

When a future PR adds a versioned schema, a new gate, or a new
diagnostic artifact, append a section to this file BEFORE merging.
Each section MUST contain:

1. **What this change adds** — bullet list of new files / schemas /
   gates / CLI flags.
2. **To roll back** — numbered steps an operator follows to revert.
3. **Data migration required for rollback** — usually "None"; if any
   migration is needed, document it explicitly.
4. **Verification that the rollback is clean** — copy-pasteable
   commands that prove the revert worked.

A PR that touches schema or governance without updating this file is
rejected by the pre-PR self-review pass (CLAUDE.md, Claude Code
Execution Standard).
