"""Tests for MarkdownAuthorityScanner — Phase I."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.governance import MarkdownAuthorityScanner

from ._fixtures import (
    stage_full_repo_copy,
    stage_minimal_repo,
    write_py_file,
)


class MarkdownAuthorityScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        stage_minimal_repo(self.repo_root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_codebase_clean(self) -> None:
        """The actual repo source must produce zero flags (EVAL-GOV-004).

        Scans a copy of the real src/ tree so we don't pollute the live
        governance/audits/ directory.
        """
        with tempfile.TemporaryDirectory() as full_tmp:
            full_root = Path(full_tmp)
            stage_full_repo_copy(full_root)
            result = MarkdownAuthorityScanner().scan(full_root)
            self.assertEqual(
                result["total_flagged"],
                0,
                f"unexpected flags: {result['flagged_items']}",
            )

    def test_disallowed_read_flagged(self) -> None:
        write_py_file(
            self.repo_root,
            "src/spectrum_systems_core/_naughty.py",
            (
                'def read():\n'
                '    with open("steal.md", "r") as fh:\n'
                '        return fh.read()\n'
            ),
        )
        result = MarkdownAuthorityScanner().scan(self.repo_root)
        flags = [
            f
            for f in result["flagged_items"]
            if "_naughty.py" in f["item_id"]
        ]
        self.assertTrue(flags)
        self.assertEqual(flags[0]["severity"], "high")

    def test_allowed_path_not_flagged(self) -> None:
        # cli.py is in ALLOWED_MD_READ_PATHS — content should be ignored.
        write_py_file(
            self.repo_root,
            "src/spectrum_systems_core/cli.py",
            (
                'def read():\n'
                '    with open("review.md", "r") as fh:\n'
                '        return fh.read()\n'
            ),
        )
        result = MarkdownAuthorityScanner().scan(self.repo_root)
        flags = [
            f
            for f in result["flagged_items"]
            if "cli.py" in f["item_id"]
        ]
        self.assertFalse(flags)

    def test_write_mode_not_flagged(self) -> None:
        write_py_file(
            self.repo_root,
            "src/spectrum_systems_core/_writer.py",
            (
                'def write():\n'
                '    with open("out.md", "w") as fh:\n'
                '        fh.write("ok")\n'
            ),
        )
        result = MarkdownAuthorityScanner().scan(self.repo_root)
        flags = [
            f
            for f in result["flagged_items"]
            if "_writer.py" in f["item_id"]
        ]
        self.assertFalse(flags)

    def test_test_files_excluded(self) -> None:
        write_py_file(
            self.repo_root,
            "tests/governance/_helper.py",
            (
                'def read():\n'
                '    with open("foo.md", "r") as fh:\n'
                '        return fh.read()\n'
            ),
        )
        result = MarkdownAuthorityScanner().scan(self.repo_root)
        flags = [
            f
            for f in result["flagged_items"]
            if "_helper.py" in f["item_id"]
        ]
        self.assertFalse(flags)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
