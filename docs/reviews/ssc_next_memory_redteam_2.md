# SSC-NEXT-MEMORY — Red Team Review #2

Scope: SSC-031 (run history index), SSC-032 (harness experience
records), SSC-033 (eval score history), SSC-034 (debug report
upgrade).

Question we are answering: are these new memory files useful, or are
they bloat that adds entropy without preventing a failure? And could
any of them silently grant authority?

---

## Findings

### M4 (must_fix) — `run_history.jsonl` and `experience_history.jsonl` overlap

`run_history.jsonl` and `experience_history.jsonl` both list one record
per workflow run with mostly the same fields (workflow_name,
artifact_id, decision, reason_codes). A new engineer hitting either
file will reasonably ask "why two?" The constitution's complexity rule
says we should merge unless there is a clear distinction.

After re-reading both records, the distinction IS real:

- `run_history` is a "where to look" projection. Its job is to point
  at canonical run records (`manifest_path`, `debug_path`,
  `run_markdown_path`).
- `experience_history` is a "what happened" projection. Its job is the
  compressed lesson (`input_hash`, `output_hash`, `eval_summary`,
  `human_readable_summary`).

But the line is too thin without enforcement. Fix: keep both but
write a docstring + a one-line "Distinguished from
`experience_history.jsonl` by ..." note in each module, and add a
test that asserts neither file's record has the other file's
unique-identifying field. This prevents future drift back into a
single-file shape.

### M5 (must_fix) — eval_history's `score` is float-or-None, ambiguous

`eval_history.jsonl` records `score` directly from the eval payload.
For the existing pipeline this is `1.0` or `0.0`. A future eval that
emits a different shape (string, bucket, missing) would silently land
in the JSONL and a reader would not be able to tell the difference
from "score not produced." Fix: coerce missing scores to `None`
explicitly and add a regression test pinning the contract.

The current code already passes through whatever was on the eval
payload (defaulting to `None` via `payload.get("score")`). Making the
test explicit guards against future regressions.

### S3 (should_fix) — `inspect_next` could grow stale

The plain-English hints in `debug.py::_INSPECTION_HINTS` are keyed by
fixed reason codes. If a new eval emits a new reason code, the hint
disappears and the report says "see eval reason code 'X'". That is an
acceptable fallback but it makes the report less useful for new
codes. Fix: leave today's behavior, but add a test that asserts every
existing reason code (the four `failed:<eval_type>` codes plus
`empty_required_field:` and `missing_field:`) maps to a non-fallback
hint.

### S4 (should_fix) — run_history.jsonl is at the meeting top level, but the artifact index already lives there

`run_history.jsonl`, `experience_history.jsonl`, and
`eval_history.jsonl` co-exist with the canonical artifact JSON files
under `processed/meetings/<meeting_id>/`. The artifact index walker
(`index.py::collect_index_records`) only matches `*.json` (not
`*.jsonl`) and the meeting directory is the documented location for
"per-meeting projections", so this is currently safe. To make the
safety load-bearing instead of accidental, add an explicit
regression test that none of the JSONL files are picked up by the
artifact index walker.

### D3 (defer_with_reason) — append-only run history vs. overwrite

Today every `process_meeting` call rewrites all three JSONL files in
full. That is required by the determinism rule (two runs over the
same inputs must produce a byte-identical file). An append-only model
would be more useful when re-running new workflows on an existing
meeting, but it would break determinism. Defer with reason: the
constitution prizes determinism and the incremental reprocessing
case is not a current need.

### D4 (defer_with_reason) — diagnose-in-2-minutes claim

The phase prompt asks "Can a new engineer diagnose a failed workflow
in under 2 minutes?" We have the artifacts (debug report,
inspect_next, run note, index reasoning) but no measured timing.
Defer the timing claim itself — instead, defend it by structure:
debug → run note → index all surface the same explanation.

---

## Authority audit

- Could any harness-memory file be mistaken for authoritative?
  - `run_history.jsonl` is plain JSONL with no `status: promoted`
    field; the existing index walker explicitly skips JSONL files. Not
    authoritative.
  - `experience_history.jsonl` records `decision` and `reason_codes`,
    but the control function never reads it back. Not authoritative.
  - `eval_history.jsonl` mirrors `eval_result` artifacts. Not
    authoritative.
  - `debug__<run_id>.json` keeps `outcome`, `failure_path`,
    `inspect_next`. Per the contract these are run records, not
    promoted artifacts.
- Did anything add a feature flag, fallback, or self-modifying
  behavior? No. The harness memory is a passive projection.

---

## Classification

- must_fix: M4, M5
- should_fix: S3, S4
- defer_with_reason: D3, D4
