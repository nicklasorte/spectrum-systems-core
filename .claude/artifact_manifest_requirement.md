# Artifact manifest requirement (non-negotiable)

`docs/architecture/artifact_manifest.md` is the single authoritative
list of every artifact type the pipeline writes to disk. Every Claude
Code session that adds a new artifact type, or that changes an
existing artifact's path, schema, or git-tracked status, MUST:

1. Update `docs/architecture/artifact_manifest.md` so the entry
   reflects the new path / schema / tracked status.
2. Run `python scripts/_gitignore_audit.py` and confirm it passes.
   The audit reads the manifest and asserts every "Git-tracked: YES"
   path template is NOT shadowed by any `.gitignore` rule.
3. If the artifact is read by a `scripts/*.py` consumer, add or
   update the factory function in `tests/integration/fixtures.py`
   so the integration-test layer can produce the artifact via the
   real writer (per the integration test requirement).

## Compliance check

```bash
python scripts/_gitignore_audit.py
```

The audit must exit 0. If it fails, either un-ignore the path in the
appropriate `.gitignore` (mirror the existing `!**/processed/**/source_record.json`
pattern), move the artifact to a different on-disk path, or — if the
artifact is genuinely runtime-only — flip its manifest entry to
`Git-tracked: NO` and remove any workflow `git add` that targets it.
