# spectrum-systems-core

A small governed artifact engine. Take a real input (such as a meeting
transcript), pass it through a defined workflow, evaluate the result, make
a control decision, and promote the artifact only if that decision allows
it.

## Core loop

```
Produce -> Evaluate -> Decide -> Promote
```

- **Produce**: a workflow generates an artifact from inputs.
- **Evaluate**: required evals run against the artifact.
- **Decide**: a control function reads eval results and emits a decision.
- **Promote**: an artifact moves to `promoted` only on an `allow` decision.

The constitution that binds this repo is in
`docs/architecture/system_constitution.md`.

## First MVP (SSC-001)

Deterministic text/transcript -> promoted `meeting_minutes` artifact.

```python
from spectrum_systems_core.workflows import run_meeting_minutes_workflow

result = run_meeting_minutes_workflow("""
Quarterly planning sync
DECISION: Approve Q3 roadmap.
ACTION: Draft SSC-002 scope.
QUESTION: Do we need an empty-transcript eval?
""")

assert result.promoted
assert result.meeting_minutes.status == "promoted"
```

## Modules

Active in this slice:

- `artifacts` — one envelope, in-memory store, status validation.
- `context` — context bundle builder.
- `workflows` — deterministic meeting-minutes workflow.
- `evals` — required eval runner producing `eval_result` artifacts.
- `control` — pure decision function (`allow` / `block`).
- `promotion` — gates `promoted` status on `allow`.

## Deferred

Live model calls, autonomous agents, dashboards, failure-learning,
ai_adapter, certification gates, PR-readiness systems, and persistence
layers are deliberately out of scope until the core loop is real and
useful. They are reserved by the constitution but not built here.

## Develop

```
pip install -e ".[dev]"
pytest
```
