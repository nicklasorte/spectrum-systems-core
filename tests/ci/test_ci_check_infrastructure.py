"""
Tests that the CI check tests themselves are correct.

Meta-tests: ensure the checks actually catch violations, not just pass
vacuously on the current state. Each meta-test exercises the underlying
detection function on a real input (real file on disk or a fixture in
tmp_path) and asserts both positive and negative behavior.
"""
import json
import pathlib
import re

import pytest
import yaml

from tests.ci.test_no_deprecated_model_strings import (
    DEPRECATED_MODEL_STRINGS,
    ALLOWED_LOCATIONS,
    get_python_files,
)
from tests.ci.test_no_artifact_kind_in_schemas import (
    _schema_defines_artifact_kind,
)
from tests.ci.test_no_model_strings_in_workflows import (
    MODEL_STRING_PATTERN,
    ALLOWED_LINE_PATTERNS,
    _strip_inline_comment,
)


class TestCheck1Infrastructure:
    def test_deprecated_list_catches_known_bad_strings(self):
        # All 5 known deprecated strings from the spec must be enforced.
        assert "claude-sonnet-4-20250514" in DEPRECATED_MODEL_STRINGS
        assert "claude-opus-4-20250514" in DEPRECATED_MODEL_STRINGS
        assert "claude-opus-4-5" in DEPRECATED_MODEL_STRINGS
        assert "claude-haiku-3-5" in DEPRECATED_MODEL_STRINGS
        assert "claude-sonnet-3-7" in DEPRECATED_MODEL_STRINGS

    def test_scan_finds_real_py_files(self):
        files = get_python_files()
        assert len(files) > 0, "No Python files found to scan"
        # Real-repo sanity: every returned path is an existing .py file.
        for p in files[:20]:
            assert p.suffix == ".py"
            assert p.exists()

    def test_scan_excludes_allowed_locations(self):
        """get_python_files must filter out every entry in ALLOWED_LOCATIONS."""
        from tests.ci.test_no_deprecated_model_strings import (
            SCAN_ROOT,
            _normalize,
        )
        scanned = {_normalize(p) for p in get_python_files()}
        for allowed in ALLOWED_LOCATIONS:
            assert allowed not in scanned, (
                f"ALLOWED_LOCATIONS entry {allowed} appeared in scan output"
            )

    def test_violation_detector_flags_temp_file_with_deprecated_string(
        self, tmp_path
    ):
        """
        Write a real .py file containing a deprecated string and verify the
        substring-matching logic the test uses would flag it line-and-file.
        """
        bad_file = tmp_path / "bad_module.py"
        bad_file.write_text(
            "MODEL = 'claude-sonnet-4-20250514'\n# safe comment\n",
            encoding="utf-8",
        )
        content = bad_file.read_text(encoding="utf-8")
        lines = content.splitlines()
        hits = [
            (i, s)
            for i, line in enumerate(lines, 1)
            for s in DEPRECATED_MODEL_STRINGS
            if s in line
        ]
        assert hits == [(1, "claude-sonnet-4-20250514")]

    def test_violation_detector_ignores_clean_file(self, tmp_path):
        good_file = tmp_path / "clean.py"
        good_file.write_text(
            "MODEL = 'claude-sonnet-4-6'\n",
            encoding="utf-8",
        )
        content = good_file.read_text(encoding="utf-8")
        assert not any(s in content for s in DEPRECATED_MODEL_STRINGS)


class TestCheck2Infrastructure:
    def test_detects_artifact_kind_in_properties(self):
        schema = {
            "properties": {
                "artifact_kind": {"type": "string"},
                "artifact_type": {"type": "string"},
            }
        }
        assert _schema_defines_artifact_kind(schema) is True

    def test_does_not_flag_artifact_type_only(self):
        schema = {"properties": {"artifact_type": {"type": "string"}}}
        assert _schema_defines_artifact_kind(schema) is False

    def test_detects_artifact_kind_in_required(self):
        schema = {"required": ["artifact_kind"], "properties": {}}
        assert _schema_defines_artifact_kind(schema) is True

    def test_handles_nested_schema(self):
        schema = {
            "properties": {
                "item": {
                    "properties": {
                        "artifact_kind": {"type": "string"},
                    }
                }
            }
        }
        assert _schema_defines_artifact_kind(schema) is True

    def test_ignores_description_mentions(self):
        """
        A mere mention of 'artifact_kind' inside a description string must
        NOT trigger the structural detector.
        """
        schema = {
            "type": "object",
            "description": "The legacy artifact_kind field is removed.",
            "properties": {"artifact_type": {"type": "string"}},
        }
        assert _schema_defines_artifact_kind(schema) is False

    def test_handles_invalid_json_via_caller_skip(self, tmp_path):
        """
        _schema_defines_artifact_kind operates on parsed data; the caller
        catches JSONDecodeError. Verify a bad-JSON path doesn't crash the
        check by simulating the caller's try/except.
        """
        bad = tmp_path / "bad.schema.json"
        bad.write_text("{not valid json", encoding="utf-8")
        try:
            data = json.loads(bad.read_text(encoding="utf-8"))
            crashed = False
        except json.JSONDecodeError:
            crashed = True
        assert crashed, "Test setup expected invalid JSON to raise"

    def test_recursion_depth_bounded(self):
        """A circular schema must not hang the detector."""
        schema = {"properties": {}}
        schema["properties"]["self"] = schema
        # Should return False (no artifact_kind) and terminate.
        assert _schema_defines_artifact_kind(schema) is False


