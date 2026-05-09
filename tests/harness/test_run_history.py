"""Tests for RunHistoryStore (Phase G — FINDING-G-001)."""
from __future__ import annotations

import datetime
import json
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.harness import (
    MAX_ACTIVE_RUN_HISTORY,
    RUN_HISTORY_RETENTION_DAYS,
    RunHistoryStore,
)

from ._fixtures import utcnow_iso, write_synthesis_run


def _past_iso(days_ago: int) -> str:
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=days_ago
    )
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


class RunHistoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_record_run_writes_to_index(self) -> None:
        run_id = write_synthesis_run(self.repo_root)
        manifest = json.loads(
            (self.repo_root / "synthesis" / run_id / "run_manifest.json").read_text()
        )
        result = RunHistoryStore().record_run(manifest, str(self.repo_root))
        self.assertEqual(result["status"], "success")
        index = json.loads(
            (self.repo_root / "harness" / "runs" / "index.json").read_text()
        )
        self.assertEqual(len(index["runs"]), 1)
        self.assertEqual(index["runs"][0]["run_id"], run_id)
        self.assertEqual(index["runs"][0]["outcome"], "success")

    def test_retention_archives_old_runs(self) -> None:
        # Recent run.
        recent = write_synthesis_run(self.repo_root)
        # Old run.
        old = write_synthesis_run(
            self.repo_root,
            started_at=_past_iso(RUN_HISTORY_RETENTION_DAYS + 5),
            completed_at=_past_iso(RUN_HISTORY_RETENTION_DAYS + 5),
        )
        for rid in (recent, old):
            manifest = json.loads(
                (self.repo_root / "synthesis" / rid / "run_manifest.json").read_text()
            )
            RunHistoryStore().record_run(manifest, str(self.repo_root))
        index = json.loads(
            (self.repo_root / "harness" / "runs" / "index.json").read_text()
        )
        run_ids = [e["run_id"] for e in index["runs"]]
        self.assertIn(recent, run_ids)
        self.assertNotIn(old, run_ids)
        self.assertIsNotNone(index["last_archived_at"])

    def test_archive_file_exists_after_retention(self) -> None:
        old = write_synthesis_run(
            self.repo_root,
            started_at=_past_iso(RUN_HISTORY_RETENTION_DAYS + 1),
            completed_at=_past_iso(RUN_HISTORY_RETENTION_DAYS + 1),
        )
        manifest = json.loads(
            (self.repo_root / "synthesis" / old / "run_manifest.json").read_text()
        )
        RunHistoryStore().record_run(manifest, str(self.repo_root))
        archived = self.repo_root / "harness" / "runs" / "archive" / f"{old}.json"
        self.assertTrue(archived.is_file())
        archived_data = json.loads(archived.read_text())
        self.assertEqual(archived_data["history_entry"]["run_id"], old)

    def test_retention_caps_at_max_active(self) -> None:
        store = RunHistoryStore()
        # Fake-write more than the cap entries directly to index for speed.
        index_path = self.repo_root / "harness" / "runs" / "index.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        runs = []
        for i in range(MAX_ACTIVE_RUN_HISTORY + 5):
            entry_id = f"00000000-0000-4000-8000-{i:012d}"
            run_uuid = f"00000000-0000-4000-9000-{i:012d}"
            runs.append(
                {
                    "entry_id": entry_id,
                    "run_id": run_uuid,
                    "run_type": "synthesis",
                    "source_ids": [],
                    "audience": "policy",
                    "purpose": "report",
                    "started_at": utcnow_iso(),
                    "completed_at": utcnow_iso(),
                    "outcome": "success",
                    "eval_pass_count": 1,
                    "eval_fail_count": 0,
                    "eval_warn_count": 0,
                    "block_reason_codes": [],
                    "total_cost_usd": 0.001,
                    "artifact_ids_produced": [],
                    "recorded_at": (
                        datetime.datetime.now(datetime.timezone.utc)
                        - datetime.timedelta(seconds=MAX_ACTIVE_RUN_HISTORY + 5 - i)
                    ).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                }
            )
        index_path.write_text(
            json.dumps({"last_archived_at": None, "runs": runs}, indent=2)
            + "\n",
            encoding="utf-8",
        )
        store._apply_retention(str(self.repo_root))
        index = json.loads(index_path.read_text())
        self.assertEqual(len(index["runs"]), MAX_ACTIVE_RUN_HISTORY)

    def test_record_run_fails_gracefully_on_bad_manifest(self) -> None:
        result = RunHistoryStore().record_run({}, str(self.repo_root))
        self.assertEqual(result["status"], "failure")
        self.assertEqual(result["entry_id"], "")

    def test_get_recent_runs_returns_n(self) -> None:
        for _ in range(3):
            run_id = write_synthesis_run(self.repo_root)
            manifest = json.loads(
                (self.repo_root / "synthesis" / run_id / "run_manifest.json").read_text()
            )
            RunHistoryStore().record_run(manifest, str(self.repo_root))
        recent = RunHistoryStore().get_recent_runs(str(self.repo_root), n=2)
        self.assertEqual(len(recent), 2)

    def test_get_runs_by_outcome_filters_correctly(self) -> None:
        for _ in range(2):
            run_id = write_synthesis_run(self.repo_root)
            manifest = json.loads(
                (self.repo_root / "synthesis" / run_id / "run_manifest.json").read_text()
            )
            RunHistoryStore().record_run(manifest, str(self.repo_root))
        # Add a blocked run.
        blocked_id = write_synthesis_run(
            self.repo_root, ungrounded_sections=2, grounded_sections=0
        )
        manifest = json.loads(
            (self.repo_root / "synthesis" / blocked_id / "run_manifest.json").read_text()
        )
        RunHistoryStore().record_run(manifest, str(self.repo_root))
        successes = RunHistoryStore().get_runs_by_outcome(
            "success", str(self.repo_root)
        )
        self.assertEqual(len(successes), 2)
        blocks = RunHistoryStore().get_runs_by_outcome(
            "blocked", str(self.repo_root)
        )
        self.assertEqual(len(blocks), 1)

    def test_projection_written_with_view_only_banner(self) -> None:
        run_id = write_synthesis_run(self.repo_root)
        manifest = json.loads(
            (self.repo_root / "synthesis" / run_id / "run_manifest.json").read_text()
        )
        RunHistoryStore().record_run(manifest, str(self.repo_root))
        path = RunHistoryStore().write_run_history_projection(str(self.repo_root))
        body = Path(path).read_text(encoding="utf-8")
        self.assertIn("VIEW ONLY", body)
        self.assertIn(run_id[:12], body)


if __name__ == "__main__":
    unittest.main()
