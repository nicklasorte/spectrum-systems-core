# CLAUDE.md — Obsidian Bridge Governance

Fail-closed: if an action is not explicitly permitted below, it is forbidden.

## Permitted actions

- Read any .md file in Inbox/ for ingestion processing
- Write to Reviews/Pending/ (review form creation only)
- Move files from Reviews/Pending/ to Reviews/Completed/ (post-review only)
- Write to Artifacts/Promoted/ (index notes only, after pipeline allow decision)
- Stamp these frontmatter fields on Inbox/ notes only:
  ingestion_artifact_id, ingestion_status, ingestion_at,
  ingestion_failure_reason, promoted_artifact_id, promoted_at,
  promoted_note, rejection_reason, rejection_at

## Forbidden actions

- Delete any note in Inbox/ — move to Inbox/Archived/ only
- Edit any note in Artifacts/Promoted/ after creation
- Modify frontmatter fields not listed above
- Call any LLM in any bridge module
- Proceed past any eval failure — block immediately

## Fail-closed rule

Missing artifact → halt.
Ambiguous scope → halt and report.
Never proceed on inference.
