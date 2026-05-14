"""Phase P3-A end-to-end integration tests for the runner.

Drives ``run_typed_extraction`` with stubbed LLM callers across both
modes (``two_stage``, ``single_pass``) and verifies the on-disk
contract:

  - two_stage emits the chunk_classifications artifact next to the
    meeting_extraction; single_pass does NOT.
  - The meeting_extraction carries extraction_mode + glossary_version
    + the new rate fields.
  - Source-turn orphan rate is non-zero when an extracted item cites
    a fake chunk_id.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spectrum_systems_core.extraction.typed_extraction_runner import (
    run_typed_extraction,
)


def _seed_source(store_root: Path, source_id: str, chunks: list) -> None:
    family = "meetings"
    processed = store_root / "processed" / family / source_id
    processed.mkdir(parents=True, exist_ok=True)
    sa_id = "11111111-1111-1111-1111-111111111111"
    (processed / "source_record.json").write_text(
        json.dumps({
            "artifact_type": "source_record",
            "artifact_id": sa_id,
            "payload": {
                "source_id": source_id, "source_family": family,
                "title": "T", "source_type": "txt",
                "raw_path": "x", "raw_hash": "sha256:" + "0" * 64,
                "text_unit_count": len(chunks),
                "processed_path": str(processed),
                "metadata": {},
            },
        }),
        encoding="utf-8",
    )
    stories = processed / "stories"
    stories.mkdir(parents=True, exist_ok=True)
    with (stories / "chunks.jsonl").open("w", encoding="utf-8") as fh:
        for c in chunks:
            fh.write(json.dumps(c) + "\n")


def _seed_glossary(data_lake: Path, version: int) -> None:
    gloss = data_lake / "store" / "artifacts" / "glossary"
    gloss.mkdir(parents=True, exist_ok=True)
    (gloss / f"spectrum_glossary_v{version}.json").write_text(
        json.dumps({
            "artifact_type": "spectrum_glossary",
            "schema_version": "1.0.0",
            "glossary_version": str(version),
            "term_count": 1,
            "content_hash": "sha256:" + ("a" * 64),
            "created_at": "1970-01-01T00:00:00+00:00",
            "terms": [{
                "term_id": "t-1",
                "term": "FSS",
                "abbreviation": "FSS",
                "definition": "Fixed Satellite Service",
                "short_definition": "Fixed Satellite Service",
                "authoritative_source": "ITU",
                "domain_scope": "spectrum",
                "related_term_ids": [],
            }],
        }, indent=2),
        encoding="utf-8",
    )


def _classifier_router(rules: dict):
    def caller(prompt: str) -> dict:
        for needle, cls in rules.items():
            if needle in prompt:
                return {"classification": cls, "confidence": 0.9}
        return {"classification": "off_topic", "confidence": 0.1}
    return caller


def _decision_caller(turn_id: str):
    def caller(_prompt: str) -> dict:
        return {"items": [{
            "decision_text": "Approve plan A.",
            "decision_type": "approved",
            "stakeholders": ["NTIA"],
            "rationale": "Stated rationale.",
            "decision_outcome": "approval",
            "source_turn_ids": [turn_id],
            "confidence": 0.9,
        }]}
    return caller


def _empty_caller(_prompt: str) -> dict:
    return {"items": []}


class P3AIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_lake = Path(self._tmp.name)
        self.store_root = self.data_lake / "store"
        # Disable AGENDA detection for these tests so the chunks don't
        # need agenda_item_id; with STRICT_CHUNK_METADATA off the
        # missing-agenda-id is a warn finding, not a halt.
        os.environ["AGENDA_DETECTION_ENABLED"] = "false"

    def tearDown(self) -> None:
        os.environ.pop("AGENDA_DETECTION_ENABLED", None)
        os.environ.pop("EXTRACTION_MODE", None)
        os.environ.pop("STRICT_CHUNK_METADATA", None)
        os.environ.pop("GLOSSARY_VERSION", None)
        self._tmp.cleanup()

    def _chunks(self) -> list:
        return [
            {
                "chunk_id": "c1",
                "speaker": "FCC.Smith",
                "agenda_item_id": "AI-001",
                "text": "We approve plan A.",
                "source_id": "m1",
            },
            {
                "chunk_id": "c2",
                "speaker": "NTIA.Jones",
                "agenda_item_id": "AI-002",
                "text": "Filler administrative discussion.",
                "source_id": "m1",
            },
        ]

    def _run(self, **overrides) -> dict:
        defaults = {
            "api_callers": {
                "classifier": _classifier_router({
                    "approve plan A": "decision",
                }),
                "decision": _decision_caller("c1"),
                "claim": _empty_caller,
                "action_item": _empty_caller,
            }
        }
        defaults.update(overrides)
        env = {"DATA_LAKE_PATH": str(self.data_lake)}
        with mock.patch.dict(os.environ, env, clear=False):
            return run_typed_extraction("m1", **defaults)

    def test_two_stage_default_writes_chunk_classifications(self) -> None:
        _seed_source(self.store_root, "m1", self._chunks())
        _seed_glossary(self.data_lake, 1)
        result = self._run()
        self.assertEqual(result["status"], "success", msg=result)
        self.assertEqual(result["extraction_mode"], "two_stage")
        self.assertEqual(result["glossary_version"], 1)
        self.assertEqual(result["extraction_path_breakdown"]["decision"], 1)
        self.assertEqual(result["extraction_path_breakdown"]["off_topic"], 1)
        self.assertAlmostEqual(result["off_topic_rate"], 0.5)
        # Chunk_classifications artifact written next to meeting_extraction.
        cc_path = result["chunk_classifications_path"]
        self.assertTrue(cc_path)
        self.assertTrue(Path(cc_path).is_file())
        cc = json.loads(Path(cc_path).read_text())
        self.assertEqual(cc["artifact_type"], "chunk_classifications")
        self.assertEqual(cc["chunk_count"], 2)
        # Population rates: stakeholders + rationale are populated.
        self.assertEqual(result["stakeholders_populated_rate"], 1.0)
        self.assertEqual(result["rationale_populated_rate"], 1.0)
        # Meeting_extraction on disk carries the new fields.
        me = json.loads(Path(result["path"]).read_text())
        self.assertEqual(me["extraction_mode"], "two_stage")
        self.assertEqual(me["glossary_version"], 1)
        self.assertEqual(me["off_topic_rate"], 0.5)

    def test_single_pass_skips_chunk_classifications_artifact(self) -> None:
        _seed_source(self.store_root, "m1", self._chunks())
        os.environ["EXTRACTION_MODE"] = "single_pass"
        result = self._run()
        self.assertEqual(result["status"], "success", msg=result)
        self.assertEqual(result["extraction_mode"], "single_pass")
        # The artifact path is empty in single_pass (the file is not written).
        self.assertEqual(result["chunk_classifications_path"], "")
        # No file on disk either.
        artifact_dir = (
            self.data_lake / "store" / "artifacts" / "extractions"
        )
        # Only the meeting_extraction should be present.
        files = [p.name for p in artifact_dir.glob("*.json")]
        self.assertTrue(any("meeting_extraction" in n for n in files))
        self.assertFalse(any("chunk_classifications" in n for n in files))
        # meeting_extraction stamps single_pass.
        me = json.loads(Path(result["path"]).read_text())
        self.assertEqual(me["extraction_mode"], "single_pass")

    def test_orphan_detection_when_decision_cites_fake_turn(self) -> None:
        _seed_source(self.store_root, "m1", self._chunks())
        result = self._run(
            api_callers={
                "classifier": _classifier_router({
                    "approve plan A": "decision",
                }),
                # Decision cites a chunk_id NOT in chunks.jsonl.
                "decision": _decision_caller("FAKE-TURN-999"),
                "claim": _empty_caller,
                "action_item": _empty_caller,
            },
        )
        self.assertEqual(result["status"], "success", msg=result)
        # Even though source_turn_validation flags the item, the orphan
        # rate must surface as >0 in the rollup.
        self.assertEqual(result["source_turn_orphan_rate"], 1.0)
        # And the finding lands in phase_w_findings.
        codes = [f["finding_code"] for f in result["phase_w_findings"]]
        self.assertIn("source_turn_orphan_detected", codes)

    def test_population_rate_warn_not_halt(self) -> None:
        _seed_source(self.store_root, "m1", self._chunks())
        # Decision returns ALL with empty stakeholders + null rationale.

        def _starved_decision(_prompt: str) -> dict:
            return {"items": [{
                "decision_text": "Bare decision.",
                "decision_type": "approved",
                "stakeholders": [],
                "rationale": None,
                "decision_outcome": "approval",
                "source_turn_ids": ["c1"],
                "confidence": 0.9,
            }]}

        result = self._run(
            api_callers={
                "classifier": _classifier_router({
                    "approve plan A": "decision",
                }),
                "decision": _starved_decision,
                "claim": _empty_caller,
                "action_item": _empty_caller,
            },
        )
        # Run still succeeds (fail-OPEN); the finding surfaces.
        self.assertEqual(result["status"], "success", msg=result)
        self.assertEqual(result["stakeholders_populated_rate"], 0.0)
        self.assertEqual(result["rationale_populated_rate"], 0.0)
        codes = [f["finding_code"] for f in result["phase_w_findings"]]
        self.assertIn("low_field_population_rate", codes)

    def test_strict_metadata_mode_halts_run(self) -> None:
        # Seed chunks WITHOUT speaker so the gate fails.
        bad_chunks = [
            {
                "chunk_id": "c1",
                # speaker absent
                "agenda_item_id": "AI-001",
                "text": "We approve plan A.",
                "source_id": "m1",
            },
        ]
        _seed_source(self.store_root, "m1", bad_chunks)
        os.environ["STRICT_CHUNK_METADATA"] = "true"
        result = self._run()
        self.assertEqual(result["status"], "failure")
        self.assertIn(
            "chunk_metadata_contract_violation",
            result["reason"],
        )


if __name__ == "__main__":
    unittest.main()
