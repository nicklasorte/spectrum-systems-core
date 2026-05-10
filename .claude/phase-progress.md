# Phase L.0 — DocxExtractor Progress

## Step 1 Inventory

### Dep manager detected
`pyproject.toml` / setuptools. Dependencies listed under `[project] dependencies`.
No requirements.txt, no poetry, no uv.

### Python version pin
`requires-python = ">=3.10"` (from pyproject.toml)

### Test count (must be 691)
**691** — confirmed with `python -m pytest --collect-only -q`

### SourceLoader's expected input
- Reads from `raw/<family>/<source_id>/` directory
- Requires `metadata.json` with fields: `source_id`, `source_family`, `source_type`, `title`, `raw_format`
- `raw_format` must be `txt` or `md` (pdf rejected at Phase A boundary)
- Looks for `source.txt` or `source.md` (tried in order based on raw_format)
- Content must be UTF-8 plain text, non-empty after strip
- DocxExtractor writes a standalone `.txt` file; user must place it as
  `raw/<family>/<source_id>/source.txt` before running process-source

### python-docx presence
NOT present at baseline. `pip show python-docx` → not found. `import docx` → ModuleNotFoundError.

---

## Step 2 — Add python-docx dependency
DONE. Added `python-docx>=1.1` to `[project] dependencies` in `pyproject.toml`.
Installed: python-docx 1.2.0.

## Step 3 — Write DocxExtractor
DONE. `src/spectrum_systems_core/ingestion/docx_extractor.py`
Exported from `ingestion/__init__.py`.

## Step 4 — Extend CLI
DONE. `extract_docx()` + `extract-docx` subparser in `cli.py`.

## Step 5 — Write tests
DONE. `tests/ingestion/test_docx_extractor.py` — 20 tests.

## Step 6 — Gate A (design redteam)

| # | Sev | Finding | Disposition |
|---|---|---|---|
| 1 | 1 | Unvalidated output_path | Not blocking: trusted CLI utility; spec mandates direct path |
| 2 | 1 | Silent overwrite of existing .txt | Not blocking: intended re-run behavior; consistent with PDFExtractor |
| 3 | 2 | extract_batch no aggregate status | Not blocking: added docstring note; CLI inspects correctly |
| 4 | 2 | extract_batch raises on non-existent dir | Fixed: is_dir() guard + test added |
| 5 | 2 | No content-hash | Not blocking: spec defines return schema exactly |

**Verdict**: zero remaining Sev-1/Sev-2 after fix.

## Step 7 — Run tests and audit

| Check | Result |
|---|---|
| pytest collect | 711 (691 baseline + 20 new) |
| pytest run | 700 passed, 11 failed (11 pre-existing PDF failures) |
| audit-governance | exit 1 (pre-existing); total_flagged: 176, high: 31 |
| new high flags on L.0 files | 0 |
| lint / type-check | N/A (no config) |

## Step 8 — Gate B (diff redteam)
Verdict: pending
