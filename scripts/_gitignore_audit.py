"""Gitignore audit: enforce the artifact manifest's git-tracked claims.

Reads ``docs/architecture/artifact_manifest.md``, walks every artifact
entry whose status is ``Git-tracked: YES``, instantiates the path
template with synthetic ids, and verifies the path is NOT shadowed by
a gitignore rule in the repo that owns it.

Two repos own these paths:

  1. ``spectrum-systems-core`` — owns ``docs/decisions/*.judgment_record.json``
     (judgment records live alongside the constitution in this repo).
  2. ``nicklasorte/data-lake`` — owns every ``data-lake/store/...`` path.

After the data-lake migration (see ``migrate-data-lake-artifacts.yml``)
the ``data-lake/`` directory is a SEPARATE git repo. The audit's job
becomes:

  * Verify spectrum-systems-core's ``.gitignore`` carries the
    ``data-lake/`` rule so the separate repo cannot accidentally be
    re-committed into spectrum-systems-core.
  * For any path whose template starts with ``data-lake/``, check the
    data-lake repo's own gitignore (if a local clone is present).
    When the clone is absent (e.g. on the pytest workflow that does
    not need data-lake), the audit skips the per-path check and
    instead reports that data-lake was not on disk.
  * For any other manifest path (e.g. ``docs/decisions/...``), check
    spectrum-systems-core's gitignore as before.

Why a separate script (not a pytest case): the audit must run BEFORE
``pip install -e`` in CI so a missing-test-deps environment cannot
silently skip it. Pure stdlib for the same reason.

Usage:

    python scripts/_gitignore_audit.py
    python scripts/_gitignore_audit.py --manifest docs/architecture/artifact_manifest.md
    python scripts/_gitignore_audit.py --data-lake-root data-lake/

Exit codes:

    0 — every git-tracked path is un-ignored.
    1 — at least one path is ignored, OR the manifest is malformed.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
from typing import Dict, List, Tuple

# Synthetic placeholders the audit substitutes into path templates
# before calling ``git check-ignore``. Values are deliberately
# obviously-fake so an operator scanning git status after a failed
# audit can recognise them.
_PLACEHOLDER_VALUES: Dict[str, str] = {
    "<artifact_id>": "test-audit-uuid-0001",
    "<source_id>": "test-source-id-audit",
    "<source_artifact_id>": "test-source-artifact-uuid-0001",
    "<run_id>": "test-run-id-0001",
    "<failure_id>": "test-failure-id-0001",
    "<flag_name>": "test-flag-audit",
    "<chunk_id>": "test-chunk-0001",
    "<call_type>": "decision",
    "<datestamp>": "2026-01-01",
    "<slug>": "test-slug-audit",
    "<N>": "1",
}

# Regex pulls the H3 artifact name from a manifest section header.
_HEADING_RE = re.compile(r"^###\s+([A-Za-z0-9_().,/ -]+?)\s*$")

# Prefix used by every path template that lives in the data-lake repo.
_DATA_LAKE_PREFIX = "data-lake/"

# spectrum-systems-core rule that pins data-lake/ as a separate repo.
_REQUIRED_GITIGNORE_RULE = "data-lake/"


def _instantiate(path_template: str) -> str:
    out = path_template
    for placeholder, value in _PLACEHOLDER_VALUES.items():
        out = out.replace(placeholder, value)
    return out


def parse_manifest(manifest_path: pathlib.Path) -> List[Dict[str, object]]:
    """Extract artifact entries from the manifest.

    Returns a list of dicts with keys ``name``, ``path_template``,
    ``git_tracked`` (bool). The parser is intentionally lenient: it
    walks ``### <name>`` sections, looks for the FIRST
    ``Path template:`` and FIRST ``Git-tracked:`` line in the
    section, and skips sections that lack either. Sections under
    the "Runtime / debug artifacts" heading default to
    ``git_tracked=False`` and use the simpler ``Path:`` line shape.
    """
    text = manifest_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    entries: List[Dict[str, object]] = []
    current: Dict[str, object] = {}

    def flush() -> None:
        if not current:
            return
        if "name" in current and "path_template" in current:
            entries.append(dict(current))
        current.clear()

    for raw in lines:
        line = raw.rstrip()
        m = _HEADING_RE.match(line)
        if m:
            flush()
            current["name"] = m.group(1).strip()
            current["git_tracked"] = False
            continue
        if not current:
            continue

        # Tolerate either "Path template:" (top section) or "Path:"
        # (runtime / debug section). The first hit wins so a
        # secondary mention later in the prose is ignored. The
        # ``stripped`` value drops list-bullet, bold-marker, and
        # whitespace prefixes so the check is robust against the
        # markdown variants the manifest actually uses
        # (``- **Path template:** `...` ``).
        stripped = line.strip().lstrip("-").strip().strip("*").strip()
        if "path_template" not in current and stripped.lower().startswith(
            ("path template:", "path templates:", "path:")
        ):
            tail = stripped.split(":", 1)[1].strip()
            tick = re.search(r"`([^`]+)`", tail)
            if tick:
                current["path_template"] = tick.group(1)
        elif "path_template" not in current:
            tick = re.search(r"-\s*`(data-lake/[^`]+)`", line)
            if tick:
                current["path_template"] = tick.group(1)

        if stripped.lower().startswith("git-tracked:"):
            # Drop residual ``**`` that survives the prefix strip on
            # bold-marker-decorated lines like
            # ``- **Git-tracked:** YES`` (the trailing ``**`` lives
            # mid-string, so ``strip("*")`` does not remove it).
            verdict = stripped.split(":", 1)[1].strip().lstrip("*").strip().lower()
            current["git_tracked"] = verdict.startswith("yes")
    flush()
    return entries


def _check_ignore(cwd: pathlib.Path, test_path: str) -> Tuple[bool, str]:
    """Return ``(is_ignored, rule_text)`` from ``git -C cwd check-ignore``.

    Handles the negation-pattern case the same way the original
    implementation did: ``git check-ignore -v`` returns rc=0 for any
    matched pattern including ``!`` un-ignore patterns, so we must
    inspect the matched pattern itself before declaring the path
    ignored.
    """
    result = subprocess.run(
        ["git", "-C", str(cwd), "check-ignore", "-v", "--no-index", test_path],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 1:
        return False, ""
    if result.returncode == 0:
        line = result.stdout.strip()
        try:
            head, _ = line.split("\t", 1)
            pattern = head.rsplit(":", 1)[1]
        except (IndexError, ValueError):
            raise RuntimeError(
                f"git check-ignore -v produced unparseable output: {line!r}"
            )
        if pattern.startswith("!"):
            return False, ""
        return True, line
    raise RuntimeError(
        f"git check-ignore returned unexpected rc={result.returncode}: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def _verify_data_lake_separation(repo_root: pathlib.Path) -> List[str]:
    """Verify ``data-lake/`` is gitignored in spectrum-systems-core.

    The repo-level invariant after the data-lake migration: every
    workflow clones nicklasorte/data-lake into ``data-lake/`` at run
    time, and that directory must NEVER be re-committed into
    spectrum-systems-core. The simplest way to enforce that is to
    require ``data-lake/`` to live in ``.gitignore`` so a stray
    ``git add data-lake`` is a no-op.
    """
    gitignore = repo_root / ".gitignore"
    if not gitignore.is_file():
        return [f"missing .gitignore at {gitignore}"]
    body = gitignore.read_text(encoding="utf-8")
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in (_REQUIRED_GITIGNORE_RULE, _REQUIRED_GITIGNORE_RULE.rstrip("/")):
            return []
    return [
        f"required .gitignore rule '{_REQUIRED_GITIGNORE_RULE}' not present at {gitignore}. "
        "data-lake/ is a separate git repository (nicklasorte/data-lake); "
        "it must NEVER be re-committed into spectrum-systems-core."
    ]


def _is_data_lake_repo(path: pathlib.Path) -> bool:
    """Return True when ``path`` is a checkout of nicklasorte/data-lake."""
    if not (path / ".git").exists():
        return False
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    if result.returncode != 0:
        return False
    return "nicklasorte/data-lake" in result.stdout


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default="docs/architecture/artifact_manifest.md",
        help="Path to artifact_manifest.md",
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Path to the spectrum-systems-core repo root (default: cwd)",
    )
    parser.add_argument(
        "--data-lake-root",
        default="data-lake",
        help="Path where the data-lake repo is (or would be) cloned (default: data-lake)",
    )
    args = parser.parse_args(argv)

    repo_root = pathlib.Path(args.repo_root).resolve()
    manifest_path = pathlib.Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = repo_root / manifest_path
    if not manifest_path.is_file():
        print(f"ERROR: manifest not found at {manifest_path}", file=sys.stderr)
        return 1

    findings: List[str] = []
    findings.extend(_verify_data_lake_separation(repo_root))

    try:
        entries = parse_manifest(manifest_path)
    except (OSError, ValueError) as exc:
        print(f"ERROR: failed to parse manifest: {exc}", file=sys.stderr)
        return 1

    tracked = [e for e in entries if e.get("git_tracked")]
    if not tracked:
        print(
            "ERROR: manifest contains zero git-tracked entries — refusing "
            "to silently pass.",
            file=sys.stderr,
        )
        return 1

    data_lake_path = pathlib.Path(args.data_lake_root)
    if not data_lake_path.is_absolute():
        data_lake_path = repo_root / data_lake_path
    data_lake_present = _is_data_lake_repo(data_lake_path)
    if data_lake_present:
        print(f"Data-lake clone detected at {data_lake_path}; per-path audit enabled.")
    else:
        print(
            f"Data-lake clone NOT present at {data_lake_path}; "
            "per-path audit limited to non-data-lake paths."
        )

    print(f"Auditing {len(tracked)} git-tracked artifact type(s) from manifest...")
    failures: List[Dict[str, str]] = []
    skipped_data_lake = 0
    for entry in tracked:
        name = str(entry["name"])
        template = str(entry["path_template"])
        instantiated = _instantiate(template)

        if instantiated.startswith(_DATA_LAKE_PREFIX):
            # Strip the data-lake/ prefix and check against the
            # data-lake repo's own gitignore.
            rel_path = instantiated[len(_DATA_LAKE_PREFIX):]
            if not data_lake_present:
                print(f"  SKIP: {name} (data-lake not cloned locally)")
                skipped_data_lake += 1
                continue
            try:
                is_ignored, rule = _check_ignore(data_lake_path, rel_path)
            except RuntimeError as exc:
                print(f"  ERROR: {name}: {exc}", file=sys.stderr)
                return 1
            if is_ignored:
                failures.append({"name": name, "path": template, "rule": rule})
                print(f"  FAIL: {name} — gitignored inside nicklasorte/data-lake")
                print(f"    path: {template}")
                print(f"    rule: {rule}")
            else:
                print(f"  OK:   {name}")
            continue

        # Non-data-lake path — audit against spectrum-systems-core.
        try:
            is_ignored, rule = _check_ignore(repo_root, instantiated)
        except RuntimeError as exc:
            print(f"  ERROR: {name}: {exc}", file=sys.stderr)
            return 1
        if is_ignored:
            failures.append({"name": name, "path": template, "rule": rule})
            print(f"  FAIL: {name} — gitignored inside spectrum-systems-core")
            print(f"    path: {template}")
            print(f"    rule: {rule}")
        else:
            print(f"  OK:   {name}")

    if findings:
        print(
            f"\nGITIGNORE AUDIT FAILED: {len(findings)} repo-level invariant(s) "
            "missing",
            file=sys.stderr,
        )
        for f in findings:
            print(f"  - {f}", file=sys.stderr)
        return 1

    if failures:
        print(
            f"\nGITIGNORE AUDIT FAILED: {len(failures)} path(s) are ignored",
            file=sys.stderr,
        )
        print(
            "Fix: add an explicit '!<path>' negation to the appropriate "
            ".gitignore in the repo that owns the path "
            "(spectrum-systems-core for non-data-lake paths, "
            "nicklasorte/data-lake for data-lake/ paths).",
            file=sys.stderr,
        )
        return 1

    print(
        f"\nOK: data-lake separation rule present; "
        f"{len(tracked) - skipped_data_lake} audited path(s) un-ignored "
        f"({skipped_data_lake} data-lake path(s) skipped — clone absent)."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
