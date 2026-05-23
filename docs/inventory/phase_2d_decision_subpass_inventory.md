# Phase 2.D Decision Sub-Pass — Step 1 Source Inventory

Status: read-only inventory. No code, schemas, prompts, or workflows are
changed by this document. Every claim cites a file and a line; "missing"
means the search returned nothing — it does NOT mean "present under
another name."

The headline result is short: **no decision sub-pass exists.** Decisions
are extracted today by `DecisionExtractor` as part of the main extraction
pipeline. The Fernández taxonomy is present in the codebase only as
**prompt guidance text** inside `meeting_minutes_llm.md`, not as a
per-item label on any schema, not as a CLI flag, not as a separate
artifact type, not as an eval surface, and not as a tested rejection
path. Phase 2.D should be framed as a build (with measurement gate)
against an empty slot — not a hookup against existing wiring (as Phase
2.C was for the cascade).

Three caveats sit on top of that headline:

1. **The task prompt's claimed taxonomy and the prompt's actual
   taxonomy diverge on one label.** The Phase 2.D task brief lists the
   4 Fernández classes as `issue / proposal / resolution / agreement`.
   The taxonomy currently embedded in `meeting_minutes_llm.md:494-558`
   lists them as `issue / proposal / resolution / scope`, where
   sub-type 3 is titled "Resolution / Agreement" (one label) and
   sub-type 4 is "Scope / Boundary ruling." Any Phase 2.D implementation
   must pin the canonical 4-class enum explicitly and reconcile this
   drift before code is written; otherwise the rejection gate will
   block on the wrong label set.
2. **`production_haiku` is a `prompt_variant` LABEL, not a separate
   prompt file.** The actual extraction prompt is
   `src/spectrum_systems_core/workflows/prompts/meeting_minutes_llm.md`.
   The string `"production_haiku"` appears only as the
   `extraction_config.prompt_variant` value stamped on artifacts
   (`meeting_minutes.schema.json:1611`, `comparison_result.schema.json:48`).
   The task brief's question "what does the `production_haiku` prompt
   say about decisions" maps to the contents of `meeting_minutes_llm.md`.
3. **Data-lake artifacts are not in this repo.** CLAUDE.md §"Data-lake
   separation" pins all pipeline artifacts to the external
   `nicklasorte/data-lake` repository. `processed/meetings/` in this
   checkout contains only `.gitkeep`. Any question about whether a
   decision sub-pass artifact has ever been written, or how many
   `decisions`-typed items the Dec 18 Haiku run produced, cannot be
   answered from this repo — it must be answered by inspecting the
   external data-lake.

## Inventory table

