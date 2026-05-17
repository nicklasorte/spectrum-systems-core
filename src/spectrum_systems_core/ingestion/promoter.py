"""Promoter: write a source_record to the data lake under SDL_ROOT.

Tries an external DataLake first (DATA_LAKE_PATH env or ../data-lake/ sibling).
Falls back to a local file-based store rooted at SDL_ROOT when no external
DataLake class is available. Returns failure if neither is reachable.

Never raises.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

from spectrum_systems_core.governance.artifact_validator import validate_and_log


def _summary_for(payload: dict[str, Any]) -> str:
    metadata = payload.get("metadata", {}) or {}
    date = metadata.get("date", "")
    return (
        f"{payload['title']} — {payload['source_family']} source with "
        f"{payload['text_unit_count']} text units ingested {date}"
    )


class _LocalDataLake:
    """Minimal SDL_ROOT-backed lake. Writes <artifact_id>.json files."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def store(self, artifact: dict[str, Any]) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        artifact_id = artifact["artifact_id"]
        validate_and_log(artifact, schema_path=str(self.root / f"{artifact_id}.json"))
        out = self.root / f"{artifact_id}.json"
        out.write_text(
            json.dumps(artifact, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return f"sdl://{artifact_id}"


def _load_external_data_lake_class():
    candidates = []

    env_path = os.environ.get("DATA_LAKE_PATH")
    if env_path:
        candidates.append(Path(env_path).resolve())

    repo_root = Path(__file__).resolve().parents[3]
    candidates.append((repo_root.parent / "data-lake").resolve())

    for candidate in candidates:
        if not candidate.is_dir():
            continue
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
        try:
            module = importlib.import_module("data_lake")
            data_lake_cls = getattr(module, "DataLake", None)
            if data_lake_cls is not None:
                return data_lake_cls
        except ImportError:
            continue
    return None


class Promoter:
    """Promote a source_record to the data lake."""

    def promote(self, source_record: dict[str, Any]) -> dict[str, Any]:
        try:
            payload = source_record["payload"]
            payload["summary"] = _summary_for(payload)

            data_lake_cls = _load_external_data_lake_class()
            lake: Any
            if data_lake_cls is not None:
                lake = data_lake_cls()
            else:
                sdl_root = os.environ.get("SDL_ROOT")
                if not sdl_root:
                    return {
                        "status": "failure",
                        "sdl_ref": "",
                        "reason": (
                            "data_lake_not_found: no DataLake module found "
                            "and SDL_ROOT not set"
                        ),
                    }
                lake = _LocalDataLake(Path(sdl_root).resolve())

            validate_and_log(source_record, schema_path="promoter.promote")
            sdl_ref = lake.store(source_record)
            if not isinstance(sdl_ref, str) or not sdl_ref.startswith("sdl://"):
                sdl_ref = f"sdl://{source_record['artifact_id']}"
            return {"status": "success", "sdl_ref": sdl_ref, "reason": ""}
        except Exception as exc:  # never raise
            return {
                "status": "failure",
                "sdl_ref": "",
                "reason": f"data_lake_store_error: {exc}",
            }
