---
artifact_id: e9b5d267-3a1c-4f8e-b723-5c0a2d4f8b91
artifact_type: judgment_record
schema_version: "1.0.0"
created_at: 2026-05-13T11:33:00Z
judgment_type: evidence_sufficiency
question: "Can SKL trace extraction begin before pre-conditions are met?"
outcome: block
confidence: 0.98
canonical_json_path: docs/decisions/2026-05-13-skl-sequencing.judgment_record.json
canonical: false
---
## Question

Can SKL trace extraction begin before pre-conditions are met?

## Outcome

- Selected outcome: **block**
- Confidence: 0.98
- Human review required: true

## Rationale

SKL trace extraction has five explicit hard pre-conditions: all cleanup work complete, shared contract module stable, skill_proposal_artifact schema merged through the contract module, SKL autonomy policy artifact authored and active, and both EVAL-SKL-001 and EVAL-SKL-002 passing. None are currently met. Starting SKL trace extraction before these are met would either violate governing constraints or stall at the first gate. The correct sequencing is: pre-conditions first, then SKL extraction agent. SKL-J runs in parallel throughout and is not blocked by any of these.

## Alternatives Rejected

### Begin SKL trace extraction work now and complete it when pre-conditions are met

The skill_proposal_artifact schema cannot be finalised until the contract module is stable. Beginning extraction agent work before the schema is locked creates rework risk. Schema first, then extraction agent.

### Waive the autonomy policy artifact requirement given the human approval gate

Human approval is necessary but not sufficient. The policy artifact defines what SKL is permitted to read, propose, and not propose — without it, the boundary is undefined and unenforceable.

## Assumptions

- **Assumption:** All pre-condition work can be completed sequentially before SKL trace extraction begins. **Falsification signal:** A pre-condition is discovered to be unresolvable, blocking SKL trace extraction indefinitely.
- **Assumption:** The shared contract module in its post-cleanup state will be certifiable without additional rework. **Falsification signal:** The schema PR fails certification due to issues in the contract module not covered by the known cleanup items.

## Consequences

### Expected positive

- SKL trace extraction begins on a clean, fully compliant foundation
- No rework risk from starting on an unstable contract module
- Sequencing is explicit and auditable — this judgment record blocks future attempts to skip pre-conditions

### Expected negative

- SKL trace extraction cannot start until all five pre-conditions are met
- Five sequential pre-conditions means trace extraction is several PRs away

### Monitoring plan

Track pre-condition completion status. Once all five are met, re-evaluate explicitly before beginning schema PR work. This judgment record should be cited in the skill_proposal_artifact schema PR description as the sequencing authority.

## Open Questions

- Exact timeline for pre-condition completion.
- Whether additional cleanup items will be discovered that are not covered by the known set.

## Claims Considered

- **CLAIM-001** (materiality: high): A new artifact schema must go through the shared contract module, which requires the module to be stable and certified first.
- **CLAIM-002** (materiality: high): An autonomy policy artifact must be authored and active before the SKL extraction agent is enabled.
- **CLAIM-003** (materiality: high): EVAL-SKL-001 and EVAL-SKL-002 must both pass before the SKL extraction agent is enabled.
- **CLAIM-004** (materiality: high): SKL-J has zero dependencies on any of the five pre-conditions and can run in parallel throughout.

## Rules Applied

- SSC-DESIGN-001 section 12: every new module must make the system safer, more measurable, or more trustworthy
- fail-closed: do not start work that cannot complete without violating governing constraints
- autonomy-expansion-gate: autonomy increases require policy constraints and eval coverage before enablement

## Traceability

- artifact_id: `e9b5d267-3a1c-4f8e-b723-5c0a2d4f8b91`
- created_at: 2026-05-13T11:33:00Z
- judgment_id: `e9b5d267-3a1c-4f8e-b723-5c0a2d4f8b91`
