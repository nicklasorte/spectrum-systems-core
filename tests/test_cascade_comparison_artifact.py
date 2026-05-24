"""Phase 4.C — comparison_result cascade-field tests.

Pins the additive cascade fields' schema-validity and the
``_compute_phase_4c_fields`` reader behaviour. The fields are
optional, so the contract is:

* a comparison_result built without cascade artifacts on disk has
  every cascade field ABSENT (not zero, not null);
* once the cascade artifacts land, the corresponding fields appear
  on the comparison artifact;
* the recall_collapse_warning trips at post_cascade_haiku_recall <
  0.50.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
SCRIPTS_ROOT = REPO_ROOT / "scripts"
for p in (str(SRC_ROOT), str(SCRIPTS_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Import the comparison helpers without invoking the LLM-touching
# pipeline. The module is pure aside from optional Anthropic-SDK
# imports gated behind a try/except.
import compare_opus_haiku as cmp  # noqa: E402


SOURCE_ID = "fixture-source-id"


def _make_meeting_dir(data_lake: pathlib.Path) -> pathlib.Path:
    mdir = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    mdir.mkdir(parents=True)
    return mdir


def _write_grounded_artifact(mdir: pathlib.Path, decisions: list[dict]) -> None:
    envelope = {
        "artifact_type": "grounded_meeting_minutes",
        "schema_version": "1.5.0",  # gate's own envelope version
        "source_id": SOURCE_ID,
        "run_id": "run-1",
        "gate_passed": True,
        "payload": {
            "artifact_type": "meeting_minutes",
            "schema_version": "1.6.0",  # Phase 4.B meeting_minutes contract
            "title": "fix",
            "summary": "",
            "decisions": decisions,
            "action_items": [],
            "open_questions": [],
            "provenance": {"produced_by": "haiku_llm", "model_id": "claude-haiku-4-7"},
        },
    }
    (mdir / "grounded_items__run-1.json").write_text(
        json.dumps(envelope, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_cascade_artifacts(
    mdir: pathlib.Path,
    *,
    decisions: list[dict],
    kept: int,
    dropped: int,
    modified: int,
    drop_rate: float,
) -> None:
    filtered = {
        "artifact_type": "cascade_filtered",
        "schema_version": "1.0.0",
        "source_id": SOURCE_ID,
        "run_id": "run-1",
        "filter_model": "claude-sonnet-4-6",
        "bypassed": False,
        "payload": {
            "artifact_type": "meeting_minutes",
            "schema_version": "1.6.0",
            "title": "fix",
            "summary": "",
            "decisions": decisions,
            "action_items": [],
            "open_questions": [],
            "provenance": {"produced_by": "haiku_llm", "model_id": "claude-haiku-4-7"},
        },
    }
    (mdir / "cascade_filtered__run-1.json").write_text(
        json.dumps(filtered, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    result = {
        "artifact_type": "cascade_filter_result",
        "schema_version": "1.0.0",
        "source_id": SOURCE_ID,
        "run_id": "run-1",
        "filter_model": "claude-sonnet-4-6",
        "total_items": kept + dropped + modified,
        "kept_count": kept,
        "dropped_count": dropped,
        "modified_count": modified,
        "batches_used": 1,
        "cascade_drop_rate": drop_rate,
        "bypassed": False,
    }
    (mdir / "cascade_filter_result__run-1.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_phase_4c_fields_absent_when_cascade_did_not_run(tmp_path: pathlib.Path) -> None:
    data_lake = tmp_path / "data-lake"
    mdir = _make_meeting_dir(data_lake)
    # No grounded, no cascade artifacts — every field stays None.
    fields = cmp._compute_phase_4c_fields(
        data_lake=data_lake,
        source_id=SOURCE_ID,
        baseline_rows=[],
        gt_pairs=None,
        types=["decisions"],
    )
    for v in fields.values():
        assert v is None
    _ = mdir  # silence unused


def test_phase_4c_fields_populated_when_cascade_ran(tmp_path: pathlib.Path) -> None:
    data_lake = tmp_path / "data-lake"
    mdir = _make_meeting_dir(data_lake)
    decisions_in = [{"text": "real decision", "source_quote": "real decision"}]
    decisions_out = [{"text": "real decision", "source_quote": "real decision"}]
    _write_grounded_artifact(mdir, decisions_in)
    _write_cascade_artifacts(
        mdir,
        decisions=decisions_out,
        kept=1,
        dropped=0,
        modified=0,
        drop_rate=0.0,
    )
    fields = cmp._compute_phase_4c_fields(
        data_lake=data_lake,
        source_id=SOURCE_ID,
        baseline_rows=[{"extraction_type": "decisions",
                        "ground_truth_text": "real decision",
                        "text": "real decision",
                        "model_id": "opus", "source_id": SOURCE_ID}],
        gt_pairs=None,
        types=["decisions"],
    )
    # Cascade counts come from the result artifact.
    assert fields["cascade_kept_count"] == 1
    assert fields["cascade_dropped_count"] == 0
    assert fields["cascade_modified_count"] == 0
    assert fields["cascade_drop_rate"] == 0.0
    # Pre/post counts come from running compute_comparison on the
    # grounded/filtered artifacts.
    assert fields["pre_cascade_haiku_count"] is not None
    assert fields["post_cascade_haiku_count"] is not None


def test_cascade_recall_collapse_warning_trips_below_threshold(
    tmp_path: pathlib.Path,
) -> None:
    data_lake = tmp_path / "data-lake"
    mdir = _make_meeting_dir(data_lake)
    # Baseline has two items; cascade kept zero — recall = 0.0.
    decisions_in = [
        {"text": "real decision A", "source_quote": "real decision A"},
        {"text": "real decision B", "source_quote": "real decision B"},
    ]
    decisions_out: list[dict] = []
    _write_grounded_artifact(mdir, decisions_in)
    _write_cascade_artifacts(
        mdir,
        decisions=decisions_out,
        kept=0,
        dropped=2,
        modified=0,
        drop_rate=1.0,
    )
    baseline = [
        {
            "extraction_type": "decisions",
            "ground_truth_text": "real decision A",
            "text": "real decision A",
            "model_id": "opus",
            "source_id": SOURCE_ID,
        },
        {
            "extraction_type": "decisions",
            "ground_truth_text": "real decision B",
            "text": "real decision B",
            "model_id": "opus",
            "source_id": SOURCE_ID,
        },
    ]
    fields = cmp._compute_phase_4c_fields(
        data_lake=data_lake,
        source_id=SOURCE_ID,
        baseline_rows=baseline,
        gt_pairs=None,
        types=["decisions"],
    )
    assert fields["cascade_recall_collapse_warning"] is True


def test_cascade_recall_collapse_warning_false_when_recall_holds(
    tmp_path: pathlib.Path,
) -> None:
    data_lake = tmp_path / "data-lake"
    mdir = _make_meeting_dir(data_lake)
    # Recall = 1.0 — cascade kept the only baseline item.
    decisions_in = [{"text": "real decision", "source_quote": "real decision"}]
    decisions_out = [{"text": "real decision", "source_quote": "real decision"}]
    _write_grounded_artifact(mdir, decisions_in)
    _write_cascade_artifacts(
        mdir,
        decisions=decisions_out,
        kept=1,
        dropped=0,
        modified=0,
        drop_rate=0.0,
    )
    baseline = [
        {
            "extraction_type": "decisions",
            "ground_truth_text": "real decision",
            "text": "real decision",
            "model_id": "opus",
            "source_id": SOURCE_ID,
        }
    ]
    fields = cmp._compute_phase_4c_fields(
        data_lake=data_lake,
        source_id=SOURCE_ID,
        baseline_rows=baseline,
        gt_pairs=None,
        types=["decisions"],
    )
    assert fields["cascade_recall_collapse_warning"] is False


def test_build_comparison_artifact_includes_phase_4c_fields(tmp_path: pathlib.Path) -> None:
    """When phase_4c_fields is passed, only non-None entries land on the artifact."""
    artifact = cmp.build_comparison_artifact(
        source_id=SOURCE_ID,
        haiku_artifact={"payload": {"decisions": []}},
        baseline_rows=[],
        metrics={
            "summary": {
                "total_opus_items": 0,
                "total_haiku_items": 0,
                "true_positives": 0,
                "false_negatives": 0,
                "haiku_only": 0,
                "gt_covered_by_haiku": 0,
                "gt_missed_by_haiku": 0,
                "gt_covered_by_opus": 0,
                "haiku_recall_vs_opus": 1.0,
                "haiku_precision_vs_opus": 1.0,
                "haiku_f1_vs_opus": 1.0,
                "gt_recall_haiku": 1.0,
                "gt_recall_opus": 1.0,
            },
            "by_type": {},
            "false_negatives": [],
            "haiku_only_items": [],
            "gt_missed": [],
            "gt_pairs_present": False,
        },
        compared_at="2026-05-24T00:00:00+00:00",
        phase_4c_fields={
            "cascade_kept_count": 3,
            "cascade_dropped_count": 2,
            "cascade_modified_count": 1,
            "cascade_drop_rate": 0.333,
            "cascade_recall_collapse_warning": False,
            # An absent field stays absent on the artifact:
            "pre_cascade_haiku_count": None,
        },
    )
    assert artifact["cascade_kept_count"] == 3
    assert artifact["cascade_dropped_count"] == 2
    assert artifact["cascade_modified_count"] == 1
    assert artifact["cascade_drop_rate"] == 0.333
    assert artifact["cascade_recall_collapse_warning"] is False
    # None-valued fields must NOT appear on the artifact.
    assert "pre_cascade_haiku_count" not in artifact


def test_phase_4c_schema_allows_all_cascade_fields() -> None:
    """The added cascade_* fields validate under the comparison schema."""
    import jsonschema

    schema_path = (
        REPO_ROOT
        / "src"
        / "spectrum_systems_core"
        / "schemas"
        / "comparison_result.schema.json"
    )
    schema = json.loads(schema_path.read_text())
    # Minimum two-way comparison_result + every cascade field set.
    artifact = {
        "artifact_type": "comparison_result",
        "schema_version": "1.0.0",
        "source_id": SOURCE_ID,
        "haiku_run_id": "h1",
        "opus_model_id": "opus",
        "compared_at": "2026-05-24T00:00:00+00:00",
        "by_type": {},
        "summary": {
            "total_opus_items": 0, "total_haiku_items": 0,
            "true_positives": 0, "false_negatives": 0,
            "haiku_only": 0, "gt_covered_by_haiku": 0,
            "gt_missed_by_haiku": 0, "gt_covered_by_opus": 0,
            "haiku_recall_vs_opus": 1.0, "haiku_precision_vs_opus": 1.0,
            "haiku_f1_vs_opus": 1.0, "gt_recall_haiku": 1.0,
            "gt_recall_opus": 1.0,
        },
        "false_negatives": [],
        "haiku_only_items": [],
        "gt_missed": [],
        "pre_cascade_haiku_count": 10,
        "pre_cascade_haiku_f1": 0.6,
        "pre_cascade_haiku_precision": 0.5,
        "pre_cascade_haiku_recall": 0.7,
        "post_cascade_haiku_count": 7,
        "post_cascade_haiku_f1": 0.72,
        "post_cascade_haiku_precision": 0.8,
        "post_cascade_haiku_recall": 0.65,
        "cascade_kept_count": 6,
        "cascade_dropped_count": 3,
        "cascade_modified_count": 1,
        "cascade_drop_rate": 0.3,
        "cascade_recall_collapse_warning": False,
    }
    # Should not raise.
    jsonschema.Draft202012Validator(schema).validate(artifact)
