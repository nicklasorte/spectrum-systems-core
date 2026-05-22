# Phase 2.B Chunking — Step 1 Source Inventory

Status: read-only inventory. No code, schemas, or workflows are
changed by this document. Every claim cites a file and a line; "not
found" means the search returned nothing — it does NOT mean "present
under another name".

Two distinct chunkers exist in this repo, and they are easy to confuse
because both produce per-turn records and both speak about "chunks":

- `src/spectrum_systems_core/extraction/chunker.py` — the **cascade
  pipeline** chunker. Reads `text_units.jsonl` and writes
  `chunks.jsonl` for the deterministic cascade extraction stages
  (chunk-classifier, story extractor, etc.). Entry:
  `Chunker.chunk(source_id, repo_root)` at line 541.
- `src/spectrum_systems_core/data_lake/chunker.py` — the **live-LLM
  extraction** chunker. Pure function `chunk_transcript(text)` at line
  329 used by `workflows/meeting_minutes_llm.py` (and the
  sonnet/haiku/opus variants). No `chunks.jsonl` is written here; the
  chunks live in memory and feed the LLM call directly.

The roadmap MUST distinguish these. A change that re-shapes
`extraction/chunker.py` does NOT automatically reshape what the LLM
sees, and vice versa.

## Inventory table

