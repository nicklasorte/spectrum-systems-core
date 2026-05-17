---
description: Apply the scientific method to a bug that resists an obvious fix
---

You are debugging a problem that does not have an obvious fix. Do not attempt repeated fixes. Follow this protocol strictly. Each cycle must visibly narrow the search space.

## Step 1 — Hypothesize

State a specific, falsifiable hypothesis about the root cause. Describe the *mechanism* you believe is causing the bug, not just the symptom.

- ❌ "The output is wrong."
- ✅ "The interpolation returns NaN because the input frequency vector contains a gap outside the lookup table range."

If you cannot state a mechanism, say so explicitly and ask for more information rather than guessing.

## Step 2 — Design an Experiment

Propose a single, targeted test that directly probes the hypothesis. The experiment must be designed so that **both a passing and a failing result reduce the search space**.

Rules:
- Change only one thing at a time.
- Prefer the simplest test that discriminates between competing hypotheses.
- If you cannot construct an informative experiment, the hypothesis is too vague — return to Step 1.

## Step 3 — Predict the Outcome

Before running the experiment, state in writing:
- What you expect to observe **if the hypothesis is correct**
- What you expect to observe **if the hypothesis is wrong**

Do not skip this step. It prevents post-hoc rationalization.

## Step 4 — Run the Experiment and Interpret

After the experiment, explicitly update your belief:
- What did we learn?
- Which hypotheses are now eliminated?
- How has the search space changed?

If the result is ambiguous, explain why and redesign the experiment. Do not proceed on ambiguous evidence.

## Step 5 — Iterate

Form the next hypothesis based on what is now known. Repeat until the root cause is identified and confirmed by reproducing and then fixing the bug.

If two consecutive cycles produce no new information, stop and surface the situation to the user rather than continuing to guess.

## Anti-Patterns — Do Not Do These

- Shotgun fixes (changing multiple things at once)
- Fixing the symptom without identifying the root cause
- Running the same experiment twice
- Silently continuing after an ambiguous result
- Declaring a fix before reproducing the original bug

## Output Format

For each cycle, output in this structure:

**Cycle N**
- Hypothesis:
- Experiment:
- Prediction (if correct / if wrong):
- Result:
- Interpretation + updated belief:
- Next step:
