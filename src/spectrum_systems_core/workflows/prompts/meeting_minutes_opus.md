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

This Opus reference prompt INTENTIONALLY omits those guardrails. The
production prompt's job is to be precise. THIS prompt's job is to be
exhaustive. We pair an exhaustive Opus reference against a precise
Haiku extraction so the F1 measurement has a meaningful ceiling.

## Output schema

Return a single JSON object. EVERY one of the 23 item-type arrays
below MUST be present even if empty (`[]` is a valid value for any
array). Do not wrap the object in another object. Do not add fields
the schema does not declare.

```
{
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

`action_items` and `open_questions` are arrays of strings. `decisions`
items may be plain strings OR objects (see below). The remaining
arrays carry structured objects.

## Item-type fields

For each structured item, include the type-specific fields below.
For EVERY emitted item (regardless of type), also include the
grounding-companion fields:

* `source_quote` — the verbatim transcript substring that supports
  this item. Copy character-for-character including disfluencies,
  repetitions, and false starts. Do not paraphrase or clean up.
* `quote_offset_normalized` — byte offset of `source_quote` in the
  normalized transcript (0-indexed). Best estimate is acceptable;
  the comparison engine recomputes the authoritative value.
* `source_turn_ids` — list of integer turn IDs that this item spans
  (use the turn IDs shown in the user message after the transcript).

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
  "verb": "approved|deferred|directed|agreed|... or unclassified",
  "stakeholders": ["DoD", "NTIA", ...],
  "confidence": 0.0-1.0,
  "rationale": "<the stated reason WHY, or null>",
  "source_quote": "...",
  "quote_offset_normalized": <int>,
  "source_turn_ids": [<int>, ...]
}
```

Prefer the object form when stakeholders or a verb are identifiable.

### action_items (verbatim string array)

Every stated next step, commitment to post a document, schedule a
call, or follow up on any item. Procedural and administrative
commitments — "we will be posting X to the working site", "our next
meeting is Y", "comments are due Z" — ARE action_items. Be
comprehensive: every "we will...", "we'll...", "let's...", "[owner]
will...", and "we need to..." that names a future act belongs here.

Even when the action_items field is a plain string, the comprehensive
extraction should be exhaustive — extract every assigned task and
every group commitment to a future act.

### open_questions (verbatim string array)

Every question the meeting raised that was left unresolved. Include
questions that the group identified for follow-up, items punted to a
later session, and unresolved ambiguities a speaker explicitly named.

### commitments

```
{
  "commitment_id": "<short slug>",
  "owner": "<named person>",
  "commitment_text": "<verbatim commitment text>",
  "due": "<deadline or null>",
  "source_speaker": "<who spoke the commitment>",
  "source_quote": "...", "quote_offset_normalized": <int>,
  "source_turn_ids": [<int>, ...]
}
```

Individual "I will do X" statements by one named person, distinct
from a group action_item.

### risks

```
{
  "risk_id": "<short slug>",
  "risk_text": "<verbatim risk text>",
  "raised_by": "<speaker>",
  "severity": "low|medium|high or null",
  "mitigation_mentioned": "<mitigation, or null>",
  "source_quote": "...", "quote_offset_normalized": <int>,
  "source_turn_ids": [<int>, ...]
}
```

Anything a speaker flagged as a potential problem, concern, or threat
to the work. Be comprehensive — every flagged concern, however
informal, counts as a risk for the reference baseline.

### claims

```
{
  "claim_id": "<short slug>",
  "claim_text": "<verbatim assertion>",
  "speaker": "<speaker or null>",
  "external_references": ["OB3", "47 CFR 96.41", ...],
  "evidence_in_transcript": ["t0003", "t0007", ...],
  "claim_complexity": "atomic|compound",
  "source_quote": "...", "quote_offset_normalized": <int>,
  "source_turn_ids": [<int>, ...]
}
```

Factual or analytical assertions made in the meeting, distinct from
decisions (which commit the group) and risks (which flag problems).

### cross_references

```
{
  "ref_id": "<short slug>",
  "ref_type": "meeting|document|report|artifact",
  "ref_text": "<the reference text>",
  "ref_date": "<ISO date or null>",
  "ref_url": "<URL or null>",
  "source_turn_ids": [<int>, ...]
}
```

Every reference to another meeting, document, or external work.

### attendees

```
{
  "name": "<participant name>",
  "agency": "<agency>",
  "role": "<role>",
  "present": true|false,
  "source_turn_ids": [<int>, ...]
}
```

Extract every person mentioned as present or participating, including
those mentioned in passing (e.g. someone whose absence was noted).

### topics

```
{
  "topic_id": "<short slug>",
  "title": "<topic title>",
  "start_timestamp": "<or null>",
  "end_timestamp": "<or null>",
  "summary": "<short summary, or null>",
  "source_turn_ids": [<int>, ...]
}
```

Every discussion segment or agenda topic.

### regulatory_references

```
{
  "ref_id": "<short slug>",
  "reference_text": "<the citation>",
  "context": "<context, or null>",
  "speaker": "<speaker>",
  "source_quote": "...", "quote_offset_normalized": <int>,
  "source_turn_ids": [<int>, ...]
}
```

Every statutory citation, rule reference, or named policy item.

### technical_parameters

