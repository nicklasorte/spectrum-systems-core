#!/usr/bin/env python3
"""Seed (or toggle) the Phase V post-hoc verification feature flag.

Writes ``<DATA_LAKE_PATH>/store/artifacts/config/
phase_v_post_hoc_verification_enabled.json``. Idempotent.

Usage::

  python scripts/seed_phase_v_flag.py --enable
  python scripts/seed_phase_v_flag.py --disable
  DATA_LAKE_PATH=/custom/path python scripts/seed_phase_v_flag.py --enable

Rollback: re-run with ``--disable`` and commit the resulting JSON to the
data-lake. All Phase V components short-circuit on this single check.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import sys

from spectrum_systems_core.config import PHASE_V_FLAG_NAME


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def write_flag(data_lake_path: pathlib.Path, enabled: bool) -> pathlib.Path:
    target_dir = data_lake_path / "store" / "artifacts" / "config"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{PHASE_V_FLAG_NAME}_enabled.json"
    payload = {
        "artifact_type": "feature_flag",
        "schema_version": "1.0.0",
        "created_at": _now_iso(),
        "flag_name": PHASE_V_FLAG_NAME,
        "enabled": bool(enabled),
        "rollback_instructions": (
            "Set enabled:false and commit to data-lake. All Phase V "
            "components read this flag and short-circuit on disabled."
        ),
        "provenance": {
            "produced_by": "seed_phase_v_flag",
            "phase": "V",
        },
    }
    target.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def _resolve_data_lake_path(cli_path: str | None) -> pathlib.Path:
    raw = cli_path or os.environ.get("DATA_LAKE_PATH", "")
    if not raw:
        print(
            "seed_phase_v_flag: DATA_LAKE_PATH not set and --data-lake "
            "not provided",
            file=sys.stderr,
        )
        sys.exit(2)
    return pathlib.Path(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--enable", action="store_true")
    group.add_argument("--disable", action="store_true")
    parser.add_argument("--data-lake", default=None, help="DATA_LAKE_PATH override")
    args = parser.parse_args(argv)

    data_lake = _resolve_data_lake_path(args.data_lake)
    enabled = bool(args.enable)
    path = write_flag(data_lake, enabled)
    print(f"wrote {path} enabled={enabled}")
    return 0


if __name__ == "__main__":  # pragma: no cover -- script entry
    sys.exit(main())
