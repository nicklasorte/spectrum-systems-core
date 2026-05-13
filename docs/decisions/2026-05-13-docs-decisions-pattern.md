---
artifact_id: f2d8b341-6c9a-4e7f-a523-8b1d0e5c9f27
artifact_type: judgment_record
schema_version: "1.0.0"
created_at: 2026-05-13T12:00:00Z
judgment_type: risk_assessment
question: "Should judgment records be written through the core Produce-Evaluate-Decide-Promote loop or stored as governed docs outside the loop?"
outcome: approve
confidence: 0.97
canonical_json_path: docs/decisions/2026-05-13-docs-decisions-pattern.judgment_record.json
canonical: false
---
## Question

Should judgment records be written through the core Produce-Evaluate-Decide-Promote loop or stored as governed docs outside the loop?

## Outcome

- Selected outcome: **approve**
- Confidence: 0.97
- Human review required: true

## Rationale

Judgment records serve institutional memory, not artifact production. The core loop's trust guarantees (eval gates, control decisions, promotion semantics) apply to artifacts derived from transcripts that need independent verification before being trusted. Judgment records are authored directly by the engineer who made the decision — they need git provenance and version control, not eval gates. Storing them in docs/ alongside the constitution and contracts places them in the correct authority tier: binding reference documents, human-authored, version-controlled, permanently traceable. No constitution amendment required. No loop involvement. No ceremony.

## Alternatives Rejected

### Write judgment records through the core loop as a new artifact type

Would require constitution amendment (section 5), evals runner update (REQUIRED_FIELDS_BY_TYPE), and data lake contract update. The trust gained — an eval gate on a human-authored document — is zero. The overhead is high. The constitution itself prohibits this pattern.

### Store judgment records in Obsidian only, outside the repo

Obsidian is an output layer and human interface — not a source of truth. Files there have no git provenance, no version history, and are not readable by Claude Code sessions. The repo is the correct location for documents that future sessions must reference.

### Store judgment records as plain Markdown only, no JSON

Plain Markdown is not machine-readable in a structured way. The JSON artifact enables future tooling to query judgment records programmatically — by judgment_type, selected_outcome, or confidence. The Markdown companion satisfies the human-readable requirement.

## Assumptions

- **Assumption:** docs/decisions/ as a location is consistent with the constitution's docs/ pattern and requires no amendment to SSC-DESIGN-001. **Falsification signal:** A constitution review determines that docs/decisions/ requires a formal amendment before use.
- **Assumption:** Claude Code sessions will read docs/decisions/ when CLAUDE.md instructs them to do so. **Falsification signal:** Future Claude Code PRs propose architectural changes that contradict existing judgment records — indicating the retrieval instruction is not being followed.

## Consequences

### Expected positive

- Zero constitution amendments required
- Zero loop involvement — no eval or control overhead
- Judgment records are in the same authority tier as the constitution and contracts
- Future Claude Code sessions can read docs/decisions/ as binding context before architectural changes
- Git history provides full provenance without additional tooling

### Expected negative

- Judgment records are not subject to the eval gate — their quality depends on the author's judgment
- No automated enforcement that future sessions read docs/decisions/ — relies on CLAUDE.md instruction

### Monitoring plan

Track whether Claude Code PRs cite judgment record artifact_ids in descriptions when making architectural changes. Zero citations after 5 PRs indicates the CLAUDE.md retrieval instruction is not working.

## Open Questions

- Whether a future slice should add a preflight check that reads docs/decisions/ and emits a finding if a proposed change contradicts an existing judgment record.

## Claims Considered

- **CLAIM-001** (materiality: high): The core loop exists to produce trusted spectrum-work artifacts from transcripts. Judgment records are not derived from transcripts — they are authored by a human reflecting on architectural decisions.
- **CLAIM-002** (materiality: high): Running judgment records through evals and the control function would be ceremony with no trust benefit — the author is the reviewer.
- **CLAIM-003** (materiality: high): The constitution (section 12) warns explicitly against repo-governance ceremony that does not serve the core loop.
- **CLAIM-004** (materiality: high): docs/ already holds the most authoritative files in the repo: the constitution and the data lake contract. Judgment records belong in the same tier.
- **CLAIM-005** (materiality: high): Adding judgment records to the core loop would require a constitution amendment, an evals runner update, and a data lake contract update — significant overhead for zero trust gain.

## Rules Applied

- SSC-DESIGN-001 section 12: prefer product workflow value over repo-governance ceremony
- SSC-DESIGN-001 section 3: no new top-level modules without amending the constitution
- SSC-DESIGN-001 section 5: adding a module requires amending the constitution

## Traceability

- artifact_id: `f2d8b341-6c9a-4e7f-a523-8b1d0e5c9f27`
- created_at: 2026-05-13T12:00:00Z
- judgment_id: `f2d8b341-6c9a-4e7f-a523-8b1d0e5c9f27`
