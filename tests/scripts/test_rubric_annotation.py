"""Phase X2.5 — rubric annotation tests."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import annotate_rubric  # noqa: E402

# Schema lives under contracts/schemas/ingestion -- we validate using
# jsonschema directly because the package-level validation registry
# only knows about src/spectrum_systems_core/schemas/.
import jsonschema


GT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts" / "schemas" / "ingestion"
    / "ground_truth_pair.schema.json"
)


def _gt_schema() -> Dict[str, Any]:
    return json.loads(GT_SCHEMA_PATH.read_text(encoding="utf-8"))


def _base_pair(pair_id: str) -> Dict[str, Any]:
    return {
        "pair_id": pair_id,
        "source_artifact_id": "src-001",
        "minutes_artifact_id": "min-001",
        "meeting_date": "2026-02-19",
        "meeting_name": "Phase X2 test pair",
        "match_confidence": "high",
        "status": "confirmed",
        "created_at": "2026-05-11T00:00:00+00:00",
        "confirmed_at": "2026-05-11T00:00:00+00:00",
        "confirmed_by": "fixture",
        "schema_version": "1.0.0",
        "provenance": {"produced_by": "GroundTruthLinker"},
    }


def _seed_gt_pair(sdl_root: Path, pair_id: str, **extras: Any) -> Path:
    target = sdl_root / "ground_truth"
    target.mkdir(parents=True, exist_ok=True)
    rec = _base_pair(pair_id)
    rec.update(extras)
    path = target / f"{pair_id}.json"
    path.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    return path


# --- Schema additivity --------------------------------------------


def test_pair_schema_validates_without_rubric_notes(tmp_path: Path) -> None:
    schema = _gt_schema()
    pair = _base_pair("11111111-1111-4111-1111-111111111111")
    jsonschema.Draft202012Validator(schema).validate(pair)


def test_pair_schema_validates_with_rubric_notes() -> None:
    schema = _gt_schema()
    pair = _base_pair("22222222-2222-4222-2222-222222222222")
    pair["rubric_notes"] = {
        "expected_decision_outcome": "approval",
        "verb_discrimination_example": True,
        "annotator_id": "alice",
        "annotated_at": "2026-05-11T00:00:00+00:00",
        "notes": "FCC approved -- not 'considered'.",
    }
    pair["target_type"] = "decision"
    pair["decision_id"] = "d_abc"
    pair["ground_truth_pass"] = True
    jsonschema.Draft202012Validator(schema).validate(pair)


def test_pair_schema_rejects_unknown_outcome() -> None:
    schema = _gt_schema()
    pair = _base_pair("33333333-3333-4333-3333-333333333333")
    pair["rubric_notes"] = {
        "expected_decision_outcome": "frobnicated",  # invalid enum
        "verb_discrimination_example": True,
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(pair)


# --- annotate_rubric behaviour ------------------------------------


def test_annotate_pair_returns_new_dict_with_rubric_notes() -> None:
    pair = _base_pair("p1")
    out = annotate_rubric.annotate_pair(
        pair,
        expected_decision_outcome="approval",
        verb_discrimination_example=True,
        annotator_id="alice",
        notes="ok",
    )
    assert pair.get("rubric_notes") is None  # original NOT mutated
    rubric = out["rubric_notes"]
    assert rubric["expected_decision_outcome"] == "approval"
    assert rubric["verb_discrimination_example"] is True
    assert rubric["annotator_id"] == "alice"
    assert rubric["annotated_at"] is not None
    assert rubric["notes"] == "ok"


def test_apply_annotations_from_file_writes_rubric(tmp_path: Path) -> None:
    sdl_root = tmp_path / "store" / "artifacts"
    pair_id = "44444444-4444-4444-4444-444444444444"
    _seed_gt_pair(sdl_root, pair_id, target_type="decision")
    annotations_file = tmp_path / "annot.json"
    annotations_file.write_text(json.dumps([
        {
            "pair_id": pair_id,
            "expected_decision_outcome": "deferral",
            "verb_discrimination_example": True,
            "annotator_id": "bob",
        }
    ]), encoding="utf-8")
    rc = annotate_rubric.main([
        "--data-lake", str(tmp_path),
        "--apply-from", str(annotations_file),
    ])
    assert rc == 0
    updated = json.loads(
        (sdl_root / "ground_truth" / f"{pair_id}.json").read_text(encoding="utf-8")
    )
    rubric = updated["rubric_notes"]
    assert rubric["expected_decision_outcome"] == "deferral"
    assert rubric["annotator_id"] == "bob"


def test_list_candidates_filters_target_type(tmp_path: Path) -> None:
    sdl_root = tmp_path / "store" / "artifacts"
    _seed_gt_pair(sdl_root, "p1", target_type="decision")
    _seed_gt_pair(sdl_root, "p2", target_type="claim")
    _seed_gt_pair(sdl_root, "p3")  # no target_type -- included
    candidates = annotate_rubric.list_candidates(sdl_root)
    pair_ids = {c["pair_id"] for c in candidates}
    assert "p1" in pair_ids
    assert "p3" in pair_ids
    assert "p2" not in pair_ids


def test_list_candidates_respects_limit(tmp_path: Path) -> None:
    sdl_root = tmp_path / "store" / "artifacts"
    for i in range(5):
        _seed_gt_pair(sdl_root, f"pair-{i:04d}", target_type="decision")
    candidates = annotate_rubric.list_candidates(sdl_root, limit=3)
    assert len(candidates) == 3


def test_main_no_candidates_returns_nonzero(tmp_path: Path) -> None:
    sdl_root = tmp_path / "store" / "artifacts"
    sdl_root.mkdir(parents=True)
    rc = annotate_rubric.main(["--data-lake", str(tmp_path)])
    assert rc == 2


# --- Codex P1 fix: --source-id filter on production-shaped pairs ----


def test_filter_matches_production_source_artifact_id(tmp_path: Path) -> None:
    """A pair whose ONLY source field is ``source_artifact_id`` (the
    canonical production field) must be returned when --source-id
    matches that value. Regression test for the Codex P1 finding."""
    sdl_root = tmp_path / "store" / "artifacts"
    _seed_gt_pair(
        sdl_root, "55555555-5555-4555-5555-555555555555",
        target_type="decision",
    )  # _base_pair sets source_artifact_id="src-001" and no fixture_source_id
    matched = annotate_rubric.list_candidates(sdl_root, source_id="src-001")
    assert len(matched) == 1
    assert matched[0]["source_artifact_id"] == "src-001"


def test_filter_matches_fixture_source_id_when_present(tmp_path: Path) -> None:
    """Fixture pairs that carry an additional ``fixture_source_id`` field
    can still be filtered by that field. Both source-id fields are honoured."""
    sdl_root = tmp_path / "store" / "artifacts"
    _seed_gt_pair(
        sdl_root, "66666666-6666-4666-6666-666666666666",
        target_type="decision",
        fixture_source_id="fixture-meeting-002",
    )
    matched = annotate_rubric.list_candidates(
        sdl_root, source_id="fixture-meeting-002",
    )
    assert len(matched) == 1


def test_unknown_source_id_returns_helpful_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Filter with a typo'd source_id must exit non-zero AND print the
    available identifiers — never silently return zero pairs. Codex P1."""
    sdl_root = tmp_path / "store" / "artifacts"
    _seed_gt_pair(
        sdl_root, "77777777-7777-4777-7777-777777777777",
        target_type="decision",
    )
    rc = annotate_rubric.main([
        "--data-lake", str(tmp_path),
        "--source-id", "not-a-real-source-id",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not-a-real-source-id" in err
    assert "matched 0 ground truth pairs" in err
    # The error must point the operator at what IS available.
    assert "Available source identifiers" in err
    assert "src-001" in err


# --- judge calibration uses rubric_notes -----------------------


def test_judge_calibration_reads_verb_discrimination(tmp_path: Path) -> None:
    from spectrum_systems_core.evals.judge import run_judge, RUBRIC_CHECKS
    from spectrum_systems_core.evals.judge_calibration import calibrate
    import os
    os.environ["JUDGE_ENABLED"] = "true"
    try:
        result = run_judge(
            decisions=[{"decision_id": "d1", "decision_text": "x"}],
            source_texts_by_chunk={},
            api_caller=lambda p: json.dumps({k: True for k in RUBRIC_CHECKS}),
        )
        pairs = [{
            "pair_id": "p1", "target_type": "decision", "decision_id": "d1",
            "ground_truth_pass": True,
            "rubric_notes": {
                "expected_decision_outcome": "approval",
                "verb_discrimination_example": True,
            },
        }]
        record = calibrate(result, ground_truth_pairs=pairs)
        assert record.agreement_rate_verb_discrimination == 1.0
        assert record.verb_discrimination_pairs == 1
    finally:
        os.environ.pop("JUDGE_ENABLED", None)
