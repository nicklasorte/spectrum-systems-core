# Debugging Protocol

When you encounter a bug you cannot immediately fix, apply the scientific method.
Do not attempt repeated fixes without following this process — each cycle must move
closer to the root cause.

---

## Step 1 — Hypothesize

State a specific, falsifiable hypothesis about the root cause. Don't just describe
the symptom; explain the *mechanism* you believe is causing it.

> ❌ "The output is wrong."
> ✅ "The interpolation is returning NaN because the input frequency vector contains
>    a gap that falls outside the lookup table range."

---

## Step 2 — Design an Experiment

Propose a single, targeted test that directly probes the hypothesis. The experiment
must be designed so that **both a passing and a failing result tell us something
useful** — it must reduce the search space regardless of outcome.

**Rules:**
- Change only one thing at a time. Multi-variable experiments obscure causality.
- Prefer the simplest test that still discriminates between competing hypotheses.
- If you cannot construct an informative experiment, your hypothesis is too vague —
  go back to Step 1.

---

## Step 3 — Predict the Outcome

Before running anything, state:
- What you expect to observe **if the hypothesis is correct**
- What you expect to observe **if the hypothesis is wrong**

This forces honest reasoning and prevents post-hoc rationalization of results.

---

## Step 4 — Interpret the Result

After the experiment, explicitly update your belief:
- What did we learn?
- Which hypotheses are now eliminated?
- How has the search space changed?

If the result is ambiguous, that itself is data — explain why it's ambiguous
and redesign the experiment before proceeding.

---

## Step 5 — Iterate

Form the next hypothesis based on what is now known. Repeat until the root cause
is identified and confirmed.

Each cycle must visibly narrow the problem space. If two consecutive cycles produce
no new information, stop and surface the situation to the user rather than continuing
to guess.

---

## Anti-Patterns to Avoid

| Anti-Pattern | Why It Fails |
|---|---|
| Shotgun fixes (change multiple things at once) | Can't determine what actually worked |
| Fixing symptoms without identifying root cause | Bug returns in a different form |
| Running the same experiment twice | Wastes cycles, adds no information |
| Silently continuing after an ambiguous result | Compounds uncertainty |
| Declaring a fix before reproducing the original bug | No baseline to verify against |
