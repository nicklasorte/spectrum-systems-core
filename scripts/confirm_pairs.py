"""Confirm pending ground_truth_pair artifacts in the data-lake.

Usage:
    python scripts/confirm_pairs.py \\
        --data-lake /path/to/data-lake \\
        --schema-dir /path/to/spectrum-systems-core/contracts/schemas

Add --dry-run to print the pairing table without writing any files.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator


GROUND_TRUTH_SUBDIR = Path("store") / "artifacts" / "ground_truth"
SCHEMA_RELPATH = Path("ingestion") / "ground_truth_pair.schema.json"
CONFIRMED_BY = "human_operator"


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _load_validator(schema_dir: Path) -> Draft202012Validator:
    schema_path = schema_dir / SCHEMA_RELPATH
    with schema_path.open("r", encoding="utf-8") as fh:
        schema = json.load(fh)
    if "status" not in schema.get("properties", {}):
        raise SystemExit(
            f"STOP: schema at {schema_path} has no 'status' field; "
            "confirm-pairs cannot proceed."
        )
    return Draft202012Validator(schema)


def _iter_pair_files(ground_truth_dir: Path):
    if not ground_truth_dir.is_dir():
        return []
    return sorted(p for p in ground_truth_dir.iterdir() if p.suffix == ".json")


def _print_table(rows: list[dict]) -> None:
    headers = ["pair_id", "meeting_date", "meeting_name", "match_confidence", "status"]
    widths = {h: len(h) for h in headers}
    for r in rows:
        for h in headers:
            widths[h] = max(widths[h], len(str(r.get(h, ""))))
    line = " | ".join(h.ljust(widths[h]) for h in headers)
    print(line)
    print("-+-".join("-" * widths[h] for h in headers))
    for r in rows:
        print(" | ".join(str(r.get(h, "")).ljust(widths[h]) for h in headers))


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-lake",
        required=True,
        type=Path,
        help="Path to the data-lake repo root.",
    )
    parser.add_argument(
        "--schema-dir",
        required=True,
        type=Path,
        help="Path to contracts/schemas/ in spectrum-systems-core.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the pairing table but do not write any files.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    validator = _load_validator(args.schema_dir)

    ground_truth_dir = args.data_lake / GROUND_TRUTH_SUBDIR
    files = _iter_pair_files(ground_truth_dir)
    if not files:
        print(f"warning: no ground_truth_pair artifacts found in {ground_truth_dir}")
        return 0

    rows: list[dict] = []
    confirmed = 0
    skipped = 0
    failed = 0
    total_pending = 0

    for path in files:
        try:
            with path.open("r", encoding="utf-8") as fh:
                artifact = json.load(fh)
        except Exception as exc:
            print(f"error: could not read {path.name}: {exc}", file=sys.stderr)
            failed += 1
            continue

        status = artifact.get("status")
        row = {
            "pair_id": artifact.get("pair_id", ""),
            "meeting_date": artifact.get("meeting_date", ""),
            "meeting_name": artifact.get("meeting_name", ""),
            "match_confidence": artifact.get("match_confidence", ""),
            "status": status or "",
        }

        if status == "confirmed":
            skipped += 1
            rows.append(row)
            continue

        if status != "pending_review":
            rows.append(row)
            continue

        total_pending += 1

        if args.dry_run:
            rows.append(row)
            continue

        updated = dict(artifact)
        updated["status"] = "confirmed"
        updated["confirmed_at"] = _utc_now_iso()
        updated["confirmed_by"] = CONFIRMED_BY

        errors = sorted(validator.iter_errors(updated), key=lambda e: e.path)
        if errors:
            messages = "; ".join(e.message for e in errors)
            print(
                f"error: validation failed for {path.name}: {messages}",
                file=sys.stderr,
            )
            failed += 1
            rows.append(row)
            continue

        try:
            with path.open("w", encoding="utf-8") as fh:
                json.dump(updated, fh, indent=2, sort_keys=True)
                fh.write("\n")
        except Exception as exc:
            print(f"error: could not write {path.name}: {exc}", file=sys.stderr)
            failed += 1
            rows.append(row)
            continue

        confirmed += 1
        row["status"] = "confirmed"
        rows.append(row)

    _print_table(rows)
    print()
    if args.dry_run:
        print(
            f"dry run: {total_pending} pending_review pair(s) would be confirmed. "
            f"{skipped} already confirmed. {failed} failed."
        )
    else:
        print(
            f"{confirmed} of {total_pending} pairs confirmed. "
            f"{skipped} skipped (already confirmed). {failed} failed."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
