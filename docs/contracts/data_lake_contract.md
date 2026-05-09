# Data Lake Contract

Document ID: SSC-CONTRACT-001
Status: Binding for SSC-003 onward
Scope: Boundary between `spectrum-data-lake` and `spectrum-systems-core`.

This contract is binding. The system constitution
(`docs/architecture/system_constitution.md`) takes precedence on any conflict.

---

## 1. Purpose

`spectrum-data-lake` is the storage system. `spectrum-systems-core` is a pure
processor. Core does not own storage. The data lake does not produce artifacts.
This file pins the file paths, the file formats, and the rules at the
boundary so a new engineer can locate any input or output by inspection.

---

## 2. Layout

The data lake is a directory tree rooted at `<lake_root>`. All paths below are
relative to that root.

```
raw/meetings/<meeting_id>/transcript.txt
raw/meetings/<meeting_id>/metadata.json
processed/meetings/<meeting_id>/<artifact_type>__<slug>.json
processed/meetings/<meeting_id>/manifest__<run_id>.json        # optional
processed/meetings/<meeting_id>/debug__<run_id>.json           # optional
indexes/meetings/artifact_index.jsonl
```

- `raw/meetings/<meeting_id>/` — the inputs core reads. Owned by the lake.
- `processed/meetings/<meeting_id>/` — the outputs core writes. Only
  promoted artifacts, plus optional run manifests and debug reports, live here.
- `indexes/meetings/artifact_index.jsonl` — deterministic JSONL index over
  promoted processed artifacts.

Core never reads from or writes to other locations under `<lake_root>`.

---

## 3. meeting_id Naming Rules

`meeting_id` identifies one source meeting. It is the directory name under
`raw/meetings/` and `processed/meetings/`.

Rules:

- Non-empty string.
- Lowercase ASCII letters, digits, hyphen, and underscore only:
  pattern `^[a-z0-9][a-z0-9_-]{0,127}$`.
- No whitespace. No path separators. No leading hyphen.
- The `meeting_id` value in `metadata.json` must equal the directory name.

If these rules are violated, the loader rejects the meeting before any
artifact is produced.

---

## 4. Raw Transcript

Path: `raw/meetings/<meeting_id>/transcript.txt`

Rules:

- Plain UTF-8 text.
- Newline-separated lines. Line numbers are 1-based.
- The file must exist and be non-empty for the meeting to be loadable.
- Core treats the transcript as immutable. Core never writes back to it.

---

## 5. Raw Metadata

Path: `raw/meetings/<meeting_id>/metadata.json`

Format: a single JSON object.

### 5.1 Required fields

- `meeting_id` — string. Must equal the directory name.
- `title` — non-empty string.
- `date` — string in ISO-8601 calendar form `YYYY-MM-DD`.
- `source_type` — string. One of: `transcript`, `notes`, `summary`.

A meeting with any required field missing or invalid is rejected by the
loader.

### 5.2 Accepted optional fields

- `agency` — string. Owning or associated agency.
- `topic` — string.
- `participants` — list of strings.
- `speakers` — list of strings.
- `notes` — short string.

Unknown fields are preserved verbatim by the loader and ignored by core.
Adding a new accepted field requires updating this contract.

---

## 6. Processed Artifacts

Path: `processed/meetings/<meeting_id>/<artifact_type>__<slug>.json`

Filename rules:

- `<artifact_type>` is the artifact's `artifact_type` field.
- `<slug>` is a stable identifier: either the artifact's `artifact_id`, or a
  caller-supplied slug that is deterministic for the same input.
- The double-underscore `__` separates the two segments. No other
  double-underscore appears in `<artifact_type>` or `<slug>`.
- File extension is always `.json`.

Content rules:

- One JSON object per file containing the full artifact envelope:
  `artifact_id`, `artifact_type`, `schema_version`, `status`, `created_at`,
  `trace_id`, `input_refs`, `content_hash`, `payload`.
- Encoding: UTF-8.
- Serialization: deterministic — keys sorted, no trailing whitespace, single
  trailing newline. Two writes of the same artifact produce byte-identical
  files.

### 6.1 Promotion rule

Only artifacts with `status == "promoted"` may be written under
`processed/meetings/`. Artifacts with status `draft`, `evaluated`, or
`rejected` are not written as promoted product artifacts.

Manifests (`manifest__<run_id>.json`) and debug reports
(`debug__<run_id>.json`) are run-level records and are not subject to the
promotion rule. They are written even when promotion is blocked, because
they exist to explain the run.

### 6.2 Eval, control, manifest, debug artifacts

`eval_result` and `control_decision` artifacts are part of the run, not the
product. They are not written as promoted product artifacts. They appear
inside manifests and debug reports.

### 6.3 Markdown views

Layout (binding for SSC-025 onward):

```
processed/meetings/<meeting_id>/markdown/index.md
processed/meetings/<meeting_id>/markdown/artifacts/<artifact_type>.md
processed/meetings/<meeting_id>/markdown/agencies/<agency_slug>.md
processed/meetings/<meeting_id>/markdown/topics/<topic_slug>.md
processed/meetings/<meeting_id>/markdown/runs/<run_id>.md
```

