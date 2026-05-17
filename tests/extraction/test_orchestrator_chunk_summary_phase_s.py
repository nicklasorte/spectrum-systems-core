"""Phase S.3: orchestrator chunk count rollup smoke tests.

Coverage:
  1. All chunks succeed -> stage_status="ok", summary shows blocked=0
     (``clm✓``).
  2. Minority blocked -> stage_status="partial", summary shows correct
     counts (``clm⚠``).
  3. Majority blocked -> stage_status="failed", summary shows correct
     counts (``clm✗``).
  4. orchestration_result artifact contains the rollup fields after a
     mocked partial run.
  5. The partial-stage smoke fixture passes (smoke_test_fixture.py
     entry point).

All mock-driven -- no real API calls, no real Anthropic SDK invocation.
``unittest.mock.patch`` is used to substitute the failure_artifact emit
helper so the on-disk write path is exercised against a tmp_path
(filesystem behaviour real, not faked).
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
import uuid
from unittest import mock

from spectrum_systems_core.cli import (
    _CLM_STATUS_SYMBOLS,
    _format_chunk_summary,
)
from spectrum_systems_core.extraction._chunk_counters import (
    STAGE_FAILED,
    STAGE_OK,
    STAGE_PARTIAL,
    ChunkCounters,
)
from spectrum_systems_core.extraction._failure_artifacts import (
    ARTIFACT_EMPTY_RESPONSE,
    emit_empty_response,
)

_SMOKE_FIXTURE_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "scripts"
    / "smoke_test_fixture.py"
)


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location(
        "smoke_test_fixture_phase_s", _SMOKE_FIXTURE_PATH,
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


class ChunkSummaryFormatTests(unittest.TestCase):
    def test_all_succeed_renders_ok(self) -> None:
        counters = ChunkCounters()
        counters.record_attempt(5)
        counters.record_success(5)
        result = {
            **counters.as_dict(),
            "stage_status": counters.stage_status(),
        }
        self.assertEqual(counters.stage_status(), STAGE_OK)
        summary = _format_chunk_summary(result)
        self.assertIn("blocked=0", summary)
        self.assertIn(_CLM_STATUS_SYMBOLS["ok"], summary)

    def test_minority_blocked_renders_partial(self) -> None:
        counters = ChunkCounters()
        counters.record_attempt(5)
        counters.record_success(3)
        counters.record_block("empty_response", n=2)
        result = {
            **counters.as_dict(),
            "stage_status": counters.stage_status(),
        }
        self.assertEqual(counters.stage_status(), STAGE_PARTIAL)
        summary = _format_chunk_summary(result)
        self.assertIn("attempted=5", summary)
        self.assertIn("succeeded=3", summary)
        self.assertIn("blocked=2", summary)
        self.assertIn(_CLM_STATUS_SYMBOLS["partial"], summary)

    def test_majority_blocked_renders_failed(self) -> None:
        counters = ChunkCounters()
        counters.record_attempt(5)
        counters.record_success(1)
        counters.record_block("parse_error", n=4)
        result = {
            **counters.as_dict(),
            "stage_status": counters.stage_status(),
        }
        self.assertEqual(counters.stage_status(), STAGE_FAILED)
        summary = _format_chunk_summary(result)
        self.assertIn("blocked=4", summary)
        self.assertIn(_CLM_STATUS_SYMBOLS["failed"], summary)


class FailureArtifactEmissionTests(unittest.TestCase):
    def test_emit_writes_artifact_to_disk_and_bumps_counter(self) -> None:
        """The failure_artifact helper writes a JSON file under
        ``<sdl_root>/failures/`` AND bumps the counter. Both must hold
        for the orchestrator's ``chunks_blocked`` rollup to be trustworthy.
        """
        counters = ChunkCounters()
        counters.record_attempt(2)
        with tempfile.TemporaryDirectory() as tmp:
            sdl_root = pathlib.Path(tmp)
            emit_empty_response(
                counters,
                chunk_id="chunk-1",
                source_id="smoke-test",
                component="story_extractor",
                detail="empty response",
                sdl_root=sdl_root,
            )
            files = list((sdl_root / "failures").glob("*.json"))
            self.assertEqual(len(files), 1)
            artifact = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(artifact["artifact_type"], ARTIFACT_EMPTY_RESPONSE)
            self.assertEqual(artifact["chunk_id"], "chunk-1")
        self.assertEqual(counters.chunks_blocked, 1)
        self.assertEqual(counters.block_reasons["empty_response"], 1)


class OrchestrationResultArtifactTests(unittest.TestCase):
    def test_orchestration_result_carries_counts_after_partial_run(self) -> None:
        """``_write_orchestration_result`` serialises counter values 1:1
        so a downstream reader (verification, eval, smoke harness) can
        trust the artifact as the on-disk mirror of in-memory state.
        """
        from spectrum_systems_core.extraction.typed_extraction_runner import (
            _write_orchestration_result,
        )
        counters = ChunkCounters()
        counters.record_attempt(5)
        counters.record_success(3)
        counters.record_block("empty_response", n=2)
        with tempfile.TemporaryDirectory() as tmp:
            sdl_root = pathlib.Path(tmp)
            run_id = "tex-" + uuid.uuid4().hex[:16]
            target = _write_orchestration_result(
                counters,
                run_id=run_id,
                source_id="smoke-test",
                sdl_root=sdl_root,
            )
            self.assertIsNotNone(target)
            assert target is not None
            artifact = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(artifact["chunks_attempted"], 5)
            self.assertEqual(artifact["chunks_succeeded"], 3)
            self.assertEqual(artifact["chunks_blocked"], 2)
            self.assertEqual(artifact["stage_status"], "partial")
            self.assertEqual(
                artifact["block_reasons"]["empty_response"], 2
            )


class PartialStageSmokeTests(unittest.TestCase):
    def test_partial_stage_smoke_script_passes(self) -> None:
        """S.3 unit 5: scripts/smoke_test_fixture.py --partial-stage exits 0."""
        module = _load_smoke_module()
        with mock.patch(
            "spectrum_systems_core.extraction._failure_artifacts._emit"
        ) as patched_emit:
            patched_emit.return_value = {}
            self.assertTrue(module.run_partial_stage_smoke_test())


if __name__ == "__main__":
    unittest.main()
