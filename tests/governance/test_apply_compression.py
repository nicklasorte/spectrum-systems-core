"""Tests for apply-compression — Phase I (FINDING-I-006)."""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from spectrum_systems_core.governance._io import write_json
from spectrum_systems_core.governance._paths import (
    candidates_archive_dir,
    candidates_dir,
    ensure_governance_tree,
)
from spectrum_systems_core.governance.apply_compression import (
    apply_compression,
)

from ._fixtures import stage_minimal_repo, write_py_file


def _seed_candidate(
    repo_root: Path,
    *,
    candidate_type: str = "class",
    candidate_path: str = "src/spectrum_systems_core/_lonely.py",
    candidate_name: str = "LonelyClass",
    recommended_action: str = "investigate",
) -> str:
    ensure_governance_tree(repo_root)
    candidate_id = str(uuid.uuid4())
    candidate = {
        "candidate_id": candidate_id,
        "candidate_type": candidate_type,
        "candidate_path": candidate_path,
        "candidate_name": candidate_name,
        "reason": "Synthetic test candidate for unit coverage.",
        "evidence": {"days_since_use": 60},
        "recommended_action": recommended_action,
        "status": "proposed",
        "proposed_at": "2026-05-09T00:00:00+00:00",
        "applied_at": None,
        "applied_by": None,
        "applied_action_detail": "",
    }
    write_json(
        candidates_dir(repo_root) / f"{candidate_id}.json", candidate
    )
    return candidate_id


class ApplyCompressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        stage_minimal_repo(self.repo_root)
        write_py_file(
            self.repo_root,
            "src/spectrum_systems_core/_lonely.py",
            "class LonelyClass:\n    pass\n",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_apply_investigate_records_only_no_changes(self) -> None:
        candidate_id = _seed_candidate(self.repo_root)
        before = (
            self.repo_root / "src" / "spectrum_systems_core" / "_lonely.py"
        ).read_text(encoding="utf-8")
        result = apply_compression(
            candidate_id=candidate_id,
            action="investigate",
            human_id="alice",
            note="not now",
            repo_root=self.repo_root,
        )
        after = (
            self.repo_root / "src" / "spectrum_systems_core" / "_lonely.py"
        ).read_text(encoding="utf-8")
        self.assertEqual(result["status"], "success")
        self.assertEqual(before, after)
        archive_path = (
            candidates_archive_dir(self.repo_root) / f"{candidate_id}.json"
        )
        self.assertTrue(archive_path.is_file())

    def test_apply_remove_does_not_auto_delete(self) -> None:
        candidate_id = _seed_candidate(
            self.repo_root, recommended_action="remove"
        )
        result = apply_compression(
            candidate_id=candidate_id,
            action="remove",
            human_id="alice",
            repo_root=self.repo_root,
        )
        self.assertEqual(result["status"], "success")
        # File must still exist — no auto-deletion.
        target = (
            self.repo_root / "src" / "spectrum_systems_core" / "_lonely.py"
        )
        self.assertTrue(target.is_file())

    def test_apply_deprecate_renames_or_warns(self) -> None:
        candidate_id = _seed_candidate(
            self.repo_root, recommended_action="deprecate"
        )
        result = apply_compression(
            candidate_id=candidate_id,
            action="deprecate",
            human_id="alice",
            repo_root=self.repo_root,
        )
        self.assertEqual(result["status"], "success")
        original = (
            self.repo_root / "src" / "spectrum_systems_core" / "_lonely.py"
        )
        renamed = (
            self.repo_root
            / "src"
            / "spectrum_systems_core"
            / "_lonely.deprecated.py"
        )
        # One of these is expected: rename succeeded OR detail noted.
        self.assertTrue(
            renamed.is_file() or "deprecation noted" in result.get(
                "applied_action_detail", ""
            )
        )

    def test_apply_already_applied_fails(self) -> None:
        candidate_id = _seed_candidate(self.repo_root)
        first = apply_compression(
            candidate_id=candidate_id,
            action="investigate",
            human_id="alice",
            repo_root=self.repo_root,
        )
        self.assertEqual(first["status"], "success")
        # Re-applying after archive should fail since status is "applied".
        second = apply_compression(
            candidate_id=candidate_id,
            action="investigate",
            human_id="alice",
            repo_root=self.repo_root,
        )
        self.assertEqual(second["status"], "failure")

    def test_apply_archives_candidate(self) -> None:
        candidate_id = _seed_candidate(self.repo_root)
        apply_compression(
            candidate_id=candidate_id,
            action="investigate",
            human_id="alice",
            repo_root=self.repo_root,
        )
        archive_path = (
            candidates_archive_dir(self.repo_root) / f"{candidate_id}.json"
        )
        proposed_path = candidates_dir(self.repo_root) / f"{candidate_id}.json"
        self.assertTrue(archive_path.is_file())
        self.assertFalse(proposed_path.is_file())
        archived = json.loads(archive_path.read_text(encoding="utf-8"))
        self.assertEqual(archived["status"], "applied")
        self.assertEqual(archived["applied_by"], "alice")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