Markdown views are a separate, read-only-from-core's-perspective rendering
of promoted artifacts and the harness-memory projections that explain
each run. They exist so a human can read the outputs in any plain-text
editor or in Obsidian.

Rules:

- Markdown is regenerated from canonical JSON / JSONL. It is never the
  canonical form of an artifact.
- Core never reads Markdown as input to the loop. The CLI may rewrite
  any file under `markdown/` on every run.
- Markdown is not a product artifact and is not subject to the
  promotion gate. The promotion gate applies to JSON artifacts only.
- Markdown files MUST NOT appear in `indexes/meetings/artifact_index.jsonl`.
- Filenames: `<artifact_type>.md` for one promoted artifact's view
  (under `artifacts/`), `index.md` for the per-meeting index,
  `<slug>.md` under `agencies/` and `topics/`, and `<run_id>.md` under
  `runs/`. The `__` separator is reserved for the JSON artifact
  filename convention and must not appear in Markdown filenames.
- Determinism: identical inputs MUST produce byte-identical Markdown
  files. Two `process-meeting` runs over the same raw inputs leave the
  whole `markdown/` subtree byte-identical.

Each Markdown file begins with YAML frontmatter. The required keys
depend on the file's `artifact_type` token:

| File | `artifact_type` | Required frontmatter keys |
| --- | --- | --- |
| `index.md` | `meeting_index` | `meeting_id`, `date`, `title`, `status: view`, `trace_id`, `canonical: false` |
| `artifacts/<type>.md` | `meeting_minutes` / `decision_brief` / `agency_question_summary` / `meeting_action_log` | `artifact_id`, `meeting_id`, `date`, `title`, `status: promoted`, `trace_id`, `content_hash`, `canonical_json_path` |
| `agencies/<slug>.md` | `agency_note` | `meeting_id`, `date`, `title`, `agency`, `status: view`, `canonical: false` |
| `topics/<slug>.md` | `topic_note` | `meeting_id`, `date`, `title`, `topic`, `status: view`, `canonical: false` |
| `runs/<run_id>.md` | `run_note` | `meeting_id`, `run_id`, `workflow_name`, `decision`, `promoted`, `status: view`, `canonical: false` |

The tokens `meeting_index`, `agency_note`, `topic_note`, and
`run_note` are part of this contract and may not be renamed without
amending it. They are not artifact types of the core envelope; they
are the Markdown layer's view shapes.

Markdown is a view of the source of truth, not the source of truth.

### 6.4 Per-meeting harness memory

Three meeting-level JSONL files complement the canonical artifacts and
the Markdown views:

```
processed/meetings/<meeting_id>/run_history.jsonl
processed/meetings/<meeting_id>/experience_history.jsonl
processed/meetings/<meeting_id>/eval_history.jsonl
```

Rules:

- Harness-memory JSONL files are NOT product artifacts. They never
  appear in `indexes/meetings/artifact_index.jsonl`.
- Each file is one JSON object per line, deterministic field order
  via `serialize.canonical_json`, sorted on a stable key.
- Two runs over the same raw inputs produce byte-identical files.
- Records carry pointers (`manifest_path`, `debug_path`) but no
  authority. The control function and promotion gate are unaffected.
- A future learning-loop step (constitution §10) MUST read JSON, not
  these JSONL files, when making governed decisions.

---

## 7. Index

Path: `indexes/meetings/artifact_index.jsonl`

- One JSON object per line. UTF-8. Trailing newline after the last line.
- Built only from promoted processed artifacts. Non-promoted artifacts
  never appear in the index.
- Deterministic: identical inputs produce a byte-identical file. Records
  are sorted by `(meeting_id, artifact_type, artifact_id)`.
- See the index field list in SSC-011.

---

## 8. Boundary Rules

These rules separate `spectrum-systems-core` from `spectrum-data-lake`:

- Core reads `raw/meetings/<meeting_id>/transcript.txt` and
  `raw/meetings/<meeting_id>/metadata.json`. Core does not write to `raw/`.
- Core writes only under `processed/meetings/` and `indexes/meetings/`.
- Core never deletes anything. The data lake is append-only from core's
  perspective.
- Core does not assume a database, schema registry, or remote service. The
  lake is a directory tree.
- The lake does not interpret transcripts or run evals. It stores bytes.
- Core never owns global state across runs. Two runs over the same inputs
  produce the same outputs.

---

## 9. Determinism

All core outputs under `processed/` and `indexes/` are deterministic given
the same raw inputs. JSON is canonicalized with sorted keys and stable
separators. JSONL records are sorted before writing.

Determinism is the testable replacement for trust in this layer.

---

## 10. Out of Scope

The contract intentionally excludes:

- Vector indexes, embeddings, semantic search.
- Live model calls.
- Dashboards.
- Cross-tenant or auth concerns.
- Any storage backend other than a local directory tree.

These are deferred by the constitution.
