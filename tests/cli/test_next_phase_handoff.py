"""Phase P — tests for next-phase-handoff CLI command and schema."""
from __future__ import annotations

import datetime
import io
import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict

import jsonschema
import pytest

from spectrum_systems_core.cli import next_phase_handoff_cli
from spectrum_systems_core.verification.next_phase_handoff import (
    build_next_phase_briefing,
    write_next_phase_briefing,
)


CONTRACT_DIR = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "schemas"
    / "verification"
)


def _load_schema() -> Dict[str, Any]:
    return json.loads(
        (CONTRACT_DIR / "next_phase_briefing.schema.json").read_text(
            encoding="utf-8"
        )
    )


def _seed_pipeline_state_record(
    sdl: Path,
    *,
    record_id: str | None = None,
    artifact_kind_only: int = 0,
) -> Dict[str, Any]:
    verif = sdl / "verifications"
    verif.mkdir(parents=True, exist_ok=True)
    rid = record_id or str(uuid.uuid4())
    record = {
        "pipeline_state_record_id": rid,
        "artifact_type": "pipeline_state_record",
        "schema_version": "1.0.0",
        "created_at": "2026-05-11T00:00:00+00:00",
        "data_lake_path": str(sdl.parent),
        "sdl_root": str(sdl),
        "total_artifacts_scanned": 5,
        "artifacts_by_type": {"source_record": 3, "minutes_record": 2},
        "artifacts_by_schema_version": {"1.0.0": 5},
        "validation_failures_by_type": {},
        "artifacts_with_artifact_kind_only": artifact_kind_only,
        "artifacts_with_both_fields": 0,
        "artifacts_with_artifact_type_only": 5,
        "expected_artifacts": {
            "source_record_count": 3,
            "minutes_record_count": 2,
            "confirmed_pair_count": 2,
            "chunks_files_present": 3,
            "meeting_extraction_count": 2,
            "alignment_result_count": 2,
            "eval_result_count": 2,
            "baseline_eval_summary_present": True,
            "glossary_term_count": 7,
        },
        "next_required_actions": [
            "run eval-ground-truth --set-baseline after human review",
        ],
        "warnings": [],
        "provenance": {"produced_by": "verify-pipeline-state"},
    }
    (verif / f"{rid}.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return record


def _seed_eval_summary(sdl: Path) -> Dict[str, Any]:
    evals = sdl / "evals"
    evals.mkdir(parents=True, exist_ok=True)
    rec = {
        "eval_summary_id": str(uuid.uuid4()),
        "pipeline_run_id": "run-x",
        "artifact_type": "eval_summary",
        "schema_version": "1.1.0",
        "created_at": "2026-05-11T01:00:00+00:00",
        "pairs_evaluated": 2,
        "pairs_skipped_pending_review": 0,
        "aggregate_coverage": 0.85,
        "aggregate_precision": 0.72,
        "total_items_requiring_review": 3,
        "by_chunking_strategy": {
            "speaker_turn": {
                "coverage": 0.85,
                "precision": 0.72,
                "pairs_count": 2,
            },
            "character_count_fallback": {
                "coverage": 0.0,
                "precision": 0.0,
                "pairs_count": 0,
            },
        },
        "eval_results": [],
        "is_baseline": True,
        "baseline_eval_summary_id": None,
        "regression_detected": False,
        "regression_detail": [],
        "partial_run_warning": False,
        "partial_run_detail": None,
        "provenance": {"produced_by": "EvalRunner"},
    }
    (evals / f"eval_summary_{rec['pipeline_run_id']}.json").write_text(
        json.dumps(rec, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return rec


def _seed_meeting_extraction(
    sdl: Path,
    source_id: str,
    *,
    decisions: int = 3,
    claims: int = 3,
    action_items: int = 3,
) -> None:
    target = sdl / "extractions"
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{source_id}_meeting_extraction.json").write_text(
        json.dumps(
            {
                "artifact_type": "meeting_extraction",
                "schema_version": "1.0.0",
                "meeting_extraction_id": str(uuid.uuid4()),
                "source_id": source_id,
                "total_chunks_classified": 100,
                "off_topic_count": 0,
                "regulatory_verb_fallback_count": 0,
                "requires_human_dedup_count": 0,
                "decisions": [{"id": i} for i in range(decisions)],
                "claims": [{"id": i} for i in range(claims)],
                "action_items": [{"id": i} for i in range(action_items)],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_writes_briefing_artifact(tmp_path: Path, monkeypatch) -> None:
    """End-to-end: a real run writes a schema-valid briefing under
    $SDL_ROOT/verifications/briefings/."""
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    _seed_pipeline_state_record(sdl)
    _seed_eval_summary(sdl)
    _seed_meeting_extraction(sdl, "a")
    monkeypatch.setenv("SDL_ROOT", str(sdl))

    buf = io.StringIO()
    rc = next_phase_handoff_cli(
        data_lake=str(tmp_path), cycle_id="phase-P-cycle-test", out_stream=buf
    )
    assert rc == 0, buf.getvalue()
    briefings = list((sdl / "verifications" / "briefings").glob("*.json"))
    assert len(briefings) == 1
    record = json.loads(briefings[0].read_text(encoding="utf-8"))
    # Validate against the schema we shipped.
    schema = _load_schema()
    jsonschema.Draft202012Validator(schema).validate(record)
    assert record["artifact_type"] == "next_phase_briefing"
    assert record["cycle_id"] == "phase-P-cycle-test"


def test_briefing_includes_valid_until_offset(
    tmp_path: Path, monkeypatch
) -> None:
    """valid_until == created_at + freshness_window_hours."""
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    _seed_pipeline_state_record(sdl)
    _seed_eval_summary(sdl)
    monkeypatch.setenv("SDL_ROOT", str(sdl))

    buf = io.StringIO()
    rc = next_phase_handoff_cli(
        data_lake=str(tmp_path),
        cycle_id="phase-P-cycle-test",
        freshness_hours=12,
        out_stream=buf,
    )
    assert rc == 0, buf.getvalue()
    record = json.loads(
        next((sdl / "verifications" / "briefings").glob("*.json")).read_text(
            encoding="utf-8"
        )
    )
    assert record["freshness_window_hours"] == 12
    created = datetime.datetime.fromisoformat(record["created_at"])
    valid_until = datetime.datetime.fromisoformat(record["valid_until"])
    delta = valid_until - created
    assert delta == datetime.timedelta(hours=12), (
        f"valid_until offset mismatch: created={created} "
        f"valid_until={valid_until} delta={delta}"
    )


def test_briefing_references_specific_pipeline_state_id(
    tmp_path: Path, monkeypatch
) -> None:
    """The briefing's pipeline_state_record_id must match the latest record."""
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    chosen_id = str(uuid.uuid4())
    _seed_pipeline_state_record(sdl, record_id=chosen_id)
    monkeypatch.setenv("SDL_ROOT", str(sdl))

    buf = io.StringIO()
    rc = next_phase_handoff_cli(
        data_lake=str(tmp_path),
        cycle_id="phase-P-cycle-test",
        out_stream=buf,
    )
    assert rc == 0, buf.getvalue()
    record = json.loads(
        next((sdl / "verifications" / "briefings").glob("*.json")).read_text(
            encoding="utf-8"
        )
    )
    assert record["pipeline_state_record_id"] == chosen_id


def test_briefing_metrics_snapshot_null_when_no_eval(
    tmp_path: Path, monkeypatch
) -> None:
    """Red-team scenario 3: no eval_summary => metrics_snapshot is null,
    and the command must not crash."""
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    _seed_pipeline_state_record(sdl)
    # NO eval_summary seeded.
    monkeypatch.setenv("SDL_ROOT", str(sdl))

    buf = io.StringIO()
    rc = next_phase_handoff_cli(
        data_lake=str(tmp_path),
        cycle_id="phase-P-cycle-test",
        out_stream=buf,
    )
    assert rc == 0, buf.getvalue()
    record = json.loads(
        next((sdl / "verifications" / "briefings").glob("*.json")).read_text(
            encoding="utf-8"
        )
    )
    assert record["metrics_snapshot"] is None
    assert record["eval_summary_id"] is None


def test_prompt_opening_contains_inventory_section(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The prompt_opening printed to stdout must include both sections."""
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    _seed_pipeline_state_record(sdl)
    _seed_eval_summary(sdl)
    monkeypatch.setenv("SDL_ROOT", str(sdl))

    buf = io.StringIO()
    rc = next_phase_handoff_cli(
        data_lake=str(tmp_path),
        cycle_id="phase-P-cycle-test",
        out_stream=buf,
    )
    text = buf.getvalue()
    assert rc == 0, text
    assert "### Inventory" in text
    assert "### Next required actions" in text


def test_prompt_opening_includes_validity_warning(
    tmp_path: Path, monkeypatch
) -> None:
    """The prompt opening must mention valid_until so a stale briefing
    is obviously stale."""
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    _seed_pipeline_state_record(sdl)
    _seed_eval_summary(sdl)
    monkeypatch.setenv("SDL_ROOT", str(sdl))

    buf = io.StringIO()
    rc = next_phase_handoff_cli(
        data_lake=str(tmp_path),
        cycle_id="phase-P-cycle-test",
        out_stream=buf,
    )
    text = buf.getvalue()
    assert rc == 0, text
    assert "valid until" in text or "valid_until" in text


def test_writes_step_summary_when_set(
    tmp_path: Path, monkeypatch
) -> None:
    """$GITHUB_STEP_SUMMARY receives the prompt_opening when set."""
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    _seed_pipeline_state_record(sdl)
    _seed_eval_summary(sdl)
    monkeypatch.setenv("SDL_ROOT", str(sdl))
    step_path = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step_path))

    buf = io.StringIO()
    rc = next_phase_handoff_cli(
        data_lake=str(tmp_path),
        cycle_id="phase-P-cycle-test",
        out_stream=buf,
    )
    assert rc == 0, buf.getvalue()
    body = step_path.read_text(encoding="utf-8")
    assert "### Inventory" in body
    assert "### Next required actions" in body


def test_build_uses_provided_now_for_determinism() -> None:
    """build_next_phase_briefing respects an explicit ``now`` for testability."""
    now = datetime.datetime(2026, 5, 11, 12, 0, 0, tzinfo=datetime.timezone.utc)
    record = build_next_phase_briefing(
        cycle_id="phase-P-cycle-test",
        freshness_window_hours=6,
        pipeline_state_record={
            "pipeline_state_record_id": "x",
            "created_at": "2026-05-10T00:00:00+00:00",
            "expected_artifacts": {},
            "next_required_actions": ["one", "two"],
        },
        eval_summary=None,
        verification_findings=None,
        meeting_extractions=[],
        now=now,
    )
    assert record["created_at"] == "2026-05-11T12:00:00+00:00"
    assert record["valid_until"] == "2026-05-11T18:00:00+00:00"
    assert record["metrics_snapshot"] is None
    assert record["next_required_actions"] == ["one", "two"]


def test_build_outstanding_findings_from_verification_findings() -> None:
    """The briefing copies (severity, area, title) tuples from findings."""
    record = build_next_phase_briefing(
        cycle_id="phase-P-cycle-test",
        pipeline_state_record={"pipeline_state_record_id": "x"},
        eval_summary={"eval_summary_id": "y"},
        verification_findings={
            "verification_findings_id": "z",
            "findings": [
                {
                    "severity": "sev_1",
                    "area": "pipeline",
                    "title": "broken_stage",
                    "description": "...",
                    "affected_artifacts": [],
                    "proposed_remediation": "...",
                    "github_issue_url": None,
                },
                {
                    "severity": "sev_2",
                    "area": "eval",
                    "title": "rate_high",
                    "description": "...",
                    "affected_artifacts": [],
                    "proposed_remediation": "...",
                    "github_issue_url": None,
                },
            ],
            "next_required_actions": [],
        },
        meeting_extractions=[],
    )
    titles = [f["title"] for f in record["outstanding_findings"]]
    assert "broken_stage" in titles
    assert "rate_high" in titles
