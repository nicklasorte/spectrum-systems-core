"""Tests for EvalAligner + meeting_extraction integration (Phase M3 C5).

Verifies:
- aligner accepts items derived from a meeting_extraction artifact,
- artifact_source field reflects which input shape was used,
- eval_input_warning fires when zero decisions AND zero claims,
- backward-compat: story-shaped items still work.
"""
from __future__ import annotations

import unittest

from spectrum_systems_core.evals.m4.aligner import EvalAligner


_MEETING_EXTRACTION_WITH_ITEMS = {
    "meeting_extraction_id": "11111111-1111-1111-1111-111111111111",
    "source_artifact_id": "22222222-2222-2222-2222-222222222222",
    "artifact_type": "meeting_extraction",
    "schema_version": "1.0.0",
    "created_at": "1970-01-01T00:00:00+00:00",
    "decisions": [
        {
            "decision_text": "FSS protection criterion -10.5 dB approved",
            "decision_type": "approved",
            "stakeholders": ["NTIA"],
            "rationale": None,
            "source_turn_ids": ["t1"],
            "source_turn_validation": "verified",
        }
    ],
    "claims": [
        {
            "claim_text": "ITU two-point criterion uses -6 dB at 0.03%",
            "claim_type": "technical",
            "speaker": "LaSorte",
            "source_turn_ids": ["t2"],
            "source_turn_validation": "verified",
        }
    ],
    "action_items": [
        {
            "action": "Update working paper section 3.2",
            "owner": "LaSorte",
            "due": None,
            "source_turn_ids": ["t3"],
            "source_turn_validation": "verified",
        }
    ],
    "total_chunks_classified": 3,
    "off_topic_count": 0,
    "regulatory_verb_fallback_count": 0,
    "routing_quality_warning": False,
    "requires_human_dedup_count": 0,
    "extraction_run_id": "run-1",
    "provenance": {"produced_by": "ExtractionMerger"},
}


class EvalAlignerMeetingExtractionTests(unittest.TestCase):
    def test_aligner_uses_meeting_extraction_when_present(self) -> None:
        items = EvalAligner.items_from_meeting_extraction(
            _MEETING_EXTRACTION_WITH_ITEMS
        )
        self.assertEqual(len(items), 3)
        kinds = {i["kind"] for i in items}
        self.assertEqual(kinds, {"decision", "claim", "action_item"})

        minutes_text = (
            "DECISION: FSS protection criterion -10.5 dB approved\n"
            "ACTION: Update working paper section 3.2\n"
        )
        result = EvalAligner().align(
            extracted_items=items,
            minutes_text=minutes_text,
            source_id="s1",
            minutes_artifact_id="m1",
            artifact_source="meeting_extraction",
        )
        # Content assertion: the actual artifact_source field is present
        # and equal to "meeting_extraction".
        self.assertEqual(result["artifact_source"], "meeting_extraction")
        self.assertFalse(result["eval_input_warning"])

    def test_aligner_falls_back_to_story_artifacts(self) -> None:
        # Caller provides story-shaped items; artifact_source defaults
        # to "story_artifacts".
        items = [{"text": "Some story-shaped extracted item.", "id": "x"}]
        result = EvalAligner().align(
            extracted_items=items,
            minutes_text="ITEM: Some story-shaped extracted item.\n",
            source_id="s1",
            minutes_artifact_id="m1",
        )
        # Content assertion: artifact_source field in the actual artifact
        # equals "story_artifacts" (not just no exception).
        self.assertEqual(result["artifact_source"], "story_artifacts")
        self.assertFalse(result["eval_input_warning"])

    def test_eval_input_warning_on_zero_typed_artifacts(self) -> None:
        # A meeting_extraction with empty decisions and claims signals
        # an extraction problem -- the aligner records the warning.
        empty = dict(_MEETING_EXTRACTION_WITH_ITEMS)
        empty["decisions"] = []
        empty["claims"] = []
        # action_items alone is not enough to suppress the warning.
        empty["action_items"] = [{"action": "x", "owner": "y", "due": None,
                                   "source_turn_ids": ["t"],
                                   "source_turn_validation": "verified"}]
        self.assertTrue(EvalAligner.has_zero_typed_inputs(empty))
        items = EvalAligner.items_from_meeting_extraction(empty)
        result = EvalAligner().align(
            extracted_items=items,
            minutes_text="ITEM: x",
            source_id="s",
            minutes_artifact_id="m",
            artifact_source="meeting_extraction",
            eval_input_warning=True,
        )
        # Continues without raising; records the warning.
        self.assertTrue(result["eval_input_warning"])
        self.assertEqual(result["artifact_source"], "meeting_extraction")

    def test_has_zero_typed_inputs_false_when_claims_present(self) -> None:
        # If at least one claim exists, the warning is not justified.
        m = dict(_MEETING_EXTRACTION_WITH_ITEMS)
        m["decisions"] = []
        self.assertFalse(EvalAligner.has_zero_typed_inputs(m))

    def test_align_from_meeting_extraction_auto_sets_warning_when_zero(self) -> None:
        # Production entry point: even if the caller forgets to set the
        # eval_input_warning flag, align_from_meeting_extraction auto-derives
        # it from has_zero_typed_inputs. This locks in the Sev-1 fix from
        # Red Team Pass 1.
        empty = dict(_MEETING_EXTRACTION_WITH_ITEMS)
        empty["decisions"] = []
        empty["claims"] = []
        empty["action_items"] = []
        result = EvalAligner().align_from_meeting_extraction(
            meeting_extraction=empty,
            minutes_text="ITEM: anything",
            source_id="s",
            minutes_artifact_id="m",
        )
        self.assertEqual(result["artifact_source"], "meeting_extraction")
        self.assertTrue(result["eval_input_warning"])

    def test_align_from_meeting_extraction_no_warning_when_items_present(self) -> None:
        result = EvalAligner().align_from_meeting_extraction(
            meeting_extraction=_MEETING_EXTRACTION_WITH_ITEMS,
            minutes_text="ITEM: FSS protection criterion -10.5 dB approved",
            source_id="s",
            minutes_artifact_id="m",
        )
        self.assertEqual(result["artifact_source"], "meeting_extraction")
        self.assertFalse(result["eval_input_warning"])


if __name__ == "__main__":
    unittest.main()
