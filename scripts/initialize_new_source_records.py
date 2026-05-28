#!/usr/bin/env python3
"""Backfill ``source_record.json`` for sources staged manually.

Some sources were placed under ``store/raw/meetings/<source_id>/`` with
``source.txt`` and ``metadata.json`` but were never run through the
normal pipeline ingestion (``SourceLoader.load``), so they have no
``store/processed/meetings/<source_id>/source_record.json``. Every
downstream reader (few-shot preflight, Opus baseline workflow,
comparators) resolves the stable transcript UUID by reading that
record — without it, every pipeline run over the slug HALTs.

This script invokes the canonical, deterministic, LLM-free
``SourceLoader.load`` for each listed source_id. The output is
byte-identical to what the normal pipeline would have written, because
this script REUSES the pipeline writer rather than reimplementing it
(per the CLAUDE.md Surgical Changes principle).

This is a ONE-TIME backfill for 32 known source_ids. The slug list is
hardcoded so a re-run can never drift onto unrelated sources, and any
slug already holding a ``source_record.json`` is left untouched
(``SourceLoader.load`` is idempotent for our purposes because we treat
the on-disk file as authoritative).

Usage::

    python scripts/initialize_new_source_records.py --data-lake <path>

Exits non-zero on any per-source failure. Prints a JSON summary to
stdout suitable for a workflow ``GITHUB_STEP_SUMMARY``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

from spectrum_systems_core.ingestion.source_loader import SourceLoader


# Frozen list of slugs missing source_record.json. Re-running this
# script with a present record is a no-op (we skip with status
# "already_present"), so the list is safe to re-apply.
SOURCE_IDS: tuple[str, ...] = (
    "1-26-2026-ob3-cellular-network-characteristics-loe-_-ctia-meeting-transcript",
    "7-ghz-downlink-tig-meeting---transcript-19mar2026",
    "7-ghz-downlink-tig-meeting-20260416---transcript",
    "7-ghz-downlink-tig-meeting-transcript---21may2026",
    "7-ghz-fixed_transportable-point-to-point--p2p--tig-meeting---transcript--17mar2026",
    "7-ghz-fixed_transportable-point-to-point--p2p--tig-meeting---transcript-05-19-2026",
    "7-ghz-fixed_transportable-point-to-point--p2p--tig-meeting-20260414-transcript",
    "7-ghz-study-working-group---07may26---meeting-transcript",
    "7-ghz-uplink-tig-meeting---transcript-03-18-2026",
    "7-ghz-uplink-tig-meeting---transcript-05-20-2026",
    "7-ghz-uplink-tig-meeting-20260415---transcript-04-15-2026",
    "7-ghz-wg-ad-hoc-meeting_-cellular-loe_fed-only-5-1-2026-transcript",
    "7-ghz-wg-ad-hoc-meeting_-cellular-loe_fed-only-5-20-2026-transcript",
    "7-ghz-wg-meeting-transcript---02apr26",
    "7ghz-spd-sead-sync-3-31-2026",
    "7ghz-spd-sead-sync-4-14-2026",
    "7ghz-spd-sead-sync-4-21-2026",
    "7ghz-spd-sead-sync-4-28-2026",
    "7ghz-spd-sead-sync-4-7-2026",
    "7ghz-spd-sead-sync-5-12-2026",
    "7ghz-spd-sead-sync-5-19-2026",
    "7ghz-spd-sead-sync-5-5-2026",
    "7ghz_-ad-hoc-meeting_-cellular-loe-3-26-2026-transcript",
    "apr2026---downlink-tig---meeting-prep-session---transcript",
    "apr2026---downlink-tig---technical-prep-session---transcript",
    "ob3-cellular-network-characteristics-loe--ctia-meeting-transcript-clean-2-9-2026",
    "ob3-cellular-network-characteristics-loe-_-ctia-meeting-2-23-2026-transcript",
    "ob3-cellular-network-characteristics-loe-_-ctia-meeting-3-23-2026-transcript",
    "ob3-cellular-network-characteristics-loe-_-ctia-meeting-3-9-2026-transcript",
    "ob3-cellular-network-characteristics-loe-_-ctia-meeting-4-6-2026-transcript",
    "ob3-cellular-network-characteristics-loe-_-ctia-meeting-transcript-04-20-2026",
    "ob3-cellular-network-characteristics-loe-_-ctia-meeting-transcript-5-18-2026",
)


def initialize_one(
    *, source_id: str, store_root: Path
) -> Dict[str, Any]:
    """Produce source_record.json for one slug via SourceLoader.

    Idempotent w.r.t. an already-present record: a slug that already has
    source_record.json is reported as "already_present" and left
    untouched (we never overwrite a record the normal pipeline may have
    written in the meantime).
    """
    sr_path = (
        store_root / "processed" / "meetings" / source_id
        / "source_record.json"
    )
    if sr_path.is_file():
        return {
            "source_id": source_id,
            "status": "already_present",
            "reason": "",
            "source_record_path": str(sr_path),
        }

    raw_dir = store_root / "raw" / "meetings" / source_id
    if not raw_dir.is_dir():
        return {
            "source_id": source_id,
            "status": "failure",
            "reason": f"no raw dir at {raw_dir}",
            "source_record_path": str(sr_path),
        }

    # SourceLoader.load resolves store_root from DATA_LAKE_PATH; the
    # repo_root positional arg is unused inside .load() but kept for
    # signature stability. We pass str(store_root) for clarity.
    result = SourceLoader().load(source_id, str(store_root))
    if result.get("status") != "success":
        return {
            "source_id": source_id,
            "status": "failure",
            "reason": str(result.get("reason") or "unknown"),
            "source_record_path": str(sr_path),
        }
    if not sr_path.is_file():
        return {
            "source_id": source_id,
            "status": "failure",
            "reason": (
                f"SourceLoader returned success but wrote no file at "
                f"{sr_path}"
            ),
            "source_record_path": str(sr_path),
        }
    return {
        "source_id": source_id,
        "status": "written",
        "reason": "",
        "source_record_path": str(sr_path),
    }


def initialize_all(
    *, data_lake: Path, source_ids: tuple[str, ...] = SOURCE_IDS
) -> Dict[str, Any]:
    store_root = data_lake / "store"

    # SourceLoader._resolve_store_root reads DATA_LAKE_PATH from the
    # environment, so we must set it before each .load() call. Mutating
    # os.environ globally would leak into any later in-process consumer
    # of the same env var (e.g. the obsidian projection's
    # _resolve_phase_c_dir, which is exercised by tests/paper/
    # test_claim_extractor.py). Mirror the try/finally restore pattern
    # already used by create_opus_reference_baselines.py::
    # _ensure_source_record so the global is back to its prior value
    # the moment this function returns — pass or fail.
    prev = os.environ.get("DATA_LAKE_PATH")
    os.environ["DATA_LAKE_PATH"] = str(data_lake)
    try:
        per_source: List[Dict[str, Any]] = []
        for sid in source_ids:
            per_source.append(
                initialize_one(source_id=sid, store_root=store_root)
            )
    finally:
        if prev is None:
            os.environ.pop("DATA_LAKE_PATH", None)
        else:
            os.environ["DATA_LAKE_PATH"] = prev

    counts: Dict[str, int] = {}
    for entry in per_source:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1

    all_ok = all(
        e["status"] in ("written", "already_present") for e in per_source
    )
    return {
        "status": "success" if all_ok else "failure",
        "data_lake": str(data_lake),
        "total_sources": len(source_ids),
        "counts": counts,
        "per_source": per_source,
    }


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-lake", required=True)
    args = parser.parse_args(argv)
    data_lake = Path(args.data_lake.strip())
    if not data_lake.is_dir():
        print(
            json.dumps(
                {
                    "status": "failure",
                    "reason": "data_lake_not_a_directory",
                    "detail": str(data_lake),
                },
                indent=2,
                sort_keys=True,
            )
        )
        print(
            f"FAIL: --data-lake is not a directory: {data_lake}",
            file=sys.stderr,
        )
        return 2

    summary = initialize_all(data_lake=data_lake)
    print(json.dumps(summary, indent=2, sort_keys=True))
    for entry in summary["per_source"]:
        print(
            f"{entry['source_id']} | {entry['status']} "
            f"| {entry['reason']}",
            file=sys.stderr,
        )
    return 0 if summary["status"] == "success" else 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
