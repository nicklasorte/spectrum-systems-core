# Red Team Review #1 — Pipeline Foundations

Document ID: SSC-REDTEAM-001
Scope: SSC-003 contract, SSC-004 loader, SSC-005 writer, SSC-006 manifest,
SSC-007 grounding, SSC-008 debug report.
Reviewer stance: skeptical new engineer with the constitution open.

---

## Method

Read each module against the constitution and the data lake contract. Trace
the path raw → produce → evaluate → decide → promote → write → manifest →
debug. Flag anything that breaks determinism, hides intent, leaks ceremony,
or makes a failed run hard to explain.

---

## Findings

### must_fix

**M1. Artifact envelope is not byte-deterministic.**
`Artifact.artifact_id` is generated with `uuid.uuid4()` and `created_at` is
`datetime.now(timezone.utc).isoformat()`. The data lake contract section 9
declares all outputs under `processed/` deterministic given the same raw
inputs. They are not — the envelope drifts every run.
*Fix*: derive `artifact_id` and `created_at` deterministically inside the
transcript pipeline (from `content_hash` and `trace_id`), so two runs over
the same `transcript.txt` and `metadata.json` produce byte-identical
processed files.

**M2. Slug fallback uses non-deterministic UUID prefix.**
`_slug_for` in `writer.py` falls back to `<title-slug>-<artifact_id[:8]>`.
With M1 unfixed this drifts; even with M1 fixed, deriving from a uuid is
fragile. The slug should come from a stable hash of payload content so the
filename never changes for the same input.
*Fix*: derive the slug fallback from `content_hash`.

**M3. Manifest sort key depends on `artifact_id`.**
`build_manifest` sorts `produced_artifacts` and `eval_artifacts` by
`(artifact_type, artifact_id)`. Until M1 is fixed, `artifact_id` is random
and the ordering — and thus the manifest bytes — varies across runs. After
M1 the ordering is stable, but the manifest also embeds `artifact_id`
strings directly, so M1 is required to deliver on the contract.
*Fix*: enforced by M1; add a regression test that asserts byte-equal
manifests across two runs over the same inputs.

**M4. Writer does not block `__` inside slug.**
The contract reserves `__` as the type/slug separator. Writer rejects `__`
inside `artifact_type` but not in the slug. A caller-supplied slug like
`"v__draft"` produces a filename with three segments and breaks any future
parser that splits on `__`.
*Fix*: reject `__` in slug values.

### should_fix

**S1. Optional metadata is loaded but not surfaced.**
`metadata` is preserved on `TranscriptInput.metadata`, but the debug
report records only `title`, `date`, and `source_type`. A reviewer checking
"why did this pass?" cannot see the full input metadata in one place.
*Fix*: include the optional fields (`agency`, `topic`, etc.) in the debug
report's `input` section.

**S2. Header-only transcripts still promote.**
A transcript that contains only a single header line — no `DECISION:`,
`ACTION:`, or `QUESTION:` lines — produces a `meeting_minutes` artifact with
empty lists in three of five fields and zero grounding entries. Required
evals all pass; control allows. The pipeline calls this "promoted" even
though there is nothing to promote.
*Fix*: add a `transcript_evidence` eval that fails when `source_type ==
"transcript"` and the grounding list is empty. Block promotion in that
case with a clear reason code.

### defer_with_reason

**D1. `_trace_id_for` in pipeline duplicates `derive_trace_id` in `_loop.py`.**
Both produce a 16-hex-char trace id, but with different inputs. Two
formulas are not strictly worse than one — they serve different sources
(plain text vs hashed transcript+metadata). Consolidating now would force
the workflow loop to know about the data lake. Defer to SSC-019 (entropy
review). Reason: removing now risks coupling that the constitution warns
against.

**D2. Stricter validation of optional metadata fields.**
`agency`, `topic`, `participants` are loaded as-is. Validating their types
or shapes adds rejection paths for real-world data variation. The contract
intentionally lists these as accepted but unstructured. Defer: only
required fields gate the loader.

**D3. Grounding eval is not registered in `evals/runner.py`.**
The grounding eval is constructed in `pipeline.py` instead of inside
`run_required_evals`. Registering it in the runner would force the runner
to depend on `data_lake.grounding`, which inverts the layering (core does
not depend on data lake). Defer: the pipeline is the only producer of
grounded artifacts, and its eval injection is explicit and testable.

---

## Loop integrity check

- Produce: deterministic extractor + grounding. After M1/M2, byte-stable.
- Evaluate: required-fields + non-empty + grounding. After S2, also catches
  no-evidence transcripts.
- Decide: pure function of eval results, unchanged.
- Promote: gates filesystem writes correctly. Manifest and debug are
  written either way, which is correct for explainability.

---

## Verdict

Foundations are sound but the determinism promise is currently a lie. Fix
M1–M4 and the lake actually delivers what the contract says. S1 and S2
make failures explainable. D1–D3 are not bugs.
