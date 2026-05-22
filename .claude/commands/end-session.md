---
description: End-of-session documentation hook. Summarizes the session, surfaces CLAUDE.md friction, proposes specific edits, and appends a JSONL row to docs/session_log.jsonl. Proposes only — does not auto-apply CLAUDE.md edits.
---

You are closing out a Claude Code session in this repo. Follow every step. Do
not skip any. Do not auto-apply edits to `CLAUDE.md` — propose them only and
let the human approve via the PR.

## Step 1 — Reconstruct what changed this session

State, in plain prose:

- The files written, modified, or deleted this session (group by directory).
- The decisions made (e.g. "renamed X to Y because Z", "blocked promotion when
  source quote length < N"). Tie each decision to the constitution rule or
  contract clause it serves.
- The blockers hit and how they were resolved (or, if unresolved, what the
  open question is).

If nothing was changed this session, write "no code changes" and skip to
Step 4 with empty arrays.

## Step 2 — Identify CLAUDE.md friction

Re-read `CLAUDE.md`. For each instruction that:

- caused a repeated tool-calling mistake this session, OR
- was ambiguous enough that you had to guess, OR
- conflicted with an `@import` target or another section,

write one paragraph that names the section, quotes the ambiguous text, and
explains how it tripped you up.

If `CLAUDE.md` was clear throughout, write "no friction" explicitly. Do not
invent friction to justify the section.

## Step 3 — Propose specific edits

For each friction item from Step 2, draft a concrete edit:

- The exact path (always `CLAUDE.md` unless an `@import` target is the
  source of the ambiguity).
- The exact `old_string` block being replaced (copy verbatim from the file).
- The exact `new_string` block that should replace it.
- One sentence explaining why the new wording removes the ambiguity.

Vague suggestions ("clarify the integration-test section") are not acceptable.
If you cannot draft exact replacement text, mark the proposal `DEFERRED` with
a reason.

**Do not apply the edits. Do not call `Edit` or `Write` on `CLAUDE.md`.** The
human reviews these proposals in the PR description.

## Step 4 — Append a JSONL row to docs/session_log.jsonl

Append exactly one line to `docs/session_log.jsonl`. The line must be a valid
JSON object with these fields (in this key order, sorted alphabetically when
serialized):

- `blockers`: array of strings. One string per unresolved blocker. Empty
  array if none.
- `claude_md_proposals`: array of objects, each with keys `path`,
  `old_string`, `new_string`, `rationale`. Empty array if no friction.
- `date`: ISO-8601 calendar date (`YYYY-MM-DD`), the local date of the
  session close.
- `files_changed`: array of strings, repo-relative paths.
- `phase`: short string identifying the phase or topic of the session
  (e.g. "phase-AB.3 comparison", "infra: workflow patterns"). If unknown,
  use `UNKNOWN`.

Write the row using canonical JSON (sorted keys, no trailing whitespace,
no embedded newlines in strings — escape with `\n`). Append, do not
overwrite.

Before writing, verify the file currently ends with a newline (or is
empty). After writing, the file must end with exactly one trailing
newline.

## Step 5 — Print the summary

Print to the chat:

- A 3-5 line plain-English session summary.
- The exact JSONL row that was appended.
- The list of proposed `CLAUDE.md` edits, each as a fenced diff block, so a
  human can scan and accept.

Do not open a PR from this command. The session log is the durable record;
PRs for code changes are separate.
