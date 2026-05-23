# Schema Enum Audit — May 2026

Document ID: SSC-AUDIT-001
Status: Retroactive class-wide audit of `meeting_minutes`
producer-facing enum constraints, motivated by the three-instance
`stochastic schema brittleness` pattern documented in
`docs/conventions/github_actions_workflows.md` §7.2.

Outcome (this audit): **A — no schema enum gaps detected as of this
audit**. One prompt-doc hardening opportunity flagged for a future
session (§5).

---

## 1. Why this audit exists

Three prior PRs share one failure class:

| PR | Field | Stochastic Haiku value | Fix |
| --- | --- | --- | --- |
| #182 | `attendees[].agency` | `null` | type → `["string","null"]` |
| #205 | `scheduled_events[].event_id` (root cause: `date` nullability) | `null` | type → `["string","null"]` (date), and per PR #224 the `event_id` recurrence turned out to be diagnostic-surfacing, not a schema gap |
| #228 | `position_statement[].position_type` | `"clarification"` | enum extended by one value |

The class is "a faithful Haiku extraction surfaces a real domain
value (or `null` for an unstated subfield) outside an over-narrow
producer-facing constraint." Patching one field at a time means the
next stochastic miss spends another operator-hour deriving the same
conclusion. The convention in §7.2 of the conventions doc binds
future sessions to a class-wide audit on the second instance; this
file is that audit, run retroactively for the existing constraints.

## 2. Scope

This audit covers the `meeting_minutes` artifact schema at
`src/spectrum_systems_core/schemas/meeting_minutes.schema.json` —
the artifact every Haiku extraction produces. The audit also covers
adjacent schemas the same prompt populates:

- `meeting_extraction.schema.json` (typed-extraction wrapper enums)
- `typed_extraction.schema.json` (same enums as `meeting_extraction`)
- `chunk_classifications.schema.json` (per-chunk classification enum)

Other schemas (`eval_result`, `cascade_filter_log`, `health_finding`,
etc.) are NOT producer-facing — their enums are populated by the
deterministic harness, not by Haiku. They are out of scope for the
"stochastic schema brittleness" class.

## 3. Method

In a normal session this audit reads three sources and cross-checks
them:

1. The schema's allowed values (the constraint).
2. The Haiku prompt's instructed values (the producer contract).
3. Real Haiku output on disk in `nicklasorte/data-lake` —
   specifically the Dec 18 baseline artifact and any blocked
   `cascade_filter_log` whose `decisions[].decision="invalid_response_passthrough"`
   carries a `schema_violation` detail.

