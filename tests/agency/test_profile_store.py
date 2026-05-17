"""Tests for AgencyProfileStore."""
from __future__ import annotations

import datetime
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.agency.profile_store import AgencyProfileStore

from ._fixtures import make_position_entry, read_jsonl


class AgencyProfileStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.store = AgencyProfileStore()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_get_or_create_new_profile(self) -> None:
        profile = self.store.get_or_create("FCC", str(self.repo_root))
        self.assertEqual(profile["agency_slug"], "fcc")
        self.assertEqual(profile["agency_name"], "FCC")
        self.assertTrue(
            (self.repo_root / "agency" / "fcc" / "profile.json").is_file()
        )

    def test_get_or_create_existing_profile_returns_same(self) -> None:
        first = self.store.get_or_create("FCC", str(self.repo_root))
        second = self.store.get_or_create(
            "Federal Communications Commission", str(self.repo_root)
        )
        self.assertEqual(first["profile_id"], second["profile_id"])
        # Only one directory.
        self.assertEqual(
            len(list((self.repo_root / "agency").iterdir())), 1
        )

    def test_add_position_validates_schema(self) -> None:
        self.store.get_or_create("FCC", str(self.repo_root))
        invalid = make_position_entry(
            agency_slug="fcc",
            topic="x",  # too short
            statement="too short",
        )
        result = self.store.add_position("fcc", invalid, str(self.repo_root))
        self.assertEqual(result["status"], "failure")
        self.assertIn("schema_violation", result["reason"])

    def test_add_position_rejects_invalid_date_range(self) -> None:
        self.store.get_or_create("FCC", str(self.repo_root))
        position = make_position_entry(
            agency_slug="fcc",
            topic="spectrum allocation",
            statement="A very serious concern about the methodology used here.",
            valid_from="2025-01-01",
            valid_until="2020-01-01",
        )
        result = self.store.add_position("fcc", position, str(self.repo_root))
        self.assertEqual(result["status"], "failure")
        self.assertEqual(result["reason"], "valid_until_before_valid_from")

    def test_get_active_positions_applies_recency_cutoff(self) -> None:
        self.store.get_or_create("FCC", str(self.repo_root))
        today = datetime.date.today()
        five_years_ago = today - datetime.timedelta(days=365 * 5)
        old_expiry = (five_years_ago + datetime.timedelta(days=30)).isoformat()
        recent = make_position_entry(
            agency_slug="fcc",
            topic="topic-a",
            statement="A very recent and active position about topic A.",
        )
        old = make_position_entry(
            agency_slug="fcc",
            topic="topic-b",
            statement="An old position that has long since expired and is stale.",
            valid_from=five_years_ago.isoformat(),
            valid_until=old_expiry,
        )
        active_recent = make_position_entry(
            agency_slug="fcc",
            topic="topic-c",
            statement="Another recent position with no expiry, should be kept.",
        )
        for pos in [recent, old, active_recent]:
            res = self.store.add_position("fcc", pos, str(self.repo_root))
            self.assertEqual(res["status"], "success", pos.get("topic"))

        active = self.store.get_active_positions(
            "fcc", str(self.repo_root), recency_years=3
        )
        topics = {p["topic"] for p in active}
        self.assertIn("topic-a", topics)
        self.assertIn("topic-c", topics)
        self.assertNotIn("topic-b", topics)
        # Old position is still on disk (not deleted).
        all_pos = read_jsonl(
            self.repo_root / "agency" / "fcc" / "positions.jsonl"
        )
        self.assertEqual(len(all_pos), 3)

    def test_update_counts_increments_correctly(self) -> None:
        self.store.get_or_create("FCC", str(self.repo_root))
        self.store.update_counts("fcc", 3, 2, str(self.repo_root))
        profile = self.store.load("fcc", str(self.repo_root))
        self.assertEqual(profile["total_comment_count"], 3)
        self.assertEqual(profile["total_objection_count"], 2)

    def test_objection_history_appended_not_overwritten(self) -> None:
        self.store.get_or_create("FCC", str(self.repo_root))
        from ._fixtures import write_objection_history_entry

        # Add via direct file write. Verify add_objection_history dedups by entry_id.
        first = write_objection_history_entry(
            self.repo_root,
            agency_slug="fcc",
            objection_text="The FCC objects to the proposed methodology choices.",
        )
        # Re-add the same entry — should be skipped, not duplicated.
        result = self.store.add_objection_history(
            "fcc", first, str(self.repo_root)
        )
        self.assertEqual(result["status"], "skipped_duplicate")
        # New entry is appended.
        from ._fixtures import write_objection_history_entry as _w
        second = _w(
            self.repo_root,
            agency_slug="fcc",
            objection_text="A second distinct objection from the FCC about scope.",
        )
        # _w wrote it directly. Make sure file has both.
        history = read_jsonl(
            self.repo_root / "agency" / "fcc" / "objection_history.jsonl"
        )
        self.assertEqual(len(history), 2)


if __name__ == "__main__":
    unittest.main()
