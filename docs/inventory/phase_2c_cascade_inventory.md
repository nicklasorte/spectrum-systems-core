# Phase 2.C Cascade Hookup — Step 1 Source Inventory

Status: read-only inventory. No code, schemas, prompts, or workflows are
changed by this document. Every claim cites a file and a line; "not
found" means the search returned nothing — it does NOT mean "present
under another name".

The headline result is short: **the cascade is built.** Phase 6 / PR #203
landed a complete Stage 2 filter — module, prompt, response schema,
filtered-artifact schema, CLI flags, comparison-engine integration,
cost estimator, rollback entry, and a dedicated `tests/cascade/` suite.
Phase 2.C should be framed as a hookup / sequencing question against an
existing implementation, not a build.

Two caveats sit on top of that headline:

1. The cascade was built **before** Phase 1 verbatim grounding shipped
   in PR #205-era code, but `executor.py:381` reads `source_quote` off
   items whose `grounding_mode == "verbatim"` — so the cascade DOES
   consume the schema-1.4.0 grounding field. The "cascade predates
   grounding so it can't verify against verbatim spans" risk in the
   Phase 2.C prompt is already mitigated in code.
2. The cascade has never been observed running end-to-end against a
   real transcript from inside this repo. The data lake is the external
   `nicklasorte/data-lake` repository (`CLAUDE.md` §"Data-lake
   separation"), so cascade-produced artifacts on disk are out of
   inspection range here. The integration test
   `tests/integration/test_cascade_source_artifact_invariant.py`
   exercises the full path end-to-end on a fixture, which is the closest
   evidence the repo offers.

## Inventory table

| Capability | Status | Evidence (file:line) | Notes |
| --- | --- | --- | --- |
| Cascade filter module exists | **present** | `src/spectrum_systems_core/cascade/__init__.py:1-62`; `src/spectrum_systems_core/cascade/executor.py:1-941` | Import path: `spectrum_systems_core.cascade`. Public surface: `run_cascade_filter`, `write_filtered_artifact`, `write_cascade_filter_log`, `DEFAULT_CASCADE_FILTER_MODEL`, `CascadeError`, `CascadeFilterResult`. |
| Cascade filter prompt (separate from production_haiku) | **present** | `src/spectrum_systems_core/workflows/prompts/cascade_filter_sonnet.md` (40 lines) | Template variables `<chunk_text>` and `<items_json_without_reason_field>`. Keep/drop instruction lives on lines 5-15. Loaded via `CASCADE_FILTER_PROMPT_PATH` at `cascade/executor.py:95-100`; sha256 stamped on every filtered artifact (`cascade_filter_prompt_content_hash`, executor.py:228-234). |
| Cascade filter model | **present** | `cascade/executor.py:70` `DEFAULT_CASCADE_FILTER_MODEL: str = "claude-sonnet-4-6"`; `cost/estimator.py:206` (mirrored); `cli.py:3492` (constructed in dispatch) | Sonnet 4.6. Hardcoded constant — no env-var override, no CLI flag to switch the filter model. |
| Cascade input contract — does the filter receive `source_quote`? | **present** | `cascade/executor.py:379-392` | YES. When `grounding_mode == "verbatim"`, the executor reads `item_copy.get("source_quote")`, locates it inside the chunk text via `chunk_text.find(quote)`, and emits a ±100-char (`_VERBATIM_CONTEXT_PAD`, executor.py:84) padded `_chunk_context` so the filter sees the local context. The `reason` field is stripped (executor.py:377) so the filter judges independently of Haiku's reasoning. |
| Cascade output contract — keep/drop/modify with reason | **partial** | `src/spectrum_systems_core/cascade/cascade_filter_response.schema.json:6-29`; `cascade/executor.py:290-332` (`_validate_filter_response`); `cascade/executor.py:163-169` (`FilterDecision` dataclass) | Structured: top-level JSON array of `{item_idx: int, decision: enum["keep","drop"], reason: minLength 1 string}`. **"modify" is NOT a valid decision** — the cascade only includes or excludes items verbatim. Spec at `cascade/executor.py:5-7`: "the cascade NEVER invents or mutates items". |
| CLI flag to enable cascade | **present** | `cli.py:5388-5410` (mutually-exclusive argparse group); `cli.py:2799` (function param `enable_cascade_filter`); `cli.py:3414-3426` (dispatch); `cli.py:3432` (`_dispatch_cascade_filter`) | `--enable-cascade-filter` / `--disable-cascade-filter` mutually exclusive; default OFF. Env vars `ENABLE_CASCADE_FILTER` / `DISABLE_CASCADE_FILTER` have **no effect** — asserted at `tests/cascade/test_cli_flag.py:74-86`. Threshold gate `--confirm-cost` required when items > `cascade_confirmation_item_threshold` (50 default). |
| Cascade-produced artifact type | **present** | `cascade/executor.py:63-64` `FILTERED_ARTIFACT_TYPE = "meeting_minutes_filtered"`, `FILTERED_SCHEMA_VERSION = "1.0.0"`; `src/spectrum_systems_core/schemas/meeting_minutes_filtered.schema.json:1-129` | New artifact type, **not a sub-record on `meeting_minutes`**. Envelope carries `source_artifact_path`, `filter_metadata{...}`, `filtered_items{23 array keys}`, optional `extraction_config`. `additionalProperties: false` enforced everywhere. |
| Cascade-produced artifact promoted to data-lake | **partial** | `cascade/executor.py:887-941` `write_filtered_artifact` (writes to source artifact's directory); workflow `.github/workflows/debug-llm-extraction.yml:243-265` (logs path and `git add`s it via the data-lake push action) | Written to `<lake>/processed/meetings/<source_id>/meeting_minutes_filtered__<ts>.json`, **side-by-side with `meeting_minutes__*.json`**. The filtered artifact is NOT a promoted product artifact: it does not pass through `promotion/promoter.py`, does not flow into `indexes/meetings/artifact_index.jsonl`, and the data-lake contract (`docs/contracts/data_lake_contract.md` §6.1) restricts that index to promoted artifacts. Diagnostic log lives at `<lake>/processed/meetings/<source_id>/diagnostics/cascade_filter_log__<ts>.json` (30-day TTL per `CASCADE_FILTER_LOG_TTL_DAYS`, executor.py:68). |
| Cascade comparison support (cascaded vs Opus) | **present** | `scripts/compare_opus_haiku.py:1700` (`use_cascade_output: bool = False`); `compare_opus_haiku.py:1721-1729` (dispatch); `compare_opus_haiku.py:644-767` (`find_cascade_filtered_artifact`); `tests/comparison/test_use_cascade_output.py` | `--use-cascade-output` flag swaps the Haiku artifact for the cascade-filtered one; fail-closed `cascade_artifact_not_found` halt if absent (compare_opus_haiku.py:678-681). Workflow input: `.github/workflows/run-comparison.yml:27, 40, 91`. The cascade-vs-Opus path is NOT a passthrough — full substring / F1 / precision / recall comparison runs, with `prompt_variant=production_haiku_with_cascade_filter` stamped on the result. |
| Cascade `chunking_strategy_version` handling | **partial** | `cascade/executor.py` — grepped, the cascade never reads or writes `chunking_strategy_version`; `compare_opus_haiku.py:1748-1762` (gate runs BEFORE cascade vs raw selection) | The cascade output **inherits** the field by carrying through `extraction_config` from the source artifact (executor.py:3514-3520 in CLI dispatch). The cascade itself is chunking-strategy-agnostic: it groups items by their source chunk (executor.py:474 `_assign_items_to_chunks`) via substring match / turn-id lookup, with no assumption about overlap. Implication for Phase 2.B's `CHUNK_OVERLAP_TURNS`: cascade still works at overlap=2; the items it filters are whatever the Haiku artifact produced under that overlap. |
| Cascade `schema_version` | **present** | `meeting_minutes_filtered.schema.json:19-22` `enum: ["1.0.0"]`; `cascade/executor.py:64` | Cascade output uses its own schema-`1.0.0`, **separate** from `meeting_minutes` 1.4.0. They are independent versioned contracts. The cascade output's `filtered_items` mirrors the 23 extraction arrays on `meeting_minutes` 1.4.0; tests assert key-set equality at `tests/cascade/test_schema_round_trip.py` (per cross-reference at executor.py:107). |
| Cascade gates (rejection tests) | **present** | `cascade/executor.py:290-332` (`_validate_filter_response`); `tests/cascade/test_schemas.py`; `tests/cascade/test_executor.py` (12K-line suite); `tests/cascade/test_writers.py`; `tests/cascade/test_backwards_compat.py` | Three rejection paths: (a) JSON-Schema violation against `cascade_filter_response.schema.json`; (b) duplicate `item_idx` (executor.py:306-314); (c) item-index set mismatch (executor.py:320-330). All three trigger a conservative pass-through: every item in the chunk is KEPT, marked `invalid_response_passthrough` (executor.py:74), and counted under `chunks_with_invalid_filter_response`. The filtered artifact envelope itself is validated by `validation.validate_artifact` (executor.py:25-26, 56) so unknown keys fail closed via `additionalProperties: false`. |
| Cascade observability — kept/dropped counts, reasons | **present** | `meeting_minutes_filtered.schema.json:28-91` (`filter_metadata` block); `src/spectrum_systems_core/schemas/cascade_filter_log.schema.json` (per-decision detail); `cli.py:3550-3560` (stdout summary) | Counters on every cascade run: `items_kept_count`, `items_dropped_count`, `chunks_evaluated`, `chunks_with_invalid_filter_response`, `truncation_count`, `filter_started_at`, `filter_completed_at`. Per-item reasons in the `cascade_filter_log` artifact (`{chunk_index, item_idx, extraction_type, decision, reason}` rows). CLI prints a one-line `CASCADE OK items_in=... items_kept=... items_dropped=...` summary on success. |
| Cascade rollback runbook | **missing** | `docs/runbooks/` contents: `first_run.md`, `phase_2b_chunking_rollback.md`, `verification-cycle-recovery.md` — no `cascade_rollback.md` | No dedicated runbook file. Rollback semantics live only inside `docs/architecture/rollback_contracts.md` (see next row). If Phase 2.C extends or re-sequences the cascade, the CLAUDE.md / repo pattern (Phase 2.B shipped a `phase_2b_chunking_rollback.md`) implies a `phase_2c_*.md` runbook should be added. |
| Cascade rollback contracts entry | **present** | `docs/architecture/rollback_contracts.md:1052-1116` ("Phase 6 — Stage 2 cascade filter (Haiku extract → Sonnet keep/drop) (PR #203)") | Documents what PR #203 added (flags, artifact types, schemas, CLI, cost-estimator extensions, compare-engine flag, workflow input), the 5-step revert plan, and the verification command `pytest tests/cascade/ tests/cost/test_cascade_cost.py tests/comparison/test_use_cascade_output.py`. Records "Data migration required: None" because the cascade is append-only. Cross-PR dependencies listed: #192 (grounding), #193 (eval alignment), #196 (variance budget). |
| Cascade cost tracking (tokens / requests) | **present** | `cost/estimator.py:206` (filter model); `cost/estimator.py:214-230` (`load_cascade_confirmation_item_threshold`); `cost/estimator.py:258-327` (`estimate_cascade_cost`); `data/cost_constants.json:19` (`cascade_confirmation_item_threshold: 50`, bounded [10,500]) | Per-run: `total_filter_tokens` (sum of input+output from api_client), `total_filter_cost_usd` (best-effort `Decimal` from `estimate_extraction_cost`, fallback `Decimal("0")` if estimator unavailable) — both on `CascadeFilterResult` (executor.py:201) and surfaced in the log. Pre-run: `estimate_cascade_cost(haiku_items_count, avg chunk bytes, per-chunk output tokens, filter_model)`. Threshold gate at CLI: above 50 items, `--confirm-cost` is required or the run halts with `cascade_cost_confirmation_required` (cli.py:3474-3484). |
| Cascade smoke test (3-item: keep, modify, drop) | **missing** | `tests/cascade/_helpers.py` (`always_keep_rule`, `always_drop_rule`, `drop_indexes_rule([...])`, `DeterministicFilterClient`); `tests/cascade/test_executor.py` (parameterized) | No 3-item file named `test_smoke_keep_drop_modify` or similar exists. The cascade's keep/drop semantics ARE covered via parameterized rules (`always_keep_rule`, `always_drop_rule`, `drop_indexes_rule([1, 3])`). "Modify" cannot be smoke-tested because modify is not a valid decision in the cascade response schema (see Cascade output contract row). |
| Cascade ever run end-to-end on a real transcript | **unknown** | `tests/integration/test_cascade_source_artifact_invariant.py:77-203` (Dec 18 fixture, full CLI dispatch); `.github/workflows/debug-llm-extraction.yml:106-265` (operator-triggered cascade path with data-lake push); `.gitignore` excludes `data-lake/` per CLAUDE.md §"Data-lake separation" | The integration test runs the full path on the `dec18_transcript.txt` fixture, which proves the wiring works against a synthetic but real-shaped input. **Real-transcript artifacts on disk live in `nicklasorte/data-lake`** and are not visible from this repo. To verify the cascade has run on a real transcript, look in the external data-lake for `meeting_minutes_filtered__*.json` files under `processed/meetings/<source_id>/`. |

## Exact code path: `meeting_minutes` → cascade → filtered output

1. `cli.py:2799` — `meeting_minutes_llm(... enable_cascade_filter: bool = False, cascade_api_client=None, ...)` — CLI handler.
2. After the canonical `meeting_minutes` artifact is written, `cli.py:3414-3426` — `if enable_cascade_filter: _dispatch_cascade_filter(...)`. The comment block at lines 3405-3413 makes the contract explicit: cascade reads the freshly-written artifact off disk so its input is byte-identical to what was promoted.
3. `cli.py:3432` `_dispatch_cascade_filter(...)`:
   - 3450-3454: imports `run_cascade_filter`, `write_filtered_artifact`, `write_cascade_filter_log` from `cascade/`.
   - 3459-3484: enforces the `cascade_confirmation_item_threshold` (default 50; `--confirm-cost` required above it).
   - 3486-3492: constructs an `AnthropicJSONClient` pinned to `DEFAULT_CASCADE_FILTER_MODEL = "claude-sonnet-4-6"` if no api_client was supplied.
   - 3510-3520: copies `extraction_config` from the source artifact and overrides `prompt_variant = "production_haiku_with_cascade_filter"`.
   - 3522: `result = run_cascade_filter(source_artifact=..., chunks=..., api_client=...)`.
4. Inside the executor:
   - `cascade/executor.py:474` `_assign_items_to_chunks` — group source items by chunk (substring match for verbatim items, turn-id lookup for turn-aggregate, chunk-0 fallback for ungrounded).
   - `cascade/executor.py:370-415` — per-item context preparation: strip `reason`, splice ±100 char `_chunk_context` around `source_quote` for verbatim items, render `_turn_text` for turn-aggregate items (10-turn budget, overflow truncated).
   - `cascade/executor.py:418-471` `_render_filter_prompt` — substitute `<chunk_text>` and `<items_json_without_reason_field>` placeholders in the prompt template.
   - One filter API call per non-empty chunk; response validated by `_validate_filter_response` (executor.py:290-332). On validation failure, every item is kept and counted under `chunks_with_invalid_filter_response`.
5. `cli.py:3535-3548` — write artifacts:
   - `write_filtered_artifact(...)` → `<lake>/processed/meetings/<source_id>/meeting_minutes_filtered__<ts>.json`.
   - `write_cascade_filter_log(...)` → `<lake>/processed/meetings/<source_id>/diagnostics/cascade_filter_log__<ts>.json`.

## Answers to the four "additional reads" questions

**A. Exact code path** — see the 5-step trace above.

**B. Does the cascade assume `overlap=0`?** — No. Grepped `cascade/executor.py` for `overlap`: zero hits. The chunk-assignment logic at `cascade/executor.py:474` `_assign_items_to_chunks` finds the FIRST chunk whose text contains each item's `source_quote` (or matches its `source_turn_ids`); no boundary arithmetic, no chunk-shape assumption. At `CHUNK_OVERLAP_TURNS=2`, an item whose verbatim quote sits in the overlap region between chunks A and B will be assigned to whichever chunk reports a match first (the substring search at executor.py:385 returns the first hit, which is the earlier chunk by iteration order). The cascade still filters that item exactly once; no double-counting risk. The Phase 2.C prompt's concern that cascade might mis-key chunks at overlap=2 does not surface in the current code.

**C. Does the cascade receive items WITH `source_quote` from schema 1.4.0?** — Yes. `cascade/executor.py:381` reads `item_copy.get("source_quote")` directly off each item whose `grounding_mode == "verbatim"`, then surfaces ±100 char of surrounding chunk text as `_chunk_context` (executor.py:385-392). The cascade does not predate verbatim grounding in any operational sense — it consumes the field whose presence is enforced by `promotion/gate.py` on 1.4.0 artifacts. The cascade does NOT re-verify the byte-match of the quote against the transcript; that is the promotion gate's job, and the cascade is downstream of promotion (cli.py:3405-3413: "AFTER it has been written").

**D. Does the cascade output ever get compared against Opus?** — Yes, via `--use-cascade-output` on `scripts/compare_opus_haiku.py`. The cascade output is loaded by `find_cascade_filtered_artifact` (compare_opus_haiku.py:644-767), wrapped in a synthetic `meeting_minutes`-shaped envelope whose `payload` is the cascade's `filtered_items` plus provenance, and then driven through the same comparison pipeline as the raw Haiku artifact — substring grounding, F1, precision, recall. Result is stamped `prompt_variant="production_haiku_with_cascade_filter"` so the prompt-drift gate at compare_opus_haiku.py:1823-1830 is intentionally skipped for cascade artifacts (their schema_version mirrors the source artifact's, and would otherwise fire spuriously). Not a passthrough; a full comparison.

## Key constants and paths

| Constant | Value | Location |
| --- | --- | --- |
| `FILTERED_ARTIFACT_TYPE` | `"meeting_minutes_filtered"` | `cascade/executor.py:63` |
| `FILTERED_SCHEMA_VERSION` | `"1.0.0"` | `cascade/executor.py:64` |
| `CASCADE_FILTER_LOG_ARTIFACT_TYPE` | `"cascade_filter_log"` | `cascade/executor.py:66` |
| `CASCADE_FILTER_LOG_SCHEMA_VERSION` | `"1.0.0"` | `cascade/executor.py:67` |
| `CASCADE_FILTER_LOG_TTL_DAYS` | `30` | `cascade/executor.py:68` |
| `DEFAULT_CASCADE_FILTER_MODEL` | `"claude-sonnet-4-6"` | `cascade/executor.py:70` |
| `FILTER_RESPONSE_INVALID_PASSTHROUGH` | `"invalid_response_passthrough"` | `cascade/executor.py:74` |
| `_TURN_AGGREGATE_BUDGET` | `10` (turn-aggregate items truncated above) | `cascade/executor.py:79` |
| `_VERBATIM_CONTEXT_PAD` | `100` (chars before/after `source_quote`) | `cascade/executor.py:84` |
| `DEFAULT_PER_CHUNK_OUTPUT_TOKENS` | `800` | `cascade/executor.py:88` |
| `CASCADE_FILTER_PROMPT_PATH` | `workflows/prompts/cascade_filter_sonnet.md` | `cascade/executor.py:95-100` |
| `cascade_confirmation_item_threshold` | `50` (bounded [10, 500]) | `data/cost_constants.json:19`; loader at `cost/estimator.py:214-230` |
| `prompt_variant` stamp on cascade outputs | `"production_haiku_with_cascade_filter"` | `cli.py:3519` |

## Implications — what changes if we run cascade today

The cascade is wired end-to-end. The honest framing for Phase 2.C is
not "build the cascade" but "decide when, and behind which sequencing
gates, to flip `--enable-cascade-filter` on the production extraction
path." Three things follow.

### What already works

1. **Cascade consumes the verbatim grounding field.** The Phase 2.C
   prompt's hypothesis that the cascade might predate verbatim grounding
   and lack the field to verify against is **not borne out** by the
   code. `cascade/executor.py:381` reads `source_quote` and surfaces
   ±100 char of surrounding chunk text as the local context.
2. **Cascade is overlap-agnostic.** `CHUNK_OVERLAP_TURNS=2` (Phase 2.B)
   does not require any cascade code change. The chunk-assignment
   logic finds the first chunk whose text contains each item's quote;
   no overlap arithmetic, no boundary assumption.
3. **Cascade-vs-Opus comparison is wired.** `compare_opus_haiku.py`'s
   `--use-cascade-output` flag and `.github/workflows/run-comparison.yml`'s
   `use_cascade_output` input give Phase 2.C a measurement path the day
   it lands: run the cascade on an overlap=2 Haiku artifact, then
   `--use-cascade-output` to compare F1 against the existing Opus
   reference.
4. **Cost is gated.** `cascade_confirmation_item_threshold=50` plus
   `--confirm-cost` plus the per-run `total_filter_tokens` /
   `total_filter_cost_usd` counters means cascade cost stays bounded
   and observable.

### What is genuinely missing

1. **No `phase_2c_cascade_rollback.md` runbook.** The Phase 2.B pattern
   established a per-phase rollback runbook (`phase_2b_chunking_rollback.md`).
   Phase 2.C should add `docs/runbooks/phase_2c_cascade_rollback.md`
   covering the operator procedure for: turning the cascade off mid-run,
   reconciling raw vs filtered artifacts in the data lake, and which
   evals to re-run when the filter is rolled back.
2. **No 3-item keep/drop smoke test.** The cascade test suite parameterizes
   keep/drop via `tests/cascade/_helpers.py` rules (`always_keep_rule`,
   `always_drop_rule`, `drop_indexes_rule`). Phase 2.C may want a
   transparent 3-item fixture that exercises one verbatim-keep, one
   verbatim-drop, and one turn-aggregate item to stamp the canonical
   keep/drop semantics in one place. "Modify" cannot be added — the
   cascade response schema only accepts `keep` and `drop`.
3. **No evidence of an end-to-end real-transcript cascade run.** The
   integration test runs against a fixture (`dec18_transcript.txt`),
   not a corpus transcript. Before Phase 2.C flips cascade on by
   default, the cascade should be run end-to-end on at least one real
   transcript (via the existing `.github/workflows/debug-llm-extraction.yml`
   `cascade_filter: true` path) and the resulting `meeting_minutes_filtered__*.json`
   inspected in the data lake. The integration test cannot substitute
   for that because it runs in-repo against a fixture.
4. **`prompt_variant="production_haiku_with_cascade_filter"` skips the
   prompt-drift gate.** `compare_opus_haiku.py:1823-1830` intentionally
   passes `None` for the prompt-drift check when `--use-cascade-output`
   is set. That is defensible (the cascade carries the source's
   provenance) but Phase 2.C should explicitly confirm we have an
   equivalent guard against the cascade prompt itself drifting — the
   `cascade_filter_prompt_content_hash` is stamped on every cascade
   run, but nothing today asserts run-over-run hash equality.

### Sequencing implication

Phase 2.B's `chunking_strategy_version` is stamped on the raw
`meeting_minutes` artifact and on baseline rows. The cascade output
**inherits** that value through `extraction_config` but does not
re-stamp it on the filtered envelope. If Phase 2.C's measurement
session is "cascade vs no-cascade at overlap=2," the comparison engine
will see the same `chunking_strategy_version` on both sides (because
the cascade copies it from the source) and the
`_chunking_strategy_version_of` / `_baseline_chunking_strategy_version`
gate at `compare_opus_haiku.py:1748-1762` will not fire. That is the
correct behavior — but it means the cascade run itself does not
produce a separable "cascade-at-overlap-2" version label. Phase 2.C
should decide whether to introduce one or to rely on the source
artifact's version label as the single identifier.

### Bottom line

Phase 2.C is a hookup task, not a build task. The artifacts to produce
are: a one-page rollback runbook, an explicit corpus run to land the
first real cascade artifact in the data lake, and a decision on whether
to introduce a cascade-specific version label. None of those require
touching `src/spectrum_systems_core/cascade/`.
