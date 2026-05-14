"""Phase P3-A: ExtractionMerger stamps the new optional rollup fields."""
from __future__ import annotations

from spectrum_systems_core.extraction.extraction_merger import ExtractionMerger


def test_merger_omits_p3a_fields_when_none_provided() -> None:
    artifact = ExtractionMerger().merge(
        source_artifact_id="sa-1",
        extraction_run_id="run-1",
        classifications=[],
        decisions=[],
        claims=[],
        action_items=[],
    )
    for key in (
        "extraction_mode",
        "glossary_version",
        "off_topic_rate",
        "extraction_path_breakdown",
        "source_turn_orphan_rate",
        "source_turn_diversity_rate",
        "stakeholders_populated_rate",
        "rationale_populated_rate",
        "claim_type_populated_rate",
    ):
        # Backwards compatibility: legacy callers / tests that pass
        # no p3a_fields must still produce schema-valid artifacts
        # without the new keys.
        assert key not in artifact


def test_merger_stamps_p3a_fields_when_provided() -> None:
    p3a_fields = {
        "extraction_mode": "two_stage",
        "glossary_version": 1,
        "off_topic_rate": 0.125,
        "extraction_path_breakdown": {
            "decision": 3, "claim": 2, "action_item": 1, "off_topic": 1,
        },
        "source_turn_orphan_rate": 0.0,
        "source_turn_diversity_rate": 0.8,
        "stakeholders_populated_rate": 0.67,
        "rationale_populated_rate": 0.33,
        "claim_type_populated_rate": 1.0,
    }
    artifact = ExtractionMerger().merge(
        source_artifact_id="sa-1",
        extraction_run_id="run-1",
        classifications=[],
        decisions=[],
        claims=[],
        action_items=[],
        p3a_fields=p3a_fields,
    )
    for key, value in p3a_fields.items():
        assert artifact[key] == value


def test_merger_skips_none_values_in_p3a_fields() -> None:
    artifact = ExtractionMerger().merge(
        source_artifact_id="sa-1",
        extraction_run_id="run-1",
        classifications=[],
        decisions=[],
        claims=[],
        action_items=[],
        p3a_fields={"extraction_mode": "two_stage", "glossary_version": None},
    )
    assert artifact["extraction_mode"] == "two_stage"
    # None values are skipped so the schema's `glossary_version: integer`
    # constraint cannot trip on a None when the glossary isn't loaded.
    assert "glossary_version" not in artifact
