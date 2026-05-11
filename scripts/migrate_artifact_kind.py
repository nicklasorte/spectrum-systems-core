"""One-time migration: add artifact_type alongside artifact_kind.

Scans every ``*.json`` file under ``--data-lake`` and, for each artifact
that has ``artifact_kind`` but no ``artifact_type``, copies the value of
``artifact_kind`` into a new ``artifact_type`` field. ``artifact_kind`` is
preserved (step 1 of 2 — step 2 will remove it after this migration is
verified clean).

Modes:
    --dry-run (default)  scan only; report counts; write nothing.
    --confirm            apply changes in place (overwrites the file with
                         a sorted, indented JSON serialization).

Exit code:
    0  always (script never aborts even if some files are invalid JSON;
        those files are reported but skipped so a partial run is visible
        and re-runnable).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple


def _is_artifact(obj: object) -> bool:
    """Return True if ``obj`` looks like a single artifact dict."""
    return isinstance(obj, dict) and (
        "artifact_kind" in obj or "artifact_type" in obj
    )


def _scan_file(path: Path) -> Tuple[str, Dict[str, object]]:
    """Read ``path`` and classify it.

    Returns (status, details) where status is one of:
    - "needs_migration": has artifact_kind, no artifact_type
    - "already_migrated": has artifact_type
    - "no_artifact": valid JSON but not an artifact (skip silently)
    - "invalid_json": file could not be parsed
    - "read_error": file could not be read
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return "read_error", {"error": str(exc)}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return "invalid_json", {"error": str(exc)}

    if not _is_artifact(data):
        return "no_artifact", {}

    if "artifact_type" in data:
        return "already_migrated", {"data": data}
    if "artifact_kind" in data:
        return "needs_migration", {"data": data}
    return "no_artifact", {}


def _migrate(data: Dict[str, object]) -> Dict[str, object]:
    """Return a new dict with artifact_type = artifact_kind value."""
    out = dict(data)
    out["artifact_type"] = out["artifact_kind"]
    return out


def _write_atomic(path: Path, data: Dict[str, object]) -> None:
    """Write ``data`` to ``path`` atomically via rename."""
    tmp = path.with_suffix(path.suffix + ".migrate.tmp")
    tmp.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def run(data_lake: Path, dry_run: bool) -> Dict[str, int]:
    counts = {
        "scanned": 0,
        "migrated": 0,
        "already_migrated": 0,
        "no_artifact": 0,
        "invalid_json": 0,
        "read_error": 0,
        "would_migrate": 0,
    }
    examples: Dict[str, List[str]] = {
        "would_migrate": [],
        "invalid_json": [],
        "read_error": [],
    }

    if not data_lake.exists():
        print(f"ERROR: --data-lake path does not exist: {data_lake}")
        return counts

    for path in sorted(data_lake.rglob("*.json")):
        counts["scanned"] += 1
        status, details = _scan_file(path)
        if status == "needs_migration":
            if dry_run:
                counts["would_migrate"] += 1
                if len(examples["would_migrate"]) < 5:
                    examples["would_migrate"].append(str(path))
            else:
                _write_atomic(path, _migrate(details["data"]))
                counts["migrated"] += 1
        elif status == "already_migrated":
            counts["already_migrated"] += 1
        elif status == "no_artifact":
            counts["no_artifact"] += 1
        elif status == "invalid_json":
            counts["invalid_json"] += 1
            if len(examples["invalid_json"]) < 5:
                examples["invalid_json"].append(str(path))
        elif status == "read_error":
            counts["read_error"] += 1
            if len(examples["read_error"]) < 5:
                examples["read_error"].append(str(path))

    mode = "DRY-RUN" if dry_run else "APPLY"
    print(f"=== migrate_artifact_kind ({mode}) ===")
    print(f"  data_lake: {data_lake}")
    print(f"  scanned:           {counts['scanned']}")
    if dry_run:
        print(f"  would_migrate:     {counts['would_migrate']}")
    else:
        print(f"  migrated:          {counts['migrated']}")
    print(f"  already_migrated:  {counts['already_migrated']}")
    print(f"  no_artifact:       {counts['no_artifact']}")
    print(f"  invalid_json:      {counts['invalid_json']}")
    print(f"  read_error:        {counts['read_error']}")

    if dry_run and examples["would_migrate"]:
        print("  examples (would_migrate, up to 5):")
        for p in examples["would_migrate"]:
            print(f"    - {p}")
    if examples["invalid_json"]:
        print("  examples (invalid_json, up to 5):")
        for p in examples["invalid_json"]:
            print(f"    - {p}")
    if examples["read_error"]:
        print("  examples (read_error, up to 5):")
        for p in examples["read_error"]:
            print(f"    - {p}")

    return counts


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate artifact_kind -> artifact_type (additive).",
    )
    parser.add_argument(
        "--data-lake",
        required=True,
        help="Root path of the data lake to scan.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Report changes only (DEFAULT). Mutually exclusive with --confirm.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help="Apply changes in place. Overrides --dry-run.",
    )
    args = parser.parse_args(argv)
    dry_run = not args.confirm
    run(Path(args.data_lake), dry_run=dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
