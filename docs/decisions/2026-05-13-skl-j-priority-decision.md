---
artifact_id: c7e3a124-5f8d-4b2e-8a91-3d6f0c9b2e45
artifact_type: judgment_record
schema_version: "1.0.0"
created_at: 2026-05-13T11:33:00Z
judgment_type: evidence_sufficiency
question: "Should judgment capture from chat be prioritised over trace extraction as the first SKL capability?"
outcome: approve
confidence: 0.92
canonical_json_path: docs/decisions/2026-05-13-skl-j-priority-decision.judgment_record.json
canonical: false
---
## Question

Should judgment capture from chat be prioritised over trace extraction as the first SKL capability?

## Outcome

- Selected outcome: **approve**
- Confidence: 0.92
- Human review required: true

## Rationale

SKL-J addresses the highest-value gap in Spectrum Systems at the lowest implementation complexity. The reasoning produced in chat sessions — constraint resolutions, rejected alternatives, sequencing rationale, red team findings — currently evaporates between sessions. SKL-J writes governed JSON records to docs/decisions/, following the same pattern as the constitution and contracts. It requires no new modules, no loop involvement, and no constitution change. Trace extraction, while valuable, requires five pre-conditions not yet met and addresses a lower-value gap (what was built) compared to SKL-J (why it was built that way).

## Alternatives Rejected

### Build SKL trace extraction first as originally designed

Five hard pre-conditions block it. Starting it now would either violate governing constraints or stall immediately. SKL-J can ship today.

### Build both SKL-J and trace extraction in parallel

Trace extraction is blocked on pre-conditions not yet met. Parallel development would create idle work. Sequential is correct: SKL-J now, trace extraction after pre-conditions are met.

### Skip SKL-J and use manual notes in Obsidian instead

Manual notes in Obsidian are not governed artifacts. They have no schema, no provenance, and no git traceability. SKL-J produces versioned JSON records that are part of the repo's permanent record.

## Assumptions

- **Assumption:** docs/decisions/ as a location for judgment records is consistent with the constitution's docs/ pattern and requires no amendment. **Falsification signal:** A constitution review determines that docs/decisions/ requires a formal amendment to SSC-DESIGN-001 before use.
- **Assumption:** Future Claude Code sessions will read docs/decisions/ as context when planning related work. **Falsification signal:** Claude Code PRs continue to re-litigate decisions already captured in judgment records — indicating the retrieval loop is not closed.

## Consequences

### Expected positive

- Reasoning from today's session is permanently captured before any implementation begins
- SKL-J establishes the capture workflow that will be used for all future significant decisions
- Judgment records become queryable context for future Claude Code sessions
- The feedback path from decisions to roadmap revision begins to close

### Expected negative

- SKL trace extraction remains blocked until pre-conditions are met
- SKL-J requires discipline to invoke — capture only happens when explicitly triggered

### Monitoring plan

Track judgment record count per sprint. If count is zero for two consecutive sprints, the capture habit has broken down. Track whether Claude Code PRs cite judgment record artifact_ids in their descriptions — this is the retrieval rate signal.

## Open Questions

- Whether Claude Code will automatically discover and read docs/decisions/ as context without explicit instruction in CLAUDE.md — may need a CLAUDE.md update to enforce retrieval.
- Whether judgment records should also be uploaded to claude.ai project knowledge to close the chat-side retrieval loop.

## Claims Considered

- **CLAIM-001** (materiality: high): The highest-value artifact produced by Spectrum Systems development is the reasoning behind decisions, not the decisions themselves.
- **CLAIM-002** (materiality: high): SKL-J requires no new modules, no constitution amendment, and no eval cases to start — it writes to docs/decisions/ following the existing docs/ pattern.
- **CLAIM-003** (materiality: high): SKL trace extraction has five hard pre-conditions, none of which are currently met. SKL-J has zero blocking dependencies.
- **CLAIM-004** (materiality: medium): Judgment artifacts captured today become queryable context for future Claude Code sessions reading the repo.

## Rules Applied

- SSC-DESIGN-001 section 12: prefer product workflow value over repo-governance ceremony
- SSC-DESIGN-001 section 3: no new top-level modules without amending the constitution
- fail-closed: do not start work that will immediately stall on unmet pre-conditions

## Traceability

- artifact_id: `c7e3a124-5f8d-4b2e-8a91-3d6f0c9b2e45`
- created_at: 2026-05-13T11:33:00Z
- judgment_id: `c7e3a124-5f8d-4b2e-8a91-3d6f0c9b2e45`
