"""Tests for MitigationSuggester (FINDING-E-006)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.agency.mitigation_eval import MitigationEval
from spectrum_systems_core.agency.mitigation_suggester import MitigationSuggester

from ._fixtures import make_mitigation, make_prediction, read_jsonl


def _ok_mitigation_response(mitigation_type: str = "revise_claim") -> str:
    if mitigation_type == "add_evidence":
        return json.dumps({
            "mitigation_text": "Add citations to interference modelling work.",
            "mitigation_type": "add_evidence",
            "evidence_search_terms": ["adjacent channel", "interference"],
            "expected_effectiveness": "high",
            "rationale": "Provides supporting evidence for the disputed claim.",
        })
    return json.dumps({
        "mitigation_text": "Reword the claim to acknowledge alternative interpretations.",
        "mitigation_type": "revise_claim",
        "evidence_search_terms": [],
        "expected_effectiveness": "medium",
        "rationale": "Softens the claim to address agency concern.",
    })


def _bad_add_evidence_no_terms() -> str:
    return json.dumps({
        "mitigation_text": "Add evidence somewhere — but unspecified terms.",
        "mitigation_type": "add_evidence",
        "evidence_search_terms": [],
        "expected_effectiveness": "low",
        "rationale": "Unhelpful suggestion that should be blocked.",
    })


def _write_predictions(repo_root: Path, family: str, source_id: str, predictions) -> Path:
    target = repo_root / "processed" / family / source_id / "paper" / "objections"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "predictions.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for p in predictions:
            fh.write(json.dumps(p, sort_keys=True) + "\n")
    return path


class MitigationSuggesterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.paper_id = "paper-A"
        self.family = "working_papers"
        self.prediction = make_prediction(
            agency_slug="fcc",
            evidence_basis=["pos-1"],
            confidence="medium",
        )
        self.prediction["no_evidence_basis_flag"] = False
        _write_predictions(
            self.repo_root, self.family, self.paper_id, [self.prediction]
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_add_evidence_empty_search_terms_blocked(self) -> None:
        suggester = MitigationSuggester(
            api_caller=lambda _p: _bad_add_evidence_no_terms()
        )
        result = suggester.suggest_for_predictions(
            self.paper_id, str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["mitigations"], 0)
        self.assertEqual(result["blocked"], 1)
        # No entry written.
        path = (
            self.repo_root
            / "processed"
            / self.family
            / self.paper_id
            / "paper"
            / "objections"
            / "mitigations.jsonl"
        )
        self.assertFalse(path.is_file())

    def test_valid_mitigation_written(self) -> None:
        suggester = MitigationSuggester(
            api_caller=lambda _p: _ok_mitigation_response()
        )
        result = suggester.suggest_for_predictions(
            self.paper_id, str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["mitigations"], 1)
        records = read_jsonl(
            self.repo_root
            / "processed"
            / self.family
            / self.paper_id
            / "paper"
            / "objections"
            / "mitigations.jsonl"
        )
        self.assertEqual(len(records), 1)

    def test_api_failure_skips_not_crashes(self) -> None:
        def _raises(_prompt: str) -> str:
            raise RuntimeError("boom")

        suggester = MitigationSuggester(api_caller=_raises)
        result = suggester.suggest_for_predictions(
            self.paper_id, str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["mitigations"], 0)

    def test_orphan_prediction_id_blocked(self) -> None:
        # Build a mitigation that references a non-existent prediction.
        orphan = make_mitigation(
            prediction_id="00000000-0000-0000-0000-000000000000",
            agency_slug="fcc",
        )
        eval_result = MitigationEval().run([orphan], [self.prediction])
        self.assertEqual(eval_result["decision"], "block")
        self.assertIn(
            "EVAL-MIT-003:orphan_prediction_id",
            eval_result["reason_codes"],
        )


if __name__ == "__main__":
    unittest.main()
