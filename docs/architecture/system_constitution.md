# Spectrum Systems Core — System Constitution

Document ID: SSC-DESIGN-001
Status: Binding
Scope: Spectrum Systems Core (this repository)

---

## 1. Purpose

Spectrum Systems Core is a small governed artifact engine for producing trusted spectrum-work artifacts.

It exists to take a real input (such as a meeting transcript), pass it through a defined workflow, evaluate the result, make a control decision, and promote the artifact only if that decision allows it. Everything else in the system is in service of that loop.

This document is the constitution for the rebuild. It is binding. Other documents must defer to it.

---

## 2. Core Product Goal

The first product goal is one end-to-end path:

Transcript or text input → context bundle → meeting minutes artifact → eval results → control decision → promoted artifact.

If the system can do this reliably, deterministically, and with fail-closed control, the architecture has earned the right to grow. If it cannot, no further modules are justified.

---

## 3. Non-Goals

The following are explicitly out of scope for the initial system:

- No large acronym architecture.
- No autonomous agents.
- No live model calls initially.
- No dashboard.
- No multi-repo ecosystem.
- No complex certification gate.
- No giant PR readiness maze.

These may be revisited later, but only after the core loop is real and useful.

---

## 4. Core Loop

The system has one loop:

Produce → Evaluate → Decide → Promote

- Produce: a workflow generates an artifact from inputs.
- Evaluate: required evals run against the artifact.
- Decide: a control function reads eval results and emits a decision.
- Promote: an artifact moves to promoted status only on an allow decision.

Every module must serve this loop or be deferred.

---

## 5. Core Modules

The system has only the following modules. Names are deliberately plain.

Active in the first slice:

- artifacts
- context
- workflows
- evals
- control
- promotion

Deferred (named so they have a home, but not built yet):

- failure_learning, deferred
- ai_adapter, deferred

No other top-level modules are introduced without amending this document.

---

## 6. Artifact Model

There is one artifact envelope. All artifact types share it.

Fields:

- artifact_id: stable unique id
- artifact_type: e.g. "meeting_minutes"
- schema_version: integer; payload must conform
- status: one of draft, evaluated, promoted, rejected
- created_at: timestamp
- trace_id: id linking artifact to its production run
- input_refs: references to inputs used (transcript id, source text id, etc.)
- content_hash: hash of payload for integrity and replay
- payload: the typed content for this artifact_type

Artifacts are the system of record. State changes happen by writing new envelopes or updating status fields, not by editing payload in place.

---

## 7. Control Semantics

Allowed decisions:

- allow
- warn
- freeze
- block

Rules:

- Missing required evals block.
- Failed required evals block.
- Valid required evals allow.
- warn and freeze are reserved for later extensions and are not used in the first slice.
- Model output never decides. Decisions come from the control function reading eval results.

Control is external to model execution. An artifact that has not been allowed has not been promoted.

---

## 8. Promotion Semantics

Promotion is the act of moving an artifact from draft or evaluated to promoted.

Rules:

- Promotion requires a control decision of allow.
- No allow, no promotion.
- A promoted artifact is the trusted form. Downstream consumers should read promoted artifacts only.
- Rejection (on block) is a terminal state for that artifact instance; a new run produces a new artifact.

---

## 9. Testing Philosophy

Tests must prove useful trust properties. Tests must not exist for ceremony.

Categories:

- Unit tests: small functions, pure logic (artifact construction, hashing, decision rules).
- Contract tests: payloads conform to declared schema_version for their artifact_type.
- Golden workflow tests: a known input produces a known artifact and a known decision end to end.
- Fail-closed tests: missing required evals block; failed required evals block; only allow leads to promotion.

If a test does not defend one of these properties, it should not be added.

---

## 10. Self-Improvement / Governed Learning

This loop is deferred but reserved:

failure → failure_record → eval_case_candidate → reviewed eval_case → regression suite

When a real failure occurs, it is recorded. The record can become a candidate eval case. A human review turns it into an eval case. That eval case enters the regression suite so the same failure cannot pass again.

Human corrections are the source of new evals. Models do not promote their own corrections into the trust system.

---

## 11. Relationship to Original spectrum-systems Repo

The original spectrum-systems repository is a quarry, not a blueprint.

- Reuse: ideas, schemas, reason codes, failure lessons, and any clear lessons about where trust broke.
- Do not reuse: the acronym-heavy subsystem map, governance ceremony, or PR-readiness machinery.
- Specifically reject in the new core: AEX, PQX, EVL, TPA, CDE, SEL terminology. The new core uses plain module names.

The old repo informs; it does not bind.

---

## 12. Design Rules

These rules apply to every change to this system:

- Every new module must make the system safer, more measurable, or more trustworthy.
- Prefer one artifact envelope over many artifact families.
- Prefer one control model over many authority systems.
- Prefer product workflow value over repo-governance ceremony.
- If a feature does not help the first useful artifact workflow, defer it.

A change that does not satisfy these rules is rejected by default.

---

## 13. First Implementation Slice

SSC-001 is the first slice. It implements the smallest end-to-end path that exercises the core loop.

Scope of SSC-001:

- Initialize the Python package.
- Artifact model (the envelope from section 6).
- In-memory artifact store.
- Context bundle builder.
- Basic eval runner.
- Control decision function.
- Promotion function.
- Deterministic meeting-minutes workflow (no live model calls).
- Tests covering unit, contract, golden workflow, and fail-closed properties.

SSC-001 is complete when a deterministic meeting-minutes input produces a promoted artifact end to end, and when missing or failed required evals demonstrably prevent promotion.

---

## Implementation Implications for SSC-001

- One Python package with modules named exactly: artifacts, context, workflows, evals, control, promotion. No other top-level modules in this slice.
- One artifact envelope class. The meeting_minutes payload is one schema; do not generalize prematurely.
- The artifact store is in-memory only. No database, no filesystem persistence layer in SSC-001.
- The meeting-minutes workflow is deterministic: given the same input, it produces the same payload and the same content_hash. No external model calls.
- The eval runner runs a fixed list of required evals against the artifact and returns structured results.
- The control function is a pure function: eval results in, decision out. It blocks on missing or failed required evals; it allows only when all required evals pass.
- Promotion is a single function call gated on an allow decision. There is no other path to promoted status.
- Tests required before SSC-001 is considered done: artifact envelope unit tests, meeting_minutes contract test, one golden workflow test (input → promoted artifact), and fail-closed tests for both missing-required-evals and failed-required-evals cases.
- No dashboard, no agent loop, no failure_learning, no ai_adapter implementation in SSC-001. Those modules remain deferred.
