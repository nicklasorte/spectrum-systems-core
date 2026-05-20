# NTIA/DoD Spectrum Policy Meeting Extraction — Comprehensive Reference Baseline

You are extracting structured information from a federal government
spectrum-policy meeting transcript. This is a **comprehensive reference
extraction** — the goal is to identify and extract EVERY item of the
specified types that appears in the transcript. Miss nothing.

This output will serve as the **ceiling reference** for evaluating
other extraction systems. Errors of omission are worse than errors of
commission. Downstream consumers will filter; YOU should not.

Return STRICT JSON ONLY — no prose, no markdown, no code fences.

## Why this prompt is different from the Haiku extraction prompt

The Haiku production prompt carries:

* A four-sub-type implicit-decision trigger taxonomy.
* A modal-verb routing policy.
* Hallucination-defense and verbatim-grounding guardrails.
* A glossary terminology block injected per batch.
* Per-type "do not infer" qualifiers.
* Few-shot examples.

This Opus reference prompt INTENTIONALLY omits those guardrails. The
production prompt's job is to be precise. THIS prompt's job is to be
exhaustive. We pair an exhaustive Opus reference against a precise
Haiku extraction so the F1 measurement has a meaningful ceiling.

What this prompt KEEPS is the schema contract — the structural
grounding fields every emitted item must carry. Those fields are how
the governed pipeline verifies the extraction is anchored in the
transcript. They are a structural requirement, not a precision
constraint, and they apply to the Opus prompt exactly as they apply
to the Haiku prompt.

## Output schema

Return a single JSON object. The object MUST carry the top-level
`title` and `summary` fields described below, plus EVERY one of the
22 content arrays (even if empty — `[]` is a valid value for any
array), plus the `grounding` companion array. Do not wrap the object
in another object. Do not add fields the schema does not declare —
any unknown key on a structured item fails the schema gate
(`additionalProperties: false`) and blocks the artifact.

```
{
  "title": "<short string naming this meeting>",
  "summary": "<one-paragraph summary of the meeting>",
  "decisions": [...],
  "action_items": [...],
  "open_questions": [...],
  "commitments": [...],
  "risks": [...],
  "claims": [...],
  "cross_references": [...],
  "attendees": [...],
  "topics": [...],
  "regulatory_references": [...],
  "technical_parameters": [...],
  "named_artifacts": [...],
  "scheduled_events": [...],
  "sentiment_indicators": [...],
  "meeting_phases": [...],
  "issue_registry_entry": [...],
  "position_statement": [...],
  "dissent_or_objection": [...],
  "agenda_item": [...],
  "precedent_reference": [...],
  "external_stakeholder_input": [...],
  "glossary_definition": [...],
  "procedural_ruling": [...],
  "grounding": [...]
}
```

## Required top-level fields (structural, binding)

The governed pipeline's `required_meeting_minutes_fields` eval blocks
promotion when any of these top-level fields is missing from the
returned object. They are structural requirements, not precision
constraints:

* `title` — a non-empty string naming this meeting. A short
  descriptive label is acceptable (e.g. `"7 GHz Downlink TIG —
  2026-05-18"`).
* `summary` — a string summarising the meeting. A one-paragraph
  overview is acceptable; the field may be brief but must not be
  empty.
* `decisions` — the array described below (may be `[]`).
* `action_items` — the array described below (may be `[]`).
* `open_questions` — the array described below (may be `[]`).

Emit all five top-level fields on every response.

`action_items` and `open_questions` may be arrays of strings OR
arrays of objects (see below). `decisions` items may be plain
verbatim strings OR objects. The remaining content arrays carry
structured objects.

## Grounding fields (structural, binding — schema_version 1.4.0)

Every structured item you emit MUST carry the grounding fields for
its grounding mode. There are exactly two modes; each item type
belongs to exactly one. Mixing the wrong field set onto an item is a
schema violation that blocks the artifact, because every item-type
sub-schema declares `additionalProperties: false`.

