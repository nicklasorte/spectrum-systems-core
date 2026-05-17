"""Phase G — Red Team #5 audits.

CHECK-RT5-001: no autonomous mutation of contracts/evals/.
CHECK-RT5-002: harness failures NEVER block synthesis pipeline.
CHECK-RT5-004: harness markdown is never read as pipeline input.
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spectrum_systems_core import cli
from spectrum_systems_core.harness import (
    EntropyAuditor,
    FailurePatternIndex,
    RunHistoryStore,
)

from ._fixtures import make_failure, write_synthesis_run

HARNESS_SRC = Path(cli.__file__).parent / "harness"
INGESTION_SRC = Path(cli.__file__).parent / "ingestion"


class RT5_001_NoAutonomousMutationTests(unittest.TestCase):
    """No code path writes to contracts/evals/ except promote-eval-case."""

    def _scan_python_files(self, root: Path):
        return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]

    def test_only_promote_eval_case_writes_contracts_evals(self) -> None:
        # Find every reference to contracts/evals/ writes.
        write_pattern = re.compile(
            r"contracts.*evals.*write_text|"
            r"contracts.*evals.*\.write\(|"
            r"contracts.*evals.*\.json\)\s*\.write_text",
            re.MULTILINE,
        )
        offenders = []
        # Allowed write site: cli.promote_eval_case.
        for src_file in self._scan_python_files(HARNESS_SRC):
            text = src_file.read_text(encoding="utf-8")
            if write_pattern.search(text):
                offenders.append(str(src_file))
        self.assertEqual(
            offenders,
            [],
            "harness modules must not write to contracts/evals/",
        )

    def test_no_harness_code_deletes_protected_directories(self) -> None:
        protected = [
            "contracts/",
            "paper/",
            "stories/",
            "knowledge/",
            "agency/",
            "synthesis/",
        ]
        for src_file in self._scan_python_files(HARNESS_SRC):
            text = src_file.read_text(encoding="utf-8")
            for keyword in ("rmtree", "os.remove", "os.rmdir"):
                for protected_dir in protected:
                    if keyword in text and protected_dir in text:
                        # only flag if both appear within proximity (heuristic).
                        self.fail(
                            f"{src_file} appears to delete files in {protected_dir!r}"
                        )


class RT5_002_PipelineIndependenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_record_run_failure_is_isolated(self) -> None:
        run_id = write_synthesis_run(self.repo_root)
        with mock.patch.object(
            RunHistoryStore,
            "record_run",
            side_effect=RuntimeError("simulated harness crash"),
        ):
            # Helper must not propagate the exception.
            cli._record_synthesis_run_in_harness(run_id, self.repo_root, vault=None)

    def test_index_corruption_does_not_crash_recent_runs(self) -> None:
        # Corrupt the index file — store must degrade gracefully.
        idx = self.repo_root / "harness" / "runs" / "index.json"
        idx.parent.mkdir(parents=True, exist_ok=True)
        idx.write_text("{not json", encoding="utf-8")
        # Should not raise.
        recent = RunHistoryStore().get_recent_runs(str(self.repo_root))
        self.assertEqual(recent, [])

    def test_missing_run_manifest_returns_failure_not_crash(self) -> None:
        result = RunHistoryStore().record_run({}, str(self.repo_root))
        # Missing required fields should produce a non-success failure,
        # not propagate an exception.
        self.assertEqual(result["status"], "failure")
        self.assertEqual(result["entry_id"], "")


class RT5_003_DebuggabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_failure_pattern_reason_codes_human_readable(self) -> None:
        index = FailurePatternIndex()
        for i in range(2):
            index.ingest_failures(
                f"run-{i}",
                [
                    make_failure(
                        reason_code="ungrounded_section",
                        detail=(
                            "section context_a missing inline citation evidence"
                        ),
                    )
                ],
                str(self.repo_root),
            )
        patterns_path = (
            self.repo_root / "harness" / "failures" / "patterns.jsonl"
        )
        pattern = json.loads(patterns_path.read_text().splitlines()[0])
        self.assertNotIn(" ", pattern["reason_code"])
        self.assertGreater(len(pattern["reason_code"]), 4)

    def test_entropy_flagged_items_each_have_action(self) -> None:
        # Create a flagged pattern.
        index = FailurePatternIndex()
        for i in range(4):
            index.ingest_failures(
                f"run-{i}",
                [make_failure(detail="section context missing inline citation evidence")],
                str(self.repo_root),
            )
        result = EntropyAuditor().run_audit(str(self.repo_root))
        for item in result["report"]["flagged_items"]:
            self.assertGreater(len(item["recommended_action"]), 5)
            self.assertIn(item["severity"], ("high", "medium", "low"))


class RT5_004_MarkdownAuthorityLeakTests(unittest.TestCase):
    """Harness Markdown projections must never be read as pipeline inputs."""

    def test_no_harness_code_reads_md_files(self) -> None:
        # Detect actual READS of .md files (read_text, open).
        # WRITES (write_text, target paths) are allowed.
        read_patterns = [
            re.compile(r'\.md["\']\s*\)\s*\.read_text'),
            re.compile(r'open\([^)]*\.md["\']'),
            re.compile(r'read_text\([^)]*\.md'),
        ]
        offenders = []
        for src_file in HARNESS_SRC.rglob("*.py"):
            if "__pycache__" in src_file.parts:
                continue
            text = src_file.read_text(encoding="utf-8")
            for pattern in read_patterns:
                if pattern.search(text):
                    offenders.append(str(src_file))
                    break
        self.assertEqual(offenders, [])

    def test_harness_md_files_have_view_only_banner(self) -> None:
        repo_root = Path(tempfile.mkdtemp())
        try:
            run_id = write_synthesis_run(repo_root)
            manifest = json.loads(
                (repo_root / "synthesis" / run_id / "run_manifest.json").read_text()
            )
            RunHistoryStore().record_run(manifest, str(repo_root))
            path = RunHistoryStore().write_run_history_projection(
                str(repo_root)
            )
            body = Path(path).read_text(encoding="utf-8")
            self.assertIn("VIEW ONLY", body)
        finally:
            import shutil
            shutil.rmtree(repo_root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
