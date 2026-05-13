# Known Infrastructure Failures

Pre-existing CI environment failures that reproduce on main without
any PR changes. These are documented, not fixed, unless the
infrastructure is upgraded.

Do NOT treat these as blocking failures for PR merges.
Do NOT attempt to fix these in application code.

## Active failures

### PDF extractor / prepare_pdf_cli (11 tests)
- **Tests:** `tests/ingestion/test_pdf_extractor.py`,
  `tests/ingestion/test_prepare_pdf_cli.py`
- **Error:** `pyo3_runtime.PanicException` / missing `_cffi_backend`
- **Root cause:** `cryptography` Python package requires a compiled C
  extension (`_cffi_backend`) that is not present in the GitHub Actions
  runner environment used by this repo
- **Reproduces on main:** YES (confirmed 2026-05-13)
- **Fix path:** Upgrade the Actions runner image or add a build step
  for the `cryptography` wheel — tracked in GitHub issue #[TBD]
- **First documented:** 2026-05-13
