"""Clean up three data-lake issues left behind by earlier pipeline runs.

Issue 1 — Spurious source_record artifacts that were minutes files
misprocessed as transcripts before orchestrator Fix B landed. These are
identified by ``title`` or ``raw_path`` containing the substring
"minutes" (case-insensitive) and retired to
``$SDL_ROOT/retired/<artifact_id>.json`` with a sidecar
``<artifact_id>.retired_reason.json``.

Issue 2 — The failed ingestion eval that pointed at one of the spurious
source_records (correct evidence of hollow extraction before the
DocxExtractor table fix) is retired alongside its source_record into
``$SDL_ROOT/evals/retired/``.

Issue 3 — Source records that have no ingestion eval at all (processed
before IngestionEval was deployed in the very first pipeline run) get a
fresh ``IngestionEval.evaluate()`` written to
``$SDL_ROOT/evals/<source_artifact_id>_ingestion_eval.json``.

No artifact is ever deleted; retirement is move + sidecar. Singleton
records that look fine are left alone. Never raises.

Usage:
    python scripts/cleanup_data_lake.py \\
        --data-lake /path/to/data-lake \\
        --schema-dir /path/to/spectrum-systems-core/contracts/schemas

Add --dry-run to print what would change without moving or writing files.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

EXPECTED_REMAINING_SOURCE_RECORDS = 13
RETIRED_REASON_MINUTES = "minutes_file_misprocessed_as_transcript"


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _iter_top_level_json(sdl_root: Path) -> List[Path]:
    if not sdl_root.is_dir():
        return []
    return sorted(
        p for p in sdl_root.iterdir() if p.is_file() and p.suffix == ".json"
    )


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except Exception as exc:
        print(f"warning: could not read {path.name}: {exc}", file=sys.stderr)
        return None
    return obj if isinstance(obj, dict) else None


def _is_source_record(artifact: Dict[str, Any]) -> bool:
    if artifact.get("artifact_kind") != "source_record":
        return False
    payload = artifact.get("payload")
    return isinstance(payload, dict)


def _record_title(artifact: Dict[str, Any]) -> str:
    payload = artifact.get("payload") or {}
    title = payload.get("title", "")
    return title if isinstance(title, str) else ""


def _record_raw_path(artifact: Dict[str, Any]) -> str:
    payload = artifact.get("payload") or {}
    raw_path = payload.get("raw_path", "")
    return raw_path if isinstance(raw_path, str) else ""


def _record_source_id(artifact: Dict[str, Any]) -> str:
    payload = artifact.get("payload") or {}
    source_id = payload.get("source_id", "")
    return source_id if isinstance(source_id, str) else ""


def _is_minutes_file(artifact: Dict[str, Any]) -> bool:
    title = _record_title(artifact).lower()
    raw_path = _record_raw_path(artifact).lower()
    return "minutes" in title or "minutes" in raw_path


def _looks_like_transcript(artifact: Dict[str, Any]) -> bool:
    title = _record_title(artifact).lower()
    raw_path = _record_raw_path(artifact).lower()
    return "transcript" in title or "transcript" in raw_path


# ---------- Part A: retire spurious source_records ----------------------------


def _retire_spurious_records(
    sdl_root: Path,
    records: List[Tuple[Path, Dict[str, Any]]],
    dry_run: bool,
) -> List[Dict[str, str]]:
    """Retire source_records whose title/raw_path mark them as minutes files.

    Returns the list of retired-record summaries (one per record).
    """
    retired_dir = sdl_root / "retired"
    evals_dir = sdl_root / "evals"
    evals_retired_dir = evals_dir / "retired"

    if not dry_run:
        try:
            retired_dir.mkdir(parents=True, exist_ok=True)
            evals_retired_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"error: could not create retired dirs: {exc}", file=sys.stderr)
            return []

    summaries: List[Dict[str, str]] = []

    for path, artifact in records:
        try:
            if not _is_minutes_file(artifact):
                continue

            artifact_id = artifact.get("artifact_id") or path.stem
            title = _record_title(artifact)
            raw_path = _record_raw_path(artifact)
            dest = retired_dir / f"{artifact_id}.json"
            sidecar = retired_dir / f"{artifact_id}.retired_reason.json"
            reason = {
                "original_artifact_id": artifact_id,
                "retired_at": _utc_now_iso(),
                "retired_reason": RETIRED_REASON_MINUTES,
                "title": title,
                "raw_path": raw_path,
            }

            eval_src = evals_dir / f"{artifact_id}_ingestion_eval.json"
            eval_dst = evals_retired_dir / f"{artifact_id}_ingestion_eval.json"
            eval_exists = eval_src.is_file()

            if dry_run:
                print(
                    f"would retire source_record {path.name} -> "
                    f"retired/{dest.name} (reason: {RETIRED_REASON_MINUTES}, "
                    f"title={title!r})"
                )
                if eval_exists:
                    print(
                        f"would retire ingestion_eval {eval_src.name} -> "
                        f"evals/retired/{eval_dst.name}"
                    )
            else:
                shutil.move(str(path), str(dest))
                with sidecar.open("w", encoding="utf-8") as fh:
                    json.dump(reason, fh, indent=2, sort_keys=True)
                    fh.write("\n")
                if eval_exists:
                    shutil.move(str(eval_src), str(eval_dst))

            summaries.append(
                {
                    "artifact_id": artifact_id,
                    "title": title,
                    "reason": RETIRED_REASON_MINUTES,
                    "eval_retired": "yes" if eval_exists else "no",
                }
            )
        except Exception as exc:
            print(
                f"error: failed to retire {path.name}: {exc}\n"
                f"{traceback.format_exc()}",
                file=sys.stderr,
            )
            continue

    return summaries


# ---------- Part B: generate missing ingestion evals --------------------------


def _find_docx_for_record(
    transcripts_dir: Path, artifact: Dict[str, Any]
) -> Optional[Path]:
    """Locate the original .docx by matching its stem to ``payload.title``."""
    title = _record_title(artifact)
    if not title or not transcripts_dir.is_dir():
        return None
    # First, exact stem match.
    candidate = transcripts_dir / f"{title}.docx"
    if candidate.is_file():
        return candidate
    # Fall back to case-insensitive stem match in case of platform quirks.
    title_lower = title.lower()
    for docx in transcripts_dir.glob("*.docx"):
        if docx.stem.lower() == title_lower:
            return docx
    return None


def _generate_missing_evals(
    data_lake_root: Path,
    sdl_root: Path,
    records: List[Tuple[Path, Dict[str, Any]]],
    dry_run: bool,
) -> List[Dict[str, Any]]:
    """For every source_record without an ingestion eval, run IngestionEval."""
    evals_dir = sdl_root / "evals"
    transcripts_dir = data_lake_root / "store" / "raw" / "transcripts"
    processed_root = data_lake_root / "store" / "processed" / "meetings"
    store_root = data_lake_root / "store"

    summaries: List[Dict[str, Any]] = []

    try:
        from spectrum_systems_core.ingestion.ingestion_eval import IngestionEval
    except Exception as exc:
        print(
            f"error: cannot import IngestionEval ({exc}); skipping Part B",
            file=sys.stderr,
        )
        return summaries

    ingestion_eval = IngestionEval()

    for path, artifact in records:
        try:
            if _is_minutes_file(artifact):
                # Will be retired in Part A; do not generate an eval for it.
                continue

            artifact_id = artifact.get("artifact_id") or path.stem
            existing_eval = evals_dir / f"{artifact_id}_ingestion_eval.json"
            if existing_eval.is_file():
                continue

            source_id = _record_source_id(artifact)
            title = _record_title(artifact)

            processed_dir = processed_root / source_id if source_id else None
            if processed_dir is None or not processed_dir.is_dir():
                print(
                    f"warning: no processed dir for source_record "
                    f"{artifact_id} (source_id={source_id!r}); skipping",
                    file=sys.stderr,
                )
                summaries.append(
                    {
                        "artifact_id": artifact_id,
                        "title": title,
                        "status": "skipped",
                        "reason": "processed_dir_not_found",
                        "text_unit_count": 0,
                    }
                )
                continue

            docx_path = _find_docx_for_record(transcripts_dir, artifact)
            if docx_path is None:
                print(
                    f"warning: no .docx in {transcripts_dir} matched title "
                    f"{title!r}; skipping",
                    file=sys.stderr,
                )
                summaries.append(
                    {
                        "artifact_id": artifact_id,
                        "title": title,
                        "status": "skipped",
                        "reason": "docx_not_found",
                        "text_unit_count": 0,
                    }
                )
                continue

            if dry_run:
                print(
                    f"would generate ingestion eval for {artifact_id} "
                    f"(title={title!r}, docx={docx_path.name})"
                )
                summaries.append(
                    {
                        "artifact_id": artifact_id,
                        "title": title,
                        "status": "would_generate",
                        "reason": "",
                        "text_unit_count": 0,
                    }
                )
                continue

            result = ingestion_eval.evaluate(
                str(docx_path),
                artifact,
                repo_root=str(store_root),
            )
            written = ingestion_eval.write_eval_result(
                result, sdl_root=str(sdl_root)
            )
            status = result.get("status", "not_run")
            text_unit_count = int(result.get("text_unit_count", 0) or 0)
            summaries.append(
                {
                    "artifact_id": artifact_id,
                    "title": title,
                    "status": status,
                    "reason": "written" if written else "write_failed",
                    "text_unit_count": text_unit_count,
                }
            )
        except Exception as exc:
            print(
                f"error: failed to generate eval for {path.name}: {exc}\n"
                f"{traceback.format_exc()}",
                file=sys.stderr,
            )
            continue

    return summaries


# ---------- Part C: summary report --------------------------------------------


def _count_top_level_source_records(sdl_root: Path) -> int:
    count = 0
    for path in _iter_top_level_json(sdl_root):
        artifact = _load_json(path)
        if artifact and _is_source_record(artifact):
            count += 1
    return count


def _count_ingestion_evals(sdl_root: Path) -> int:
    evals_dir = sdl_root / "evals"
    if not evals_dir.is_dir():
        return 0
    return sum(
        1
        for p in evals_dir.iterdir()
        if p.is_file() and p.name.endswith("_ingestion_eval.json")
    )


def _surface_unclassified(records: List[Tuple[Path, Dict[str, Any]]]) -> None:
    unclassified = [
        (p, a)
        for p, a in records
        if not _is_minutes_file(a) and not _looks_like_transcript(a)
    ]
    if not unclassified:
        return
    print(
        "warning: source_records whose title/raw_path contains neither "
        "'transcript' nor 'minutes' (not retired, but please review):",
        file=sys.stderr,
    )
    for path, artifact in unclassified:
        print(
            f"  - {artifact.get('artifact_id', path.stem)} "
            f"title={_record_title(artifact)!r} "
            f"raw_path={_record_raw_path(artifact)!r}",
            file=sys.stderr,
        )


# ---------- entry point -------------------------------------------------------


def _parse_args(argv: List[str]) -> argparse.Namespace:
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
        help="Print what would change without moving or writing files.",
    )
    return parser.parse_args(argv)


def _resolve_sdl_root(data_lake: Path) -> Path:
    env = os.environ.get("SDL_ROOT", "").strip()
    if env:
        return Path(env)
    return data_lake / "store" / "artifacts"


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))

    sdl_root = _resolve_sdl_root(args.data_lake)
    if not sdl_root.is_dir():
        print(f"error: SDL_ROOT does not exist: {sdl_root}", file=sys.stderr)
        return 0

    # Load all top-level source_records once. Spurious ones get retired in
    # Part A; the remaining ones are eligible for Part B.
    loaded: List[Tuple[Path, Dict[str, Any]]] = []
    for path in _iter_top_level_json(sdl_root):
        artifact = _load_json(path)
        if artifact is None or not _is_source_record(artifact):
            continue
        loaded.append((path, artifact))

    _surface_unclassified(loaded)

    # Part A — retire spurious source_records (minutes files).
    retired_summaries = _retire_spurious_records(sdl_root, loaded, args.dry_run)
    print(
        f"Retired {len(retired_summaries)} spurious source_records "
        "(minutes files)"
    )

    # Part B — generate missing ingestion evals for the remaining records.
    generated_summaries = _generate_missing_evals(
        args.data_lake, sdl_root, loaded, args.dry_run
    )
    generated_count = sum(
        1 for s in generated_summaries if s["status"] != "skipped"
        and s["status"] != "would_generate"
    )
    if args.dry_run:
        would_count = sum(
            1 for s in generated_summaries if s["status"] == "would_generate"
        )
        print(f"Would generate {would_count} missing ingestion evals")
    else:
        print(f"Generated {generated_count} missing ingestion evals")

    # Part C — summary report.
    print()
    print("=== Data Lake Cleanup Summary ===")
    print(f"Spurious source_records retired: {len(retired_summaries)}")
    for s in retired_summaries:
        print(f"  - {s['title']} (reason: {s['reason']})")

    print()
    if args.dry_run:
        would_list = [
            s for s in generated_summaries if s["status"] == "would_generate"
        ]
        print(f"Missing ingestion evals to generate: {len(would_list)}")
        for s in would_list:
            print(
                f"  - {s['title']}: would_generate "
                f"({s['text_unit_count']} units)"
            )
    else:
        actionable = [
            s
            for s in generated_summaries
            if s["status"] in ("passed", "warning", "failed")
        ]
        print(f"Missing ingestion evals generated: {len(actionable)}")
        for s in actionable:
            print(
                f"  - {s['title']}: {s['status']} "
                f"({s['text_unit_count']} units)"
            )

    skipped = [s for s in generated_summaries if s["status"] == "skipped"]
    if skipped:
        print()
        print(f"Skipped (could not generate eval): {len(skipped)}")
        for s in skipped:
            print(f"  - {s['title']}: {s['reason']}")

    # Validate post-state if not dry run.
    if not args.dry_run:
        remaining_records = _count_top_level_source_records(sdl_root)
        present_evals = _count_ingestion_evals(sdl_root)
        print()
        print(
            f"Source records remaining: {remaining_records} "
            f"(should be {EXPECTED_REMAINING_SOURCE_RECORDS})"
        )
        print(
            f"Ingestion evals present: {present_evals} "
            f"(should be {EXPECTED_REMAINING_SOURCE_RECORDS})"
        )
        if remaining_records != EXPECTED_REMAINING_SOURCE_RECORDS:
            print(
                f"warning: expected {EXPECTED_REMAINING_SOURCE_RECORDS} "
                f"source_records after cleanup but found {remaining_records}",
                file=sys.stderr,
            )
        if present_evals != EXPECTED_REMAINING_SOURCE_RECORDS:
            print(
                f"warning: expected {EXPECTED_REMAINING_SOURCE_RECORDS} "
                f"ingestion evals after cleanup but found {present_evals}",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # never raise outwards
        print(
            f"fatal: cleanup_data_lake crashed: {exc}\n{traceback.format_exc()}",
            file=sys.stderr,
        )
        raise SystemExit(0)
