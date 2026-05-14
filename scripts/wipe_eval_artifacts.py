"""Complete recursive wipe of eval artifacts for one ``source_id``.

Background. ``scripts/reset_stale_baseline.py`` only scans a fixed set
of subdirectories (``evals/``, ``evals/results/``, ``evals/alignment/``)
and matches ``source_id`` with ``==``. If a later run writes eval
artifacts under a different subdirectory, or stores ``source_id`` as a
substring of a longer key, the reset leaves them on disk and the next
``eval-ground-truth`` run still trips
``partial_run_warning_blocks_set_baseline``.

This script is the deeper wipe. It walks ``store/artifacts/evals/``
recursively, deletes every file named ``baseline_eval_summary.json`` or
``eval_run_count.json`` at any depth, and deletes every other JSON file
whose ``source_id`` or ``pair_id`` field contains the target
``source_id`` as a substring.

After this wipe the next ``eval-ground-truth`` run emits
``gate=skip_no_baseline`` and sets a fresh baseline with
``run_count=1``.

Usage::

    python scripts/wipe_eval_artifacts.py \\
        --data-lake /path/to/data-lake \\
        --source-id 7-ghz-downlink-tig-meeting-kickoff---transcript-20251218

Add ``--dry-run`` to list what would be deleted without touching disk.

Exit codes::

    0 — wipe succeeded (or dry-run completed).
    1 — verification failed; one or more matching files remain.
    2 — bad arguments or missing data-lake.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List


GLOBAL_FILENAMES = frozenset(
    {"baseline_eval_summary.json", "eval_run_count.json"}
)


def _safe_load(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _references_source(doc: dict, source_id: str) -> bool:
    if not isinstance(doc, dict):
        return False
    if source_id in str(doc.get("source_id", "")):
        return True
    if source_id in str(doc.get("pair_id", "")):
        return True
    return False


def diagnose(evals_dir: Path, data_lake: Path) -> None:
    print("=== ALL files in evals/ ===")
    if not evals_dir.is_dir():
        print(f"(no evals dir at {evals_dir})")
        return
    for path in sorted(evals_dir.rglob("*.json")):
        doc = _safe_load(path)
        print(str(path.relative_to(data_lake)))
        run_count = doc.get("run_count")
        if run_count is not None:
            print(f"  run_count: {run_count}")
        expected = doc.get("expected")
        if expected is not None:
            print(f"  expected: {expected}")
        src = doc.get("source_id") or doc.get("pair_id")
        if src:
            print(f"  source_id/pair_id: {str(src)[:60]}")


def plan(evals_dir: Path, source_id: str) -> List[Path]:
    if not evals_dir.is_dir():
        return []
    candidates: List[Path] = []
    for path in sorted(evals_dir.rglob("*.json")):
        if path.name in GLOBAL_FILENAMES:
            candidates.append(path)
            continue
        if _references_source(_safe_load(path), source_id):
            candidates.append(path)
    return candidates


def delete(paths: List[Path], data_lake: Path) -> int:
    deleted = 0
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            print(f"warning: could not delete {path}: {exc}", file=sys.stderr)
            continue
        print(f"Deleted: {path.relative_to(data_lake)}")
        deleted += 1
    return deleted


def verify(evals_dir: Path, source_id: str, data_lake: Path) -> bool:
    print("=== Remaining eval files ===")
    if not evals_dir.is_dir():
        print("Total remaining: 0")
        print("baseline_eval_summary.json: False (should be False)")
        print("eval_run_count.json: False (should be False)")
        print("OK: ready for fresh baseline")
        return True

    remaining = sorted(evals_dir.rglob("*.json"))
    print(f"Total remaining: {len(remaining)}")

    ok = True
    for name in ("baseline_eval_summary.json", "eval_run_count.json"):
        hits = [p for p in remaining if p.name == name]
        present = bool(hits)
        print(f"{name}: {present} (should be False)")
        if present:
            ok = False

    stragglers = [
        p for p in remaining if _references_source(_safe_load(p), source_id)
    ]
    if stragglers:
        ok = False
        for path in stragglers:
            print(
                f"WARNING: still references source_id: "
                f"{path.relative_to(data_lake)}"
            )

    if ok:
        print("OK: ready for fresh baseline")
    return ok


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-lake",
        required=True,
        help="Path to the data-lake clone (contains 'store/').",
    )
    parser.add_argument(
        "--source-id",
        required=True,
        help="source_id whose eval artifacts should be wiped.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without touching disk.",
    )
    args = parser.parse_args(argv)

    data_lake = Path(args.data_lake).resolve()
    evals_dir = data_lake / "store" / "artifacts" / "evals"
    if not (data_lake / "store" / "artifacts").is_dir():
        print(
            f"error: store/artifacts not found under {data_lake}",
            file=sys.stderr,
        )
        return 2

    diagnose(evals_dir, data_lake)

    candidates = plan(evals_dir, args.source_id)
    print(f"\n=== Plan: {len(candidates)} files ===")
    for path in candidates:
        print(f"  {path.relative_to(data_lake)}")

    if args.dry_run:
        print("\n(dry-run; no files deleted)")
        return 0

    deleted = delete(candidates, data_lake)
    print(f"\nTotal deleted: {deleted}")

    print()
    return 0 if verify(evals_dir, args.source_id, data_lake) else 1


if __name__ == "__main__":
    sys.exit(main())