The data-lake is a separate repository
(`docs/contracts/data_lake_contract.md`). **This session is
scope-restricted to `nicklasorte/spectrum-systems-core` and cannot
read `nicklasorte/data-lake` via the GitHub MCP nor clone it locally
(no `DATA_LAKE_TOKEN` outside workflow context).** The live-artifact
read is therefore documented but not executed here; the audit
proceeds from the two in-repo sources (schema + prompt), which is
the same evidence the §7.2 rule grants when the data-lake is offline
(see the data-lake contract §8 "When the data-lake is not on disk
… tests and audits that depend on live data-lake files skip
cleanly. The contract still binds; it is just verified at a
different time"). The follow-up step is in §6.

Within-repo evidence used:

- Schema enum lists: enumerated by walking each `*.schema.json` for
  the `enum` key (see §4 inventory).
- Prompt instructions: every enum value the schema enforces is also
  named in `src/spectrum_systems_core/workflows/prompts/meeting_minutes_llm.md`
  — either inline next to the field description, or in the explicit
  "Enforced enum values (1.3.0 — must match EXACTLY)" block at
  L438–450.
- Git log of prior schema-violation patches: confirms exactly the
  three-instance recurrence in §1 and no other field has been
  patched the same way (`git log --all --grep='schema_violation'`,
  `git log --all --grep='enum'`, plus the merged-PR titles since
  PR #128).
- Test fixtures: `tests/test_meeting_minutes_schema.py` exercises
  every schema enum value parametrically (the `_each_..._validates`
  family) and rejects-out-of-enum companions.

## 4. Inventory — `meeting_minutes` enums

Every enum-constrained property on the `meeting_minutes` schema, the
authority (who emits the value), the prompt section that documents
it, and the catch-all status:

All line numbers below are in
`src/spectrum_systems_core/workflows/prompts/meeting_minutes_llm.md`
as of this audit (line numbers shift when the prompt is edited;
verify against `git blame` if drift is suspected).

| Path | Allowed values | Catch-all | Set by | Documented in prompt at |
| --- | --- | --- | --- | --- |
| `schema_version` | 1.0.0, 1.1.0, 1.2.0, 1.3.0, 1.4.0 | n/a (version) | harness | — (operator-stamped) |
| `action_items[].status` | open, in_progress, completed | none | extraction model | **not enumerated in prompt — see §5.2 finding** |
| `action_items[].priority` | low, medium, high | none (omit key) | extraction model | L586-605 (modal-verb policy; `priority: "medium"` shown at L596 — only `"medium"` is named in prompt, see §5.3) |
| `risks[].severity` | low, medium, high, **null** | null permitted | extraction model | L236-237 ("is one of low, medium, high, or null") |
| `claims[].claim_complexity` | atomic, compound | none (default atomic) | extraction model | L429-436 |
| `cross_references[].ref_type` | meeting, document, report, artifact | none | extraction model | L243-244 ("exactly one of") |
| `sentiment_indicators[].sentiment` | disagreement, concern, strong_endorsement, uncertainty, frustration | none | extraction model | L328-334 ("exactly one of") |
| `meeting_phases[].phase_name` | opening, working_session, q_and_a, wrap_up, **other** | `other` | extraction model | L339-344 ("exactly one of") |
| `issue_registry_entry[].issue_type` | technical, policy, procedural, regulatory, coordination | none | extraction model | L446 (enforced block) |
| `issue_registry_entry[].status` | open, in_progress, resolved, deferred | none | extraction model | L447 (enforced block) |
| `position_statement[].position_type` | support, opposition, conditional, neutral, unclear, clarification | none | extraction model | L448 (enforced block); `clarification` added by PR #228 |
| `precedent_reference[].purpose` | justification, contrast, correction, context, **unknown** | `unknown` | extraction model | L401-402 (inline list) |
| `external_stakeholder_input[].input_type` | industry_comment, itu_submission, congressional_direction, agency_guidance, public_comment, **other** | `other` (`use "other" if unsure`) | extraction model | L449 (enforced block) |
| `procedural_ruling[].ruling_type` | scope_boundary, process_rule, meeting_procedure, participation_rule, classification_handling, **other** | `other` (`use "other" if unsure`) | extraction model | L450 (enforced block) |
| `provenance.extraction_config.prompt_variant` | production_haiku, haiku_prompt_with_sonnet_model, opus_prompt_with_sonnet_model, opus_baseline, production_haiku_with_cascade_filter | n/a | harness (CLI stamp) | — (operator-stamped) |

## 4A. Inventory — adjacent producer schemas

| Schema / path | Allowed values | Catch-all | Set by | Documented in prompt at |
| --- | --- | --- | --- | --- |
| `meeting_extraction.decisions[].decision_type` | approved, rejected, deferred, noted, considered, action_required, open_question, to_be_determined | `to_be_determined` | extraction model | (typed-extraction prompt; same `taxonomy.py` source per CLAUDE.md "Taxonomy" section) |
| `meeting_extraction.decisions[].decision_outcome` | approval, rejection, deferral, action_required, noted, question | none | extraction model | (typed-extraction prompt) |
| `meeting_extraction.claims[].claim_type` | technical, procedural, regulatory, opinion | `opinion` | extraction model | (typed-extraction prompt) |
| `meeting_extraction.decisions[].source_turn_validation` | verified, invalid, missing | n/a | harness | — (set by `source_turn_validity` eval) |
| `meeting_extraction.claims[].source_turn_validation` | verified, invalid, missing | n/a | harness | — (set by `source_turn_validity` eval) |
| `meeting_extraction.action_items[].source_turn_validation` | verified, invalid, missing | n/a | harness | — (set by `source_turn_validity` eval) |
| `meeting_extraction.extraction_mode` | two_stage, single_pass | n/a | harness | — (operator flag) |
| `typed_extraction.*` | (same as `meeting_extraction.*` above — same taxonomy import) | (same) | (same) | (same) |
| `chunk_classifications.classifications[].classification` | decision, claim, action_item, **off_topic** | `off_topic` | extraction model | (chunk-classifier prompt) |

## 5. Findings

### 5.1 No schema enum gaps detected (Outcome A)

Every enum value the schema enforces is also instructed in the Haiku
prompt — either inline in the field's description, or in the
explicit "Enforced enum values" block, or both — with one prompt-vs-
schema misalignment called out separately in §5.2 below. No field in
§4 / §4A has an enum value documented in the prompt that the schema
rejects (which would be a producer-vs-gate inversion).

The `_each_<field>_value_validates` parametric tests in
`tests/test_meeting_minutes_schema.py` exercise every value in §4
once. The rejection-companion tests
(`*_outside_enum_fails`) prove the gate fail-closes on a value the
prompt does not authorize.

Risk-ranked summary of "what's most likely to break next" if a
future Haiku output drifts (no-catch-all enums, by domain
plausibility of a near-miss value):

1. `sentiment_indicators[].sentiment` — 5 values, no catch-all,
   domain has plausible near-misses (`agreement`, `support`,
   `frustration` → already in but `annoyance`/`relief`/`surprise`
   not in).
2. `cross_references[].ref_type` — 4 values, no catch-all
   (`email`/`law`/`rule` are plausible Haiku misses).
3. `action_items[].status` — 3 values, no catch-all
   (`pending`/`done`/`blocked` are plausible misses; `done` is the
   exact wrong-value the existing
   `test_action_item_status_outside_enum_fails` test pins).
4. `risks[].severity` — 4 values incl. null, but `critical` is the
   most plausible miss (already pinned by
   `test_risk_severity_outside_enum_fails`).
5. `issue_registry_entry[].issue_type` and `.status` — both no
   catch-all; either could trip on the next class of issue.
6. `meeting_extraction.decisions[].decision_outcome` — 6 values, no
   catch-all, no live measurement yet.

This list is informational, not actionable. Per §1, the rule is
"patch on evidence" — a stochastic miss is the evidence that
extends the enum, never a speculative pre-extension. If §6's
follow-up step finds a real Haiku value outside one of these enums
on the Dec 18 baseline, that PR bundles the fix(es) per the §7.2
rule.

### 5.2 Prompt-vs-schema misalignment — `action_items[].status`

The schema enforces `action_items[].status ∈ {open, in_progress,
completed}` on the structured-object form of an action_item. The
Haiku prompt does NOT enumerate these three values anywhere. The
prompt mentions `status` only as part of the structured-form JSON
key list (L18, L84-89 manifests) and the `follow_up_required`
discussion at L314-319; neither names the three allowed values.

Risk assessment:

- `status` is OPTIONAL on the structured action_item form (the
  schema's only required field there is `action`). A Haiku output
  that emits `action_items` as plain strings (the common path) is
  unaffected. A structured action_item without a `status` key is
  also unaffected.
- The risk is narrow: Haiku emits a structured action_item AND
  spontaneously decides to populate `status` AND picks a value
  outside `{open, in_progress, completed}` (the most plausible
  near-misses are `"done"`, `"pending"`, `"complete"`,
  `"blocked"`).
- No prior PR has patched this — git log searches for
  `action_items.status`, `done`, and the relevant reason codes
  return nothing related to this field.

This is reported as a prompt-doc finding, NOT a schema enum gap
(the schema is fine as-is; the producer contract just doesn't
explicitly close the door on `"done"`). The durable fix is to add
this enum to the L438-450 "Enforced enum values" block:

```
- `action_items[].status` (structured form only): open | in_progress | completed
```

This is NOT executed in this PR because:

1. Changing the prompt is a producer-behaviour change that requires
   a measurement against the data-lake baseline F1 to confirm no
   regression. The data-lake is not accessible from this session
   (per §3).
2. The fix belongs in a `chore(prompt): close enum-coverage gap on
   action_items.status` PR with its own measurement; bundling it
   with the conventions changes in THIS PR would make the
   conventions changes harder to revert independently.

### 5.3 Prompt-doc hardening opportunity (NOT executed in this PR)

The Haiku prompt has two places where enum values appear:

- Inline at each field's description (every enum is covered, with
  the §5.2 `action_items.status` exception).
- A consolidated "Enforced enum values (1.3.0 — must match EXACTLY)"
  block at L438-450 (5 of 14 producer-facing enums covered:
  `issue_type`, `issue_registry_entry.status`, `position_type`,
  `input_type`, `ruling_type`).

Adding the remaining 9 enums (the `action_items.status` of §5.2 plus
`action_items.priority`, `risks.severity`, `claim_complexity`,
`ref_type`, `sentiment`, `phase_name`, `precedent_reference.purpose`,
`chunk_classifications.classification`) to the consolidated block
would give Haiku a single fail-safe table at the end of the prompt
covering every enforced enum.

This is NOT executed in this PR for the same reason as §5.2 — it is
a producer-behaviour change that needs measurement. Recommended
future session: `chore(prompt): consolidate all enforced enum
values in single block`, gated on a fresh Dec 18 baseline F1
comparison. The §5.2 fix may be folded into this session.

## 6. Follow-up — verification against the live Dec 18 baseline

The audit in §4 / §5 reads the schema + prompt only (per the access
constraint named in §3). To close the audit fully, a session with
data-lake access MUST also:

1. Clone `nicklasorte/data-lake` via the
   `./.github/actions/clone-data-lake` composite action (or the
   workflow analogue).
2. For the canonical Dec 18 transcript
   (`7-ghz-downlink-tig-meeting-kickoff---transcript-20251218`),
   read the latest `meeting_minutes__*.json` under
   `store/processed/meetings/<source_id>/` and assert every value
   in every enum-constrained field is in the schema's enum list.
3. Read every `cascade_filter_log__*.json` under the same
   `<source_id>/` and assert no entry carries a
   `decision: "invalid_response_passthrough"` whose detail names a
   `schema_violation` on an enum field.
4. Repeat (2) and (3) for every other meeting in
   `data-lake/store/raw/meetings/` that has a baseline artifact.

If any value or `schema_violation` detail surfaces an enum miss,
that session opens an Outcome-B PR per the conventions doc rule
(bundle ALL gaps in one PR; rejection + happy-path test per added
value; no removal of existing values; no schema_version bump per
the additivity note in `meeting_minutes.schema.json:29`).

## 7. References

- Conventions: `docs/conventions/github_actions_workflows.md` §7
  (the rule set this audit satisfies).
- Constitution: `docs/architecture/system_constitution.md` §6
  (artifact envelope; state changes by new envelopes not in-place
  edits — relevant to the §7.3 append-only rule).
- Data-lake contract: `docs/contracts/data_lake_contract.md` §6
  (filename convention and promotion gate), §8 (boundary rules,
  including the data-lake-offline clause).
- Canonical prior PRs in the failure class: #182 (agency null),
  #205 (event_id null root-caused via date null), #224 (event_id
  recurrence diagnosed as diagnostics gap, not schema gap), #228
  (position_type `clarification`).
- Schema additivity rule: inline at
  `src/spectrum_systems_core/schemas/meeting_minutes.schema.json:29`.
- Taxonomy import single-source rule: CLAUDE.md "Taxonomy" section
  and `src/spectrum_systems_core/config/taxonomy.py`.