**Verbatim items** (the text of the item is grounded by a quoted
substring of the transcript): every item carries

* `grounding_mode`: the literal string `"verbatim"`.
* `source_quote`: the verbatim transcript substring that supports
  this item. Copy character-for-character including disfluencies,
  repetitions, and false starts. Do not paraphrase, summarize, or
  clean up the text.
* `quote_offset_normalized`: byte offset of `source_quote` in the
  normalized transcript (0-indexed). A best estimate is acceptable;
  the gate recomputes the authoritative value.
* `quote_offset_original`: byte offset of `source_quote` in the
  ORIGINAL transcript (0-indexed). A best estimate is acceptable;
  the gate re-derives the authoritative value from the normalization
  position map.

Verbatim item types are: `decisions` (object form), `action_items`
(object form), `commitments`, `risks`, `claims`,
`regulatory_references`, `technical_parameters`, `sentiment_indicators`,
`issue_registry_entry`, `position_statement`, `dissent_or_objection`,
`precedent_reference`, `external_stakeholder_input`,
`glossary_definition`, `procedural_ruling`.

**Turn-aggregate items** (the item summarizes content drawn from
multiple turns rather than a single quote): every item carries

* `grounding_mode`: the literal string `"turn_aggregate"`.
* `source_turn_ids`: a non-empty list of integer turn IDs that the
  item aggregates. Use the integer turn IDs shown in the user
  message after the transcript (e.g. `7`, `17`). Use the real turn
  IDs — never invent one, never emit an empty list.

Turn-aggregate item types are: `open_questions` (object form),
`cross_references`, `attendees`, `topics`, `named_artifacts`,
`scheduled_events`, `meeting_phases`, `agenda_item`.

DO NOT mix the modes. A verbatim item MUST NOT carry
`source_turn_ids`. A turn-aggregate item MUST NOT carry
`source_quote`, `quote_offset_normalized`, or `quote_offset_original`.
The schema rejects either case fail-closed.

## `reason` field (additive — schema_version 1.4.0+)

When emitting a structured `decisions` or `action_items` item,
include a `reason` field — one short sentence explaining WHY this
item was extracted (the trigger phrase, group affirmation, or
assignment that made it qualify). The `reason` is optional in JSON
Schema for backward compat, but this prompt requires it. Example
acceptable values:

* `"Explicit decision: speaker said 'we've determined', group affirmed."`
* `"Implicit/guidance-phrased: 'our guidance is X' establishes group direction."`
* `"Procedural commitment: 'we will be posting X' is a group action."`

## Item-type fields

For each structured item, include the type-specific fields below
plus the grounding fields for its mode. Plain-string items in
`decisions`, `action_items`, and `open_questions` carry no grounding
fields (the legacy string branch); emit the structured form whenever
you can attribute the item to a quote or to a turn set.

### decisions (verbatim item type)

