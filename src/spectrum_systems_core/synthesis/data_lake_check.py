"""DataLakeChecker: verify an artifact_id exists in the data lake or repo.

GroundingEval depends on this to detect fabricated citations
(FINDING-F-004). The checker tries the external DataLake.exists() if
available, then falls back to scanning local promoted/evidenced
artifacts under the repo root.

A small in-memory cache makes the per-section grounding eval cheap.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any


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
            return getattr(module, "DataLake", None)
        except ImportError:
            continue
    return None


class DataLakeChecker:
    """Check whether artifact_ids exist in the data lake or local promoted set."""

    def __init__(self, repo_root: str):
        self.repo_root = Path(repo_root).resolve()
        self._known: set[str] | None = None
        self._external: Any = None
        self._tried_external = False

    def _ensure_external(self) -> None:
        if self._tried_external:
            return
        self._tried_external = True
        cls = _load_external_data_lake_class()
        if cls is None:
            return
        try:
            instance = cls()
        except Exception:  # noqa: BLE001
            return
        if hasattr(instance, "exists"):
            self._external = instance

    def _scan_local(self) -> set[str]:
        from ..ingestion.source_loader import SOURCE_FAMILIES

        known: set[str] = set()

        # SDL_ROOT direct .json files (Phase A local fallback).
        sdl_root_env = os.environ.get("SDL_ROOT")
        if sdl_root_env:
            sdl_root = Path(sdl_root_env)
            if sdl_root.is_dir():
                for child in sdl_root.glob("*.json"):
                    known.add(child.stem)

        base = self.repo_root / "processed"
        if base.is_dir():
            for family in SOURCE_FAMILIES:
                family_dir = base / family
                if not family_dir.is_dir():
                    continue
                for source_dir in family_dir.iterdir():
                    if not source_dir.is_dir():
                        continue
                    # Promoted stories.
                    promoted_stories = source_dir / "stories" / "promoted"
                    if promoted_stories.is_dir():
                        for child in promoted_stories.glob("*.json"):
                            try:
                                doc = json.loads(child.read_text(encoding="utf-8"))
                                sid = doc.get("story_id")
                                if isinstance(sid, str):
                                    known.add(sid)
                            except (OSError, json.JSONDecodeError):
                                continue
                    # Promoted knowledge artifacts.
                    promoted_knowledge = source_dir / "knowledge" / "promoted"
                    if promoted_knowledge.is_dir():
                        for child in promoted_knowledge.glob("*.json"):
                            try:
                                doc = json.loads(child.read_text(encoding="utf-8"))
                                for key in (
                                    "theme_id",
                                    "concept_id",
                                    "analogy_id",
                                    "connection_id",
                                ):
                                    val = doc.get(key)
                                    if isinstance(val, str):
                                        known.add(val)
                            except (OSError, json.JSONDecodeError):
                                continue
                    # Evidenced claims.
                    claims_path = source_dir / "paper" / "claims.jsonl"
                    if claims_path.is_file():
                        try:
                            with claims_path.open("r", encoding="utf-8") as fh:
                                for line in fh:
                                    line = line.strip()
                                    if not line:
                                        continue
                                    try:
                                        doc = json.loads(line)
                                    except json.JSONDecodeError:
                                        continue
                                    if (
                                        str(doc.get("status") or "")
                                        == "evidenced"
                                    ):
                                        cid = doc.get("claim_id")
                                        if isinstance(cid, str):
                                            known.add(cid)
                        except OSError:
                            pass

        return known

    def exists(self, artifact_id: str) -> bool:
        if not isinstance(artifact_id, str) or not artifact_id:
            return False
        self._ensure_external()
        if self._external is not None:
            try:
                return bool(self._external.exists(artifact_id))
            except Exception:  # noqa: BLE001
                pass
        if self._known is None:
            self._known = self._scan_local()
        return artifact_id in self._known
