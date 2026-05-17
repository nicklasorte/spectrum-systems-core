"""OverrideStore — recorded human overrides with expiry lifecycle.

FINDING-G-006: every override has expires_at (default today + 365 days).
30-day expiry warning. Expired overrides are auto-archived (never deleted).
"""
from __future__ import annotations

import datetime
import logging
import uuid
from pathlib import Path
from typing import Any

from . import OVERRIDE_DEFAULT_EXPIRY_DAYS, OVERRIDE_EXPIRY_WARNING_DAYS
from ._io import parse_iso, read_json, write_json
from ._paths import overrides_archive_dir, overrides_dir
from ._schema import validate_harness_artifact

_LOG = logging.getLogger(__name__)


class OverrideStore:
    def record_override(
        self,
        decision_context: str,
        overridden_artifact_id: str,
        overridden_eval_or_block: str,
        rationale: str,
        overriding_human_id: str,
        repo_root: str | Path,
        expires_days: int | None = None,
    ) -> dict[str, Any]:
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            days = (
                int(expires_days)
                if expires_days is not None
                else OVERRIDE_DEFAULT_EXPIRY_DAYS
            )
            expires_at_dt = now + datetime.timedelta(days=days)
            if expires_at_dt <= now:
                return {
                    "status": "failure",
                    "override_id": "",
                    "reason": "expires_at must be strictly after created_at",
                }

            override = {
                "override_id": str(uuid.uuid4()),
                "decision_context": decision_context,
                "overridden_artifact_id": overridden_artifact_id,
                "overridden_eval_or_block": overridden_eval_or_block,
                "rationale": rationale,
                "overriding_human_id": overriding_human_id,
                "created_at": now.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                "expires_at": expires_at_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                "superseded_by": None,
                "status": "active",
            }
            ok, err = validate_harness_artifact(override, "override_artifact")
            if not ok:
                return {
                    "status": "failure",
                    "override_id": "",
                    "reason": f"schema_violation: {err}",
                }
            target = overrides_dir(repo_root) / f"{override['override_id']}.json"
            write_json(target, override)
            return {
                "status": "success",
                "override_id": override["override_id"],
                "expires_at": override["expires_at"],
                "warning": self.check_override_warning(override),
                "reason": "",
            }
        except OSError as exc:  # pragma: no cover
            _LOG.warning("record_override failed: %s", exc)
            return {
                "status": "failure",
                "override_id": "",
                "reason": str(exc),
            }

    def get_active_overrides(self, repo_root: str | Path) -> list[dict[str, Any]]:
        directory = overrides_dir(repo_root)
        if not directory.is_dir():
            return []
        active: list[dict[str, Any]] = []
        now = datetime.datetime.now(datetime.timezone.utc)
        for path in sorted(directory.glob("*.json")):
            override = read_json(path)
            if override is None:
                continue
            expires_at = parse_iso(override.get("expires_at"))
            if expires_at is None:
                continue
            if expires_at <= now:
                self._archive_override(override, path, repo_root)
                continue
            override["_warning"] = self.check_override_warning(override)
            active.append(override)
        return active

    def check_override_warning(self, override: dict[str, Any]) -> bool:
        expires_at = parse_iso(override.get("expires_at"))
        if expires_at is None:
            return False
        now = datetime.datetime.now(datetime.timezone.utc)
        delta = expires_at - now
        return 0 < delta.total_seconds() <= OVERRIDE_EXPIRY_WARNING_DAYS * 86400

    def _archive_override(
        self,
        override: dict[str, Any],
        original_path: Path,
        repo_root: str | Path,
    ) -> None:
        archive_dir = overrides_archive_dir(repo_root)
        archive_dir.mkdir(parents=True, exist_ok=True)
        override = {**override, "status": "expired"}
        target = archive_dir / original_path.name
        try:
            write_json(target, override)
            try:
                original_path.unlink()
            except OSError as exc:  # pragma: no cover
                _LOG.warning("archive unlink failed: %s", exc)
        except OSError as exc:  # pragma: no cover
            _LOG.warning("archive write failed: %s", exc)

    def write_overrides_projection(
        self,
        repo_root: str | Path,
        vault_root: str | Path | None = None,
    ) -> str:
        from ..ingestion.obsidian_projection import ObsidianProjection

        return ObsidianProjection().write_overrides_projection(
            repo_root, vault_root
        )
