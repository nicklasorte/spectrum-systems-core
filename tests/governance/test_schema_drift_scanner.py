"""Tests for SchemaDriftScanner — Phase I."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.governance import SchemaDriftScanner

from ._fixtures import stage_minimal_repo


class SchemaDriftScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        stage_minimal_repo(self.repo_root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_clean_codebase_runs_without_crash(self) -> None:
        result = SchemaDriftScanner().scan(self.repo_root)
        self.assertEqual(result["audit_type"], "schema_drift")
        self.assertGreaterEqual(result["current_value"]["total_schemas"], 1)

    def test_first_audit_prior_value_null(self) -> None:
        result = SchemaDriftScanner().scan(self.repo_root)
        self.assertIsNone(result["prior_value"])
        self.assertIsNone(result["delta"])

    def test_second_audit_prior_value_populated(self) -> None:
        first = SchemaDriftScanner().scan(self.repo_root)
        second = SchemaDriftScanner().scan(self.repo_root)
        self.assertEqual(second["prior_value"], first["current_value"])
        self.assertIsNotNone(second["delta"])

    def test_broken_ref_flagged_high(self) -> None:
        target = (
            self.repo_root
            / "contracts"
            / "schemas"
            / "governance"
            / "broken_test.schema.json"
        )
        target.write_text(
            json.dumps(
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "x",
                    "title": "broken_test",
                    "type": "object",
                    "properties": {
                        "ref_field": {"$ref": "./does_not_exist.schema.json"}
                    },
                }
            ),
            encoding="utf-8",
        )
        result = SchemaDriftScanner().scan(self.repo_root)
        broken = [
            f
            for f in result["flagged_items"]
            if f["item_type"] == "broken_ref"
        ]
        self.assertTrue(broken)
        self.assertEqual(broken[0]["severity"], "high")

    def test_unused_schema_flagged_medium(self) -> None:
        target = (
            self.repo_root
            / "contracts"
            / "schemas"
            / "governance"
            / "totally_unused.schema.json"
        )
        target.write_text(
            json.dumps(
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "x",
                    "title": "totally_unused_artifact",
                    "type": "object",
                }
            ),
            encoding="utf-8",
        )
        result = SchemaDriftScanner().scan(self.repo_root)
        unused = [
            f
            for f in result["flagged_items"]
            if f["item_type"] == "unused_schema"
        ]
        self.assertTrue(unused)
        for f in unused:
            self.assertEqual(f["severity"], "medium")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
