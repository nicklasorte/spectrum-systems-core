# Entropy Review — Killing Complexity Early

Document ID: SSC-ENTROPY-001
Scope: All files added or changed under `src/spectrum_systems_core/` and
`tests/` between SSC-003 and SSC-018.

This review answers three constitutional questions explicitly:

1. Does every new module prevent a failure or improve a measurable signal?
2. Does every addition strengthen Produce → Evaluate → Decide → Promote?
3. Can a new engineer explain a failed run quickly?

It then lists each module with a verdict: keep, merge, or delete.

---

## 1. Per-module verdict

All new code lives under `src/spectrum_systems_core/data_lake/`. Twelve
files. None reach into `artifacts/`, `context/`, `workflows/`, `evals/`,
`control/`, or `promotion/` — the existing core modules are unchanged.

| File              | Lines (approx) | Single concern                                  | Verdict |
|-------------------|---------------:|--------------------------------------------------|---------|
| `paths.py`        | 50  | Layout under `<lake_root>` and run-record filename helpers. | keep |
| `serialize.py`    | 30  | Canonical JSON, slugs, dataclass→dict.           | keep |
| `loader.py`       | 130 | Read raw transcript + metadata, validate.        | keep |
| `writer.py`       | 75  | Write only promoted artifacts.                   | keep |
| `grounding.py`    | 95  | Build/verify source spans against transcript.    | keep |
| `extract.py`      | 95  | Wrap workflow extractors and attach grounding.   | keep |
| `manifest.py`     | 110 | Replay manifest builder + validator.             | keep |
| `debug.py`        | 80  | Plain-English debug report builder.              | keep |
| `pipeline.py`     | 200 | Orchestrate the loop end-to-end, write outputs.  | keep |
| `index.py`        | 130 | Walk processed/, build deterministic JSONL.      | keep |
| `query.py`        | 130 | Filter index by field/keyword, fail-closed.      | keep |
| `failure_seed.py` | 95  | failure_record + eval_case_candidate.            | keep |

No module has been kept that does not earn its place. Below, each is
justified against the three constitutional questions, and any merge or
delete I considered is recorded with the reason it was rejected.

---

## 2. Considered merges and rejections

### 2.1 `serialize.py` into `paths.py` — REJECTED

`paths.py` is about *where* files go. `serialize.py` is about *how
bytes look* on disk. Different concerns, different reasons to change.
Merging would couple a layout change to a format change. Net entropy
would rise.

### 2.2 `grounding.py` into `extract.py` — REJECTED

`grounding.py` is consumed by both `extract.py` (during produce) and
`pipeline.py` (during evaluate). Folding it into `extract.py` would
either split the consumers into two import paths or bring the eval
logic into `extract.py`, where it does not belong. Keep separate.

### 2.3 `manifest.py` into `debug.py` — REJECTED

The manifest is a tight, validated record for replay. The debug report
is a plain-English explanation. They differ in shape, schema version,
audience, and stability rules. Merging them would force two contracts
into one file and one schema. Keep separate.

### 2.4 `failure_seed.py` into `pipeline.py` — REJECTED

`failure_seed.py` is invoked only after a run, by callers who decide
what to do with a failure. Putting it inside `pipeline.py` would force
the pipeline to know about post-hoc analysis. The seed currently has
zero callers in `pipeline.py` and that is correct.

### 2.5 `_trace_id_for` (pipeline) ↔ `derive_trace_id` (workflows/_loop) — REJECTED

Both produce a 16-hex trace id, but their inputs differ: the workflow
loop uses raw text; the pipeline uses transcript+metadata hashes.
Merging would force `workflows/_loop.py` to import `data_lake/loader.py`,
inverting the dependency direction the constitution requires (core does
not depend on the lake). Documented and accepted as two-line duplication.

---

## 3. Duplications removed during this review

### 3.1 Three near-identical eval helpers in `pipeline.py`

`_grounding_eval_artifact`, `_transcript_evidence_eval`, and
`_content_signal_eval` each ended with the same eight-line block that
constructed an `eval_result` payload and called `new_artifact`. That
block is now `_make_eval(target, eval_type, reason_codes)`. The three
specific evals shrank to one decision sentence each. Behavior unchanged;
test count unchanged at 152.

