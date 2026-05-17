"""Phase G — Red Team #1: schema design checks."""
from __future__ import annotations

import datetime
import unittest
import uuid

import jsonschema

from spectrum_systems_core.harness._schema import load_harness_schema


def _validate(name: str, artifact: dict) -> None:
    schema = load_harness_schema(name)
    jsonschema.Draft202012Validator(schema).validate(artifact)


def _now() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


class RT1SchemaTests(unittest.TestCase):
    # CHECK-RT1-002 — eval_case_candidate.requires_human_promotion is const true
    def test_eval_candidate_requires_human_must_be_true(self) -> None:
        bad = {
            "candidate_id": str(uuid.uuid4()),
            "proposed_eval_type": "policy_alignment",
            "proposed_metric_name": "no_recurrence",
            "proposed_target_artifact_type": "report_draft",
            "proposed_pass_condition": "no recurrence",
            "triggering_pattern_id": str(uuid.uuid4()),
            "triggering_failure_detail": "x",
            "proposed_by": "harness_memory",
            "requires_human_promotion": False,
            "status": "candidate",
            "promotion_note": "",
            "created_at": _now(),
        }
        with self.assertRaises(jsonschema.ValidationError):
            _validate("eval_case_candidate", bad)

    # CHECK-RT1-003 — outcome_type enum enforced
    def test_outcome_type_enum_enforced(self) -> None:
        bad = {
            "record_id": str(uuid.uuid4()),
            "outcome_type": "improvement",
            "source_artifact_id": str(uuid.uuid4()),
            "paper_source_id": "p",
            "issue_type": "scope",
            "issue_severity": "high",
            "action_taken": "x",
            "human_marked_outcome": "effective",
            "final_outcome": "effective",
            "auto_downgraded": False,
            "secondary_check_performed": False,
            "pattern_keywords": [],
            "recorded_at": _now(),
        }
        with self.assertRaises(jsonschema.ValidationError):
            _validate("outcome_memory_record", bad)

    # CHECK-RT1-002 (positive) — valid candidate accepts
    def test_valid_eval_candidate_accepts(self) -> None:
        good = {
            "candidate_id": str(uuid.uuid4()),
            "proposed_eval_type": "policy_alignment",
            "proposed_metric_name": "no_recurrence",
            "proposed_target_artifact_type": "report_draft",
            "proposed_pass_condition": "no recurrence",
            "triggering_pattern_id": str(uuid.uuid4()),
            "triggering_failure_detail": "x",
            "proposed_by": "harness_memory",
            "requires_human_promotion": True,
            "status": "candidate",
            "promotion_note": "",
            "created_at": _now(),
        }
        _validate("eval_case_candidate", good)  # must not raise

    def test_failure_pattern_jaccard_threshold_const(self) -> None:
        bad = {
            "pattern_id": str(uuid.uuid4()),
            "reason_code": "x",
            "cluster_method": "reason_code_then_jaccard",
            "jaccard_threshold": 0.5,
            "member_run_ids": ["a", "b"],
            "member_failure_details": ["a", "b"],
            "first_seen_at": _now(),
            "last_seen_at": _now(),
            "occurrence_count": 2,
            "eval_candidate_id": None,
            "created_at": _now(),
        }
        with self.assertRaises(jsonschema.ValidationError):
            _validate("failure_pattern", bad)

    def test_workflow_comparison_direction_enum(self) -> None:
        bad = {
            "comparison_id": str(uuid.uuid4()),
            "run_id_a": str(uuid.uuid4()),
            "run_id_b": str(uuid.uuid4()),
            "compared_at": _now(),
            "dimensions": [
                {
                    "dimension_name": "x",
                    "value_a": 1,
                    "value_b": 2,
                    "delta": 1,
                    "direction": "weird",
                }
            ],
            "summary": "long enough summary",
            "recommended_action": "act",
            "vault_projection_path": None,
        }
        with self.assertRaises(jsonschema.ValidationError):
            _validate("workflow_comparison", bad)

    def test_entropy_report_severity_enum(self) -> None:
        bad = {
            "report_id": str(uuid.uuid4()),
            "generated_at": _now(),
            "scope": "all",
            "flagged_items": [
                {
                    "item_type": "x",
                    "item_id": "y",
                    "reason": "z",
                    "recommended_action": "w",
                    "severity": "extreme",
                }
            ],
            "total_flagged": 1,
            "total_scanned": 1,
        }
        with self.assertRaises(jsonschema.ValidationError):
            _validate("entropy_report", bad)


if __name__ == "__main__":
    unittest.main()