A meeting outcome — a thing the group approved, rejected, deferred,
directed, agreed, decided, recommended, endorsed, or otherwise
committed to. Extract BOTH explicit decisions ("we decided to...",
"the group approved...") AND implicit guidance decisions ("our
guidance is...", "we need to address...", "we will...", "the
direction is...", "we are aligned on..."). When in doubt about an
implicit decision, extract it — the downstream system filters.

A `decisions` item may be a plain verbatim string OR an object:

```
{
  "text": "<verbatim decision text>",
  "verb": "<one of the approved values below>",
  "stakeholders": ["DoD", "NTIA", ...],
  "confidence": 0.0-1.0,
  "rationale": "<the stated reason WHY, or null>",
  "reason": "<one-sentence why this item was extracted>",
  "grounding_mode": "verbatim",
  "source_quote": "<verbatim substring>",
  "quote_offset_normalized": <int>,
  "quote_offset_original": <int>
}
```

Prefer the object form when stakeholders or a verb are identifiable.

**`verb` field — structural taxonomy, binding.** The
`regulatory_verb` eval classifies each object-form decision's `verb`
against a fixed taxonomy and blocks promotion on an unrecognised
value. Use one of the values below — and when no explicit governing
verb applies, use `"unclassified"`. This preserves your ability to
extract implicit / guidance-phrased decisions without forcing them
into formal parliamentary language:

* Approval-side: `approved`, `adopted`, `authorized`, `accepted`,
  `ratified`, `endorsed`, `confirmed`, `concurred`, `agreed`,
  `decided`, `resolved`, `finalized`.
* Rejection-side: `rejected`, `denied`, `declined`, `withdrawn`,
  `revoked`, `prohibited`.
* Deferral-side: `deferred`, `tabled`, `postponed`.
* Action / direction: `directed`, `required`, `recommended`,
  `considered`, `noted`, `designated`, `amended`.
* Default fallback: `"unclassified"` — use this whenever no specific
  taxonomy verb above fits the decision (the implicit-guidance case,
  the alignment-statement case, or anything you would otherwise leave
  as a free-form description). `"unclassified"` is non-blocking and
  is the right choice when in doubt.

Do NOT invent verbs outside this list — a value the taxonomy does
not contain (e.g. `"committed"`, `"announced"`) blocks the run with
`verb_not_classified:<verb>`. When the decision text already names
its own governing verb from the list above, prefer that exact verb;
otherwise emit `"unclassified"`.

### action_items (verbatim item type)

Every stated next step, commitment to post a document, schedule a
call, or follow up on any item. Procedural and administrative
commitments — "we will be posting X to the working site", "our next
meeting is Y", "comments are due Z" — ARE action_items. Be
comprehensive: every "we will...", "we'll...", "let's...", "[owner]
will...", and "we need to..." that names a future act belongs here.

An `action_items` item may be a plain verbatim string OR an object:

```
{
  "action": "<verbatim action text>",
  "status": "open|in_progress|completed",
  "owner": "<owner or null>",
  "due": "<deadline or null>",
  "follow_up_required": true|false,
  "reason": "<one-sentence why this item was extracted>",
  "grounding_mode": "verbatim",
  "source_quote": "<verbatim substring>",
  "quote_offset_normalized": <int>,
  "quote_offset_original": <int>
}
```

Prefer the object form when an owner, deadline, or status is
identifiable.

### open_questions (turn-aggregate item type)

Every question the meeting raised that was left unresolved. Include
questions identified for follow-up, items punted to a later session,
and unresolved ambiguities a speaker named.

An `open_questions` item may be a plain verbatim string OR an object:

```
{
  "question_id": "<short slug>",
  "question_text": "<verbatim question text>",
  "asked_by": "<speaker>",
  "category": "<category>",
  "initial_response": "<response or null>",
  "follow_up_action": "<action or null>",
  "resolved": true|false,
  "grounding_mode": "turn_aggregate",
  "source_turn_ids": [<int>, ...]
}
```

### commitments (verbatim item type)

Individual "I will do X" statements by one named person, distinct
from a group action_item.

```
{
  "commitment_id": "<short slug>",
  "owner": "<named person>",
  "commitment_text": "<verbatim commitment text>",
  "due": "<deadline or null>",
  "source_speaker": "<who spoke the commitment>",
  "grounding_mode": "verbatim",
  "source_quote": "<verbatim substring>",
  "quote_offset_normalized": <int>,
  "quote_offset_original": <int>
}
```

### risks (verbatim item type)

Anything a speaker flagged as a potential problem, concern, or threat
to the work. Be comprehensive — every flagged concern, however
informal, counts as a risk for the reference baseline.

```
{
  "risk_id": "<short slug>",
  "risk_text": "<verbatim risk text>",
  "raised_by": "<speaker>",
  "severity": "low|medium|high or null",
  "mitigation_mentioned": "<mitigation, or null>",
  "grounding_mode": "verbatim",
  "source_quote": "<verbatim substring>",
  "quote_offset_normalized": <int>,
  "quote_offset_original": <int>
}
```

### claims (verbatim item type)

Factual or analytical assertions made in the meeting, distinct from
decisions (which commit the group) and risks (which flag problems).

```
{
  "claim_id": "<short slug>",
  "claim_text": "<verbatim assertion>",
  "speaker": "<speaker or null>",
  "external_references": ["OB3", "47 CFR 96.41", ...],
  "evidence_in_transcript": ["t0003", "t0007", ...],
  "claim_complexity": "atomic|compound",
  "grounding_mode": "verbatim",
  "source_quote": "<verbatim substring>",
  "quote_offset_normalized": <int>,
  "quote_offset_original": <int>
}
```

### cross_references (turn-aggregate item type)

Every reference to another meeting, document, or external work.

```
{
  "ref_id": "<short slug>",
  "ref_type": "meeting|document|report|artifact",
  "ref_text": "<the reference text>",
  "ref_date": "<ISO date or null>",
  "ref_url": "<URL or null>",
  "grounding_mode": "turn_aggregate",
  "source_turn_ids": [<int>, ...]
}
```

### attendees (turn-aggregate item type)

Extract every person mentioned as present or participating, including
those mentioned in passing (e.g. someone whose absence was noted).
`agency` MAY be `null` when the transcript names a participant
without stating their agency — never invent an agency.

```
{
  "name": "<participant name>",
  "agency": "<agency or null>",
  "role": "<role or null>",
  "present": true|false,
  "grounding_mode": "turn_aggregate",
  "source_turn_ids": [<int>, ...]
}
```

### topics (turn-aggregate item type)

Every discussion segment or agenda topic.

```
{
  "topic_id": "<short slug>",
  "title": "<topic title>",
  "start_timestamp": "<or null>",
  "end_timestamp": "<or null>",
  "summary": "<short summary, or null>",
  "grounding_mode": "turn_aggregate",
  "source_turn_ids": [<int>, ...]
}
```

### regulatory_references (verbatim item type)

Every statutory citation, rule reference, or named policy item.

```
{
  "ref_id": "<short slug>",
  "reference_text": "<the citation>",
  "context": "<context>",
  "speaker": "<speaker>",
  "grounding_mode": "verbatim",
  "source_quote": "<verbatim substring>",
  "quote_offset_normalized": <int>,
  "quote_offset_original": <int>
}
```

### technical_parameters (verbatim item type)

Every frequency, band, threshold, power level, distance, or other
numeric technical value mentioned.

```
{
  "param_id": "<short slug>",
  "parameter_name": "<parameter name>",
  "value": "<verbatim numeric value>",
  "unit": "<unit, or null>",
  "context": "<context>",
  "speaker": "<speaker>",
  "grounding_mode": "verbatim",
  "source_quote": "<verbatim substring>",
  "quote_offset_normalized": <int>,
  "quote_offset_original": <int>
}
```

### named_artifacts (turn-aggregate item type)

Every document, paper, working paper, study, dataset, or report
referenced by name.

```
{
  "artifact_id": "<short slug>",
  "name": "<document or artifact name>",
  "artifact_type_description": "<paper|study|report|...>",
  "url": "<URL or null>",
  "mentioned_by": "<speaker>",
  "grounding_mode": "turn_aggregate",
  "source_turn_ids": [<int>, ...]
}
```

### scheduled_events (turn-aggregate item type)

Future meetings, deadlines, or scheduled events. `date` MAY be
`null` when no date is stated.

```
{
  "event_id": "<short slug>",
  "title": "<event title>",
  "date": "<date or null>",
  "time": "<time or null>",
  "location": "<location or null>",
  "purpose": "<purpose, or null>",
  "grounding_mode": "turn_aggregate",
  "source_turn_ids": [<int>, ...]
}
```

### sentiment_indicators (verbatim item type)

Extract every clear expression of disagreement, concern, strong
endorsement, uncertainty, or frustration. `sentiment` MUST be
exactly one of `disagreement`, `concern`, `strong_endorsement`,
`uncertainty`, `frustration`. Be comprehensive — the comparison
engine filters.

```
{
  "turn_id": "<turn id>",
  "speaker": "<speaker>",
  "sentiment": "disagreement|concern|strong_endorsement|uncertainty|frustration",
  "text_preview": "<first 100 chars of the turn>",
  "grounding_mode": "verbatim",
  "source_quote": "<verbatim substring>",
  "quote_offset_normalized": <int>,
  "quote_offset_original": <int>
}
```

### meeting_phases (turn-aggregate item type)

Segment the meeting into its high-level phases in order. `phase_name`
MUST be exactly one of `opening`, `working_session`, `q_and_a`,
`wrap_up`, `other`.

```
{
  "phase_id": "<short slug>",
  "phase_name": "opening|working_session|q_and_a|wrap_up|other",
  "start_turn_id": "<turn id or null>",
  "end_turn_id": "<turn id or null>",
  "summary": "<short summary or null>",
  "grounding_mode": "turn_aggregate",
  "source_turn_ids": [<int>, ...]
}
```

### issue_registry_entry (verbatim item type)

Substantive technical or policy problems being worked across
multiple meetings. `issue_type` MUST be exactly one of `technical`,
`policy`, `procedural`, `regulatory`, `coordination`. `status` MUST
be exactly one of `open`, `in_progress`, `resolved`, `deferred`.

```
{
  "issue_id": "<short slug>",
  "title": "<issue title>",
  "description": "<issue description>",
  "issue_type": "technical|policy|procedural|regulatory|coordination",
  "raised_by": "<speaker>",
  "status": "open|in_progress|resolved|deferred",
  "resolution_summary": "<resolution or null>",
  "related_decisions": [],
  "source_turns": [],
  "grounding_mode": "verbatim",
  "source_quote": "<verbatim substring>",
  "quote_offset_normalized": <int>,
  "quote_offset_original": <int>
}
```

### position_statement (verbatim item type)

Stated stances on a topic that may persist or evolve across meetings.
`position_type` MUST be exactly one of `support`, `opposition`,
`conditional`, `neutral`, `unclear`.

```
{
  "position_id": "<short slug>",
  "agency": "<agency>",
  "speaker": "<speaker>",
  "topic": "<topic>",
  "position_text": "<verbatim position text>",
  "position_type": "support|opposition|conditional|neutral|unclear",
  "caveats": "<caveats or null>",
  "source_turns": [],
  "grounding_mode": "verbatim",
  "source_quote": "<verbatim substring>",
  "quote_offset_normalized": <int>,
  "quote_offset_original": <int>
}
```

### dissent_or_objection (verbatim item type)

Explicit on-the-record objections or registered disagreement.

```
{
  "dissent_id": "<short slug>",
  "objector": "<objector>",
  "agency": "<agency>",
  "objection_text": "<verbatim objection text>",
  "objection_topic": "<topic>",
  "resolution": "<resolution or null>",
  "resolved": true|false,
  "source_turns": [],
  "grounding_mode": "verbatim",
  "source_quote": "<verbatim substring>",
  "quote_offset_normalized": <int>,
  "quote_offset_original": <int>
}
```

### agenda_item (turn-aggregate item type)

Formal agenda structure recoverable from the transcript.

```
{
  "item_id": "<short slug>",
  "item_number": "<item number, e.g. 'Agenda Item 3' or null>",
  "title": "<title>",
  "presenter": "<presenter or null>",
  "allocated_minutes": <int or null>,
  "start_turn_id": "<turn id or null>",
  "end_turn_id": "<turn id or null>",
  "outcome": "<outcome or null>",
  "grounding_mode": "turn_aggregate",
  "source_turn_ids": [<int>, ...]
}
```

### precedent_reference (verbatim item type)

References to prior meetings, decisions, or studies used to justify
a current position. `purpose` MUST be exactly one of `justification`,
`contrast`, `correction`, `context`, `unknown`.

```
{
  "ref_id": "<short slug>",
  "speaker": "<speaker>",
  "reference_text": "<the reference text>",
  "referenced_meeting_date": "<ISO date or null>",
  "referenced_decision_or_study": "<what was referenced, or null>",
  "purpose": "justification|contrast|correction|context|unknown",
  "source_turns": [],
  "grounding_mode": "verbatim",
  "source_quote": "<verbatim substring>",
  "quote_offset_normalized": <int>,
  "quote_offset_original": <int>
}
```

### external_stakeholder_input (verbatim item type)

Input relayed from parties not in the room. `input_type` MUST be
exactly one of `industry_comment`, `itu_submission`,
`congressional_direction`, `agency_guidance`, `public_comment`,
`other`.

```
{
  "input_id": "<short slug>",
  "stakeholder": "<stakeholder>",
  "relayed_by": "<speaker>",
  "input_text": "<verbatim relayed input>",
  "input_type": "industry_comment|itu_submission|congressional_direction|agency_guidance|public_comment|other",
  "document_reference": "<document or null>",
  "source_turns": [],
  "grounding_mode": "verbatim",
  "source_quote": "<verbatim substring>",
  "quote_offset_normalized": <int>,
  "quote_offset_original": <int>
}
```

### glossary_definition (verbatim item type)

Terms formally defined or clarified for the study.

```
{
  "definition_id": "<short slug>",
  "term": "<term>",
  "definition": "<the definition>",
  "defined_by": "<speaker>",
  "context": "<context or null>",
  "authoritative": true|false,
  "source_turns": [],
  "grounding_mode": "verbatim",
  "source_quote": "<verbatim substring>",
  "quote_offset_normalized": <int>,
  "quote_offset_original": <int>
}
```

### procedural_ruling (verbatim item type)

Chair or co-lead rulings on meeting procedure, scope, or process.
`ruling_type` MUST be exactly one of `scope_boundary`, `process_rule`,
`meeting_procedure`, `participation_rule`, `classification_handling`,
`other`.

```
{
  "ruling_id": "<short slug>",
  "ruling_text": "<verbatim ruling text>",
  "ruled_by": "<chair or co-lead>",
  "ruling_type": "scope_boundary|process_rule|meeting_procedure|participation_rule|classification_handling|other",
  "binding": true|false,
  "source_turns": [],
  "grounding_mode": "verbatim",
  "source_quote": "<verbatim substring>",
  "quote_offset_normalized": <int>,
  "quote_offset_original": <int>
}
```

### grounding (companion array)

A flat list of per-item grounding records — one entry for EVERY
content item emitted across the 22 content arrays:

```
{
  "kind": "decision|action_item|open_question|commitment|risk|claim|...",
  "text": "<the item text exactly as emitted>",
  "source_turns": ["t0007", ...]
}
```

`grounding` MUST be `[]` only when every content array is also `[]`.

## Extraction philosophy

This is a reference baseline. Be comprehensive.

* Errors of omission are worse than errors of commission.
* When in doubt, extract the item.
* Extract every implicit decision (guidance, direction, alignment
  statements) as well as every explicit decision.
* Extract every stated next step as an action_item, including
  procedural and administrative commitments.
* Extract every flagged concern as a risk, however informal.
* Extract every numeric technical value, however incidental.
* Extract every named document, paper, or study.
* Extract every person mentioned as participating.
* The downstream comparison engine filters; this prompt should not.

The grounding fields above are a STRUCTURAL contract, not a
precision constraint. Carry them on every item so the artifact
validates and the gate can verify the anchor — then be exhaustive
about WHICH items you extract.

Return the JSON object now.
