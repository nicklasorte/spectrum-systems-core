# Integration test requirement (non-negotiable)

Every Claude Code session that writes or modifies a script that reads a
pipeline artifact MUST also write or update an integration test that:

1. Uses `tests/integration/fixtures.py` factory functions to produce
   artifacts — NEVER hand-rolled dicts. The factory must call the
   actual writer (`ExtractionMerger.merge`, runner, etc.), not
   construct a dict manually.
2. Writes artifacts to a real temp directory (not mocked).
3. Calls the script via `subprocess.run` against the temp directory.
4. Asserts the correct output on disk (not just the return code).

This rule exists because unit tests with synthetic fixtures do not catch
field name mismatches between the writer and the reader — that is the
exact bug class that produced PRs #77 / #78 / #79. Integration tests
backed by `tests/integration/fixtures.py` catch the drift at the
fixture factory level, before the script logic runs.

Co-requirement: every script that reads a pipeline artifact MUST call
`scripts/_artifact_validator.validate_artifact` on the loaded artifact
before reading any field off it. This adds a second line of defence —
the script refuses to run on an artifact whose `artifact_type` or
schema shape has drifted, instead of failing mysteriously inside the
script's logic.

The canonical integration-contract file is
`tests/integration/test_script_artifact_contracts.py`. New per-script
contract tests should either land there as additional functions or in
a sibling `tests/integration/test_<script_stem>_contract.py` file.

## Compliance check

Run before opening a PR:

```bash
python scripts/_integration_test_check.py
```

The check is scoped to scripts touched in the current PR (vs
`origin/main`) plus untracked scripts; pre-existing scripts without
contract coverage are not blocked. Coverage is accepted under either
`tests/integration/` (preferred — uses the `fixtures.py` factories per
the rule above) OR `tests/scripts/` (historical). New contract tests
MUST go under `tests/integration/` to satisfy the fixture-factory
clause.

The script runs as the `Stop` hook `_integration_test_check.py`, so a
session that ends with missing coverage is blocked before push.
