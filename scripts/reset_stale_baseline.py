"""Reset stale baseline + eval artifacts after the data-lake migration.

Context: when the eval store moved to ``nicklasorte/data-lake``, the
``baseline_eval_summary.json`` referenced 10 prior eval runs but only
8 ``eval_result`` artifacts came across. ``eval-ground-truth`` then
blocks with ``partial_run_warning_blocks_set_baseline`` because the
gate refuses to set a new baseline while prior-run artifacts are
missing.

Recovery (this script): delete the stale baseline + run-count + any
``eval_summary_*`` / ``gate_decision_*`` / ``results/*`` /
``alignment/*`` artifacts that reference the target ``source_id``,
leaving the data-lake in a clean "no prior baseline" state. The next
``eval-ground-truth`` run emits ``gate=skip_no_baseline`` and sets a
fresh baseline with ``run_count=1``.

Layout (under ``$SDL_ROOT = <data-lake>/store/artifacts``):

* ``evals/baseline_eval_summary.json``        — singleton, deleted
* ``evals/eval_run_count.json``               — singleton, deleted
* ``evals/eval_summary_<run_id>.json``        — deleted iff source_id matches
* ``evals/gate_decision_<run_id>.json``       — deleted iff source_id matches
* ``evals/results/<eval_result_id>.json``     — deleted iff source_id matches
* ``evals/alignment/<alignment_id>.json``     — deleted iff source_id matches

Source-id matching is permissive: any artifact whose top-level
``source_id`` or whose ``pair_id`` contains the source_id string is
treated as belonging to that source. This mirrors the diagnostic
logic in the task instructions and avoids a partial cleanup if the
field names drift across older artifacts.

Usage:

    python scripts/reset_stale_baseline.py \\
        --data-lake /path/to/data-lake \\
        --source-id 7-ghz-downlink-tig-meeting-kickoff---transcript-20251218

Add ``--dry-run`` to list candidates without deleting.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable, List


def _matches_source(doc: dict, source_id: str) -> bool:
    if not isinstance(doc, dict):
        return False
    if doc.get("source_id") == source_id:
        return True
    pair_id = doc.get("pair_id")
    if isinstance(pair_id, str) and source_id in pair_id:
        return True
    return False


def _safe_load(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _collect_matches(directory: Path, source_id: str) -> List[Path]:
    if not directory.is_dir():
        return []
    matches: List[Path] = []
    for path in sorted(directory.glob("*.json")):
        if _matches_source(_safe_load(path), source_id):
            matches.append(path)
    return matches


def _collect_filename_prefix_matches(
    directory: Path, prefix: str, source_id: str
) -> List[Path]:
    """Some older runs only stored source_id inside the document, but
    eval_summary / gate_decision are keyed by ``pipeline_run_id`` in
    the filename. We still match on document contents — this helper
    is the per-prefix glob the caller drives through ``_collect_matches``.
    """
    if not directory.is_dir():
        return []
    matches: List[Path] = []
    for path in sorted(directory.glob(f"{prefix}*.json")):
        if _matches_source(_safe_load(path), source_id):
            matches.append(path)
    return matches


def plan(sdl_root: Path, source_id: str) -> List[Path]:
    evals_dir = sdl_root / "evals"
    candidates: List[Path] = []

    baseline = evals_dir / "baseline_eval_summary.json"
    if baseline.is_file():
        candidates.append(baseline)

    run_count = evals_dir / "eval_run_count.json"
    if run_count.is_file():
        candidates.append(run_count)

    candidates.extend(
        _collect_filename_prefix_matches(evals_dir, "eval_summary_", source_id)
    )
    candidates.extend(
        _collect_filename_prefix_matches(evals_dir, "gate_decision_", source_id)
    )
    candidates.extend(_collect_matches(evals_dir / "results", source_id))
    candidates.extend(_collect_matches(evals_dir / "alignment", source_id))

    return candidates


def _print_diagnostic(sdl_root: Path, source_id: str) -> None:
    evals_dir = sdl_root / "evals"
    print("=== Baseline artifacts ===")
    for path in sorted(evals_dir.glob("baseline_eval_summary*")):
        doc = _safe_load(path)
        print(f"{path.name}")
        print(f"  run_count: {doc.get('run_count')}")
        print(f"  source_id: {doc.get('source_id')}")

    print("\n=== Eval run count ===")
    for path in sorted(evals_dir.glob("eval_run_count*")):
        print(f"{path.name}: {_safe_load(path)}")

    print(f"\n=== Eval results for source_id={source_id} ===")
    results_dir = evals_dir / "results"
    if results_dir.is_dir():
        matching = [p.name for p in _collect_matches(results_dir, source_id)]
        print(f"Matching result files: {len(matching)}")
        for name in matching:
            print(f"  {name}")

    print(f"\n=== Alignment for source_id={source_id} ===")
    alignment_dir = evals_dir / "alignment"
    if alignment_dir.is_dir():
        matching = [p.name for p in _collect_matches(alignment_dir, source_id)]
        print(f"Matching alignment files: {len(matching)}")
        for name in matching:
            print(f"  {name}")


def _delete_all(paths: Iterable[Path]) -> int:
    n = 0
    for path in paths:
        try:
            path.unlink()
            print(f"Deleted: {path}")
            n += 1
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"warning: could not delete {path}: {exc}", file=sys.stderr)
    return n


def _verify(sdl_root: Path, source_id: str) -> bool:
    evals_dir = sdl_root / "evals"
    baseline = evals_dir / "baseline_eval_summary.json"
    run_count = evals_dir / "eval_run_count.json"

    ok = True
    print(
        f"baseline_eval_summary.json exists: {baseline.exists()} "
        f"(should be False)"
    )
    if baseline.exists():
        ok = False
    print(
        f"eval_run_count.json exists: {run_count.exists()} "
        f"(should be False)"
    )
    if run_count.exists():
        ok = False

    results_dir = evals_dir / "results"
    remaining_results = _collect_matches(results_dir, source_id)
    print(
        f"eval results remaining for source: {len(remaining_results)} "
        f"(should be 0)"
    )
    if remaining_results:
        ok = False

    alignment_dir = evals_dir / "alignment"
    remaining_alignment = _collect_matches(alignment_dir, source_id)
    print(
        f"alignment remaining for source: {len(remaining_alignment)} "
        f"(should be 0)"
    )
    if remaining_alignment:
        ok = False

    return ok


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-lake",
        required=True,
        help="Path to the data-lake clone (the directory containing 'store/').",
    )
    parser.add_argument(
        "--source-id",
        required=True,
        help="source_id whose stale eval artifacts should be removed.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List candidates without deleting.",
    )
    args = parser.parse_args(argv)

    data_lake = Path(args.data_lake).resolve()
    sdl_root = data_lake / "store" / "artifacts"
    if not sdl_root.is_dir():
        print(f"error: SDL root not found: {sdl_root}", file=sys.stderr)
        return 2

    _print_diagnostic(sdl_root, args.source_id)

    candidates = plan(sdl_root, args.source_id)
    print(f"\n=== Plan: {len(candidates)} files ===")
    for path in candidates:
        print(f"  {path.relative_to(data_lake)}")

    if args.dry_run:
        print("\n(dry-run; no files deleted)")
        return 0

    if not candidates:
        print("\nNothing to delete; data-lake is already clean.")
        return 0

    deleted = _delete_all(candidates)
    print(f"\nDeleted {deleted} files.")

    print("\n=== Verification ===")
    return 0 if _verify(sdl_root, args.source_id) else 1


if __name__ == "__main__":
    sys.exit(main())
