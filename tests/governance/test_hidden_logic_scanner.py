"""Tests for HiddenLogicScanner — Phase I."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.governance import HiddenLogicScanner

from ._fixtures import stage_minimal_repo, write_py_file


class HiddenLogicScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        stage_minimal_repo(self.repo_root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_uuid_in_source_flagged(self) -> None:
        write_py_file(
            self.repo_root,
            "src/spectrum_systems_core/_bad_uuid.py",
            'BAD = "11111111-2222-4333-8444-555555555555"\n',
        )
        result = HiddenLogicScanner().scan(self.repo_root)
        flags = [
            f
            for f in result["flagged_items"]
            if f["item_type"] == "uuid_literal_in_source"
        ]
        self.assertTrue(flags)

    def test_uuid_in_tests_excluded(self) -> None:
        write_py_file(
            self.repo_root,
            "tests/fixtures/_uuid_fixture.py",
            'OK = "11111111-2222-4333-8444-555555555555"\n',
        )
        result = HiddenLogicScanner().scan(self.repo_root)
        flags = [
            f
            for f in result["flagged_items"]
            if f["item_type"] == "uuid_literal_in_source"
            and "tests/" in f["item_id"]
        ]
        self.assertFalse(flags)

    def test_prompt_in_registry_excluded(self) -> None:
        write_py_file(
            self.repo_root,
            "ai/registry/loader.py",
            'PROMPT = "You are a registry helper"\n',
        )
        result = HiddenLogicScanner().scan(self.repo_root)
        flags = [
            f
            for f in result["flagged_items"]
            if "ai/registry" in f["item_id"]
        ]
        self.assertFalse(flags)

    def test_prompt_in_random_file_flagged_high(self) -> None:
        write_py_file(
            self.repo_root,
            "src/spectrum_systems_core/_some_module.py",
            'PROMPT = "You are an extractor.\\nReturn ONLY JSON."\n',
        )
        result = HiddenLogicScanner().scan(self.repo_root)
        flags = [
            f
            for f in result["flagged_items"]
            if f["item_type"] == "prompt_like_string_outside_registry"
            and "_some_module.py" in f["item_id"]
        ]
        self.assertTrue(flags)
        self.assertEqual(flags[0]["severity"], "high")

    def test_eval_branching_in_eval_module_excluded(self) -> None:
        write_py_file(
            self.repo_root,
            "src/spectrum_systems_core/paper/test_branch.py",
            (
                'def f(eval_result):\n'
                '    if eval_result == "fail":\n'
                '        return 1\n'
            ),
        )
        result = HiddenLogicScanner().scan(self.repo_root)
        flags = [
            f
            for f in result["flagged_items"]
            if f["item_type"] == "eval_status_branch_outside_eval_module"
            and "paper/test_branch.py" in f["item_id"]
        ]
        self.assertFalse(flags)

    def test_eval_branching_outside_eval_module_flagged(self) -> None:
        write_py_file(
            self.repo_root,
            "src/spectrum_systems_core/control/_test_branch.py",
            (
                'def f(eval_result):\n'
                '    if eval_result == "fail":\n'
                '        return 1\n'
            ),
        )
        result = HiddenLogicScanner().scan(self.repo_root)
        flags = [
            f
            for f in result["flagged_items"]
            if f["item_type"] == "eval_status_branch_outside_eval_module"
            and "control/_test_branch.py" in f["item_id"]
        ]
        self.assertTrue(flags)
        self.assertEqual(flags[0]["severity"], "high")

    def test_path_prefix_match_not_substring(self) -> None:
        # tests_helper.py — path contains "tests" but not the prefix "tests/"
        write_py_file(
            self.repo_root,
            "src/spectrum_systems_core/tests_helper.py",
            'BAD = "11111111-2222-4333-8444-555555555555"\n',
        )
        result = HiddenLogicScanner().scan(self.repo_root)
        flags = [
            f
            for f in result["flagged_items"]
            if f["item_type"] == "uuid_literal_in_source"
            and "tests_helper.py" in f["item_id"]
        ]
        # tests_helper.py is NOT under tests/ — must be flagged.
        self.assertTrue(flags)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