### 3.2 Four-way grounder duplication in `extract.py`

(Already collapsed in SSC-018 fix pass. Recorded here so the entropy
review's audit shows a clean delta.)

---

## 4. Unused fields and dead code

A grep for every field declared in `data_lake/*.py` shows each is read
somewhere — by tests, by pipeline, or by the index. No field exists for
ceremony.

`failure_seed.is_required_eval` returns the constant `False`. It is
small but intentional: callers can ask the question and get a clear "no"
rather than implicitly assuming. It is tested. Keep.

---

## 5. Tests: behavioral or ceremonial?

A test is ceremonial if removing it would not let any new bug through.
A test is behavioral if it pins a property a future change could break.

Tests reviewed:

- `test_data_lake_loader.py` — pins fail-closed loader behavior on every
  required-field-missing case. Behavioral.
- `test_data_lake_writer.py` — pins the contract's promotion-only and
  determinism rules. Behavioral.
- `test_data_lake_grounding.py` — pins that excerpts exist verbatim.
  Behavioral.
- `test_data_lake_manifest.py` — pins replay determinism and required
  fields. Behavioral.
- `test_data_lake_debug.py` — pins both allow and block explanations.
  Behavioral.
- `test_data_lake_index.py` — pins promoted-only inclusion, byte
  determinism, and ordering. Behavioral.
- `test_data_lake_query.py` — pins each filter behavior individually.
  Behavioral.
- `test_data_lake_fix_pass_*.py` — each pins a specific bug found by red
  team. Behavioral.
- `test_golden_transcripts.py` — pins extracted decisions/actions/
  questions and byte-equal outputs across runs. Behavioral.
- `test_failure_seed.py` — pins the "no auto-promotion" rule of the
  governed-learning seed. Behavioral.

No ceremonial tests found. No tests removed.

---

## 6. Constitutional self-check

**Q1. Does every new module prevent a failure or improve a measurable
signal?**

- `paths.py` prevents off-layout reads/writes by failing on bad
  `meeting_id`. Signal: contract compliance.
- `loader.py` prevents missing-input runs by failing closed. Signal:
  required-fields presence.
- `writer.py` prevents leaking non-promoted artifacts. Signal: status
  compliance.
- `grounding.py` prevents fabricated content by gating on excerpt
  presence. Signal: substring containment in transcript.
- `extract.py` prevents two implementations of "extract" drifting.
  Signal: payload shape parity with bare workflows.
- `manifest.py` prevents irreproducible runs. Signal: same input → same
  manifest bytes.
- `debug.py` prevents unexplainable runs. Signal: every blocked run
  carries its reason codes in plain language.
- `pipeline.py` prevents partial runs by ordering produce → evaluate →
  decide → promote → write strictly. Signal: end-to-end pass rate.
- `index.py` prevents undiscoverable promoted artifacts. Signal: index
  contains every promoted artifact.
- `query.py` prevents wrong answers from semantic shortcuts. Signal:
  every match has a known matched-field.
- `failure_seed.py` prevents silent loss of failure information. Signal:
  failures land in a typed artifact.

**Q2. Does every addition strengthen Produce → Evaluate → Decide → Promote?**

- Produce: `loader`, `extract`, `grounding`.
- Evaluate: `grounding`, `_transcript_evidence_eval`, `_content_signal_eval`.
- Decide: unchanged (`control/decision.py`).
- Promote: `writer` enforces the gate. `pipeline` writes only on allow.
- Outside the loop: `manifest`, `debug`, `index`, `query` exist to make
  the loop *explainable* and *queryable*. They do not relax any control
  gate.

**Q3. Can a new engineer explain a failed run quickly?**

Yes. For any failed run, the engineer looks at
`processed/meetings/<meeting_id>/debug__<run_id>.json`. That file lists:

- the input paths and hashes,
- the produced artifact id and content_hash,
- which evals passed, which failed, with reason codes,
- which decision was reached and why (one English sentence),
- which paths were written or refused, and why.

If the manifest is needed for replay, it sits beside the debug file in
the same directory.

---

## 7. Verdict

The data lake layer carries one job per file, no duplicates, no dead
fields, no ceremonial tests. The two-line trace_id duplication and the
deferred items in red team #1–#3 are the only remaining entropy. They
are documented with the reasons they were not removed.
