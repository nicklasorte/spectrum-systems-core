# CI

PRs run `pytest` via GitHub Actions (`.github/workflows/pytest.yml`). The
same workflow runs on pushes to `main`.

## Local command

```
python -m pytest
```

## Scope

CI is intentionally minimal: one job, one Python version (3.11), one
command. No coverage gates, no linting, no matrix. Add more only when a
concrete need appears.
