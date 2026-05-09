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
processed/meetings/<meeting_id>/failures/<failure_id>.json
processed/meetings/<meeting_id>/eval_candidates/<candidate_id>.json
processed/meetings/<meeting_id>/reviewed_evals/<eval_case_id>.json
indexes/meetings/artifact_index.jsonl
```

- `raw/meetings/<meeting_id>/` — the inputs core reads. Owned by the lake.
- `processed/meetings/<meeting_id>/` — the outputs core writes. Only
  promoted artifacts, plus optional run manifests and debug reports, live
  at the top of this directory.
- `processed/meetings/<meeting_id>/failures/`,
  `eval_candidates/`, and `reviewed_evals/` — learning artifacts written
  to dedicated subdirectories. They are NOT promoted product artifacts and
  do not enter the artifact index.
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

---

## 6A. Learning Artifacts

The constitution (section 10) reserves the loop:

```
failure -> failure_record -> eval_case_candidate -> reviewed eval_case
        -> regression suite
```

Learning artifacts are the persistent form of the first three arrows. They
are kept under `processed/meetings/<meeting_id>/` so a reviewer can locate
the failure, the candidate, and the human-reviewed eval for any given
meeting in one place — but they are stored in dedicated subdirectories so
they never blur into product artifacts.

### 6A.1 Paths

```
processed/meetings/<meeting_id>/failures/<failure_id>.json
processed/meetings/<meeting_id>/eval_candidates/<candidate_id>.json
processed/meetings/<meeting_id>/reviewed_evals/<eval_case_id>.json
```

- `<failure_id>` is the `artifact_id` of the `failure_record`.
- `<candidate_id>` is the `artifact_id` of the `eval_case_candidate`.
- `<eval_case_id>` is the `artifact_id` of the `reviewed_eval_case`.
- Filenames have no `__` separator; the directory itself names the kind.
- File extension is always `.json`. UTF-8. Canonical JSON (sorted keys,
  trailing newline). Two writes of the same artifact produce byte-identical
  files.

### 6A.2 Contents

Each file contains the full artifact envelope (`artifact_id`,
`artifact_type`, `schema_version`, `status`, `created_at`, `trace_id`,
`input_refs`, `content_hash`, `payload`).

Allowed `artifact_type` values per directory:

- `failures/` — `failure_record`.
- `eval_candidates/` — `eval_case_candidate`.
- `reviewed_evals/` — `reviewed_eval_case`.

Any other `artifact_type` written through the learning-artifact writer is
rejected.

### 6A.3 Rules

- Learning artifacts are NOT promoted product artifacts. They do not enter
  the artifact index. Their envelope `status` is not `promoted`.
- Learning artifacts MAY be written even when product promotion is blocked,
  because their reason for existing is to record blocked runs.
- A `reviewed_eval_case` requires `payload.human_review_status == "accepted"`,
  `"rejected"`, or `"needs_revision"`. Only `"accepted"` may be referenced
  by a future regression fixture; `"rejected"` and `"needs_revision"`
  remain stored but never become required eval coverage.
- Promotion of a candidate into a required eval is a separate, explicit
  step (a future slice). Persistence here does not create a required eval.

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
  Learning artifacts are written under
  `processed/meetings/<meeting_id>/failures/`,
  `eval_candidates/`, and `reviewed_evals/`.
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
