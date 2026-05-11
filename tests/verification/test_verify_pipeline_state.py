"""Phase O.0 — tests for verify-pipeline-state.

Every test uses tmp_path for SDL_ROOT and contracts/ schemas as written.
No live network or model calls.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict

import jsonschema
import pytest

from spectrum_systems_core.cli import (
    verify_pipeline_state as verify_pipeline_state_cli,
)
from spectrum_systems_core.verification import (
    emit_actions_summary,
    scan_pipeline_state,
    write_pipeline_state_record,
)

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "schemas"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stage_data_lake(tmp_path: Path) -> Path:
    """Create a minimal data-lake skeleton and return its root."""
    root = tmp_path / "data-lake"
    (root / "store" / "artifacts").mkdir(parents=True)
    (root / "store" / "processed").mkdir(parents=True)
    (root / "store" / "raw" / "transcripts").mkdir(parents=True)
    return root


def _write_sdl_artifact(sdl_root: Path, name: str, obj: Dict[str, Any]) -> Path:
    sdl_root.mkdir(parents=True, exist_ok=True)
    target = sdl_root / name
    target.write_text(
        json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


def _minimal_source_record(source_id: str) -> Dict[str, Any]:
    return {
        "artifact_type": "source_record",
        "schema_version": "1.0.0",
        "artifact_id": str(uuid.uuid4()),
        "payload": {"source_id": source_id, "source_family": "meetings"},
    }


def _write_processed_source_record(
    data_lake: Path, source_id: str
) -> Dict[str, Any]:
    target = data_lake / "store" / "processed" / "meetings" / source_id
    target.mkdir(parents=True, exist_ok=True)
    record = _minimal_source_record(source_id)
    (target / "source_record.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return record


def _write_minutes_record(sdl_root: Path) -> Dict[str, Any]:
    # minutes_record schema in this repo carries artifact_kind only.
    # We give it artifact_type to ensure the scan classifies it.
    sdl_root.mkdir(parents=True, exist_ok=True)
    record = {
        "artifact_type": "minutes_record",
        "schema_version": "1.0.0",
        "minutes_id": str(uuid.uuid4()),
        "artifact_id": str(uuid.uuid4()),
        "payload": {},
    }
    target = sdl_root / "minutes"
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{record['minutes_id']}.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return record


# ---------------------------------------------------------------------------
# Direct scan_pipeline_state tests
# ---------------------------------------------------------------------------


def test_counts_artifacts_by_type(tmp_path: Path) -> None:
    data_lake = _stage_data_lake(tmp_path)
    sdl_root = data_lake / "store" / "artifacts"

    for sid in ("a", "b", "c"):
        _write_processed_source_record(data_lake, sid)
    for i in range(2):
        _write_sdl_artifact(
            sdl_root / "minutes",
            f"m{i}.json",
            {
                "artifact_type": "minutes_record",
                "schema_version": "1.0.0",
                "minutes_id": str(uuid.uuid4()),
                "artifact_id": str(uuid.uuid4()),
                "payload": {},
            },
        )

    record = scan_pipeline_state(
        data_lake_path=str(data_lake), validate_schemas=False
    )
    assert record["artifacts_by_type"].get("source_record", 0) == 3
    assert record["artifacts_by_type"].get("minutes_record", 0) == 2
    assert record["expected_artifacts"]["source_record_count"] == 3
    assert record["expected_artifacts"]["minutes_record_count"] == 2


def test_detects_artifact_kind_only_artifacts(tmp_path: Path) -> None:
    data_lake = _stage_data_lake(tmp_path)
    sdl_root = data_lake / "store" / "artifacts"
    # A legacy artifact with ONLY artifact_kind (no artifact_type).
    _write_sdl_artifact(
        sdl_root,
        "legacy.json",
        {
            "artifact_kind": "legacy_thing",
            "schema_version": "1.0.0",
            "payload": {},
        },
    )
    record = scan_pipeline_state(
        data_lake_path=str(data_lake), validate_schemas=False
    )
    assert record["artifacts_with_artifact_kind_only"] == 1
    assert record["artifacts_with_both_fields"] == 0
    assert "run migrate-artifact-kind workflow" in (
        record["next_required_actions"]
    )


def test_detects_both_fields_present(tmp_path: Path) -> None:
    data_lake = _stage_data_lake(tmp_path)
    sdl_root = data_lake / "store" / "artifacts"
    _write_sdl_artifact(
        sdl_root,
        "transitional.json",
        {
            "artifact_kind": "source_record",
            "artifact_type": "source_record",
            "schema_version": "1.0.0",
            "payload": {},
        },
    )
    record = scan_pipeline_state(
        data_lake_path=str(data_lake), validate_schemas=False
    )
    assert record["artifacts_with_both_fields"] == 1
    assert record["artifacts_with_artifact_kind_only"] == 0
    # both-fields artifacts must NOT trigger the migrate action.
    assert "run migrate-artifact-kind workflow" not in (
        record["next_required_actions"]
    )


def test_schema_validation_failures_counted(tmp_path: Path) -> None:
    """An artifact that violates its schema must show up in failures_by_type."""
    data_lake = _stage_data_lake(tmp_path)
    sdl_root = data_lake / "store" / "artifacts"
    # pipeline_state_record schema requires many fields; pass only artifact_type
    # and schema_version. This guarantees a validation failure against the
    # contract schema (used here as a "known schema" target).
    _write_sdl_artifact(
        sdl_root,
        "broken.json",
        {
            "artifact_type": "pipeline_state_record",
            "schema_version": "1.0.0",
            "missing_everything_else": True,
        },
    )
    record = scan_pipeline_state(
        data_lake_path=str(data_lake), validate_schemas=True
    )
    assert (
        record["validation_failures_by_type"].get("pipeline_state_record", 0)
        == 1
    )


def test_next_required_action_for_missing_migration(tmp_path: Path) -> None:
    data_lake = _stage_data_lake(tmp_path)
    sdl_root = data_lake / "store" / "artifacts"
    _write_sdl_artifact(
        sdl_root,
        "kind_only.json",
        {"artifact_kind": "x", "schema_version": "1.0.0", "payload": {}},
    )
    record = scan_pipeline_state(
        data_lake_path=str(data_lake), validate_schemas=False
    )
    assert record["artifacts_with_artifact_kind_only"] == 1
    assert "run migrate-artifact-kind workflow" in (
        record["next_required_actions"]
    )


def test_next_required_action_for_missing_baseline(tmp_path: Path) -> None:
    data_lake = _stage_data_lake(tmp_path)
    sdl_root = data_lake / "store" / "artifacts"
    # One eval_result artifact, no baseline_eval_summary.json on disk.
    _write_sdl_artifact(
        sdl_root / "evals" / "results",
        f"{uuid.uuid4()}.json",
        {
            "artifact_type": "eval_result",
            "schema_version": "1.0.0",
            "eval_result_id": str(uuid.uuid4()),
            "alignment_result_id": str(uuid.uuid4()),
            "source_artifact_id": "x",
            "minutes_artifact_id": "y",
            "pair_id": str(uuid.uuid4()),
            "pipeline_run_id": "run-x",
            "prompt_version": "v0",
            "created_at": "2026-05-11T00:00:00+00:00",
            "chunking_strategy": "speaker_turn",
            "coverage": 0.0,
            "precision": 0.0,
            "items_requiring_review": 0,
            "items_requiring_review_rate": 0.0,
            "total_extracted_items": 0,
            "total_minutes_items": 0,
            "provenance": {"produced_by": "EvalMetrics"},
        },
    )
    record = scan_pipeline_state(
        data_lake_path=str(data_lake), validate_schemas=False
    )
    assert (
        "run eval-ground-truth --set-baseline after human review"
        in record["next_required_actions"]
    )


def test_emit_actions_summary_writes_to_github_step_summary(
    tmp_path: Path, monkeypatch
) -> None:
    data_lake = _stage_data_lake(tmp_path)
    step_summary = tmp_path / "GITHUB_STEP_SUMMARY"
    step_summary.write_text("preamble\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step_summary))
    monkeypatch.setenv("SDL_ROOT", str(data_lake / "store" / "artifacts"))

    rc = verify_pipeline_state_cli(
        data_lake=str(data_lake), emit_actions_summary=True
    )
    assert rc == 0
    body = step_summary.read_text(encoding="utf-8")
    assert "verify-pipeline-state" in body
    assert "Total artifacts scanned" in body


def test_pipeline_state_record_validates_against_schema(tmp_path: Path) -> None:
    """write_pipeline_state_record must write a schema-conformant artifact."""
    data_lake = _stage_data_lake(tmp_path)
    sdl_root = data_lake / "store" / "artifacts"
    record = scan_pipeline_state(
        data_lake_path=str(data_lake), validate_schemas=False
    )
    written = write_pipeline_state_record(record, sdl_root=sdl_root)
    assert written is not None
    on_disk = json.loads(written.read_text(encoding="utf-8"))
    schema = json.loads(
        (CONTRACT_DIR / "verification" / "pipeline_state_record.schema.json")
        .read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(schema).validate(on_disk)


def test_empty_sdl_root_is_a_finding_not_silent_success(tmp_path: Path) -> None:
    """Sev-1 Red-Team scenario #1."""
    data_lake = _stage_data_lake(tmp_path)
    record = scan_pipeline_state(
        data_lake_path=str(data_lake), validate_schemas=False
    )
    assert record["total_artifacts_scanned"] == 0
    assert "sdl_root_empty" in record["warnings"]


def test_settings_json_is_not_classified_as_artifact(tmp_path: Path) -> None:
    """Sev-1 Red-Team scenario #6: skip config files."""
    data_lake = _stage_data_lake(tmp_path)
    sdl_root = data_lake / "store" / "artifacts"
    _write_sdl_artifact(
        sdl_root, "settings.json", {"dangerouslySkipPermissions": True}
    )
    record = scan_pipeline_state(
        data_lake_path=str(data_lake), validate_schemas=False
    )
    assert record["total_artifacts_scanned"] == 0


def test_new_schemas_use_artifact_type_not_kind() -> None:
    """Sev-1 Red-Team scenario #7."""
    for schema_name in (
        "verification/pipeline_state_record.schema.json",
        "verification/verification_findings.schema.json",
    ):
        path = CONTRACT_DIR / schema_name
        schema = json.loads(path.read_text(encoding="utf-8"))
        props = schema.get("properties", {})
        assert "artifact_type" in props, (
            f"{schema_name} missing artifact_type"
        )
        assert "artifact_kind" not in props, (
            f"{schema_name} must not declare artifact_kind"
        )
        assert "artifact_type" in schema.get("required", [])
        assert "schema_version" in schema.get("required", [])
        assert schema.get("additionalProperties") is False
