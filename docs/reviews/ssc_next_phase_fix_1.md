# Fix Pass #1 — Response to Red Team Review #1

Document ID: SSC-FIX-001
Scope: Resolutions for findings in `ssc_next_phase_redteam_1.md`.

Each finding lists status, code change, and the test(s) that lock it in.

---

## must_fix

### M1. Artifact envelope is not byte-deterministic — FIXED

**Change**: `data_lake/pipeline.py` now stabilizes every artifact it
creates via `_stabilize`, which:
- replaces `artifact_id` with `<kind-prefix>-<sha256(payload+trace_id)[:24]>`,
- replaces `created_at` with the fixed string `1970-01-01T00:00:00+00:00`.

The fix lives only in the pipeline. The `artifacts.Artifact` envelope keeps
its uuid + wall-clock defaults so unrelated callers and existing tests
remain unchanged. This keeps the data lake's determinism guarantee local
to the layer that promised it.

**Tests**:
- `test_artifact_id_and_created_at_are_stable_inside_pipeline`
- `test_processed_artifact_file_is_byte_identical_across_runs`

### M2. Slug fallback uses non-deterministic UUID prefix — FIXED

**Change**: `data_lake/writer.py::_slug_for` now derives the fallback slug
from the artifact's `content_hash` instead of `artifact_id`. The slug is
`<title-slug>-<content_hash[:12]>`, or `<content_hash[:12]>` when no title
is present. Combined with M1, this gives a stable filename per input.

**Tests**: `test_slug_fallback_is_deterministic_from_content_hash`.

### M3. Manifest sort key depends on `artifact_id` — FIXED (subsumed by M1)

**Change**: After M1, `artifact_id` values are themselves deterministic, so
the existing sort `(artifact_type, artifact_id)` produces a stable order.
The regression test asserts byte-identical manifest files across two runs
over the same inputs.

**Tests**: `test_manifest_file_is_byte_identical_across_runs`.

### M4. Writer does not block `__` inside slug — FIXED

**Change**: `data_lake/writer.py::_slug_for` now raises `WriterError` if a
caller-supplied slug contains the reserved separator `__`.

**Tests**: `test_writer_rejects_double_underscore_in_slug`.

---

## should_fix

### S1. Optional metadata is loaded but not surfaced — FIXED

**Change**: `data_lake/debug.py::build_debug_report` now includes an
`optional_metadata` block under `input` that contains every metadata field
that is not one of the four required fields. A reviewer reading the debug
report can see every input metadata field without opening
`metadata.json`.

**Tests**: `test_debug_report_includes_optional_metadata_fields`.

### S2. Header-only transcripts still promote — FIXED

**Change**: A new eval `transcript_evidence` runs for every pipeline run.
It fails with reason code `no_transcript_evidence` when
`source_type == "transcript"` and the produced artifact contains zero
grounding entries. The control function blocks on the failure as it does
for any failed required eval. `source_type` values of `notes` or `summary`
are not held to this bar (they may legitimately lack span-level grounding).

**Tests**:
- `test_transcript_evidence_blocks_when_no_grounded_spans`
- `test_transcript_evidence_passes_when_grounding_present`
- `test_transcript_evidence_does_not_apply_to_non_transcript_sources`

---

## defer_with_reason

### D1. `_trace_id_for` duplicates `derive_trace_id` — DEFERRED

These functions live in different layers and consume different inputs
(plain text vs. transcript+metadata hashes). Merging them would force
`workflows/_loop.py` to import `data_lake/loader.py`, inverting the
dependency direction the constitution requires (core does not depend on
the lake). The duplication is two lines of trivially independent hashing.
Tracked for SSC-019 (entropy review) where it will be re-examined.

### D2. Stricter validation of optional metadata fields — DEFERRED

The contract intentionally lists `agency`, `topic`, `participants` as
accepted but unstructured. Tightening here would reject real-world
metadata variation without increasing trust. Reason: only required fields
gate the loader; everything else is preserved for downstream consumers.

### D3. Grounding eval not registered in `evals/runner.py` — DEFERRED

Registering `source_grounding` in `run_required_evals` would require the
generic runner to depend on `data_lake.grounding` and on the transcript
input object. That inverts the layering and forces every artifact type to
care about transcript spans, even those produced from non-transcript
sources. The pipeline already injects the eval explicitly, so the
behavior is correct and the test coverage is in place. Reason: layering
integrity beats co-location.

---

## Verdict

All must_fix items are resolved with code and tests. should_fix items are
resolved with code and tests. defer items have explicit reasons that the
constitution and contract back up. Run count: 101 tests pass.