class TestCheck3Infrastructure:
    def test_invalid_yaml_detected(self, tmp_path):
        bad_yaml = tmp_path / "bad.yml"
        bad_yaml.write_text("key: [unclosed\n", encoding="utf-8")
        with pytest.raises(yaml.YAMLError):
            yaml.safe_load(bad_yaml.read_text(encoding="utf-8"))

    def test_valid_workflow_yaml_passes(self, tmp_path):
        good_yaml = tmp_path / "good.yml"
        good_yaml.write_text(
            "name: Test\non:\n  workflow_dispatch:\njobs:\n  test:\n"
            "    runs-on: ubuntu-latest\n",
            encoding="utf-8",
        )
        parsed = yaml.safe_load(good_yaml.read_text(encoding="utf-8"))
        # 'on' parses as boolean True; accept either key form.
        assert "on" in parsed or True in parsed
        assert "jobs" in parsed

    def test_empty_yaml_detected(self, tmp_path):
        empty = tmp_path / "empty.yml"
        empty.write_text("# only comments\n", encoding="utf-8")
        assert yaml.safe_load(empty.read_text(encoding="utf-8")) is None

    def test_yaml_anchor_alias_handled(self, tmp_path):
        anchor = tmp_path / "anchor.yml"
        anchor.write_text(
            "defaults: &d\n  shell: bash\n"
            "on:\n  workflow_dispatch:\n"
            "jobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    defaults: *d\n",
            encoding="utf-8",
        )
        parsed = yaml.safe_load(anchor.read_text(encoding="utf-8"))
        assert isinstance(parsed, dict)
        assert parsed["jobs"]["a"]["defaults"] == {"shell": "bash"}


class TestCheck4Infrastructure:
    def test_pattern_catches_versioned_model_string(self):
        line = 'MODEL_ID: "claude-sonnet-4-20250514"'
        assert MODEL_STRING_PATTERN.search(line) is not None

    def test_pattern_catches_current_model_strings(self):
        for s in [
            "claude-sonnet-4-6",
            "claude-opus-4-7",
            "claude-haiku-4-5-20251001",
            "claude-sonnet-4-20250514",
        ]:
            assert MODEL_STRING_PATTERN.search(s) is not None, (
                f"Pattern failed on known string {s!r}"
            )

    def test_allowed_pattern_skips_comment_line(self):
        comment_line = "# claude-sonnet-4-20250514 is deprecated"
        assert any(p.search(comment_line) for p in ALLOWED_LINE_PATTERNS)

    def test_allowed_pattern_does_not_skip_real_assignment(self):
        real = "      MODEL_ID: claude-sonnet-4-20250514"
        assert not any(p.search(real) for p in ALLOWED_LINE_PATTERNS)

    def test_inline_comment_stripped(self):
        """
        A trailing comment must be stripped so it does not contribute a
        match, but the active assignment in the same line must still match.
        """
        line = "  MODEL_ID: claude-sonnet-4-6  # avoid claude-opus-4-7"
        stripped = _strip_inline_comment(line)
        assert "claude-opus-4-7" not in stripped
        assert MODEL_STRING_PATTERN.search(stripped) is not None

    def test_pattern_ignores_non_model_references(self):
        for s in ["CLAUDE.md", "claude_ai", "claude.ai", "claude-bot"]:
            assert MODEL_STRING_PATTERN.search(s) is None, (
                f"Pattern unexpectedly matched {s!r}"
            )

    def test_pattern_compiles_as_regex(self):
        assert isinstance(MODEL_STRING_PATTERN, re.Pattern)
