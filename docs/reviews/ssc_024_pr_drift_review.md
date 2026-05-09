# SSC-024 — PR #4 Drift Review

Document ID: SSC-024
Status: Informational
Scope: Determine whether SSC-021 learning-artifact behavior on PR #4
       (`claude/failure-persistence-human-review-Iqq87`) has been merged,
       superseded, or is still required for this phase.

---

## 1. Open PR snapshot

| Field | Value |
| --- | --- |
| PR | [#4](https://github.com/nicklasorte/spectrum-systems-core/pull/4) |
| Title | SSC-021: failure persistence + human-review eval case format |
| State | open, not merged |
| Head | `claude/failure-persistence-human-review-Iqq87` |
| Base sha at open | `f4a9d943` (right after PR #3 merge) |
| Main sha at this review | `31d9b6a` (after PR #8 merge) |
| Drift | 4 merged PRs ahead (#5 CI, #6 CLAUDE.md, #7 CLI + Markdown, #8 hardened required fields) |

PR #4 has not been rebased onto any of those merges. Several files it
touches (`docs/contracts/data_lake_contract.md`, `data_lake/paths.py`,
`data_lake/writer.py`, `data_lake/failure_seed.py`,
`tests/test_failure_seed.py`) have moved on `main` since #3.

The PR is therefore stale. It does not auto-conflict in trivial ways
because most of its additions are new files, but the contract
amendments and `paths.py` constants would need to be reconciled with
PR #7's Markdown contract (§6.3) and the markdown subdir constant.

---

## 2. What is on `main` today

Symbol survey for the SSC-021 vocabulary
(`failure_record`, `eval_case_candidate`, `reviewed_eval_case`,
`review_eval_candidate`, `write_learning_artifact`):

| Symbol | On main? | Where |
| --- | --- | --- |
| `FAILURE_RECORD_TYPE` | yes | `data_lake/failure_seed.py` |
| `EVAL_CASE_CANDIDATE_TYPE` | yes | `data_lake/failure_seed.py` |
| `record_failure(...)` | yes | `data_lake/failure_seed.py` |
| `candidate_eval_case_from_failure(...)` | yes | `data_lake/failure_seed.py` |
| `is_required_eval(...)` | yes | `data_lake/failure_seed.py` |
| `review_eval_candidate(...)` | **no** | only on PR #4 |
| `reviewed_eval_case` artifact_type | **no** | only on PR #4 |
| `write_learning_artifact(...)` | **no** | only on PR #4 |
| `processed/meetings/<id>/failures/` | **no** | only on PR #4 |
| `processed/meetings/<id>/eval_candidates/` | **no** | only on PR #4 |
| `processed/meetings/<id>/reviewed_evals/` | **no** | only on PR #4 |
| Contract §6A "Learning Artifacts" | **no** | only on PR #4 |

Conclusion: the first two arrows of the constitution's learning loop
(failure → `failure_record`, `failure_record` →
`eval_case_candidate`) exist on main as **in-memory artifact factories
only**. Nothing is persisted to disk, and the third arrow
(`reviewed_eval_case`) is not coded at all.

---

## 3. Does this phase depend on PR #4?

This phase is "Obsidian + Harness Memory + Debuggability". The required
slices are:

- SSC-025..028 — Markdown / Obsidian vault layout, frontmatter, links,
  index upgrades. **No dependency on persisted learning artifacts.**
- SSC-031..034 — run history, harness experience records, eval score
  history, debug report upgrade. **No dependency on persisted
  learning artifacts.** Run history is keyed on the existing `run_id`
  derived in `data_lake/manifest.py`, not on `failure_record`.
- SSC-037 — explicitly contingent: "If learning artifacts exist on
  main, add Markdown views; otherwise create
  `docs/roadmap/learning_artifacts_followup.md`." Since persistence is
  absent on main, SSC-037 takes the second branch.

So this phase **does not depend on PR #4**.

---

## 4. What to do with PR #4

Three options:

1. **Leave PR #4 open and defer.** It is stale and the persistence layer
   it adds is not required for this phase. Its commits can be rebased
   later when the third learning arrow (`reviewed_eval_case`) is
   actually needed.
2. **Cherry-pick the in-memory parts.** All in-memory parts are already
   on main (PR #3's `failure_seed.py` shipped them). Nothing to pick.
3. **Reimplement persistence here.** Rejected. This phase is about
   debuggability, not learning. Adding three new on-disk subdirectories
   without a consumer would create entropy (constitution §11).

Recommendation: **defer PR #4** and record the follow-up in
`docs/roadmap/learning_artifacts_followup.md` as part of SSC-037 so the
need is not lost.

---

## 5. Re-implementation needed later?

Yes, but not in this phase. When the human-review loop is built, the
following items from PR #4 should be revisited rather than blindly
copied because main has moved:

- Contract §6A wording must be reconciled with PR #7's §6.3 Markdown
  rules and use the same `<artifact_type>__<slug>.json` filename
  convention or a clearly different one.
- `paths.py` must add `failures/`, `eval_candidates/`, `reviewed_evals/`
  constants alongside the current `markdown/` subdir constant.
- `writer.py` already enforces the promotion rule. A learning-artifact
  writer must explicitly bypass that rule and document the carve-out
  inline.
- `review_eval_candidate(...)` should be added to `failure_seed.py` (or
  a new `failure_review.py`) and accept the three review statuses
  (accepted / rejected / needs_revision).

None of that work is in scope for this phase.

---

## 6. Conclusion

- PR #4 is **stale** relative to current main (4 merges behind).
- Its in-memory `failure_record` / `eval_case_candidate` types are
  **already on main**.
- Its on-disk persistence and `reviewed_eval_case` shape are **not on
  main** and **not required** for this Obsidian + harness-memory
  phase.
- This phase proceeds without depending on PR #4. SSC-037 will
  document the deferral in
  `docs/roadmap/learning_artifacts_followup.md`.