```
{
  "param_id": "<short slug>",
  "parameter_name": "<parameter name>",
  "value": "<verbatim numeric value>",
  "unit": "<unit, or null>",
  "context": "<context, or null>",
  "speaker": "<speaker>",
  "source_quote": "...", "quote_offset_normalized": <int>,
  "source_turn_ids": [<int>, ...]
}
```

Every frequency, band, threshold, power level, distance, or other
numeric technical value mentioned.

### named_artifacts

```
{
  "artifact_id": "<short slug>",
  "name": "<document or artifact name>",
  "artifact_type_description": "<paper|study|report|...>",
  "url": "<URL or null>",
  "mentioned_by": "<speaker>",
  "source_turn_ids": [<int>, ...]
}
```

Every document, paper, working paper, study, dataset, or report
referenced by name.

### scheduled_events

```
{
  "event_id": "<short slug>",
  "title": "<event title>",
  "date": "<date or descriptor>",
  "time": "<time or null>",
  "location": "<location or null>",
  "purpose": "<purpose, or null>",
  "source_turn_ids": [<int>, ...]
}
```

Future meetings, deadlines, or scheduled events.

### sentiment_indicators

```
{
  "turn_id": "<turn id>",
  "speaker": "<speaker>",
  "sentiment": "disagreement|concern|strong_endorsement|uncertainty|frustration",
  "text_preview": "<first 100 chars of the turn>",
  "source_quote": "...", "quote_offset_normalized": <int>,
  "source_turn_ids": [<int>, ...]
}
```

Extract every clear expression of disagreement, concern, strong
endorsement, uncertainty, or frustration. Be comprehensive — the
comparison engine filters.

### meeting_phases

```
{
  "phase_id": "<short slug>",
  "phase_name": "opening|working_session|q_and_a|wrap_up|other",
  "start_turn_id": "<turn id>",
  "end_turn_id": "<turn id>",
  "summary": "<short summary or null>",
  "source_turn_ids": [<int>, ...]
}
```

Segment the meeting into its high-level phases in order.

### issue_registry_entry

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
  "source_quote": "...", "quote_offset_normalized": <int>,
  "source_turn_ids": [<int>, ...]
}
```

Substantive technical or policy problems being worked across multiple
meetings.

### position_statement

```
{
  "position_id": "<short slug>",
  "agency": "<agency>",
  "speaker": "<speaker>",
  "topic": "<topic>",
  "position_text": "<verbatim position text>",
  "position_type": "support|opposition|conditional|neutral|unclear",
  "caveats": "<caveats or null>",
  "source_quote": "...", "quote_offset_normalized": <int>,
  "source_turn_ids": [<int>, ...]
}
```

Stated stances on a topic that may persist or evolve across meetings.

### dissent_or_objection

```
{
  "dissent_id": "<short slug>",
  "objector": "<objector>",
  "agency": "<agency>",
  "objection_text": "<verbatim objection text>",
  "objection_topic": "<topic>",
  "resolution": "<resolution or null>",
  "resolved": true|false,
  "source_quote": "...", "quote_offset_normalized": <int>,
  "source_turn_ids": [<int>, ...]
}
```

Explicit on-the-record objections or registered disagreement.

### agenda_item

```
{
  "item_id": "<short slug>",
  "item_number": "<item number, e.g. 'Agenda Item 3'>",
  "title": "<title>",
  "presenter": "<presenter>",
  "allocated_minutes": <int or null>,
  "start_turn_id": "<turn id>",
  "end_turn_id": "<turn id>",
  "outcome": "<outcome or null>",
  "source_turn_ids": [<int>, ...]
}
```

Formal agenda structure recoverable from the transcript.

### precedent_reference

```
{
  "ref_id": "<short slug>",
  "speaker": "<speaker>",
  "reference_text": "<the reference text>",
  "referenced_meeting_date": "<ISO date or null>",
  "referenced_decision_or_study": "<what was referenced>",
  "purpose": "justification|contrast|correction|context|unknown",
  "source_quote": "...", "quote_offset_normalized": <int>,
  "source_turn_ids": [<int>, ...]
}
```

References to prior meetings, decisions, or studies used to justify a
current position.

### external_stakeholder_input

```
{
  "input_id": "<short slug>",
  "stakeholder": "<stakeholder>",
  "relayed_by": "<speaker>",
  "input_text": "<verbatim relayed input>",
  "input_type": "industry_comment|itu_submission|congressional_direction|agency_guidance|public_comment|other",
  "document_reference": "<document or null>",
  "source_quote": "...", "quote_offset_normalized": <int>,
  "source_turn_ids": [<int>, ...]
}
```

Input relayed from parties not in the room.

### glossary_definition

```
{
  "definition_id": "<short slug>",
  "term": "<term>",
  "definition": "<the definition>",
  "defined_by": "<speaker>",
  "context": "<context>",
  "authoritative": true|false,
  "source_quote": "...", "quote_offset_normalized": <int>,
  "source_turn_ids": [<int>, ...]
}
```

Terms formally defined or clarified for the study.

### procedural_ruling

```
{
  "ruling_id": "<short slug>",
  "ruling_text": "<verbatim ruling text>",
  "ruled_by": "<chair or co-lead>",
  "ruling_type": "scope_boundary|process_rule|meeting_procedure|participation_rule|classification_handling|other",
  "binding": true|false,
  "source_quote": "...", "quote_offset_normalized": <int>,
  "source_turn_ids": [<int>, ...]
}
```

Chair or co-lead rulings on meeting procedure, scope, or process.

### grounding

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

Return the JSON object now.
