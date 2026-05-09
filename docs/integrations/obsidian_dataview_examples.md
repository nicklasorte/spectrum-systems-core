# Obsidian Dataview — Practical Examples

Document ID: SSC-NEXT-MEMORY-039
Status: Documentation only. The Dataview plugin is optional and is
not a runtime dependency of `spectrum-systems-core`. None of these
queries are executed by core; they are convenience snippets for
humans who use Obsidian as a vault.

---

## How to use these queries

Open any of the example queries below inside an Obsidian note (not
inside one of the generated `processed/<...>/markdown/` files — those
are overwritten on every `spectrum-core process-meeting` run). The
queries assume the [Dataview](https://blacksmithgu.github.io/obsidian-dataview/)
plugin is installed.

The queries match on YAML frontmatter fields written by
`data_lake/markdown.py`. The current frontmatter shape is:

- Per-meeting `index.md`:
  `artifact_type: meeting_index`, `meeting_id`, `date`, `title`,
  `status: view`, `canonical: false`, `trace_id`.
- Per-artifact `artifacts/<type>.md`:
  `artifact_type: <one of meeting_minutes / decision_brief /
  agency_question_summary / meeting_action_log>`, `artifact_id`,
  `meeting_id`, `date`, `title`, `status: promoted`, `trace_id`,
  `content_hash`, `canonical_json_path`.
- Per-agency `agencies/<slug>.md`: `artifact_type: agency_note`,
  `meeting_id`, `agency`, `status: view`, `canonical: false`.
- Per-topic `topics/<slug>.md`: `artifact_type: topic_note`,
  `meeting_id`, `topic`, `status: view`, `canonical: false`.
- Per-run `runs/<run_id>.md`: `artifact_type: run_note`, `run_id`,
  `meeting_id`, `workflow_name`, `decision`, `promoted`,
  `status: view`, `canonical: false`.

Every example below uses these fields.

---

## All meetings

```dataview
TABLE date, title
FROM "processed"
WHERE artifact_type = "meeting_index"
SORT date desc
```

Lists every meeting that has been processed, by indexing the
per-meeting `index.md`.

---

## Promoted artifacts grouped by meeting

```dataview
TABLE artifact_type, title, canonical_json_path
FROM "processed"
WHERE artifact_type IN ("meeting_minutes", "decision_brief",
                        "agency_question_summary", "meeting_action_log")
  AND status = "promoted"
GROUP BY meeting_id
SORT meeting_id asc
```

Reads `artifacts/<type>.md` files (which carry `status: promoted`)
and groups them by meeting. The `canonical_json_path` column points
back at the byte source of truth.

---

## Blocked workflows

The blocked-workflow story lives in the run notes
(`runs/<run_id>.md`).

```dataview
TABLE meeting_id, workflow_name, run_id, decision
FROM "processed"
WHERE artifact_type = "run_note"
  AND promoted = false
SORT meeting_id, workflow_name asc
```

A reader can click into any matching note for the plain-English
explanation and the link to the canonical debug JSON.

---

## Agency question summaries by agency

```dataview
TABLE meeting_id, title
FROM "processed"
WHERE artifact_type = "agency_question_summary"
  AND status = "promoted"
  AND agency = "FCC"
SORT meeting_id asc
```

Replace `"FCC"` with any agency string. Note: artifact frontmatter
does not carry the `agency` field directly — but the per-agency note
(`agencies/<slug>.md`) does, and it lists the artifacts that
referenced it.

A simpler "all agency notes for FCC across meetings" query:

```dataview
TABLE meeting_id, agency
FROM "processed"
WHERE artifact_type = "agency_note"
  AND agency = "FCC"
SORT meeting_id asc
```

---

## Decisions by topic

```dataview
TABLE meeting_id, title, canonical_json_path
FROM "processed"
WHERE artifact_type = "decision_brief"
  AND status = "promoted"
SORT meeting_id asc
```

To narrow by topic, combine with the per-topic notes:

```dataview
TABLE meeting_id, topic
FROM "processed"
WHERE artifact_type = "topic_note"
  AND topic = "3.5 GHz sharing"
SORT meeting_id asc
```

---

## Artifacts missing agency / topic metadata

A meeting with no agency or topic in its raw metadata produces no
agency / topic notes. Find such meetings indirectly:

> Dataview-specific: the `file.outlinks` predicate is part of the
> Dataview Query Language and does not work in plain Markdown
> tooling.

```dataview
TABLE meeting_id, date, title
FROM "processed"
WHERE artifact_type = "meeting_index"
  AND !contains(file.outlinks.path, "agencies/")
SORT date desc
```

This reads "meetings whose index does not link out to any
`agencies/`-shaped page." A similar query for topics swaps in
`topics/`.

---

## Direct links to canonical JSON

The canonical JSON for a promoted artifact is referenced from the
artifact Markdown's frontmatter:

```dataview
TABLE meeting_id, artifact_type, canonical_json_path
FROM "processed"
WHERE artifact_type IN ("meeting_minutes", "decision_brief",
                        "agency_question_summary", "meeting_action_log")
  AND status = "promoted"
SORT meeting_id, artifact_type asc
```

The `canonical_json_path` column is a relative path from the
artifact Markdown back to the JSON file. Click-through gives you the
byte source of truth.

---

## Important: Dataview is not a runtime dependency

These examples are documentation. `spectrum-systems-core` does NOT
require Dataview, Templater, or any other Obsidian plugin to
function. The CLI writes the same Markdown regardless. If a future
phase wants to add Dataview-aware fields, the change must keep the
generated files plain Markdown so non-Obsidian readers (Claude, a
plain editor, `cat`) still get a useful artifact view.