| Capability | Status | Evidence (file:line) | Notes |
| --- | --- | --- | --- |
| Current chunking strategy (fixed-size? sliding? speaker-aware?) | **present** | `extraction/chunker.py:590-604` (cascade); `data_lake/chunker.py:329-407` (LLM) | **Cascade**: speaker-turn first; falls back to a sliding character-unit window. **LLM**: speaker-turn (`speaker_turn_v1`) → blank-line (`blank_line_v1`) → 512-word recursive (`recursive_512`), three-tier auto-fallback. |
| Chunk size (tokens or chars or turns?) | **present** | `extraction/chunker.py:53` `CHUNK_SIZE = 8` (text units); `chunker.py:61` `MIN_CHUNK_CHARS=150`; `chunker.py:69` `MAX_CHUNK_CHARS=2500`; `data_lake/chunker.py:79` `RECURSIVE_WORD_BUDGET = 512` (words) | Cascade-pipeline char bounds are configurable via env (`MIN_CHUNK_CHARS`, `MAX_CHUNK_CHARS`). LLM-path uses a 512-word window only in the recursive fallback; the speaker-turn paths produce one chunk per turn with no per-chunk size cap. |
| Chunk overlap | **partial** | `extraction/chunker.py:54` `OVERLAP = 1`; `chunker.py:875-880`; `chunker.py:36-38` (chunk schema `overlap_unit_id`) | Overlap is **1 text unit** in the cascade character-count fallback only. **Speaker-turn chunks have no overlap** (`overlap_unit_id: null`, `chunker.py:972`). LLM chunker has **no overlap at all** in any of its three strategies. Not configurable today. |
| Speaker-turn awareness — chunks aligned to turn boundaries | **present** | `extraction/chunker.py:446-450` (regex), `chunker.py:913-1009` (`_chunk_by_speaker_turns`); `data_lake/chunker.py:65` (regex), `chunker.py:185-293` (`_split_on_speaker_labels`) | Both chunkers chose speaker-turn as the **default** for transcripts. Cascade regex is permissive (`[A-Za-z+][^\t\n:]*?` + `HH:MM`); LLM regex is strict ALL-CAPS (`[A-Z][A-Z\s\-\.]{1,40}:`). Different inputs may pass one chunker and fall back to another in the other chunker. |
| Per-item `chunk_id` carried through to extraction artifacts | **partial** | `contracts/schemas/chunk.schema.json:21` (required UUID on the chunk envelope); `contracts/schemas/extraction/meeting_extraction.schema.json:64-68, 95-99, 124-128` (`source_turn_ids: array of string`) | The chunk envelope mandates `chunk_id` (UUID). Extracted items carry chunk references via `source_turn_ids: array<string>` (minItems 1) — **not pinned to UUID format**. Fixtures use `chunk-10` / `t0007` style strings; the live-LLM workflow keys on `turn_id` (e.g. `t0042`), the cascade workflow keys on the UUID `chunk_id`. The substring/grounding gates (PR #212 family) require these strings to validate against the live chunk list. |
| Per-item `turn_id` carried through | **partial** | `data_lake/chunker.py:149` (`turn_id: f"t{index:04d}"`); used by `workflows/meeting_minutes_llm.py:247-254` to render the per-turn block | Present on LLM chunks (`t0000`-format). **Not on cascade chunks**; the cascade calls the same identifier `chunk_id` (UUID). `chunk_metadata_gate.py:41` treats `chunk_id` and `turn_id` as aliases at the gate boundary. The two namespaces do not collide because the gates check existence, not format. |
| Per-item `speaker` carried through | **partial** | Chunk: `extraction/chunker.py:975`, `data_lake/chunker.py:151`. Extraction item: `meeting_extraction.schema.json:94` (claims only) | Speaker is captured on **every chunk** in both chunkers (nullable for the LLM chunker's leading no-speaker chunk and `blank_line_v1` paths). Carried into extraction items **only on `claims`** (`speaker: string`, required). **Not on `decisions`, not on `action_items`.** |
| Per-item `timestamp` carried through | **partial** | Cascade: `extraction/chunker.py:976`; chunk schema `contracts/schemas/chunk.schema.json:46`. LLM: not extracted; `data_lake/chunker.py:160` sets `word_level_timestamps: False` | Cascade chunks carry `timestamp` (HH:MM, nullable) when the speaker label regex captures one. LLM chunks do not. **No extraction artifact item carries a timestamp field** at all — there is no field named `timestamp` in `meeting_extraction.schema.json`. |
| Agenda-item or topic-shift metadata | **present** | Cascade: `extraction/chunker.py:660-664` (`assign_agenda_item_ids`), `chunk.schema.json:47-50`. LLM: `data_lake/chunker.py:104-120` (detector), `chunker.py:165-182` (propagation), `chunker.py:161` (field) | Both chunkers attach `agenda_item_id` to each chunk. Cascade gates it on `AGENDA_DETECTION_ENABLED=true`; when off the field is absent. LLM chunker always runs the propagator; it returns `None` when no marker is detected. **No extraction artifact item carries `agenda_item_id`** — see `meeting_extraction.schema.json:42-138`; the agenda link lives on the chunk only. |
| Hierarchical overlap refinery for over-long turns | **missing** | `extraction/chunker.py:254-365` (`split_oversized_chunks`); `data_lake/chunker.py:296-327` (`_split_recursive_512`) | Cascade has a **non-recursive single-pass split** at the nearest speaker boundary inside `[1, MAX_CHUNK_CHARS]`; if no boundary exists it cuts at `max_chars` exactly and flags `chunk_split_mid_turn: true` (no nesting, no hierarchy). LLM has a deterministic 512-word window when no speaker/paragraph structure is detected — also flat, not nested. **No Chonkie-style recursion.** |
| Decision-trigger-window preservation (extend chunk on trigger phrase) | **missing** | not found | Grepped `extraction/`, `data_lake/`, `workflows/`, `glossary/` for `decision_window`, `trigger_window`, `extend_chunk`, `boundary_extension`, `G-CHUNK-TRIGGER-WINDOW` — no matches. The regulatory-verb list at `extraction/chunk_classifier.py:90-108` is a **post-extraction reclassifier** of already-formed chunks, not a chunker hook. No code today widens a chunk because a decision verb was seen. |
| Rolling summary of prior chunks injected into prompt | **missing** | not found | Grepped `extraction/`, `workflows/`, `glossary/`, `context/` for `rolling`, `running_summary`, `prior_context`, `summary_so_far`, `G-CHUNK-ROLLING` — no matches. The only between-chunk context the model receives today is the attention-direction block at `glossary/chunk_position.py:32-39`, which is **position-driven, not summary-driven** (fires on middle chunks regardless of prior content). |
| Tests pinning chunking behavior (happy path) | **present** | `tests/extraction/test_chunker.py:22-100` (cascade); `tests/data_lake/test_chunker.py` (LLM, speaker_turn / blank_line / recursive / determinism); `tests/extraction/test_phase_r_chunk_merge.py`; `tests/extraction/test_phase_t_chunk_split.py:46-89` | Multiple suites pin: overlap correctness (cascade), three-tier fallback (LLM), R.0 merge fixed-point, T.4 split-at-boundary, position assignment after merge+split, agenda propagation. Determinism is asserted at the data-lake test. |
| Rejection tests for malformed chunks | **present** | `tests/extraction/test_chunk_metadata_gate.py` (asserts on `validate_chunk_metadata` at `extraction/chunk_metadata_gate.py:121`); `tests/extraction/test_blocked_chunk_artifact.py`; `extraction/chunker.py:584-586, 666-672` (schema validation rejects chunks that don't match `chunk.schema.json`) | Rejection paths: chunk schema violation (`chunk_schema_violation`), missing required metadata (`chunk_metadata_gate`), and per-chunk block reasons recorded on the `blocked_chunk` artifact (`schemas/blocked_chunk.schema.json:27-36`). |
| Chunk-stage observability (logs, debug artifacts) | **present** | `extraction/chunker.py:689-708` (writes `chunk_merge_summary.json`, `chunk_split_summary.json`); `extraction/_chunk_counters.py:60-117` (per-stage tallies → orchestration_result); `evals/source_turn_orphan.py` (orphan / diversity rates surfaced into the extraction artifact at `meeting_extraction.schema.json:241-248`) | Merge and split passes emit their own JSON summary artifacts (originals/produced counts, per-chunk pairings, mid-turn-split flag). Stage counters land on the orchestration_result. No `chunk_preview` or per-chunk byte-range artifact today. |
| Rollback path for changing chunking strategy | **missing** | `docs/runbooks/first_run.md:40-45`; `docs/runbooks/verification-cycle-recovery.md` (no chunking section) | The only runbook mention of chunks is the `chunks_jsonl_not_found` failure code. No runbook documents how to switch chunking strategy, roll back a chunking parameter change, or rebuild dependent artifacts. Env vars exist (`MIN_CHUNK_CHARS`, `MAX_CHUNK_CHARS`, `CHUNK_MERGE_ENABLED`) but operator procedure is not written down. |
| Schema fields that depend on the current chunking shape | **present** | See "Chunk-shape-dependent schema fields" section below | Multiple downstream schemas pin chunk identifiers, chunk-position labels, and chunk-count-derived metrics. A chunking change ripples through them. |
| F1 measurement coupled to chunk strategy (any benchmark fixtures?) | **present** | `tests/fixtures/eval/ground_truth/pair_002_confirmed_character_count.json:16` (`fixture_chunking_strategy: "character_count_fallback"`); `pair_003_confirmed_low_precision.json` (`fixture_chunking_strategy: "speaker_turn"`); pair evaluation slices by strategy in `tests/test_eval_slicing.py:315` | Ground-truth pairs explicitly record which chunker strategy produced them. Pair-level F1 is sliced `by_chunking_strategy`. The fixtures still reference OLD chunk IDs — a chunking change will not auto-rewrite the fixtures. |

## Exact code path: transcript → list-of-chunks

### Cascade pipeline (deterministic, writes `chunks.jsonl`)

1. `cli.py:398` — CLI invokes `Chunker().chunk(source_id, str(store_root))`.
2. `extraction/chunker.py:541-547` — `Chunker.chunk` resolves
   `<lake>/processed/<family>/<source_id>/text_units.jsonl`.
3. `chunker.py:548-578` — loads + sorts text units.
4. `chunker.py:590-604` — strategy selection:
   - `_is_transcript(source_family, source_id)` (`chunker.py:461-464`,
     true for `source_family == "meetings"` or `"transcript"` in
     source_id) → try `_chunk_by_speaker_turns` (`chunker.py:913`).
   - On `no_speaker_turns_detected` or `all_speaker_turns_empty` →
     `_chunk_by_character_count` (`chunker.py:865`).
5. `chunker.py:614-617` — Phase R.0 merge
   (`merge_short_chunks`, `chunker.py:160-251`). Default-on; disable via
   `CHUNK_MERGE_ENABLED=false`. Merges never cross an
   `agenda_item_id` boundary (`chunker.py:142-157`).
6. `chunker.py:625-628` — Phase T.4 split
   (`split_oversized_chunks`, `chunker.py:254-365`). Always-on; splits at
   nearest newline in `[1, MAX_CHUNK_CHARS]`; mid-turn cut flagged.
7. `chunker.py:630-636` — re-merge after split (fail-open).
8. `chunker.py:646` — Phase W proportional position
   (`assign_chunk_positions` at `glossary/chunk_position.py:77`).
9. `chunker.py:660-664` — Phase X2.1 agenda assignment (only if
   `AGENDA_DETECTION_ENABLED=true`).
10. `chunker.py:666-672` — every chunk validated against
    `contracts/schemas/chunk.schema.json`.
11. `chunker.py:676-681` — write `chunks.jsonl` (canonical JSON, sorted
    fields, trailing newline).
12. `chunker.py:689-708` — write `chunk_merge_summary.json` and
    `chunk_split_summary.json` for observability.

### Live-LLM extraction (in-memory, fed straight to the model)

1. `workflows/meeting_minutes_llm.py:39` imports `chunk_transcript`
   from `data_lake/chunker.py`.
2. `data_lake/chunker.py:329-407` — `chunk_transcript(transcript_text)`:
   - `_split_on_speaker_labels` (`chunker.py:185-293`,
     `speaker_turn_v1`). On no match →
   - `_split_on_blank_lines` (`blank_line_v1`). On no match →
   - `_split_recursive_512` (`recursive_512`, 512-word windows).
3. `data_lake/chunker.py:395-406` — health signal:
   `speaker_null_rate > 0.5` → `warn`; `== 1.0` → `block`.
4. `meeting_minutes_llm.py:418` — `_CHUNKS_PER_BATCH = 25`. Ordered
   chunks are sliced into contiguous batches of ≤25 before the model
   call (workaround for 16k `max_tokens` truncation at full-transcript
   scale per the in-file comment block, `meeting_minutes_llm.py:402-417`).
5. `meeting_minutes_llm.py:234-253` — per-batch rendering: each chunk
   becomes a `[t0042] SPEAKER (lines start-end)` block in the prompt.
6. The model returns extracted items keyed on `source_turns:
   [t0042, ...]`; per-batch payloads are aggregated then evaluated.

## Code that assumes a specific chunk size or fixed-size shape

- `extraction/chunker.py:53-54` — `CHUNK_SIZE = 8` and `OVERLAP = 1`
  are module-level constants used only by `_chunk_by_character_count`
  (`chunker.py:865-911`). Changing them would re-shape every chunk in
  the fallback path.
- `agenda/__init__.py` — `MIN_CHUNKS_PER_AGENDA = 5` (referenced from
  `tests/test_eval_slicing.py:110`): agenda slices with fewer than 5
  chunks are rejected. A chunking change that yields fewer per-agenda
  chunks invalidates the slice.
- `workflows/meeting_minutes_llm.py:418` — `_CHUNKS_PER_BATCH = 25`.
  Pinned by the empirical 16k-token output ceiling at 34 chunks (see
  comment block at `meeting_minutes_llm.py:402-417`).
- `glossary/chunk_position.py:51-93` — position is **proportional**
  (33/67 thresholds). Position labels change when chunk count changes.
  The chunker is wired to recompute positions AFTER merge/split
  (`chunker.py:638-646`); any future caller that reorders this is a
  regression.

## Tests that would break if chunk boundaries moved

- `tests/extraction/test_chunker.py:38-51` — pins the cascade overlap
  invariant: `prev_chunk.unit_ids[-1] == curr_chunk.unit_ids[0] ==
  curr_chunk.overlap_unit_id`.
- `tests/extraction/test_phase_r_chunk_merge.py` — pins merge
  behaviour and the agenda-boundary no-merge rule.
- `tests/extraction/test_phase_t_chunk_split.py:46-89` — pins
  boundary-aware split and the `chunk_split_mid_turn` flag.
- `tests/data_lake/test_chunker.py` — pins LLM chunker determinism
  (`turn_id` `t0000`-format stability, three-tier fallback ordering).
- `tests/test_eval_slicing.py:315` — slices F1 metrics by
  `chunking_strategy`; a third strategy would need a slice update.
- `tests/extraction/test_chunk_metadata_gate.py` — pins the metadata
  contract (`chunk_id`/`turn_id`, `speaker`, `agenda_item_id`).
- `tests/fixtures/eval/ground_truth/pair_*.json` — every pair pins
  `fixture_chunking_strategy` AND specific `source_turn_ids`. A
  chunking shape change does not auto-rewrite these.

## Chunk-shape-dependent schema fields

- `contracts/schemas/chunk.schema.json:21,24,36,47,51` — `chunk_id`,
  `chunk_index`, `overlap_unit_id`, `agenda_item_id`, `chunk_position`.
- `contracts/schemas/extraction/meeting_extraction.schema.json:64,
  95, 124` — `source_turn_ids` (required, `minItems: 1`) on every
  decision / claim / action_item.
- `contracts/schemas/story_candidate.schema.json:39, 40-44` —
  `chunk_id` (required UUID) plus `source_turn_ids`.
- `src/spectrum_systems_core/schemas/chunk_classifications.schema.json:
  41-46` — `chunk_id` required per classification row.
- `src/spectrum_systems_core/schemas/blocked_chunk.schema.json:12, 27`
  — `chunk_id` required.
- `contracts/schemas/eval/eval_summary.schema.json:117-121` — pair
  rows pin `agenda_item_id` (nullable).
- `meeting_extraction.schema.json:140` — `total_chunks_classified`
  counter (depends on final chunk count, not boundary positions).

## Data-lake artifacts that pin `chunk_index` or `chunk_id` across runs

- `chunks.jsonl` (per source) — canonical chunk list with stable
  `chunk_id` UUIDs and contiguous 0-based `chunk_index`. Used by every
  downstream cascade stage. **Replacement = full re-extraction of all
  dependent artifacts.**
- `chunk_merge_summary.json`, `chunk_split_summary.json` — preserve
  the merge/split pairings keyed on the pre-merge / pre-split
  `chunk_id`s. Survive runs as forensic record.
- `meeting_extraction` artifacts under
  `processed/meetings/<id>/meeting_extraction__*.json` — their
  `source_turn_ids` strings are interpreted against the **live**
  chunk list at evaluation time (cascade artifact reads `chunk_id`;
  LLM artifact reads `turn_id`). If chunks change, validation
  (`source_turn_validation`) flips from `verified` → `invalid` and
  the items will be rejected.
- `story_candidate` artifacts — each pins a single `chunk_id` (UUID).
  Rewriting chunks orphans every story candidate.
- Ground-truth pair fixtures (`tests/fixtures/eval/ground_truth/`) —
  pin `fixture_chunking_strategy` AND specific `source_turn_ids`.
  Baseline regeneration is required if the strategy or the chunk
  ID shape moves.

## CLI / env-var surface for chunking

CLI flags (`src/spectrum_systems_core/cli.py`):

- `--max-chunks` (lines 5135-5143 and 5178-5188) — DEBUG ONLY:
  truncate to first N chunks.
- `--debug-chunks` (line 5189) — DEBUG ONLY: print per-chunk eval
  decomposition.
- `--single-chunk` / `--print-context` (lines 5241-5267) — DEBUG
  ONLY: extract on the single largest speaker turn.

**No CLI flag** controls chunk size, overlap, strategy selection,
merge enabling, or split bounds. Operator tuning is env-var only:

- `MIN_CHUNK_CHARS` (`extraction/chunker.py:74`).
- `MAX_CHUNK_CHARS` (`chunker.py:75`).
- `CHUNK_MERGE_ENABLED` (`chunker.py:76`).
- `AGENDA_DETECTION_ENABLED` (`chunker.py:660`, gates Phase X2.1).
- `STRICT_CHUNK_METADATA` (`chunk_metadata_gate.py:22`, promotes
  metadata-gate warnings to halts).
- `TRACE_CAPTURE_ENABLED` (constitution-bound; gates per-chunk
  trace rows in `experience_history.jsonl`).

## Workflows that exercise chunking

- `.github/workflows/find-failing-chunk-range.yml` — binary-search
  harness over `--max-chunks`; observe-only, no data-lake writes.
- `.github/workflows/debug-single-transcript.yml` — runs
  `meeting-minutes-llm --max-chunks N --debug-chunks true`.
- `.github/workflows/debug-llm-extraction.yml` — single-source
  live-LLM repro path.
- `.github/workflows/smoke-test.yml`, `smoke-e2e.yml` — full
  pipeline including chunking validation; bounded with `--max-chunks`
  to keep API spend deterministic.

## Implications — what changes if we move to speaker-turn-aware chunking

The repo **already does speaker-turn-aware chunking by default** for
transcripts in both chunkers. Phase 2.B should not be framed as
"introduce speaker-turn chunking" — that ships at HEAD. The honest
framing is one of three:

1. **Tighten the speaker regex / merge-split policy** so today's
   speaker-turn path catches more transcripts and produces
   better-shaped chunks. Likely-cheap, contained inside the existing
   chunker contracts.
2. **Add genuinely-new chunking primitives** that today's inventory
   reports as missing: decision-trigger-window preservation, rolling
   summary injection, hierarchical (Chonkie-style) recursion for
   over-long turns. Each is its own roadmap item with its own gates.
3. **Unify the two chunkers** so the cascade and the live-LLM path
   key on the same chunk identifier shape. Today they don't:
   `chunk_id` (UUID) vs `turn_id` (`t0000`-format). The metadata gate
   already tolerates the alias, but downstream artifacts do not
   round-trip across the two namespaces.

What each of those would touch (the surface area, not the design):

- **Chunk envelope schema** (`contracts/schemas/chunk.schema.json`):
  new optional fields (e.g. `decision_window_extended: bool`,
  `prior_summary_used: bool`) require additive schema versioning.
  Existing required fields stay.
- **Extraction artifact schema**
  (`contracts/schemas/extraction/meeting_extraction.schema.json`):
  `source_turn_ids` is the cross-cutting integrity link. Changing
  chunk identifier shape forces a `schema_version` bump on every
  artifact type that carries `source_turn_ids` (meeting_extraction,
  story_candidate, chunk_classifications).
- **Tests**: every test under `tests/extraction/`,
  `tests/data_lake/`, and the `tests/fixtures/eval/ground_truth/`
  pairs will need a strategy update. The R.0 merge and T.4 split
  contract tests are the most rigid — they pin exact pair counts on
  exact inputs.
- **Ground-truth baselines**:
  `tests/fixtures/eval/ground_truth/pair_*.json` carry
  `fixture_chunking_strategy` and explicit `source_turn_ids`. Each
  strategy change requires a baseline regeneration step (and a
  matching test_create_opus_reference_baselines run).
- **Data-lake artifacts already on disk**: meeting_extraction
  artifacts whose `source_turn_ids` were validated against an old
  chunk list will flip to `source_turn_validation: invalid` after a
  re-chunk. The system already has the field (`enum: verified |
  invalid | missing`) and gates promotion on it — so the failure mode
  is fail-closed, not silent. But the re-promotion cost is per-source.
- **Observability**: `chunk_merge_summary.json` and
  `chunk_split_summary.json` are non-fatal artifacts today. New
  chunking primitives should produce their own summary artifacts
  rather than overload existing ones.
- **Runbooks**: there is no rollback runbook for chunking parameter
  changes (`docs/runbooks/` mentions only `chunks_jsonl_not_found`).
  Any roadmap item that adds tunables needs a runbook entry covering
  rollback and re-extraction cost.

The substring/grounding gate from the PR #212 family is the single
strongest existing guard. As long as a new chunking primitive keeps
the model's quoted span verbatim inside the chunk text shown to it,
that gate will continue to fail closed on hallucinated identifiers
— so it is the right place to anchor the safety story for any new
chunking work, not somewhere new.
