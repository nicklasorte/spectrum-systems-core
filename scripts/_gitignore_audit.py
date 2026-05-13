"""Gitignore audit: enforce the artifact manifest's git-tracked claims.

Reads ``docs/architecture/artifact_manifest.md``, walks every artifact
entry whose status is ``Git-tracked: YES``, instantiates the path
template with synthetic ids, and shells out to ``git check-ignore``.

If any path IS ignored, the audit fails loudly with the path AND the
exact ``.gitignore`` rule that ignores it so the operator can patch
the right file.

Why a separate script (not a pytest case): the audit must run BEFORE
``pip install -e`` in CI so a missing-test-deps environment cannot
silently skip it. Pure stdlib for the same reason.

Usage:

    python scripts/_gitignore_audit.py
    python scripts/_gitignore_audit.py --manifest docs/architecture/artifact_manifest.md

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
}

# Regex pulls the H3 artifact name from a manifest section header.
_HEADING_RE = re.compile(r"^###\s+([A-Za-z0-9_().,/ -]+?)\s*$")


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


def check_path_not_ignored(path_template: str) -> Tuple[bool, str]:
    """Return ``(is_ignored, rule_text)`` for a single path template.

    ``rule_text`` is empty when the path is not ignored.

    ``git check-ignore -v`` exits 0 whenever ANY pattern matches the
    path — even a negation (``!``) pattern that re-includes a
    previously-ignored path. To decide whether the path is actually
    ignored, we must inspect the matched pattern itself: if it starts
    with ``!`` the path is being un-ignored, so we treat it as
    not-ignored. rc=1 means no pattern matched at all (also
    not-ignored). Any other rc is a hard error.
    """
    test_path = _instantiate(path_template)
    result = subprocess.run(
        ["git", "check-ignore", "-v", "--no-index", test_path],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 1:
        return False, ""
    if result.returncode == 0:
        line = result.stdout.strip()
        # Format: ``<source>:<linenum>:<pattern>\t<pathname>``
        # Split on the LAST ':' before the tab to recover <pattern>.
        try:
            head, _ = line.split("\t", 1)
            pattern = head.rsplit(":", 1)[1]
        except (IndexError, ValueError):
            # Malformed output — surface it rather than silently
            # accepting the path.
            raise RuntimeError(
                f"git check-ignore -v produced unparseable output: {line!r}"
            )
        if pattern.startswith("!"):
            return False, ""
        return True, line
    # rc=128 from git typically means "not in a git repo" — surface
    # it loudly so a CI misconfiguration does not silently pass.
    raise RuntimeError(
        f"git check-ignore returned unexpected rc={result.returncode}: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default="docs/architecture/artifact_manifest.md",
        help="Path to artifact_manifest.md (default: docs/architecture/artifact_manifest.md)",
    )
    args = parser.parse_args(argv)

    manifest_path = pathlib.Path(args.manifest)
    if not manifest_path.is_file():
        print(f"ERROR: manifest not found at {manifest_path}", file=sys.stderr)
        return 1

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

    print(f"Auditing {len(tracked)} git-tracked artifact type(s) from manifest...")
    failures: List[Dict[str, str]] = []
    for entry in tracked:
        name = str(entry["name"])
        template = str(entry["path_template"])
        try:
            is_ignored, rule = check_path_not_ignored(template)
        except RuntimeError as exc:
            print(f"  ERROR: {name}: {exc}", file=sys.stderr)
            return 1
        if is_ignored:
            failures.append({"name": name, "path": template, "rule": rule})
            print(f"  FAIL: {name} — path is gitignored")
            print(f"    path: {template}")
            print(f"    rule: {rule}")
        else:
            print(f"  OK:   {name}")

    if failures:
        print(
            f"\nGITIGNORE AUDIT FAILED: {len(failures)} path(s) are ignored",
            file=sys.stderr,
        )
        print(
            "Fix: add an explicit '!<path>' negation to the appropriate "
            ".gitignore (mirror the existing "
            "'!**/processed/**/source_record.json' pattern), or move the "
            "artifact to a path that is not shadowed by a broader rule.",
            file=sys.stderr,
        )
        return 1

    print(
        f"\nOK: all {len(tracked)} git-tracked artifact paths are un-ignored."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
