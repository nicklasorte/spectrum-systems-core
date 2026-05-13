---
artifact_id: d4a7e923-8f2b-4c1d-b645-9e3a1f7d0b58
artifact_type: judgment_record
schema_version: "1.0.0"
created_at: 2026-05-13T12:00:00Z
judgment_type: risk_assessment
question: "Do the four production readiness cleanup items from the predecessor spectrum-systems repo apply to spectrum-systems-core?"
outcome: block
confidence: 0.99
canonical_json_path: docs/decisions/2026-05-13-stale-memory-finding.judgment_record.json
canonical: false
---
## Question

Do the four production readiness cleanup items from the predecessor spectrum-systems repo apply to spectrum-systems-core?

## Outcome

- Selected outcome: **block**
- Confidence: 0.99
- Human review required: true

## Rationale

A diagnostic session on 2026-05-13 revealed that memory carried forward from the predecessor spectrum-systems repo contained four cleanup items scoped to TypeScript files and subsystems that do not exist in spectrum-systems-core. Attempting to run a Claude Code prompt against these items would have produced either silent no-ops (files not found) or incorrect behavior (inventing files to match the prompt). The correct action was to read the actual repo — CLAUDE.md, the constitution, the PR list — before generating any fix prompt. The actual repo state: 0 open PRs, 60 closed, 100% Python, clean architecture. No cleanup items apply.

## Alternatives Rejected

### Run the cleanup prompt anyway and let Claude Code fail on missing files

Fail-closed means halt on stale information, not proceed and observe the failure. Running a prompt known to be based on incorrect assumptions wastes a Claude Code session and risks Claude Code inventing plausible-looking fixes to non-existent problems.

### Update memory to reflect the correct repo state and continue without capturing this finding

The stale memory finding is itself a significant architectural decision: it establishes that repo state must be read directly before any Claude Code prompt is generated, not inferred from memory. That principle is worth capturing permanently.

## Assumptions

- **Assumption:** All future Claude Code prompts targeting spectrum-systems-core will read CLAUDE.md and the constitution before generating any fix or feature prompt. **Falsification signal:** A Claude Code prompt references TypeScript files, AEX/PQX/EVL/TPA/CDE/SEL terminology, or the closed spectrum-systems repo.
- **Assumption:** Memory summaries will be updated to remove stale references to the closed repo's cleanup items. **Falsification signal:** A future session generates a prompt based on the four stale cleanup items without first reading the actual repo.

## Consequences

### Expected positive

- Establishes a permanent record that the four cleanup items from the old repo do not apply here
- Establishes the principle: read the actual repo before generating any prompt
- Prevents future sessions from wasting time on non-existent cleanup items

### Expected negative

- None — this is a pure information artifact with no implementation cost

### Monitoring plan

If any future Claude Code prompt references pipeline-connector.ts, artifact_kind migration, TLC routing rules, or AEX/PQX/EVL terminology in the context of spectrum-systems-core, this judgment record has not been read. That is a retrieval failure.

## Open Questions

- Whether there are other stale memory items from the predecessor repo that have not yet been identified and corrected.

## Claims Considered

- **CLAIM-001** (materiality: high): The four cleanup items (pipeline-connector.ts artifact_kind remnants, MVP-3 eval_summary migration, TLC routing rule corrections, integration/e2e test rewrites) reference TypeScript files and subsystem terminology that do not exist in spectrum-systems-core.
- **CLAIM-002** (materiality: high): spectrum-systems-core is 100% Python. There is no pipeline-connector.ts, no TLC subsystem, no AEX/PQX/EVL/TPA/CDE/SEL terminology. The constitution explicitly rejects these.
- **CLAIM-003** (materiality: high): The predecessor spectrum-systems repo is closed. Its cleanup items are not inherited by spectrum-systems-core — the constitution states the old repo is a quarry, not a blueprint.
- **CLAIM-004** (materiality: high): Memory summaries carried forward from the old repo contained stale references to closed PRs, TypeScript modules, and governance subsystems that no longer exist.

## Rules Applied

- SSC-DESIGN-001 section 11: the original spectrum-systems repo is a quarry, not a blueprint
- SSC-DESIGN-001 section 3: reject AEX/PQX/EVL/TPA/CDE/SEL terminology
- fail-closed: acting on stale information is equivalent to acting on missing information — halt and verify

## Traceability

- artifact_id: `d4a7e923-8f2b-4c1d-b645-9e3a1f7d0b58`
- created_at: 2026-05-13T12:00:00Z
- judgment_id: `d4a7e923-8f2b-4c1d-b645-9e3a1f7d0b58`
