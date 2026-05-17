"""Tests for Phase G CLI commands and pipeline-independence (RT5-002)."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spectrum_systems_core import cli
from spectrum_systems_core.harness import (
    FailurePatternIndex,
    RunHistoryStore,
)

from ._fixtures import make_failure, write_synthesis_run


class CliRecordRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_LAKE_PATH"] = self._tmp.name
        self.store_root = Path(self._tmp.name) / "store"

    def tearDown(self) -> None:
        self._tmp.cleanup()
        os.environ.pop("DATA_LAKE_PATH", None)

    def test_record_run_e2e(self) -> None:
        run_id = write_synthesis_run(
            self.store_root, ungrounded_sections=1, grounded_sections=1
        )
        out = io.StringIO()
        rc = cli.record_run(
            run_id=run_id, repo_root=self.store_root, out_stream=out
        )
        self.assertEqual(rc, 0)
        index = json.loads(
            (self.store_root / "harness" / "runs" / "index.json").read_text()
        )
        self.assertEqual(len(index["runs"]), 1)
        # eval history written.
        self.assertTrue(
            (
                self.store_root / "harness" / "evals" / "report_draft_history.jsonl"
            ).is_file()
        )


class CliCompareRunsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_LAKE_PATH"] = self._tmp.name
        self.store_root = Path(self._tmp.name) / "store"

    def tearDown(self) -> None:
        self._tmp.cleanup()
        os.environ.pop("DATA_LAKE_PATH", None)

    def test_compare_runs_cli(self) -> None:
        a = write_synthesis_run(self.store_root, cost_usd=0.10)
        b = write_synthesis_run(self.store_root, cost_usd=0.05)
        for rid in (a, b):
            manifest = json.loads(
                (self.store_root / "synthesis" / rid / "run_manifest.json").read_text()
            )
            RunHistoryStore().record_run(manifest, str(self.store_root))
        out = io.StringIO()
        rc = cli.compare_runs(
            run_id_a=a, run_id_b=b, repo_root=self.store_root, out_stream=out
        )
        self.assertEqual(rc, 0)


class CliPromoteEvalCaseTests(unittest.TestCase):
    """Verify promote-eval-case is the ONLY path to contracts/evals/ writes."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_LAKE_PATH"] = self._tmp.name
        self.store_root = Path(self._tmp.name) / "store"

    def tearDown(self) -> None:
        self._tmp.cleanup()
        os.environ.pop("DATA_LAKE_PATH", None)

    def _seed_candidate(self) -> str:
        index = FailurePatternIndex()
        for i in range(4):
            index.ingest_failures(
                f"run-{i}",
                [make_failure(detail="section context missing inline citation evidence")],
                str(self.store_root),
            )
        # Reload pattern from disk so eval_candidate_id is None.
        patterns_path = (
            self.store_root / "harness" / "failures" / "patterns.jsonl"
        )
        patterns = [
            json.loads(line)
            for line in patterns_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        result = index.propose_eval_candidate(patterns[0], str(self.store_root))
        self.assertEqual(result["status"], "success")
        return result["candidate_id"]

    def test_promote_writes_to_contracts_evals(self) -> None:
        candidate_id = self._seed_candidate()
        out = io.StringIO()
        rc = cli.promote_eval_case(
            candidate_id=candidate_id,
            reviewer_id="reviewer-1",
            note="approved",
            auto_confirm=True,
            repo_root=self.store_root,
            out_stream=out,
        )
        self.assertEqual(rc, 0)
        # Was written.
        contracts_dir = self.store_root / "contracts" / "evals"
        files = list(contracts_dir.glob("*.json"))
        self.assertEqual(len(files), 1)
        registry = json.loads(files[0].read_text())
        self.assertEqual(len(registry), 1)
        self.assertTrue(registry[0]["name"].startswith("EVAL-PROMOTED-"))
        self.assertEqual(registry[0]["promoted_by"], "reviewer-1")
        # Candidate status flipped.
        cands_path = (
            self.store_root / "harness" / "failures" / "eval_candidates.jsonl"
        )
        cand = next(
            json.loads(line)
            for line in cands_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        self.assertEqual(cand["status"], "promoted")

    def test_promote_requires_confirmation(self) -> None:
        candidate_id = self._seed_candidate()
        out = io.StringIO()
        # Provide bad confirmation input.
        rc = cli.promote_eval_case(
            candidate_id=candidate_id,
            reviewer_id="reviewer-1",
            note="",
            auto_confirm=False,
            in_stream=io.StringIO("no\n"),
            repo_root=self.store_root,
            out_stream=out,
        )
        self.assertEqual(rc, 1)
        # contracts/evals/ NOT written.
        contracts_dir = self.store_root / "contracts" / "evals"
        if contracts_dir.is_dir():
            self.assertEqual(list(contracts_dir.glob("*.json")), [])


class PipelineIndependenceTests(unittest.TestCase):
    """RT5-002 — harness failures must NOT block the pipeline."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_record_synthesis_ignores_record_run_failure(self) -> None:
        run_id = write_synthesis_run(self.repo_root)
        with mock.patch.object(
            RunHistoryStore,
            "record_run",
            side_effect=RuntimeError("simulated failure"),
        ):
            # Must not raise.
            cli._record_synthesis_run_in_harness(run_id, self.repo_root, vault=None)


if __name__ == "__main__":
    unittest.main()