| Capability | Status | Evidence (file:line) | Notes |
| --- | --- | --- | --- |
| Dedicated decision sub-pass module | **missing** | grep across `src/`, `scripts/`, `tests/`, `.github/`, `docs/` for `decision_subpass`, `decision_classifier`, `decision_taxonomy`, `fernandez`, `issue_proposal_resolution` returns zero hits outside the prompt block and `scripts/verify_trigger_taxonomy.py` | `extraction/decision_extractor.py:75` `DecisionExtractor` exists and extracts decisions, but it is NOT a sub-pass over an existing `meeting_minutes` artifact — it is one of three typed extractors (`DecisionExtractor`, `ClaimExtractor`, `ActionItemExtractor`) that feed `extraction_merger.py:116` → `meeting_extraction` artifact. No module re-classifies an already-extracted decision against the Fernández taxonomy. |
| Fernández taxonomy (issue/proposal/resolution/agreement) as label enum | **partial** (prompt text only, not an enum) | `workflows/prompts/meeting_minutes_llm.md:476-558`; `scripts/verify_trigger_taxonomy.py:31-36` | The four sub-types appear as Markdown `## Sub-type N: …` headers inside the prompt: `(issue, "Issue identification")`, `(proposal, "Proposal / Direction")`, `(resolution, "Resolution / Agreement")`, `(scope, "Scope / Boundary ruling")` (`verify_trigger_taxonomy.py:32-35`). No `enum` declaration in any JSON-Schema file. `verify_trigger_taxonomy.py` only string-asserts that the headers are present in the prompt — it does NOT verify any artifact's items carry these labels. The 4th class in the prompt is `scope`, not `agreement` — see "Caveats" above. |
| Decision-specific prompt or sub-prompt | **partial** (embedded, not separate) | `workflows/prompts/meeting_minutes_llm.md:477-632`; `ls workflows/prompts/` → `cascade_filter_sonnet.md`, `meeting_minutes_llm.md`, `meeting_minutes_opus.md` | The implicit-decision-taxonomy section (lines 477-558), modal verb policy (lines 587-608), hallucination defense (lines 610-616), and domain notes (lines 618-632) all live inside the single `meeting_minutes_llm.md` prompt that drives the full Haiku extraction. There is no separate decision-only prompt file analogous to `cascade_filter_sonnet.md`. The `DecisionExtractor` itself reads its prompt blocks from `extraction/_prompt_blocks.py` (`decision_extractor.py:38-46`) and renders a per-extractor prompt internally — those blocks do not mention Fernández. |
| Per-decision-item label field on schema | **missing** | `schemas/meeting_minutes.schema.json:38-111` (decisions array); `schemas/meeting_extraction.schema.json:42-122` (decisions array) | Neither schema carries a `decision_subtype`, `decision_label`, `decision_kind`, `taxonomy_class`, or `fernandez_class` field on decision items. Both schemas declare `additionalProperties: false`, so the field cannot be added by a producer without a schema bump. The closest existing fields are: `decision_type` (8-value enum: approved/rejected/deferred/noted/considered/action_required/open_question/to_be_determined, `meeting_extraction.schema.json:53-58`) and `decision_outcome` (6-value enum: approval/rejection/deferral/action_required/noted/question, `meeting_extraction.schema.json:78-85`). Neither matches the 4-class Fernández taxonomy — they describe outcome, not discourse function. |
| Routing logic: explicit vs implicit decision | **missing** | `extraction/decision_extractor.py` (full file), `extraction/extraction_merger.py:116` `merge` | The `DecisionExtractor` returns one undifferentiated stream of decision items. The implicit-vs-explicit distinction lives only as prose in the prompt at `meeting_minutes_llm.md:476-616` (modal verb policy + implicit taxonomy). No code path inspects an item and routes it to a "classify as Fernández" branch; no `source_quote` vs `source_turn_ids` discriminator drives a sub-pass; no `grounding_mode` switch fires a classifier. The cascade analog (`cascade/executor.py:379-415`) shows what such routing would look like — that pattern does NOT exist for decisions. |
| Per-call item cap on decision classification | **missing** (the cascade cap exists but is unrelated) | `cascade/executor.py:98` `MAX_ITEMS_PER_FILTER_CALL: int = 30`; `cascade/executor.py:785-795` (sub-batching loop) | The cascade has the 30-item cap because Sonnet truncated mid-JSON when asked to filter 230 items in one call (PR #226 fix). No decision-specific cap exists anywhere — there is no decision sub-pass to need one. If Phase 2.D adds a decision sub-pass that classifies 230 decisions in one call, it WILL hit the same Sonnet truncation ceiling. The cap pattern must be ported (separate constant, separate batching loop, separate truncation counter), not inherited from cascade. |
| Decision sub-pass output artifact_type | **missing** | full inventory of `schemas/*.schema.json`: no `decision_subpass`, `decision_classification`, `decision_taxonomy_assignment`, or similar artifact_type | Today decisions flow into `meeting_extraction` (`meeting_extraction.schema.json:42`) and `meeting_minutes` (`meeting_minutes.schema.json:38`). A Phase 2.D sub-pass must NOT mutate either of those — the append-only / append-with-discriminator pattern (cf. cascade's separate `meeting_minutes_filtered` artifact, `cascade/executor.py:63 FILTERED_ARTIFACT_TYPE = "meeting_minutes_filtered"`) is what to mirror. |
| Decision sub-pass append-only path | **missing** | `processed/meetings/` in this repo is only `.gitkeep`; the canonical lake path would be `<lake>/processed/meetings/<meeting_id>/decision_subpass__<ts>.json` by analogy to `cascade/executor.py:887-941` and `data_lake_contract.md` §6 | No such path exists. The data-lake contract's promotion rule (`data_lake_contract.md:6.1`) requires `status == "promoted"`; the cascade chose to ship its filtered artifact as a non-promoted, side-by-side product artifact. Phase 2.D must decide which side of that rule the decision sub-pass output sits on. |
| Rejection tests for invalid Fernández label | **missing** | no test under `tests/` references `fernandez`, `decision_subtype`, `decision_label`, or the 4-class enum | Cannot exist because the field that the gate would reject does not exist on the schema. The closest analog is `cascade/test_executor.py`'s coverage of the cascade's invalid-decision passthrough (`cascade/executor.py:74 FILTER_RESPONSE_INVALID_PASSTHROUGH = "invalid_response_passthrough"`) — but cascade fails OPEN (keeps all items), which is the OPPOSITE of what a fail-closed taxonomy gate needs. |
| Rejection tests for missing decision-classification | **missing** | same null search across `tests/` | Same as above. A decision item without a `decision_subtype` field today validates cleanly against both schemas (the field is not declared, so it does not exist). A Phase 2.D `required` declaration on `decision_subtype` plus a test that constructs an item without it and asserts BLOCK is the canonical rejection-test shape — it is not present. |
| Happy-path tests for the 4 taxonomy classes | **missing** | `find tests/ -name '*decision*'` returns `test_decision_brief_workflow.py`, `test_decision_divergence_detector.py`, `test_control_decision.py`, `fixtures/phase_y/m-y-decision-brief/` — none cover Fernández-class extraction | `test_decision_brief_workflow.py` covers the decision_brief workflow (a separate artifact, not the per-item taxonomy). `test_decision_divergence_detector.py` covers governance, not extraction. `test_control_decision.py` covers `control/decision.py`, which is the loop's control function — not item classification. No test fires one fixture per Fernández class and asserts the correct class is emitted. |
| Decision sub-pass CLI flag | **missing** | `grep -n "subpass\|sub_pass\|decision-pass\|decision_pass\|fernandez" cli.py` returns 0 hits | No `--enable-decision-subpass` or equivalent. The 4877-line `cli.py` has no decision-sub-pass dispatch block analogous to `cli.py:3414-3426` (cascade dispatch). |
| Decision sub-pass cost estimator | **missing** | `cost/estimator.py` has `estimate_cascade_cost` (`cost/estimator.py:258-327`) and `load_cascade_confirmation_item_threshold` (`cost/estimator.py:214-230`); no decision-sub-pass analog | The cascade estimator's shape (per-chunk output tokens, item count → cost in `Decimal`, threshold gate at `cli.py:3474-3484`) is the pattern to mirror. None of it is wired for decisions. |
| Decision sub-pass smoke test (4-class fixture) | **missing** | no fixture under `tests/fixtures/` is keyed by Fernández class | One fixture per sub-type (one verbatim-issue item, one verbatim-proposal item, one verbatim-resolution item, one verbatim-scope item) with the expected class label is the minimum smoke shape. The cascade-equivalent gap was called out in `phase_2c_cascade_inventory.md:139-145` and still exists for cascade too. |
| Decision sub-pass rollback runbook | **missing** | `ls docs/runbooks/` → `first_run.md`, `phase_2b_chunking_rollback.md`, `phase_2c_cascade_rollback.md`, `verification-cycle-recovery.md` | The Phase 2.B / 2.C pattern (`phase_2b_chunking_rollback.md`, `phase_2c_cascade_rollback.md`) establishes one rollback runbook per phase. A new `docs/runbooks/phase_2d_decision_subpass_rollback.md` is the expected artifact when Phase 2.D ships. |
| Decision sub-pass rollback contracts entry | **missing** | `docs/architecture/rollback_contracts.md` exists (75K-line file, no Phase 2.D / decision-subpass section); cascade entry at lines 1052-1116 of that file is the template | A Phase 2.D entry mirroring the cascade entry's shape (PR-link, flags added, artifact types added, schemas added, CLI changes, cost-estimator extensions, comparison-engine flag changes, workflow inputs, 5-step revert plan, verification command) is the expected artifact. |
| Decision sub-pass ever run end-to-end on a real transcript | **unknown** (lake not accessible from this repo) | `processed/meetings/` in this repo is `.gitkeep` only; CLAUDE.md §"Data-lake separation" pins artifacts to external `nicklasorte/data-lake` | The negative finding above (no module, no schema field, no CLI flag, no workflow) implies the answer is "no" by construction — there is no code path that could write a decision sub-pass artifact today, with or without a real transcript. The honest framing is: even if the lake were accessible, no `decision_subpass__*.json` could be there because no writer exists. |
| Decision sub-pass measurement: F1 on `decisions` type | **missing** | `compare_opus_haiku.py` measures F1 on `decisions` items as a set (item-level, not class-level) — no per-Fernández-class F1 surface exists | The current Haiku-vs-Opus comparison treats decisions as one undifferentiated array. There is no F1 broken down by `issue` / `proposal` / `resolution` / `scope` (or `agreement`). Stage 1 / Phase 2.A / Phase 2.B baselines do not slice decision F1 by sub-type because the sub-type field does not exist. |

## Exact code path: `meeting_minutes` → (where a sub-pass would slot in) → output

There is no decision sub-pass code path today. The relevant path that
Phase 2.D would slot a sub-pass into is the existing Haiku extraction
→ `meeting_minutes` write:

1. `cli.py:2799` — `meeting_minutes_llm(...)` handler (`production_haiku`
   variant by default).
2. `workflows/meeting_minutes_llm.py` — orchestrates chunking + LLM
   call, producing the `meeting_minutes` envelope.
3. Internally the Haiku LLM is asked, in one shot, to emit all 23
   extraction arrays (`cascade/executor.py:124-148 _EXTRACTION_ARRAY_KEYS`
   enumerates them) including `decisions` — guided by the prompt
   section at `meeting_minutes_llm.md:476-632`. The model is NOT
   asked to attach a Fernández class to each decision item.
4. The artifact is canonicalized, hashed, validated against
   `meeting_minutes.schema.json` (which has no Fernández field), and
   written to `<lake>/processed/meetings/<source_id>/meeting_minutes__<ts>.json`.
5. The artifact may THEN be filtered by the cascade
   (`cli.py:3414-3426`, gated by `--enable-cascade-filter`) — the
   cascade keeps/drops items verbatim and writes a separate
   `meeting_minutes_filtered__<ts>.json`. The cascade does NOT
   classify by Fernández.

A Phase 2.D sub-pass would naturally slot between step 4 and step 5:
read the freshly-written `meeting_minutes` artifact, classify each
`decisions` item into the 4-class enum, and write a separate
`decision_subpass__<ts>.json` artifact carrying the per-item label
plus its `source_quote` and `trace_id` so the comparison engine can
slice F1 by class.

The parallel `meeting_extraction` pipeline (`extraction_merger.py:116
merge` → `meeting_extraction` artifact) has its own `decisions` array
with a richer per-item shape (`meeting_extraction.schema.json:42-122`
includes `decision_outcome`, `binding_tuple`, `grounding_overlap`,
`source_turn_ids`, etc.), but it ALSO lacks a Fernández-class field.
Phase 2.D must decide which of the two decision schemas (or both) the
sub-pass's input/output is keyed to.

## Answers to the five "additional reads" questions

**A. Exact code path** — see the 5-step trace above. The honest answer
is: no `meeting_minutes → decision sub-pass → output artifact` path
exists; the meeting_minutes write is the rightmost point that today's
code reaches before promotion.

**B. Does the existing `decisions` schema have a label field that
could host the Fernández taxonomy?** — No. Both
`meeting_minutes.schema.json:38-111` and
`meeting_extraction.schema.json:42-122` declare `additionalProperties:
false` and neither lists a field whose semantics match "discourse
function of a decision." A schema bump is required. The natural
locations are: (a) a new `decision_subtype` field on the
`meeting_minutes.decisions` item object, or (b) a parallel new
artifact type whose payload references decisions by index or
`source_quote`. The latter is the cleaner separation given the rule
that decision sub-pass output must be a separate artifact.

**C. What does the `production_haiku` prompt say about decisions?** —
The prompt FILE is
`src/spectrum_systems_core/workflows/prompts/meeting_minutes_llm.md`;
`"production_haiku"` is the `extraction_config.prompt_variant` LABEL
stamped on the resulting artifact (`meeting_minutes.schema.json:1611`,
`comparison_result.schema.json:48`), not a separate prompt file. The
prompt's decision section (lines 476-632) does the following:

- Lines 477-492: introduces the implicit-decision taxonomy as
  GUIDANCE for recognizing implicit decisions, citing Fernández et
  al. SIGDIAL 2008 and Hsueh & Moore NAACL 2007.
- Lines 494-558: lists the 4 sub-types `(Issue identification,
  Proposal / Direction, Resolution / Agreement, Scope / Boundary
  ruling)` with trigger-phrase examples for each.
- Lines 587-608: modal-verb policy routing `shall / will / should /
  may / would` to `decisions` vs `action_items` vs neither.
- Lines 610-616: hallucination defense — only emit a decision if the
  trigger phrase appears verbatim.
- Lines 618-632: domain notes (regulatory recaps → `precedent_reference`,
  procedural commitments → `action_items`, single-speaker opinion →
  `position_statement`).

The model is asked to RECOGNIZE implicit decisions via these patterns
but is NOT asked to emit a Fernández-class label per item. The prompt's
output contract for a `decisions` item is the shape declared by
`meeting_minutes.schema.json:42-110` (`text`, `verb`, `stakeholders`,
`confidence`, `rationale`, plus the four 1.4.0 verbatim-grounding
fields and the optional `reason` field). No class field is requested.

**D. What blocks measurement today?** — Several things, in dependency
order:

1. The schema does not carry a sub-type field, so producers cannot
   emit one and consumers cannot read one.
2. No extractor or sub-pass module asks the LLM to emit the field.
3. `compare_opus_haiku.py` measures decisions as an undifferentiated
   set; it has no per-class F1 surface and no `--by-decision-subtype`
   flag.
4. No fixture exists that pairs a transcript snippet with the
   expected Fernández class, so neither happy-path nor regression
   measurement is possible.
5. `processed/meetings/` is empty in this repo, so even the
   single-meeting "did we produce any decision artifacts at all"
   sanity probe must be run against the external data-lake.

The first three blocks are independent; (4) depends on (1); (5) is
infrastructure-only. (1) is the cheapest unblocking step.

**E. How many decision-typed items did the Dec 18 Haiku extraction
produce?** — **Unknown from this repo.** The Dec 18 transcript fixture
exists at `tests/fixtures/llm_extraction/dec18_transcript.txt` and the
extraction artifacts would normally live at
`<external-lake>/processed/meetings/dec18*/meeting_minutes__*.json`.
The task brief asserts the cascade run reported 230 total items on
this transcript (per PR #226). The `decisions`-typed slice of that
total is the number Phase 2.D needs to size its per-call cap against,
but it cannot be read here. The honest next step is one bash command
against the data-lake checkout:

```
jq '.payload.decisions | length' \
  data-lake/processed/meetings/dec18*/meeting_minutes__*.json
```

run by whoever opens Phase 2.D's measurement PR.

## Implications

### What is the genuine gap?

**Everything.** The Fernández taxonomy exists only as prose in one
prompt file and as four header-string assertions in one
verification script (`verify_trigger_taxonomy.py:31-36`). No code
emits the label, no schema accepts it, no eval scores it, no test
defends it, no CLI flag enables it, no workflow exercises it, no
runbook covers it, no artifact carries it. Phase 2.D is a build, not
a hookup.

### What is "built but never measured"?

One thing only: the **implicit-decision-taxonomy guidance text**
inside `meeting_minutes_llm.md:476-558`. It is "built" in the
narrowest sense (the prompt instructs the model on how to recognize
implicit decisions) but "never measured" in any sense: no eval scores
whether the model actually extracted decisions matching those
patterns, no artifact field records which sub-type the model thought
it was extracting, and `verify_trigger_taxonomy.py` only string-asserts
that the headers exist in the prompt file. The taxonomy is currently
unfalsifiable.

### Sanity check against the source-document prior

The Stage 2 brief predicts +5–8 pts precision on decisions from a
dedicated decision-classifier pass. The current Haiku-vs-Opus
baseline on the `decisions` type is **1 TP out of 2 Opus items** —
i.e., precision and recall are each 50% on a sample of 2. The prior
+5–8 pts is meaningless against a 2-item baseline; the genuine
falsification question is: **does the sub-pass produce a precision
estimate whose 95% confidence interval excludes the current 50% point
estimate on a larger sample?** The smallest defensible sample size
is the full decision count on the Dec 18 transcript (currently
unknown — see answer E above) plus at least one additional transcript
to bound variance. Phase 2.D's measurement gate should be: "either
the sub-pass beats baseline precision on a sample of ≥ N decisions
with non-overlapping confidence intervals, or it does not ship." N is
the right thing to argue about in the Phase 2.D roadmap session, not
"how many points does it add."

### What would the smallest possible Phase 2.D PR look like?

Three viable shapes, in increasing scope:

1. **Smoke test only.** Add a 4-fixture test
   (`tests/fixtures/decision_subpass/{issue,proposal,resolution,scope}_*.json`)
   and a `pytest` test that asserts a deterministic stub-classifier
   correctly labels each. No LLM call, no schema change, no workflow.
   This proves the contract shape but does not measure model behavior.
   Lowest-risk; provides a regression floor for the eventual real
   implementation.
2. **Smoke test + schema bump + dry-run classifier.** Adds a
   `decision_subtype` enum field to a NEW `decision_classification`
   artifact type (do NOT mutate `meeting_minutes`), wires a
   deterministic stub classifier into the CLI behind
   `--enable-decision-subpass=stub`, and adds the four rejection
   tests (invalid label → BLOCK, missing classification → BLOCK).
   No real LLM call yet, no measurement claim. Establishes the
   append-only contract and the rejection-gate shape before any model
   work.
3. **Full implementation with measurement gate.** Everything in (2)
   plus the Sonnet classifier prompt, the per-call item cap
   (`MAX_ITEMS_PER_DECISION_SUBPASS_CALL = 30` mirroring the cascade
   pattern, not inheriting from it), the cost estimator extension,
   the rollback runbook, the rollback contracts entry, the
   comparison-engine per-class F1 slice, and the corpus run against
   ≥ 2 real transcripts that verifies precision improves vs baseline
   with non-overlapping CIs.

The Phase 2.C precedent suggests shape (2) is the right cut for a
single PR. Shape (1) is too narrow to be worth a phase label; shape
(3) is too large to self-review honestly. Shape (2) lands the
contract, the rejection gates, and the CLI surface in one reviewable
unit; the measurement PR follows behind it.

### Bottom line

Phase 2.D is a build task with a measurement gate. The artifact to
produce in PR #1 (after this inventory) is most plausibly: a new
`decision_classification` artifact type with a strict 4-class enum, a
stub classifier behind `--enable-decision-subpass`, a 4-fixture smoke
test, two rejection tests (invalid label, missing classification),
and the rollback runbook stub. The measurement PR follows; the prior
of "+5–8 pts precision" is not a contract — the contract is "a
non-overlapping confidence interval on a sample whose size we
declared in advance." Before any of that lands, the canonical 4-class
enum must be pinned (`issue / proposal / resolution / scope` per the
existing prompt, OR `issue / proposal / resolution / agreement` per
the task brief) and the prompt updated to match.
