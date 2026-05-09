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

## SSC-002: more artifact types through the same loop

Three more artifact types now run through the same envelope, the same
control function, and the same promotion gate. No new modules.

| artifact_type             | required payload fields                                               |
| ------------------------- | --------------------------------------------------------------------- |
| `meeting_minutes`         | title, summary, decisions, action_items, open_questions               |
| `decision_brief`          | title, context, options, recommendation, rationale                    |
| `agency_question_summary` | title, agency, question, summary, citations                           |
| `meeting_action_log`      | title, meeting_ref, actions, open_count                               |

The shared loop lives in `workflows/_loop.py` (`run_governed_loop`):
build context bundle → produce target artifact → run required evals →
decide control → promote if allowed. Each workflow file only supplies
its `artifact_type` and a deterministic `extract` function.

```python
from spectrum_systems_core.workflows import (
    run_decision_brief_workflow,
    run_agency_question_summary_workflow,
    run_meeting_action_log_workflow,
)

brief = run_decision_brief_workflow("""
Adopt SSC-002 second artifact type
CONTEXT: Constitution requires one envelope and one control model.
OPTION: Add decision_brief alongside meeting_minutes.
RECOMMENDATION: Add decision_brief first.
RATIONALE: Validates generality before introducing I/O complexity.
""")
assert brief.promoted

inquiry = run_agency_question_summary_workflow("""
FCC inquiry on band plan
AGENCY: FCC
QUESTION: What is the proposed sharing rule for 3.5 GHz?
CITATION: 47 CFR 96.41
""")
assert inquiry.promoted

log = run_meeting_action_log_workflow("""
Q3 planning action log
MEETING_REF: meeting-2026-05-09
ACTION: Owner Alice ships SSC-002 docs
""")
assert log.promoted
```

## Modules

Active core (unchanged since SSC-002):

- `artifacts` — one envelope, in-memory store, status validation.
- `context` — context bundle builder.
- `workflows` — deterministic extractor workflows.
- `evals` — required eval runner producing `eval_result` artifacts.
- `control` — pure decision function (`allow` / `block`).
- `promotion` — gates `promoted` status on `allow`.

Added by SSC-003 through SSC-018:

- `data_lake` — read raw transcripts, write promoted artifacts, build a
  deterministic JSONL index, and answer plain filter queries. The data
  lake is a directory tree on disk; this module is its only producer.

The data lake's binding layout and rules live in
`docs/contracts/data_lake_contract.md`.

## Transcript pipeline

```python
from spectrum_systems_core.data_lake import run_transcript_pipeline, query

# Given a layout under /lake/raw/meetings/<meeting_id>/transcript.txt and metadata.json:
result = run_transcript_pipeline(
    lake_root="/lake",
    meeting_id="m-2026-05-09-q3",
    workflow_name="meeting_minutes",
)
assert result.promoted

# Cross-meeting query (no vector search, no embeddings):
hits = query("/lake", agency="FCC", artifact_type="meeting_minutes")
```

## Quickstart: one command, one meeting

`spectrum-core process-meeting` runs all four supported workflows over a
single meeting in the data lake and writes both governed JSON artifacts
and human-readable Markdown views.

### Expected data lake layout

```
<lake_root>/
  raw/meetings/<meeting_id>/transcript.txt
  raw/meetings/<meeting_id>/metadata.json
```

`metadata.json` is a single JSON object. Required fields:
`meeting_id`, `title`, `date` (`YYYY-MM-DD`), `source_type` (one of
`transcript`, `notes`, `summary`). The `meeting_id` value must equal the
directory name.

### Run it

```bash
pip install -e ".[dev]"
spectrum-core process-meeting --lake /path/to/lake --meeting-id m-2026-05-09-q3
```

`--workflow <name>` is repeatable to run a subset (e.g.
`--workflow meeting_minutes`).

### Where outputs appear

```
<lake_root>/processed/meetings/<meeting_id>/
  meeting_minutes__<slug>.json            # canonical promoted artifacts
  meeting_action_log__<slug>.json
  agency_question_summary__<slug>.json
  decision_brief__<slug>.json
  manifest__<run_id>.json                 # one per workflow run
  debug__<run_id>.json
  markdown/                               # human-readable views
    meeting_minutes.md
    meeting_action_log.md
    agency_question_summary.md
    decision_brief.md
    index.md
```

JSON is the canonical, governed artifact. Markdown is a regenerated
view: it never feeds back into the loop, and editing it does not change
any artifact. The Markdown contract is in §6.3 of
`docs/contracts/data_lake_contract.md`.

A workflow whose extractor finds nothing in the transcript is blocked
fail-closed. The index Markdown lists blocked workflows alongside the
reason code and a one-line plain-English explanation.

## Deferred

Live model calls, autonomous agents, dashboards, vector indexes,
embeddings, semantic search, ai_adapter, certification gates,
PR-readiness systems, and remote persistence are deliberately out of
scope. They are reserved by the constitution but not built here.

## Develop

```
pip install -e ".[dev]"
pytest
```
