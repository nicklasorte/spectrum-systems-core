# Rollback contracts requirement (non-negotiable)

The `verify-rollback-contracts` CI workflow
(`.github/workflows/verify-rollback-contracts.yml`) fires on every
PR that touches any of:

- `src/spectrum_systems_core/schemas/**`
- `src/spectrum_systems_core/pipeline/**`
- `src/spectrum_systems_core/calibration/**`
- `src/spectrum_systems_core/promotion/**`
- `scripts/verify_rollback_contracts.py`
- `docs/architecture/rollback_contracts.md`

It runs `scripts/verify_rollback_contracts.py --pr <PR#>` and FAILS
when `docs/architecture/rollback_contracts.md` does not contain an
entry that:

1. References the PR number (e.g. `(PR #236)` or `PR #236`).
2. Mentions at least one of the PR's changed file paths.
3. Includes a `verification_command` from the whitelist:
   `pytest <path>`, `python scripts/<name>.py`, or
   `python -m spectrum_systems_core.<module>`.

## Pre-PR compliance check

```bash
python scripts/verify_rollback_contracts.py --pr <N> \
  --changed-files "$(git diff --name-only origin/main|paste -sd,-)"
```

When no PR number exists yet (most of the session), the proxy
check is: did this session modify any path in the watched globs
above? If yes, append an entry to
`docs/architecture/rollback_contracts.md` with the structure
documented at the bottom of that file ("How to add a new entry"
section) — at minimum, **What this change adds**, **To roll back**,
**Data migration required for rollback**, **Verification that the
rollback is clean**, and a `verification_command:` line on its own
line from the whitelist. Mirror the most recent existing entry's
structure.

The entry heading must reference the assigned PR number. After
opening the PR, run `python scripts/finalize_rollback_entry.py
--pr <N> --commit && git push`. The Stop hook
`scripts/_pre_pr_rollback_check.py` blocks on both "no entry" and
"heading still says `PR #TBD` after push".
