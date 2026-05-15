#!/usr/bin/env python3
"""Match ingested transcripts to human-authored minutes ``.docx`` files.

This is the planner for the ``create-human-gt-pairs-batch`` workflow. It
does NOT read any pipeline-output artifact: it only

  1. lists the ingested transcripts (directories under
     ``store/processed/meetings/<source_id>/`` that carry a
     ``source_record.json`` — the ingestion identity record, the same
     pre-condition ``create_human_gt_pairs.py`` already enforces), and
  2. lists the ``*.docx`` files under ``store/raw/minutes/``,

then pairs them by the ``YYYYMMDD`` (or ``YYYY-MM-DD`` / ``YYYY_MM_DD``)
date token found in the transcript slug and the minutes filename.

It never parses ``source_record.json`` content (it only checks the file
exists), never reads ``meeting_extraction`` or any other pipeline
output, and never calls the runner — so the batch it drives stays as
non-circular as the single-transcript path.

A transcript is auto-matched to a minutes file only when their shared
date has exactly ONE transcript and exactly ONE minutes file. Any
many-to-one / one-to-many collision is reported as ``ambiguous`` and
skipped rather than guessed — emitting wrong ground truth is worse than
emitting none. Operators resolve ambiguous dates with the
single-transcript ``create-human-gt-pairs`` workflow and explicit args.

Output (``--format json``)::

    {
      "to_process":   [{"source_id","minutes_file","date"}, ...],
      "skipped_existing": [{"source_id","minutes_file","date"}, ...],
      "no_minutes":   [{"source_id","date"|null,"reason"}, ...],
      "ambiguous":    [{"date","source_ids","minutes_files","reason"}, ...],
      "minutes_without_transcript": [{"minutes_file","date"|null}, ...]
    }

``--format table`` prints the same data as a human-readable match table.
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Same compact form used by scripts/create_human_gt_pairs.py's
# _SLUG_DATE_RE, plus a hyphen/underscore variant so a minutes filename
# like "Minutes 2025-12-18.docx" still matches the compact slug token.
_DATE_RES = (
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{4})[-_](\d{2})[-_](\d{2})(?!\d)"),
)

_GT_FILENAME = "human_minutes_gt_pairs.jsonl"


def extract_date(text: str) -> Optional[str]:
    """Return the first valid calendar date in ``text`` as YYYY-MM-DD.

    Returns ``None`` when no parseable date token is present. A missing
    date never raises and never sentinel-matches (contrast
    create_human_gt_pairs._meeting_date_from_slug which returns the
    "1970-01-01" sentinel — that sentinel would false-match here, so we
    deliberately do not reuse it for the matching key).
    """
    for rx in _DATE_RES:
        for m in rx.finditer(text or ""):
            y, mo, d = m.group(1), m.group(2), m.group(3)
            try:
                return datetime.date(int(y), int(mo), int(d)).isoformat()
            except ValueError:
                continue
    return None


def list_transcripts(data_lake: Path) -> List[str]:
    """Source_ids of ingested transcripts (have a source_record.json)."""
    meetings = data_lake / "store" / "processed" / "meetings"
    if not meetings.is_dir():
        return []
    out: List[str] = []
    for d in sorted(meetings.iterdir(), key=lambda p: p.name):
        if d.is_dir() and (d / "source_record.json").is_file():
            out.append(d.name)
    return out


def list_minutes(data_lake: Path) -> List[str]:
    """Filenames (not paths) of every .docx under store/raw/minutes/."""
    minutes_dir = data_lake / "store" / "raw" / "minutes"
    if not minutes_dir.is_dir():
        return []
    return sorted(
        p.name
        for p in minutes_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".docx"
    )


def _gt_exists(data_lake: Path, source_id: str) -> bool:
    return (
        data_lake
        / "store"
        / "processed"
        / "meetings"
        / source_id
        / "ground_truth"
        / _GT_FILENAME
    ).is_file()


def build_match_table(
    data_lake: Path, *, skip_existing: bool
) -> Dict[str, Any]:
    """Pair ingested transcripts to minutes by date. Pure given the tree.

    Never raises on a missing/extra/dateless file — every unmatched
    input lands in a reported bucket so the workflow loop has a total
    function to iterate and the operator can see exactly why each
    transcript was or was not processed.
    """
    transcripts = list_transcripts(data_lake)
    minutes = list_minutes(data_lake)

    tx_by_date: Dict[str, List[str]] = {}
    tx_no_date: List[str] = []
    for sid in transcripts:
        dt = extract_date(sid)
        if dt is None:
            tx_no_date.append(sid)
        else:
            tx_by_date.setdefault(dt, []).append(sid)

    mins_by_date: Dict[str, List[str]] = {}
    mins_no_date: List[str] = []
    for fn in minutes:
        dt = extract_date(fn)
        if dt is None:
            mins_no_date.append(fn)
        else:
            mins_by_date.setdefault(dt, []).append(fn)

    to_process: List[Dict[str, str]] = []
    skipped_existing: List[Dict[str, str]] = []
    no_minutes: List[Dict[str, Optional[str]]] = []
    ambiguous: List[Dict[str, Any]] = []
    minutes_without_transcript: List[Dict[str, Optional[str]]] = []

    for sid in tx_no_date:
        no_minutes.append(
            {"source_id": sid, "date": None, "reason": "no_date_in_slug"}
        )

    seen_ambiguous_dates: set = set()
    for date in sorted(set(tx_by_date) | set(mins_by_date)):
        tx = tx_by_date.get(date, [])
        mn = mins_by_date.get(date, [])
        if tx and not mn:
            for sid in tx:
                no_minutes.append(
                    {
                        "source_id": sid,
                        "date": date,
                        "reason": "no_minutes_for_date",
                    }
                )
            continue
        if mn and not tx:
            for fn in mn:
                minutes_without_transcript.append(
                    {"minutes_file": fn, "date": date}
                )
            continue
        if len(tx) == 1 and len(mn) == 1:
            sid, fn = tx[0], mn[0]
            rel_minutes = f"store/raw/minutes/{fn}"
            row = {
                "source_id": sid,
                "minutes_file": rel_minutes,
                "date": date,
            }
            if skip_existing and _gt_exists(data_lake, sid):
                skipped_existing.append(row)
            else:
                to_process.append(row)
            continue
        # Date collision: >1 transcript and/or >1 minutes for one date.
        # Refuse to guess; report so an operator can resolve manually.
        seen_ambiguous_dates.add(date)
        ambiguous.append(
            {
                "date": date,
                "source_ids": tx,
                "minutes_files": [f"store/raw/minutes/{f}" for f in mn],
                "reason": "date_collision_needs_manual_pairing",
            }
        )

    for fn in mins_no_date:
        minutes_without_transcript.append({"minutes_file": fn, "date": None})

    return {
        "to_process": to_process,
        "skipped_existing": skipped_existing,
        "no_minutes": no_minutes,
        "ambiguous": ambiguous,
        "minutes_without_transcript": minutes_without_transcript,
        "counts": {
            "transcripts_total": len(transcripts),
            "minutes_total": len(minutes),
            "matched": len(to_process) + len(skipped_existing),
            "to_process": len(to_process),
            "skipped_existing": len(skipped_existing),
            "no_minutes": len(no_minutes),
            "ambiguous": len(ambiguous),
            "minutes_without_transcript": len(minutes_without_transcript),
        },
    }


def _render_table(t: Dict[str, Any]) -> str:
    c = t["counts"]
    lines: List[str] = []
    lines.append("=== Transcript / Minutes match table ===")
    lines.append(
        f"transcripts={c['transcripts_total']} "
        f"minutes={c['minutes_total']} matched={c['matched']} "
        f"to_process={c['to_process']} "
        f"skipped_existing={c['skipped_existing']} "
        f"no_minutes={c['no_minutes']} ambiguous={c['ambiguous']} "
        f"minutes_without_transcript={c['minutes_without_transcript']}"
    )
    lines.append("")
    lines.append("-- TO PROCESS --")
    if t["to_process"]:
        for r in t["to_process"]:
            lines.append(
                f"  [{r['date']}] {r['source_id']}  <-  {r['minutes_file']}"
            )
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("-- SKIPPED (already has GT pairs) --")
    if t["skipped_existing"]:
        for r in t["skipped_existing"]:
            lines.append(f"  [{r['date']}] {r['source_id']}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("-- NO MINUTES (skipped) --")
    if t["no_minutes"]:
        for r in t["no_minutes"]:
            lines.append(
                f"  [{r.get('date') or '????-??-??'}] "
                f"{r['source_id']}  ({r['reason']})"
            )
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("-- AMBIGUOUS (skipped, manual pairing needed) --")
    if t["ambiguous"]:
        for r in t["ambiguous"]:
            lines.append(
                f"  [{r['date']}] transcripts={r['source_ids']} "
                f"minutes={r['minutes_files']}"
            )
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("-- MINUTES WITHOUT A MATCHING TRANSCRIPT --")
    if t["minutes_without_transcript"]:
        for r in t["minutes_without_transcript"]:
            lines.append(
                f"  [{r.get('date') or '????-??-??'}] {r['minutes_file']}"
            )
    else:
        lines.append("  (none)")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-lake", required=True)
    parser.add_argument(
        "--skip-existing",
        dest="skip_existing",
        action="store_true",
        default=True,
        help="Skip source_ids that already have human_minutes_gt_pairs.jsonl "
        "(default).",
    )
    parser.add_argument(
        "--no-skip-existing",
        dest="skip_existing",
        action="store_false",
        help="Re-process even source_ids that already have GT pairs.",
    )
    parser.add_argument(
        "--format", choices=("json", "table"), default="table"
    )
    args = parser.parse_args(argv)

    # Mobile workflow_dispatch inputs frequently arrive with a trailing
    # space pasted from a phone keyboard; strip the path arg.
    data_lake = Path(args.data_lake.strip())
    if not data_lake.is_dir():
        print(
            f"error: --data-lake is not a directory: {data_lake}",
            file=sys.stderr,
        )
        return 1

    table = build_match_table(
        data_lake, skip_existing=args.skip_existing
    )
    if args.format == "json":
        print(json.dumps(table, sort_keys=True))
    else:
        print(_render_table(table))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
