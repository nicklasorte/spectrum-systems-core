"""AgencyProfileStore: CRUD for agency_profile + positions.jsonl + objection_history.jsonl.

Append-only at the file level. Profile counts are read-modify-write.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

import jsonschema

from ._paths import agency_dir, agency_schema_path
from .alias_normalizer import AliasNormalizer

_COMPONENT_NAME = "agency_profile_store"
_COMPONENT_VERSION = "1.0.0"
DEFAULT_RECENCY_YEARS = 3


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _today_date() -> datetime.date:
    return datetime.datetime.now(datetime.timezone.utc).date()


def _execution_fingerprint(*parts: str) -> str:
    seed = "|".join(parts) + f"|{_COMPONENT_NAME}:{_COMPONENT_VERSION}"
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.is_file():
        return out
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


def _validate(record: dict[str, Any], schema_name: str) -> str | None:
    schema = json.loads(agency_schema_path(schema_name).read_text(encoding="utf-8"))
    try:
        jsonschema.Draft202012Validator(schema).validate(record)
    except jsonschema.ValidationError as exc:
        return exc.message
    return None


class AgencyProfileStore:
    """Read/write agency profiles and their position + objection history files."""

    def get_or_create(self, agency_name: str, repo_root: str) -> dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        normalizer = AliasNormalizer()
        slug = normalizer.normalize(agency_name, str(repo_root_path))
        target_dir = agency_dir(repo_root_path, slug, create=False)
        profile_path = target_dir / "profile.json"
        if profile_path.is_file():
            try:
                return json.loads(profile_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        # Create new profile.
        target_dir.mkdir(parents=True, exist_ok=True)
        now = _now_iso()
        profile = {
            "profile_id": str(uuid.uuid4()),
            "agency_name": agency_name.strip() or slug,
            "agency_slug": slug,
            "aliases": [],
            "jurisdiction": "",
            "description": "",
            "active": True,
            "created_at": now,
            "updated_at": now,
            "total_comment_count": 0,
            "total_objection_count": 0,
            "provenance": {
                "produced_by": {
                    "component": _COMPONENT_NAME,
                    "version": _COMPONENT_VERSION,
                },
                "execution_fingerprint_hash": _execution_fingerprint(slug, now),
            },
        }
        validation_error = _validate(profile, "agency_profile")
        if validation_error is not None:
            # Surface schema problems so they cannot be ignored silently.
            raise ValueError(f"agency_profile schema invalid: {validation_error}")
        profile_path.write_text(
            json.dumps(profile, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return profile

    def load(self, agency_slug: str, repo_root: str) -> dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        profile_path = agency_dir(repo_root_path, agency_slug) / "profile.json"
        if not profile_path.is_file():
            raise FileNotFoundError(f"profile_not_found: {agency_slug}")
        return json.loads(profile_path.read_text(encoding="utf-8"))

    def update_counts(
        self,
        agency_slug: str,
        comment_count_delta: int,
        objection_count_delta: int,
        repo_root: str,
    ) -> None:
        repo_root_path = Path(repo_root).resolve()
        profile_path = agency_dir(repo_root_path, agency_slug) / "profile.json"
        if not profile_path.is_file():
            return
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        profile["total_comment_count"] = max(
            0, int(profile.get("total_comment_count", 0)) + int(comment_count_delta)
        )
        profile["total_objection_count"] = max(
            0,
            int(profile.get("total_objection_count", 0)) + int(objection_count_delta),
        )
        profile["updated_at"] = _now_iso()
        profile_path.write_text(
            json.dumps(profile, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def add_position(
        self, agency_slug: str, position: dict[str, Any], repo_root: str
    ) -> dict[str, Any]:
        # Validate temporal range BEFORE schema (CHECK-RT2-003).
        valid_from = position.get("valid_from")
        valid_until = position.get("valid_until")
        if valid_from and valid_until:
            try:
                vf = datetime.date.fromisoformat(str(valid_from))
                vu = datetime.date.fromisoformat(str(valid_until))
                if vu < vf:
                    return {
                        "status": "failure",
                        "reason": "valid_until_before_valid_from",
                    }
            except ValueError as exc:
                return {"status": "failure", "reason": f"date_parse_error: {exc}"}

        validation_error = _validate(position, "position_entry")
        if validation_error is not None:
            return {
                "status": "failure",
                "reason": f"schema_violation: {validation_error}",
            }

        repo_root_path = Path(repo_root).resolve()
        target = agency_dir(repo_root_path, agency_slug, create=True) / "positions.jsonl"
        try:
            _append_jsonl(target, position)
        except OSError as exc:
            return {"status": "failure", "reason": f"write_error: {exc}"}
        return {"status": "success", "reason": ""}

    def get_active_positions(
        self,
        agency_slug: str,
        repo_root: str,
        recency_years: int = DEFAULT_RECENCY_YEARS,
    ) -> list[dict[str, Any]]:
        repo_root_path = Path(repo_root).resolve()
        positions = _read_jsonl(
            agency_dir(repo_root_path, agency_slug) / "positions.jsonl"
        )
        cutoff = _today_date() - datetime.timedelta(days=365 * max(recency_years, 0))
        active: list[dict[str, Any]] = []
        for pos in positions:
            if pos.get("superseded_by") is not None:
                continue
            valid_until = pos.get("valid_until")
            if valid_until is None:
                active.append(pos)
                continue
            try:
                vu = datetime.date.fromisoformat(str(valid_until))
            except ValueError:
                continue
            if vu >= cutoff:
                active.append(pos)
        # Most recent first.
        active.sort(key=lambda p: str(p.get("valid_from") or ""), reverse=True)
        return active

    def add_objection_history(
        self, agency_slug: str, entry: dict[str, Any], repo_root: str
    ) -> dict[str, Any]:
        validation_error = _validate(entry, "objection_history_entry")
        if validation_error is not None:
            return {
                "status": "failure",
                "reason": f"schema_violation: {validation_error}",
            }
        repo_root_path = Path(repo_root).resolve()
        target = (
            agency_dir(repo_root_path, agency_slug, create=True)
            / "objection_history.jsonl"
        )
        # Skip if entry_id already present (CHECK-RT2-005).
        existing = _read_jsonl(target)
        existing_ids = {e.get("entry_id") for e in existing}
        if entry.get("entry_id") in existing_ids:
            return {"status": "skipped_duplicate", "reason": "entry_id_exists"}
        try:
            _append_jsonl(target, entry)
        except OSError as exc:
            return {"status": "failure", "reason": f"write_error: {exc}"}
        return {"status": "success", "reason": ""}

    def get_objection_history(
        self, agency_slug: str, repo_root: str, limit: int | None = None
    ) -> list[dict[str, Any]]:
        repo_root_path = Path(repo_root).resolve()
        entries = _read_jsonl(
            agency_dir(repo_root_path, agency_slug) / "objection_history.jsonl"
        )
        entries.sort(key=lambda e: str(e.get("raised_at") or ""), reverse=True)
        if limit is not None:
            return entries[:limit]
        return entries

    def write_agency_projection(
        self,
        agency_slug: str,
        repo_root: str,
        vault_root: str | None = None,
    ) -> str:
        from ..ingestion.obsidian_projection import ObsidianProjection

        repo_root_path = Path(repo_root).resolve()
        profile = self.load(agency_slug, str(repo_root_path))
        positions = self.get_active_positions(agency_slug, str(repo_root_path))
        all_positions = _read_jsonl(
            agency_dir(repo_root_path, agency_slug) / "positions.jsonl"
        )
        history = self.get_objection_history(
            agency_slug, str(repo_root_path), limit=10
        )
        projection_path = ObsidianProjection().write_agency_profile_projection(
            profile, positions, all_positions, history, str(repo_root_path)
        )
        if vault_root:
            try:
                vault_path = (
                    Path(vault_root).resolve() / "Agency" / f"{agency_slug}.md"
                )
                vault_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(projection_path, vault_path)
            except OSError:
                pass
        return projection_path
