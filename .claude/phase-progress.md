## Phase J — Discovery Note (Step 1, halted)

Multiple stop conditions from the Phase J task spec are triggered before any
implementation work could begin. Per the task, I am stopping and surfacing
to the user instead of proceeding or pivoting.

### 1. Hard conflict between CLAUDE.md and the Phase J task (task rule: surface and stop)

The task says: *"Read CLAUDE.md and AGENTS.md FIRST. If conventions there conflict
with anything below, surface the conflict and stop. Do not silently pick one."*

`CLAUDE.md` (binding per its own text, deferring to
`docs/architecture/system_constitution.md`) says:

- "Top-level module names are fixed: `artifacts`, `context`, `workflows`, `evals`,
  `control`, `promotion`, `data_lake`. Adding a new top-level module requires
  amending the constitution."
- "live model calls, autonomous agents, dashboards, vector indexes, embeddings,
  semantic search, **certification gates**, and remote persistence. Do not add them."
- The system loop is **Produce → Evaluate → Decide → Promote** over
  meetings/transcripts (workflows: `meeting_minutes`, `decision_brief`,
  `agency_question_summary`, `meeting_action_log`). No papers, no AI, no
  governance dashboards, no harness.

The Phase J task asks me to:

- Work inside `spectrum_systems_core/paper/` (not in the fixed module list).
- Add `publication_metadata.status` with enum values including
  `ready_for_certification` and `certified` — i.e. a certification gate, which
  CLAUDE.md explicitly forbids.
- Reference Phase I scanners, AIAdapter, harness memory, governance dashboard,
  etc. — all of which CLAUDE.md says do not exist / must not exist.

Meanwhile, the **actual repo state** matches the task, not CLAUDE.md:

- `git log` shows 18 merged PRs (Phases A–I) that already added
  `paper/`, `ai/`, `governance/`, `harness/`, `ingestion/`, `synthesis/`,
  `agency/`, `extraction/` modules.
- Schemas under `contracts/schemas/paper/`, `contracts/schemas/ai/`,
  `contracts/schemas/governance/`, `contracts/schemas/harness/` exist.
- `governance_evals.json` exists.

So CLAUDE.md is **stale** relative to the codebase. The task description
appears correct about repo reality but contradicts the only document the
repo declares binding. Per the task's own opening rule, I do not get to
silently pick one. **User decision required.**

### 2. `AGENTS.md` is missing

Task says read both `CLAUDE.md` and `AGENTS.md`. Repo root contains no
`AGENTS.md`, no `agents.md`, no equivalent.

### 3. Stop condition: `contracts/schemas/paper/revised_draft.schema.json` is missing

Task spec, "Stop conditions":
> `contracts/schemas/paper/revised_draft.schema.json` is missing or does not
> include `title`, `abstract`, and `sections` fields → stop, write discovery
> note to `.claude/phase-progress.md`, surface to user.

`ls contracts/schemas/paper/` returns:

```
assumption_record.schema.json
evidence_record.schema.json
issue_record.schema.json
revision_diff.schema.json
revision_instruction.schema.json
technical_claim.schema.json
unstructured_comment_warning.schema.json
```

No `revised_draft.schema.json`. The `paper/` source module also has no
producer that writes `paper/revised_draft.json` — the closest files are
`revision_workflow.py`, `revision_generator.py`, `revision_eval.py`, none
of which match the Phase D output the task assumes.

Without that schema and that producer, Step 4 of the roadmap (read
`paper/revised_draft.json`, validate against input schema, transform) has
no input contract and no real input.

### 4. Cannot establish the required test baseline of 659

Task spec, "Stop conditions":
> Test baseline going in is not 659 → stop, surface to user (repo state
> differs from expected).

`python -m pytest --collect-only` fails with `No module named pytest`. The
dev environment is not installed (would need `pip install -e ".[dev]"`),
which I am not running unprompted because the Python guardrails section
warns about silent installs and the tool permission policy puts new
network/install actions in "ask first".

### Inventory captured before halt

- Dependency manager: `pyproject.toml` present at repo root.
- Python version pin: not yet read (CI says Python 3.11 per CLAUDE.md).
- Branch: `claude/publication-formatter-EQlHd` (matches task instructions).
- Last commit: `c1c61d8` Merge PR #20 (Phase I governance audit).
- `src/spectrum_systems_core/` modules present:
  agency, ai, artifacts, cli.py, context, control, data_lake, evals,
  extraction, governance, harness, ingestion, obsidian_bridge, paper,
  promotion, synthesis, utils, workflows.
- `src/spectrum_systems_core/paper/` files: `_paths.py`,
  `assumption_extractor.py`, `claim_eval.py`, `claim_extractor.py`,
  `comment_processor.py`, `contradiction_detector.py`,
  `evidence_builder.py`, `evidence_eval.py`, `issue_eval.py`,
  `issue_registry.py`, `revision_eval.py`, `revision_generator.py`,
  `revision_workflow.py`. No `publication_formatter.py` (expected, since
  Phase J would create it).

### Decisions needed from user before I can proceed

1. **CLAUDE.md vs Phase J task — which is authoritative?**
   - Option A: CLAUDE.md is stale; treat the Phase A–I PRs as the real
     ground truth and proceed with Phase J. Update CLAUDE.md as part of
     the PR (or in a separate PR first).
   - Option B: CLAUDE.md is binding; Phase J as specified violates the
     constitution and should not be implemented. The certification-gate
     status enum and the new `paper/` module both fail the constitution
     test.
   - Option C: Something in between (e.g. proceed with Phase J but drop
     the `certified` status enum entry to avoid the certification-gate
     prohibition).
2. **`revised_draft.schema.json` is missing.** Should I
   (a) reverse-engineer it from `revision_workflow.py` output before
   Phase J, (b) treat Phase D as incomplete and stop, or (c) accept a
   different upstream artifact (e.g. `revision_diff.schema.json`) as the
   formatter's input?
3. **Where is `AGENTS.md`?** If it doesn't exist, please confirm so I
   can proceed without it.
4. **OK to `pip install -e ".[dev]"`** to get pytest available so I can
   confirm the 659 baseline?

I have not modified any tracked files. Only this discovery note was
written, under `.claude/`.
