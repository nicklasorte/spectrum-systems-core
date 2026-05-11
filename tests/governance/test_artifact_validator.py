"""Tests for the artifact_kind -> artifact_type deprecation validator.

Phase Pre-N. The validator must:
- Emit a deprecation warning when artifact_kind is present but
  artifact_type is not.
- Emit no warning when artifact_type is present.
- Not block any write.
- The migration script in --dry-run mode must NEVER touch a file.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spectrum_systems_core.governance.artifact_validator import (
    log_warnings,
    validate_and_log,
    validate_artifact_fields,
)


class ArtifactValidatorTests(unittest.TestCase):
    def test_deprecation_warning_on_artifact_kind_only(self) -> None:
        artifact = {"artifact_kind": "source_record", "artifact_id": "abc"}
        warnings = validate_artifact_fields(artifact, schema_path="ignored")
        self.assertEqual(len(warnings), 1)
        # Content assertion: must mention "DEPRECATION" and the id so a
        # reader can find the source artifact.
        self.assertIn("DEPRECATION", warnings[0])
        self.assertIn("abc", warnings[0])

    def test_no_warning_on_artifact_type_present(self) -> None:
        artifact = {"artifact_type": "source_record", "artifact_id": "x"}
        self.assertEqual(validate_artifact_fields(artifact), [])

    def test_no_warning_when_both_present_and_agree(self) -> None:
        artifact = {
            "artifact_kind": "source_record",
            "artifact_type": "source_record",
            "artifact_id": "x",
        }
        self.assertEqual(validate_artifact_fields(artifact), [])

    def test_error_when_kind_and_type_disagree(self) -> None:
        artifact = {
            "artifact_kind": "source_record",
            "artifact_type": "review_artifact",
            "artifact_id": "x",
        }
        warnings = validate_artifact_fields(artifact)
        self.assertEqual(len(warnings), 1)
        self.assertIn("ERROR", warnings[0])
        self.assertIn("disagree", warnings[0])

    def test_error_when_neither_field_present(self) -> None:
        artifact = {"artifact_id": "x"}
        warnings = validate_artifact_fields(artifact)
        self.assertEqual(len(warnings), 1)
        self.assertIn("ERROR", warnings[0])

    def test_does_not_raise_on_non_dict_input(self) -> None:
        self.assertEqual(validate_artifact_fields("not a dict"), [])  # type: ignore[arg-type]
        self.assertEqual(validate_artifact_fields(None), [])  # type: ignore[arg-type]

    def test_validate_and_log_writes_to_audit_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "audit.log"
            with mock.patch.dict(os.environ, {"SDL_AUDIT_LOG": str(audit_path)}):
                artifact = {"artifact_kind": "source_record", "artifact_id": "x"}
                warnings = validate_and_log(artifact)
                self.assertEqual(len(warnings), 1)
                self.assertTrue(audit_path.exists())
                lines = audit_path.read_text(encoding="utf-8").splitlines()
                self.assertEqual(len(lines), 1)
                entry = json.loads(lines[0])
                self.assertIn("DEPRECATION", entry["msg"])

    def test_log_warnings_never_raises_when_no_log_path_configured(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            # Should silently degrade
            log_warnings(["DEPRECATION: test"])


class ArtifactKindMigrationScriptTests(unittest.TestCase):
    """The migration script is in scripts/migrate_artifact_kind.py."""

    def _load_module(self):
        # Import the script as a module by file path.
        import importlib.util
        script = Path(__file__).resolve().parents[2] / "scripts" / "migrate_artifact_kind.py"
        spec = importlib.util.spec_from_file_location("migrate_artifact_kind", script)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["migrate_artifact_kind"] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_artifact_kind_migration_script_dry_run(self) -> None:
        mod = self._load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "a.json"
            b = root / "b.json"
            c = root / "c.json"
            a.write_text(json.dumps(
                {"artifact_kind": "source_record", "artifact_id": "A"}
            ), encoding="utf-8")
            b.write_text(json.dumps(
                {"artifact_kind": "review_artifact",
                 "artifact_type": "review_artifact",
                 "artifact_id": "B"}
            ), encoding="utf-8")
            c.write_text("{this is not json", encoding="utf-8")

            # Capture stdout
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                counts = mod.run(root, dry_run=True)

            # No files written: hashes/contents unchanged
            self.assertEqual(json.loads(a.read_text()),
                             {"artifact_kind": "source_record",
                              "artifact_id": "A"})
            self.assertEqual(json.loads(b.read_text()),
                             {"artifact_kind": "review_artifact",
                              "artifact_type": "review_artifact",
                              "artifact_id": "B"})
            # Counts correct
            self.assertEqual(counts["scanned"], 3)
            self.assertEqual(counts["would_migrate"], 1)
            self.assertEqual(counts["already_migrated"], 1)
            self.assertEqual(counts["invalid_json"], 1)
            self.assertEqual(counts["migrated"], 0)

    def test_artifact_kind_migration_script_apply(self) -> None:
        mod = self._load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "a.json"
            a.write_text(json.dumps(
                {"artifact_kind": "source_record", "artifact_id": "A"}
            ), encoding="utf-8")

            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                counts = mod.run(root, dry_run=False)

            data = json.loads(a.read_text(encoding="utf-8"))
            self.assertEqual(data.get("artifact_kind"), "source_record")
            self.assertEqual(data.get("artifact_type"), "source_record")
            self.assertEqual(counts["migrated"], 1)


if __name__ == "__main__":
    unittest.main()
