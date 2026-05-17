"""MinutesDeduplicator: retire duplicate minutes_record artifacts.

Phase L.2 maintenance pass. Run before linking when the same .docx may
have been processed multiple times (e.g., before MinutesProcessor was
made idempotent). Scans ``$SDL_ROOT/minutes/*.json`` (non-recursive),
groups by ``raw_hash``, and for each group with more than one record
keeps the OLDEST (lowest ``created_at``) and moves the rest under
``$SDL_ROOT/minutes/retired/`` with a sidecar reason file.

Rules:

* Never deletes anything. Files are MOVED to ``minutes/retired/``.
* The kept record is schema-validated against
  ``contracts/schemas/ingestion/minutes_record.schema.json`` before
  any move; a kept record that fails schema validation is reported as
  ``invalid_kept`` and no member of the group is retired (so we never
  lose the only-copy of a record because the sole survivor was
  unreadable).
* Tie-break for ``created_at`` ties: oldest by ``minutes_id`` lexical
  order. Deterministic.
* The linker's ``_load_minutes`` uses ``minutes_dir.glob("*.json")``
  (non-recursive), so retired files under ``retired/`` are excluded
  automatically — no schema change for ``minutes_record`` is required.
* Never raises. Always returns a dict.
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

import jsonschema

from ._paths import contracts_root


def _resolve_sdl_root(data_lake_path: str) -> Path | None:
    env = os.environ.get("SDL_ROOT", "").strip()
    if env:
        p = Path(env)
        if not p.exists():
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError:
                return None
        return p
    if not data_lake_path:
        return None
    base = Path(data_lake_path)
    if not base.exists():
        return None
    return base / "store" / "artifacts"


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _load_schema() -> dict[str, Any] | None:
    try:
        path = (
            contracts_root()
            / "schemas"
            / "ingestion"
            / "minutes_record.schema.json"
        )
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def deduplicate_minutes(data_lake_path: str) -> dict[str, Any]:
    """Retire duplicate minutes_record artifacts grouped by raw_hash.

    Returns a dict with keys ``status``, ``groups_found``, ``records_kept``,
    ``records_retired``, ``retired`` (list of {minutes_id, raw_hash,
    retired_path}), ``invalid_kept`` (list of group-leader records that
    failed schema validation; their groups were left intact), ``reason``.
    """
    sdl_root = _resolve_sdl_root(data_lake_path)
    if sdl_root is None:
        return {
            "status": "failure",
            "groups_found": 0,
            "records_kept": 0,
            "records_retired": 0,
            "retired": [],
            "invalid_kept": [],
            "reason": (
                "sdl_root_unresolved:set SDL_ROOT or pass a valid "
                "data_lake_path"
            ),
        }
    minutes_dir = sdl_root / "minutes"
    if not minutes_dir.is_dir():
        return {
            "status": "success",
            "groups_found": 0,
            "records_kept": 0,
            "records_retired": 0,
            "retired": [],
            "invalid_kept": [],
            "reason": "",
        }

    by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in sorted(minutes_dir.glob("*.json")):
        if not path.is_file():
            continue
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rec, dict):
            continue
        raw_hash = rec.get("raw_hash")
        if not isinstance(raw_hash, str) or not raw_hash:
            continue
        by_hash[raw_hash].append({"path": path, "record": rec})

    schema = _load_schema()
    validator = (
        jsonschema.Draft202012Validator(schema) if schema is not None else None
    )

    retired_dir = minutes_dir / "retired"

    groups_found = 0
    records_kept = 0
    records_retired = 0
    retired: list[dict[str, Any]] = []
    invalid_kept: list[dict[str, Any]] = []

    for raw_hash, entries in sorted(by_hash.items()):
        if len(entries) < 2:
            continue
        groups_found += 1
        # Sort by (created_at, minutes_id) for deterministic oldest pick.
        entries.sort(
            key=lambda e: (
                str(e["record"].get("created_at") or ""),
                str(e["record"].get("minutes_id") or ""),
            )
        )
        keeper = entries[0]
        # Validate the keeper. If invalid, leave the whole group intact —
        # losing a unique record because the survivor is malformed is
        # data loss.
        if validator is not None:
            try:
                validator.validate(keeper["record"])
            except jsonschema.ValidationError as exc:
                invalid_kept.append(
                    {
                        "minutes_id": keeper["record"].get("minutes_id", ""),
                        "raw_hash": raw_hash,
                        "reason": f"schema_violation:{exc.message}",
                    }
                )
                continue

        records_kept += 1
        try:
            retired_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {
                "status": "failure",
                "groups_found": groups_found,
                "records_kept": records_kept,
                "records_retired": records_retired,
                "retired": retired,
                "invalid_kept": invalid_kept,
                "reason": f"retired_dir_unwritable:{exc}",
            }

        for dup in entries[1:]:
            src = dup["path"]
            target = retired_dir / src.name
            # Defensive: if a file with the same name already exists in
            # retired/ (shouldn't happen with UUID filenames), preserve
            # both by appending the timestamp.
            if target.exists():
                stamp = _now_iso().replace(":", "").replace("-", "")
                target = retired_dir / f"{src.stem}.{stamp}{src.suffix}"
            try:
                shutil.move(str(src), str(target))
            except OSError as exc:
                return {
                    "status": "failure",
                    "groups_found": groups_found,
                    "records_kept": records_kept,
                    "records_retired": records_retired,
                    "retired": retired,
                    "invalid_kept": invalid_kept,
                    "reason": f"move_error:{exc}",
                }
            sidecar = target.with_name(target.stem + ".retired_reason.json")
            sidecar_payload = {
                "minutes_id": dup["record"].get("minutes_id", ""),
                "raw_hash": raw_hash,
                "retired_reason": "duplicate",
                "kept_minutes_id": keeper["record"].get("minutes_id", ""),
                "kept_artifact_path": str(keeper["path"]),
                "retired_at": _now_iso(),
            }
            try:
                sidecar.write_text(
                    json.dumps(sidecar_payload, indent=2, sort_keys=True)
                    + "\n",
                    encoding="utf-8",
                )
            except OSError:
                # Sidecar is informational; the move itself is the
                # authoritative retire signal. Don't fail the whole dedup
                # over a sidecar write error.
                pass
            records_retired += 1
            retired.append(
                {
                    "minutes_id": dup["record"].get("minutes_id", ""),
                    "raw_hash": raw_hash,
                    "retired_path": str(target),
                }
            )

    return {
        "status": "success",
        "groups_found": groups_found,
        "records_kept": records_kept,
        "records_retired": records_retired,
        "retired": retired,
        "invalid_kept": invalid_kept,
        "reason": "",
    }
