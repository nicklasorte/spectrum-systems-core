"""Tests for CompressionScanner — Phase I."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.governance import CompressionScanner

from ._fixtures import stage_minimal_repo, write_py_file


class CompressionScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        stage_minimal_repo(self.repo_root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_unused_class_flagged_investigate(self) -> None:
        write_py_file(
            self.repo_root,
            "src/spectrum_systems_core/_lonely.py",
            "class TotallyLonelyClass:\n    pass\n",
        )
        result = CompressionScanner().scan(self.repo_root)
        # Find candidate file that wraps a class with our name.
        candidates_dir = self.repo_root / "governance" / "candidates"
        files = list(candidates_dir.glob("*.json"))
        self.assertTrue(files, "expected candidate files to be written")
        names = []
        for path in files:
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("candidate_type") == "class":
                names.append(data.get("candidate_name"))
                self.assertEqual(data.get("status"), "proposed")
                self.assertEqual(
                    data.get("recommended_action"), "investigate"
                )
        self.assertIn("TotallyLonelyClass", names)

    def test_active_class_not_flagged(self) -> None:
        write_py_file(
            self.repo_root,
            "src/spectrum_systems_core/_active.py",
            "class ActiveClass:\n    pass\n",
        )
        write_py_file(
            self.repo_root,
            "src/spectrum_systems_core/_user.py",
            "from ._active import ActiveClass\n_x = ActiveClass()\n",
        )
        result = CompressionScanner().scan(self.repo_root)
        import json

        candidates_dir = self.repo_root / "governance" / "candidates"
        active_class_candidates = []
        for path in candidates_dir.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            if (
                data.get("candidate_type") == "class"
                and data.get("candidate_name") == "ActiveClass"
            ):
                active_class_candidates.append(data)
        self.assertFalse(active_class_candidates)

    def test_no_source_modified_during_scan(self) -> None:
        write_py_file(
            self.repo_root,
            "src/spectrum_systems_core/_target.py",
            "class Target:\n    pass\n",
        )
        before = (
            self.repo_root / "src" / "spectrum_systems_core" / "_target.py"
        ).read_text(encoding="utf-8")
        CompressionScanner().scan(self.repo_root)
        after = (
            self.repo_root / "src" / "spectrum_systems_core" / "_target.py"
        ).read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_candidate_action_in_enum(self) -> None:
        import json

        write_py_file(
            self.repo_root,
            "src/spectrum_systems_core/_orphan.py",
            "class OrphanClass:\n    pass\n",
        )
        CompressionScanner().scan(self.repo_root)
        valid = {"remove", "merge", "deprecate", "investigate"}
        for path in (self.repo_root / "governance" / "candidates").glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn(data["recommended_action"], valid)

    def test_candidate_written_to_proposed_dir(self) -> None:
        import json

        write_py_file(
            self.repo_root,
            "src/spectrum_systems_core/_seven.py",
            "class SevenClass:\n    pass\n",
        )
        CompressionScanner().scan(self.repo_root)
        for path in (self.repo_root / "governance" / "candidates").glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "proposed")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
