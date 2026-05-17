"""Phase W (integration wiring) smoke test.

Verifies that the Phase T/V modules built in PRs #68 and #69 are
actually CALLED by the live extraction runner -- not merely importable.
A unit test passing in isolation does not prove the runner invokes
the module on the per-chunk hot path; this integration test proves it.

Test strategy:
- Seed a synthetic data lake under a TemporaryDirectory.
- Write a 5-chunk transcript with chunk_position pre-assigned (we test
  the chunker pipeline separately; here we want the runner to read the
  positions verbatim).
- Write a minimal versioned glossary containing the FSS term so we can
  assert per-chunk injection on a known token.
- Mock every Haiku api caller so no live API calls fire.
- Run ``run_typed_extraction`` end-to-end.
- Assert the wiring artifacts (glossary_terms_injected, chunk_position,
  prompt block ordering, generalization counter, orchestration summary)
  appear in the runner's return value and on disk.

All Phase T/V modules are individually unit-tested already; the value
this test adds is proving the runner CALLS them.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

SOURCE_ID = "wired-smoke-transcript-2026-05-12"
SOURCE_ARTIFACT_ID = "44444444-4444-4444-4444-444444444444"
CHUNK_IDS = [
    "11111111-1111-1111-1111-111111111111",
    "22222222-2222-2222-2222-222222222222",
    "33333333-3333-3333-3333-333333333333",
    "44444444-4444-4444-4444-444444444444",
    "55555555-5555-5555-5555-555555555555",
]


def _seed_source(
    store_root: Path,
    *,
    chunks: list[dict],
    family: str = "meetings",
) -> Path:
    """Write a minimal source_record + chunks.jsonl under store_root."""
    processed = store_root / "processed" / family / SOURCE_ID
    processed.mkdir(parents=True, exist_ok=True)
    (processed / "source_record.json").write_text(
        json.dumps({
            "artifact_kind": "source_record",
            "artifact_type": "source_record",
            "artifact_id": SOURCE_ARTIFACT_ID,
            "payload": {"source_id": SOURCE_ID, "source_family": family},
        }),
        encoding="utf-8",
    )
    stories = processed / "stories"
    stories.mkdir(parents=True, exist_ok=True)
    with (stories / "chunks.jsonl").open("w", encoding="utf-8") as fh:
        for c in chunks:
            fh.write(json.dumps(c) + "\n")
    return processed


def _seed_versioned_glossary(sdl_root: Path) -> Path:
    """Write a minimal spectrum_glossary_v1 artifact containing FSS."""
    target_dir = sdl_root / "glossary"
    target_dir.mkdir(parents=True, exist_ok=True)
    artifact = {
        "artifact_type": "spectrum_glossary",
        "schema_version": "1.0.0",
        "glossary_version": "test-1",
        "term_count": 1,
        "content_hash": "sha256:0" + "0" * 63,
        "created_at": "1970-01-01T00:00:00+00:00",
        "terms": [
            {
                "term_id": "fss",
                "term": "Fixed Satellite Service",
                "abbreviation": "FSS",
                "definition": "A radiocommunication service between earth stations.",
                "short_definition": "Satellite service between fixed earth stations.",
                "authoritative_source": "ITU RR No. 1.21",
                "domain_scope": "spectrum_allocation",
                "related_term_ids": [],
            },
        ],
    }
    (target_dir / "spectrum_glossary_v1.json").write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target_dir


def _build_synthetic_chunks(
    *,
    overgeneralize: bool = False,
    fss_in_chunk0: bool = True,
) -> list[dict]:
    """Five chunks: 0 opening, 1-3 middle, 4 closing.

    Chunk 0 carries 'FSS' so the glossary injector matches. Chunk 4
    carries '7 GHz' so the generalization checker has a band reference
    to anchor on. When ``overgeneralize`` is true, the extracted
    decision text from chunk 4 says 'all spectrum bands' so the
    detector fires.
    """
    text_chunks = [
        "We agree FSS coordination is mandatory under ITU rules.",
        "The middle of the discussion covered technical bands.",
        "Coordination meetings will run weekly per the working group.",
        "Action items will be tracked in the project register.",
        "We approve operations in the 7 GHz band per FCC guidance.",
    ]
    if not fss_in_chunk0:
        text_chunks[0] = (
            "We agree coordination is mandatory under regulatory rules."
        )

    positions = ["opening", "middle", "middle", "middle", "closing"]
    chunks: list[dict] = []
    for idx, (cid, text, pos) in enumerate(zip(CHUNK_IDS, text_chunks, positions)):
        chunks.append({
            "chunk_id": cid,
            "source_id": SOURCE_ID,
            "source_family": "meetings",
            "chunk_index": idx,
            "unit_ids": [str(uuid.uuid4())],
            "text": text,
            "text_hash": "sha256:" + ("a" * 64),
            "unit_count": 1,
            "overlap_unit_id": None,
            "page_numbers": [],
            "char_count": len(text),
            "chunk_position": pos,
        })
    return chunks


def _classifier_caller_all_decision():
    """Batch classifier mock: classify every chunk as `decision`."""
    def caller(prompt: str) -> dict:
        lines: list[str] = []
        for m in re.finditer(
            r"Chunk \d+ \(chunk_id: ([^)]+)\)", prompt,
        ):
            cid = m.group(1).strip()
            lines.append(f"chunk_id: {cid} | classification: decision")
        return {"text": "\n".join(lines)}
    return caller


def _decision_caller_factory(decision_text: str, turn_id: str):
    """Decision extractor mock: returns one minimal valid decision."""
    def caller(_prompt: str) -> dict:
        return {
            "items": [
                {
                    "decision_text": decision_text,
                    "decision_type": "approved",
                    "stakeholders": [],
                    "rationale": None,
                    "source_turn_ids": [turn_id],
                    "confidence": 0.9,
                }
            ]
        }
    return caller


def _empty_caller(_prompt: str) -> dict:
    return {"items": []}


class BuildExtractionPromptOrderTests(unittest.TestCase):
    """W.2 / W.3 unit-style tests on the public prompt builder.

    These do not run the runner; they assert the canonical block
    ordering used by ``build_extraction_prompt``. The W.6 attack
    surface is "the runner builds the prompt internally and we cannot
    inspect it" -- this is mitigated by routing block composition
    through the public helper.
    """

    def test_attention_block_present_when_middle(self) -> None:
        from spectrum_systems_core.extraction.typed_extraction_runner import (
            build_extraction_prompt,
        )

        middle_prompt = build_extraction_prompt(
            chunk_text="test",
            extraction_type="decision",
            attention_block="ATTENTION DIRECTION\n===\nfocus here",
        )
        self.assertIn("ATTENTION DIRECTION", middle_prompt)

    def test_attention_block_absent_when_opening(self) -> None:
        from spectrum_systems_core.extraction.typed_extraction_runner import (
            build_extraction_prompt,
        )

        opening_prompt = build_extraction_prompt(
            chunk_text="test",
            extraction_type="decision",
            attention_block="",
        )
        self.assertNotIn("ATTENTION DIRECTION", opening_prompt)

    def test_canonical_block_order(self) -> None:
        from spectrum_systems_core.extraction.typed_extraction_runner import (
            build_extraction_prompt,
        )

        prompt = build_extraction_prompt(
            chunk_text="THE_CHUNK_TEXT_MARKER",
            extraction_type="decision",
            terminology_block="TERMINOLOGY FOR THIS SECTION\nfss",
            attention_block="ATTENTION DIRECTION\nfocus",
            few_shot_block="FEW-SHOT EXAMPLES\nex1",
            glossary_block="LEGACY_GLOSSARY_MARKER",
        )
        # Canonical order: role -> taxonomy -> terminology -> attention
        # -> few-shot -> legacy glossary -> chunk text.
        markers = [
            "Extract DECISION",
            "DECISION CLASSIFICATION RULES",  # taxonomy block header
            "TERMINOLOGY FOR THIS SECTION",
            "ATTENTION DIRECTION",
            "FEW-SHOT EXAMPLES",
            "LEGACY_GLOSSARY_MARKER",
            "THE_CHUNK_TEXT_MARKER",
        ]
        positions = [prompt.find(m) for m in markers]
        for i, m in enumerate(markers):
            self.assertGreaterEqual(
                positions[i], 0,
                msg=f"marker {m!r} missing from prompt",
            )
        self.assertEqual(
            positions, sorted(positions),
            msg=(
                "block order violated: positions="
                f"{dict(zip(markers, positions))!r}"
            ),
        )


class ExtractionRunnerWiringIntegrationTests(unittest.TestCase):
    """W.6 integration test: run the runner end-to-end with mocks and
    assert that every Phase T/V module wired in Phase W produced
    output on the run.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_lake = Path(self._tmp.name)
        self.store_root = self.data_lake / "store"
        self.sdl_root = self.store_root / "artifacts"
        self.sdl_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(
        self,
        *,
        overgeneralize: bool = False,
        fss_in_chunk0: bool = True,
        seed_glossary: bool = True,
    ) -> dict:
        from spectrum_systems_core.extraction.typed_extraction_runner import (
            run_typed_extraction,
        )

        chunks = _build_synthetic_chunks(
            overgeneralize=overgeneralize,
            fss_in_chunk0=fss_in_chunk0,
        )
        _seed_source(self.store_root, chunks=chunks)
        if seed_glossary:
            _seed_versioned_glossary(self.sdl_root)

        # The decision returned by the mock anchors on the LAST chunk
        # which carries the '7 GHz' band reference; overgeneralization
        # only fires when both the source has a band ref AND the
        # extracted text contains an over-broad marker.
        decision_text = (
            "Approve operations across all spectrum bands."
            if overgeneralize
            else "Approve operations in the 7 GHz band."
        )

        env = {
            "DATA_LAKE_PATH": str(self.data_lake),
            "SDL_ROOT": str(self.sdl_root),
            "GENERALIZATION_CHECK_ENABLED": "true",
            "POSITION_AWARE_PROMPTING_ENABLED": "true",
            # Phase V binding tuple stays off (default) so the count
            # is 0 in the asserts below.
            "BINDING_TUPLE_ENABLED": "false",
            # Phase V verification flag stays off so the v2.0.0
            # verification path does not require live anthropic.
            "VERIFICATION_ENABLED": "false",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            result = run_typed_extraction(
                SOURCE_ID,
                data_lake=str(self.data_lake),
                api_callers={
                    "classifier": _classifier_caller_all_decision(),
                    "decision": _decision_caller_factory(
                        decision_text, CHUNK_IDS[-1],
                    ),
                    "claim": _empty_caller,
                    "action_item": _empty_caller,
                },
            )
        return result

    def test_glossary_injection_records_present_on_every_chunk(self) -> None:
        """W.1 + W.6 assertion 1: chunk 0 carries 'FSS' so the
        glossary injector matched and recorded the term_id."""
        result = self._run()
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        records = result.get("chunk_extraction_records") or []
        self.assertEqual(len(records), 5)
        # Chunk 0 mentions FSS -> term_id 'fss' must be present.
        chunk0 = records[0]
        self.assertIn("glossary_terms_injected", chunk0)
        self.assertIn("fss", chunk0["glossary_terms_injected"])
        # Chunks that do not mention any glossary term get [] (a list,
        # never None) so downstream consumers do not have to special-
        # case the unloaded path.
        chunk_3 = records[3]
        self.assertEqual(chunk_3["glossary_terms_injected"], [])

    def test_chunk_position_recorded_on_every_record(self) -> None:
        """W.2 + W.6 assertion 2: chunk_position carried through to
        the per-chunk records."""
        result = self._run()
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        positions = [
            r["chunk_position"] for r in result["chunk_extraction_records"]
        ]
        self.assertEqual(positions, [
            "opening", "middle", "middle", "middle", "closing",
        ])
        for pos in positions:
            self.assertIn(pos, ("opening", "middle", "closing"))

    def test_few_shot_no_verified_examples_finding_fires(self) -> None:
        """W.3 + W.6 assertion 3: the V.3 loader emitted a finding
        because the synthetic data lake has zero verified examples,
        and the runner propagated it to phase_w_findings."""
        result = self._run()
        codes = [
            f["finding_code"] for f in result.get("phase_w_findings", [])
        ]
        self.assertIn("few_shot_artifact_missing", codes)
        # The matching finding's severity must be info (default), not
        # halt (FEW_SHOT_REQUIRED is off in the test env).
        matching = [
            f for f in result["phase_w_findings"]
            if f["finding_code"] == "few_shot_artifact_missing"
        ]
        self.assertTrue(matching)
        self.assertEqual(matching[0]["severity"], "info")

    def test_overgeneralization_zero_when_extraction_is_specific(self) -> None:
        """W.4: synthetic input has no overgeneralization marker in
        the extracted text, so the counter must be 0 -- AND the
        checker must still have been called (the value is present)."""
        result = self._run(overgeneralize=False)
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        self.assertIn("scope_overgeneralization_count", result)
        self.assertEqual(result["scope_overgeneralization_count"], 0)

    def test_overgeneralization_fires_on_overbroad_decision(self) -> None:
        """W.4: when the mock extraction emits 'all spectrum bands'
        and the source chunk carries '7 GHz', the detector fires."""
        result = self._run(overgeneralize=True)
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        self.assertGreaterEqual(result["scope_overgeneralization_count"], 1)
        codes = [
            f["finding_code"] for f in result.get("phase_w_findings", [])
        ]
        self.assertIn("scope_overgeneralization", codes)

    def test_orchestration_counters_present_in_artifact(self) -> None:
        """W.5: glossary_injection_summary, binding_tuple_call_count,
        and scope_overgeneralization_count are persisted into the
        orchestration_result artifact on disk."""
        result = self._run()
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        orch_path = result["orchestration_result_path"]
        self.assertTrue(orch_path)
        doc = json.loads(Path(orch_path).read_text(encoding="utf-8"))
        self.assertIn("glossary_injection_summary", doc)
        summary = doc["glossary_injection_summary"]
        self.assertGreaterEqual(summary["total_term_injections"], 1)
        self.assertGreaterEqual(summary["chunks_with_matches"], 1)
        self.assertIn("most_injected_terms", summary)
        self.assertIn("fss", summary["most_injected_terms"])
        # Binding tuple flag is off -> count is 0.
        self.assertEqual(doc["binding_tuple_call_count"], 0)
        # No overgeneralization on this synthetic run.
        self.assertEqual(doc["scope_overgeneralization_count"], 0)

    def test_missing_versioned_glossary_falls_back_silently(self) -> None:
        """W.1 backward-compat: when the versioned glossary artifact
        is absent, every chunk's glossary_terms_injected is [] (a
        list, never None) and the runner still succeeds."""
        result = self._run(seed_glossary=False)
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        records = result.get("chunk_extraction_records") or []
        for r in records:
            self.assertEqual(r["glossary_terms_injected"], [])
        self.assertEqual(
            result["glossary_injection_summary"]["total_term_injections"], 0,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
