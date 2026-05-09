"""Tests for OverrideStore (Phase G — FINDING-G-006)."""
from __future__ import annotations

import datetime
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from spectrum_systems_core.harness import (
    OVERRIDE_EXPIRY_WARNING_DAYS,
    OverrideStore,
)


def _override_kwargs(**overrides):
    base = dict(
        decision_context="bypass section grounding for paper-A keynote",
        overridden_artifact_id=str(uuid.uuid4()),
        overridden_eval_or_block="grounding_eval",
        rationale=(
            "human-verified that the citations exist in the supplementary "
            "materials archive — temporary bypass"
        ),
        overriding_human_id="reviewer-1",
    )
    base.update(overrides)
    return base


class OverrideStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.store = OverrideStore()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_record_override_writes_json(self) -> None:
        result = self.store.record_override(
            **_override_kwargs(),
            repo_root=str(self.repo_root),
        )
        self.assertEqual(result["status"], "success")
        files = list(
            (self.repo_root / "harness" / "overrides").glob("*.json")
        )
        self.assertEqual(len(files), 1)

    def test_expires_at_before_created_fails(self) -> None:
        result = self.store.record_override(
            **_override_kwargs(),
            repo_root=str(self.repo_root),
            expires_days=0,
        )
        self.assertEqual(result["status"], "failure")
        self.assertIn("expires_at", result["reason"])

    def test_expired_override_auto_archived(self) -> None:
        # Manually craft an already-expired override.
        ovr_id = str(uuid.uuid4())
        past = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=2)
        ).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        even_more_past = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=10)
        ).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        override = {
            "override_id": ovr_id,
            "decision_context": "expired override for testing auto-archive flow",
            "overridden_artifact_id": str(uuid.uuid4()),
            "overridden_eval_or_block": "grounding_eval",
            "rationale": (
                "expired-on-purpose record used to verify auto-archive lifecycle"
            ),
            "overriding_human_id": "reviewer-x",
            "created_at": even_more_past,
            "expires_at": past,
            "superseded_by": None,
            "status": "active",
        }
        target_dir = self.repo_root / "harness" / "overrides"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / f"{ovr_id}.json").write_text(
            json.dumps(override, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        actives = self.store.get_active_overrides(str(self.repo_root))
        self.assertEqual(actives, [])
        archived = (
            self.repo_root / "harness" / "overrides" / "archive" / f"{ovr_id}.json"
        )
        self.assertTrue(archived.is_file())
        archived_data = json.loads(archived.read_text())
        self.assertEqual(archived_data["status"], "expired")
        self.assertFalse(
            (target_dir / f"{ovr_id}.json").is_file()
        )

    def test_expiring_soon_has_warning_flag(self) -> None:
        result = self.store.record_override(
            **_override_kwargs(),
            repo_root=str(self.repo_root),
            expires_days=OVERRIDE_EXPIRY_WARNING_DAYS - 5,
        )
        self.assertTrue(result["warning"])
        actives = self.store.get_active_overrides(str(self.repo_root))
        self.assertEqual(len(actives), 1)
        self.assertTrue(actives[0]["_warning"])

    def test_active_override_returned_correctly(self) -> None:
        self.store.record_override(
            **_override_kwargs(),
            repo_root=str(self.repo_root),
            expires_days=200,
        )
        actives = self.store.get_active_overrides(str(self.repo_root))
        self.assertEqual(len(actives), 1)
        self.assertFalse(actives[0]["_warning"])

    def test_archive_file_exists_after_expiry(self) -> None:
        # Same as test_expired_override_auto_archived but verifying archive content.
        ovr_id = str(uuid.uuid4())
        past = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=1)
        ).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        even_more_past = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=5)
        ).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        override = {
            "override_id": ovr_id,
            "decision_context": "expired override for testing auto-archive flow",
            "overridden_artifact_id": str(uuid.uuid4()),
            "overridden_eval_or_block": "grounding_eval",
            "rationale": (
                "expired-on-purpose record used to verify auto-archive lifecycle"
            ),
            "overriding_human_id": "reviewer-x",
            "created_at": even_more_past,
            "expires_at": past,
            "superseded_by": None,
            "status": "active",
        }
        target_dir = self.repo_root / "harness" / "overrides"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / f"{ovr_id}.json").write_text(
            json.dumps(override, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.store.get_active_overrides(str(self.repo_root))
        archive = (
            self.repo_root / "harness" / "overrides" / "archive" / f"{ovr_id}.json"
        )
        self.assertTrue(archive.is_file())


if __name__ == "__main__":
    unittest.main()
