# Spectrum Systems Engineering Principles

These principles govern all design, implementation, and review
decisions in this repository. They are binding on every Claude Code
session alongside CLAUDE.md and PR_FAILURE_PROTOCOL.md.

Every principle includes a **Spectrum Systems lesson** drawn from
real failures in this codebase.

---

## 1. Kill Complexity Early

Top engineers eliminate unnecessary layers instead of managing them.

**Rule:** Every system must justify itself by preventing a failure
or improving a measurable signal. If it cannot, remove it.

**Spectrum lesson:** The 21-type extraction schema with a 6,500-token
prompt exceeded Haiku's reliable instruction-following ceiling. Adding
more prompt instructions instead of upgrading the model added
complexity without solving the capability problem. The right fix was
fewer, stronger constraints with the right model.

---

## 2. Build Fewer, Stronger Loops

They focus on strengthening core loops instead of adding systems.

**Rule:** Every addition must strengthen the
execution → eval → control → enforcement loop. If it doesn't touch
the loop, question whether it's needed.

**Spectrum lesson:** The comparison engine, correction miner, and
cross-meeting synthesis all strengthen the loop. Six consecutive PRs
that fixed simulations without fixing production weakened confidence
in the loop without strengthening it.

---

## 3. Optimize for Debuggability

They prioritize making failures easy to understand.

**Rule:** Any failure must be explainable by a new engineer reading
only the artifact and the reason_code. No tribal knowledge required.

**Spectrum lesson:** Six PRs failed because the single aggregated
control decision (BLOCKED: reason_codes=...) never said which chunk
produced the failure. The --debug-chunks flag was the correct fix —
it made failures attributable to specific chunks and items.

---

## 4. Treat Unknown States as Bugs

Ambiguity is treated as failure.

**Rule:** No silent degradation. Unknown or missing data must block
or escalate. An artifact that is ambiguous is an artifact that failed.

**Spectrum lesson:** The UNATTRIBUTED bucket in the chunk debug
report surfaces items with no resolvable grounding. An item that
cannot be attributed to a source turn is an unknown state — it
must be visible, not silently dropped.

---

## 5. Invest in Real Test Data

Datasets drive reliability more than frameworks.

**Rule:** Every simulation must be validated against at least one
real model output before it can be used as a passing test. A test
that only passes on hand-crafted stubs is not a test.

**Spectrum lesson:** Six consecutive PRs passed all simulations and
blocked in production. The stubs were hand-crafted to be valid — they
never exercised real Haiku output. Real model validation is not
optional. Continuously expand golden, adversarial, and drift datasets
from production failures.

---

## 6. Separate System Truth from Intent

Only artifacts and signals are trusted.

**Rule:** Never rely on human interpretation when artifacts can
decide. The artifact IS the record. Prose descriptions of what
the artifact contains are not evidence.

**Spectrum lesson:** "No prose as evidence" is a binding constraint
in every roadmap table and Claude Code prompt. The comparison engine
produces a numeric F1 score — that is the signal, not a description
of how the extraction felt.

---

## 7. Enforce Promotion Discipline

Nothing moves forward without strict validation.

**Rule:** Require evals, policy checks, and replay validation for
all promotions. A fix that makes CI green by weakening governance
is worse than leaving CI red (PR_FAILURE_PROTOCOL Class VI).

**Spectrum lesson:** The autonomous fix script
(scripts/autonomous_llm_fix.py) has a static Class VI guard that
blocks any proposed change that weakens an eval, flips a verdict,
removes an assertion, or shrinks net enforcement. It fires before
anything touches disk.

---

## 8. Design for Rollback First

Failure is assumed and must be reversible.

**Rule:** Every change must have an immediate rollback path.
Rollback path = one sentence per change. If you cannot state the
rollback in one sentence, the change is too big.

**Spectrum lesson:** Every schema change in this repo is additive
and backward-compatible. Every feature flag defaults to False.
Rollback = flip the flag or revert the single prompt file. Changes
that cannot be rolled back in one step require human escalation.

---

## 9. Minimize Human Intervention

Humans act only at high-leverage points.

**Rule:** Design systems that rarely need human input but capture
it when used. Human intervention should produce a durable artifact
(GT pair, correction, annotation) — not a one-time fix.

**Spectrum lesson:** The correction miner opens a PR but never
auto-merges. The human reviews the before/after F1 and merges or
rejects. That single merge decision produces a durable improvement
to the prompt that benefits all future runs. Human judgment is
captured as a governed artifact.

---

## 10. Design for Scale Failure

They anticipate breakage at scale, not just current success.

**Rule:** Before any extraction prompt goes to production, run it
at 10x the expected chunk count and confirm it promotes. Ask what
fails when usage grows 10x before shipping.

**Spectrum lesson:** The Dec 18 transcript produced 138 speaker
turns, not the 34 assumed during development. At 138 chunks, Haiku
returned empty arrays for all 21 extraction types. The 10x question
was not asked. It should have been asked before the first production
run.

---

## Enforcement

These principles are enforced by:

1. **CLAUDE.md** — references this document; every Claude Code
   session is bound by it
2. **PR_FAILURE_PROTOCOL.md** — classifies violations by severity
3. **The autonomous fix script** — enforces Principle 7 structurally
   via the Class VI guard
4. **Red team passes** — every Claude Code prompt requires three
   embedded red team passes that check for violations of these
   principles

A PR that violates any principle must document the violation and the
justification in the PR description. A violation without justification
is grounds for rejection regardless of test results.
