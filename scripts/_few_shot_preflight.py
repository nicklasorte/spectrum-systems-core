"""Preflight detection for select-few-shot-candidates workflow.

Reasons this exists: the ``select_few_shot_examples.py`` script writes a
``NEEDS_REAL_EXAMPLES.md`` marker and exits non-zero when it cannot
find a ``meeting_extraction`` artifact for the requested ``source_id``,
but a mobile operator triggering the workflow from a phone never sees
that error — it's buried in the step logs. This module is invoked from
the workflow BEFORE the selection script runs so the failure mode
surfaces in ``$GITHUB_STEP_SUMMARY`` (which the phone UI renders) with
a concrete next-step instruction ("run debug-single-transcript first").

Pure stdlib so the workflow can run it before installing the package.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from typing import List, Optional, Tuple

_SOURCE_FAMILIES: Tuple[str, ...] = (
    "meetings", "books", "comments", "working_papers", "notes",
)


def resolve_source_artifact_id(
    data_lake: pathlib.Path, source_id: str
) -> Optional[str]:
    """Mirror of ``select_few_shot_examples._resolve_source_artifact_id``.

    Duplicated here so the preflight runs without importing the script
    (the script triggers ``pip install -e`` to satisfy ``jsonschema``,
    and we want the preflight to run before that install step).
    """
    store_root = data_lake / "store"
    for family in _SOURCE_FAMILIES:
        sr = store_root / "processed" / family / source_id / "source_record.json"
        if sr.is_file():
            try:
                doc = json.loads(sr.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            aid = doc.get("artifact_id") if isinstance(doc, dict) else None
            if isinstance(aid, str) and aid:
                return aid
    return None


def find_extraction_artifact(
    data_lake: pathlib.Path, source_id: str
) -> Tuple[Optional[pathlib.Path], List[str]]:
    """Return the matching meeting_extraction path and a diag trace.

    The trace is appended whether or not a match is found so the step
    summary can show the operator what was scanned.
    """
    diag: List[str] = []
    resolved = resolve_source_artifact_id(data_lake, source_id)
    diag.append(f"resolved source_artifact_id: `{resolved}`")

    ext_dir = data_lake / "store" / "artifacts" / "extractions"
    if not ext_dir.is_dir():
        diag.append(
            f"scanned directory does not exist: `{ext_dir}` — "
            "the extraction pipeline has not committed any artifacts."
        )
        return None, diag

    files = sorted(ext_dir.glob("*.json"))
    diag.append(f"extraction files on disk: `{len(files)}`")

    for path in files:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        if doc.get("source_id") == source_id:
            return path, diag
        if resolved and doc.get("source_artifact_id") == resolved:
            return path, diag
    return None, diag


def render_missing_summary(source_id: str, diag: List[str]) -> str:
    lines = [
        "## [BLOCKED] Few-shot selection cannot run",
        "",
        f"**Source ID:** `{source_id}`",
        "",
        "**Why:** No `meeting_extraction` artifact for this source_id",
        "exists in the workspace data-lake. The selection script reads",
        "the extraction artifact written by `debug-single-transcript.yml`",
        "(or by the full pipeline run). Without it, every candidate would",
        "be a placeholder.",
        "",
        "### Next step",
        "",
        "1. Trigger **Debug single transcript** for this source_id and",
        "   wait for it to commit pipeline artifacts to `main`:",
        "",
        f"   - source_id: `{source_id}`",
        "",
        "2. Re-trigger **Select few-shot candidates** for the same",
        "   source_id once the debug workflow has committed.",
        "",
        "### Preflight diagnostics",
        "",
    ]
    for line in diag:
        lines.append(f"- {line}")
    lines.append("")
    return "\n".join(lines)


def _append_step_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)
            if not text.endswith("\n"):
                fh.write("\n")
    except OSError:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--data-lake", required=True)
    args = parser.parse_args(argv)

    source_id = args.source_id.strip()
    data_lake = pathlib.Path(args.data_lake).resolve()

    if not data_lake.is_dir():
        msg = (
            f"## [BLOCKED] data-lake path does not exist\n\n"
            f"`{data_lake}` is not a directory. Check the workflow's "
            f"checkout step.\n"
        )
        _append_step_summary(msg)
        print(msg, file=sys.stderr)
        return 1

    path, diag = find_extraction_artifact(data_lake, source_id)
    if path is None:
        summary = render_missing_summary(source_id, diag)
        _append_step_summary(summary)
        print(summary, file=sys.stderr)
        return 2

    print(f"preflight: found extraction artifact at {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
