# PR failure protocol (non-negotiable)

When a PR check fails, Claude Code MUST follow
`docs/governance/PR_FAILURE_PROTOCOL.md` BEFORE touching any code:

1. Run `scripts/_pr_triage.py` or read the actual failing log.
2. Classify the failure using the taxonomy in the protocol document.
3. If INFRASTRUCTURE: document, do not change code.
4. If logic failure: root-cause first, minimum safe repair,
   governance hardening, no-weakening assertion.
5. PR description MUST include all sections A–G from the protocol.

Skipping the protocol and jumping directly to a code fix is a
CLAUDE.md violation. The goal is never "make CI green" — the goal
is to strengthen the governed system.

The `/pr-failure` slash command enforces this protocol interactively.
Prefer it over re-reading this file mid-session.
