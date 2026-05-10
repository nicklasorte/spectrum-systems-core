"""Retire duplicate ground_truth_pair artifacts in the data-lake.

A duplicate is any ground_truth_pair where another artifact shares the
same (source_artifact_id, minutes_artifact_id). Per duplicate group, the
oldest ``confirmed`` artifact is kept (falling back to the oldest overall
if no member is confirmed). All other members are moved to
``$SDL_ROOT/store/artifacts/ground_truth/retired/<pair_id>.json`` and a
sidecar ``<pair_id>.retired_reason.json`` is written alongside.

No artifact is ever deleted. Singleton groups are left alone.

Usage:
    python scripts/cleanup_duplicate_pairs.py \\
        --data-lake /path/to/data-lake \\
        --schema-dir /path/to/spectrum-systems-core/contracts/schemas

Add --dry-run to print what would be retired without moving any files.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import sys
import traceback
from pathlib import Path

from jsonschema import Draft202012Validator


GROUND_TRUTH_SUBDIR = Path("store") / "artifacts" / "ground_truth"
RETIRED_SUBDIR = "retired"
SCHEMA_RELPATH = Path("ingestion") / "ground_truth_pair.schema.json"


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _load_validator(schema_dir: Path) -> Draft202012Validator:
    schema_path = schema_dir / SCHEMA_RELPATH
    with schema_path.open("r", encoding="utf-8") as fh:
        schema = json.load(fh)
    return Draft202012Validator(schema)


def _iter_pair_files(ground_truth_dir: Path) -> list[Path]:
    if not ground_truth_dir.is_dir():
        return []
    return sorted(
        p for p in ground_truth_dir.iterdir() if p.is_file() and p.suffix == ".json"
    )


def _is_pair_artifact(artifact: dict) -> bool:
    if not isinstance(artifact, dict):
        return False
    if not artifact.get("pair_id"):
        return False
    provenance = artifact.get("provenance") or {}
    return provenance.get("produced_by") == "GroundTruthLinker"


def _print_table(rows: list[dict]) -> None:
    headers = [
        "kept_pair_id",
        "meeting_date",
        "meeting_name",
        "status",
        "duplicates_retired",
    ]
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
        help="Print what would be retired without moving files.",
    )
    return parser.parse_args(argv)


def _pick_kept(group: list[tuple[Path, dict]]) -> tuple[Path, dict]:
    # Sort oldest first. Use the empty string as a fallback so a missing
    # created_at sorts to the front deterministically.
    ordered = sorted(group, key=lambda item: item[1].get("created_at") or "")
    confirmed = [item for item in ordered if item[1].get("status") == "confirmed"]
    if confirmed:
        return confirmed[0]
    return ordered[0]


def _process_group(
    key: tuple[str, str],
    group: list[tuple[Path, dict]],
    retired_dir: Path,
    validator: Draft202012Validator,
    dry_run: bool,
) -> dict | None:
    try:
        kept_path, kept_artifact = _pick_kept(group)
        kept_pair_id = kept_artifact.get("pair_id", "")

        errors = sorted(
            validator.iter_errors(kept_artifact), key=lambda e: list(e.path)
        )
        if errors:
            messages = "; ".join(e.message for e in errors)
            print(
                f"error: kept candidate {kept_path.name} failed schema "
                f"validation for group {key}: {messages}; skipping group",
                file=sys.stderr,
            )
            return None

        duplicates = [item for item in group if item[0] != kept_path]
        retired_count = 0

        for dup_path, dup_artifact in duplicates:
            dup_pair_id = dup_artifact.get("pair_id") or dup_path.stem
            dest = retired_dir / f"{dup_pair_id}.json"
            sidecar = retired_dir / f"{dup_pair_id}.retired_reason.json"
            reason = {
                "original_pair_id": dup_pair_id,
                "retired_at": _utc_now_iso(),
                "retired_reason": f"duplicate_of:{kept_pair_id}",
                "kept_pair_id": kept_pair_id,
            }

            if dry_run:
                print(
                    f"would retire {dup_path.name} -> "
                    f"{RETIRED_SUBDIR}/{dest.name} "
                    f"(duplicate_of:{kept_pair_id})"
                )
                retired_count += 1
                continue

            retired_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dup_path), str(dest))
            with sidecar.open("w", encoding="utf-8") as fh:
                json.dump(reason, fh, indent=2, sort_keys=True)
                fh.write("\n")
            retired_count += 1

        return {
            "kept_pair_id": kept_pair_id,
            "meeting_date": kept_artifact.get("meeting_date", ""),
            "meeting_name": kept_artifact.get("meeting_name", ""),
            "status": kept_artifact.get("status", ""),
            "duplicates_retired": retired_count,
        }
    except Exception as exc:  # never raise
        print(
            f"error: group {key} failed: {exc}\n{traceback.format_exc()}",
            file=sys.stderr,
        )
        return None


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    validator = _load_validator(args.schema_dir)

    ground_truth_dir = args.data_lake / GROUND_TRUTH_SUBDIR
    retired_dir = ground_truth_dir / RETIRED_SUBDIR

    files = _iter_pair_files(ground_truth_dir)
    if not files:
        print(f"warning: no ground_truth_pair artifacts found in {ground_truth_dir}")
        return 0

    loaded: list[tuple[Path, dict]] = []
    for path in files:
        try:
            with path.open("r", encoding="utf-8") as fh:
                artifact = json.load(fh)
        except Exception as exc:
            print(f"error: could not read {path.name}: {exc}", file=sys.stderr)
            continue
        if not _is_pair_artifact(artifact):
            continue
        loaded.append((path, artifact))

    groups: dict[tuple[str, str], list[tuple[Path, dict]]] = {}
    for path, artifact in loaded:
        key = (
            str(artifact.get("source_artifact_id", "")),
            str(artifact.get("minutes_artifact_id", "")),
        )
        groups.setdefault(key, []).append((path, artifact))

    if not args.dry_run:
        retired_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    total_kept = 0
    total_retired = 0

    for key, group in sorted(groups.items()):
        if len(group) < 2:
            continue
        result = _process_group(key, group, retired_dir, validator, args.dry_run)
        if result is None:
            continue
        rows.append(result)
        total_kept += 1
        total_retired += result["duplicates_retired"]

    if rows:
        _print_table(rows)
        print()

    if args.dry_run:
        print(
            f"dry run: would keep {total_kept} pair(s) and retire "
            f"{total_retired} duplicate(s)."
        )
    else:
        print(
            f"Cleanup complete. Kept {total_kept} pairs. "
            f"Retired {total_retired} duplicates."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
