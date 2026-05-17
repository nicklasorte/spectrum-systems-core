"""Phase O.4 — tests for partial_run_warning + --set-baseline refusal."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from spectrum_systems_core.evals.m4 import EvalRunner

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "schemas"


def _confirmed_pair(source_id: str) -> dict[str, Any]:
    """A confirmed pair that does NOT carry fixture_extracted_items, so the
    runner must look on disk for a meeting_extraction artifact."""
    return {
        "pair_id": str(uuid.uuid4()),
        "status": "confirmed",
        "source_artifact_id": str(uuid.uuid4()),
        "minutes_artifact_id": str(uuid.uuid4()),
        "fixture_source_id": source_id,
        "fixture_minutes_text": "(no inline items)",
    }


def _write_pair(sdl_root: Path, pair: dict[str, Any]) -> Path:
    target = sdl_root / "ground_truth"
    target.mkdir(parents=True, exist_ok=True)
    fp = target / f"{pair['pair_id']}.json"
    fp.write_text(
        json.dumps(pair, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return fp


def _seed_meeting_extraction(sdl_root: Path, source_id: str) -> Path:
    target = sdl_root / "extractions"
    target.mkdir(parents=True, exist_ok=True)
    fp = target / f"{source_id}_meeting_extraction.json"
    fp.write_text(
        json.dumps(
            {
                "artifact_type": "meeting_extraction",
                "schema_version": "1.0.0",
                "source_id": source_id,
                "meeting_extraction_id": str(uuid.uuid4()),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return fp


def test_partial_run_warning_when_extractions_missing(tmp_path: Path) -> None:
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    # 13 confirmed pairs total. 10 have meeting_extraction on disk; 3 do not.
    source_ids = [f"src_{i:02d}" for i in range(13)]
    for sid in source_ids:
        _write_pair(sdl, _confirmed_pair(sid))
    for sid in source_ids[:10]:
        _seed_meeting_extraction(sdl, sid)

    runner = EvalRunner(
        data_lake_path=str(tmp_path),
        sdl_root=str(sdl),
        pipeline_run_id="run-partial",
    )
    result = runner.run()
    assert result["status"] == "completed"
    summary = result["summary"]
    assert summary["partial_run_warning"] is True
    detail = summary["partial_run_detail"]
    assert detail is not None
    assert detail["expected"] == 13
    assert detail["actual"] == 10
    assert len(detail["missing_source_ids"]) == 3
    # The 3 missing are the LAST three source_ids by construction.
    assert set(detail["missing_source_ids"]) == set(source_ids[10:])

    # Implicit-baseline rule must NOT install a baseline on a partial run.
    assert summary["is_baseline"] is False
    assert not (sdl / "evals" / "baseline_eval_summary.json").exists()


def test_set_baseline_refuses_on_partial_run(tmp_path: Path) -> None:
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    source_ids = ["a", "b", "c"]
    for sid in source_ids:
        _write_pair(sdl, _confirmed_pair(sid))
    _seed_meeting_extraction(sdl, "a")  # only one extraction; partial run

    # Stage evals/ up front so the post-run assertion checks an existing
    # directory rather than evaluating to True via the missing-dir branch.
    evals_dir = sdl / "evals"
    evals_dir.mkdir(parents=True, exist_ok=True)

    runner = EvalRunner(
        data_lake_path=str(tmp_path),
        sdl_root=str(sdl),
        pipeline_run_id="run-refusal",
    )
    result = runner.run(set_baseline=True)
    assert result["exit_code"] == 1
    assert result.get("partial_run_warning") is True
    assert "partial_run_warning_blocks_set_baseline" in str(
        result.get("reason", "")
    )
    # No baseline file was written.
    assert not (evals_dir / "baseline_eval_summary.json").exists()
    # No eval_summary should be written either — refusal blocks before
    # the per-pair eval loop.
    assert not list(evals_dir.glob("eval_summary_*.json"))


def test_set_baseline_succeeds_on_complete_run(tmp_path: Path) -> None:
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    source_ids = ["a", "b"]
    for sid in source_ids:
        _write_pair(sdl, _confirmed_pair(sid))
    for sid in source_ids:
        _seed_meeting_extraction(sdl, sid)

    runner = EvalRunner(
        data_lake_path=str(tmp_path),
        sdl_root=str(sdl),
        pipeline_run_id="run-complete",
    )
    result = runner.run(set_baseline=True)
    assert result["exit_code"] == 0
    # Baseline file was installed.
    assert (sdl / "evals" / "baseline_eval_summary.json").is_file()


def test_eval_summary_schema_includes_partial_run_fields() -> None:
    """Schema 1.1.0 must declare the new fields as required."""
    schema = json.loads(
        (CONTRACT_DIR / "eval" / "eval_summary.schema.json")
        .read_text(encoding="utf-8")
    )
    required = schema.get("required", [])
    assert "partial_run_warning" in required
    assert "partial_run_detail" in required
    # Phase W relaxed this from const to enum so 1.1.0 stays valid while
    # also admitting 1.2.0 (hidden_stratification fields).
    sv = schema["properties"]["schema_version"]
    if "const" in sv:
        assert sv["const"] == "1.1.0"
    else:
        assert "1.1.0" in sv.get("enum", [])


def test_zero_confirmed_pairs_does_not_divide_by_zero(tmp_path: Path) -> None:
    """Sev-1 Red-Team scenario #3."""
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    runner = EvalRunner(
        data_lake_path=str(tmp_path),
        sdl_root=str(sdl),
        pipeline_run_id="run-empty",
    )
    result = runner.run()
    assert result["status"] == "completed"
    summary = result["summary"]
    assert summary["partial_run_warning"] is False
    assert summary["partial_run_detail"] == {
        "expected": 0,
        "actual": 0,
        "missing_source_ids": [],
    }


def _write_production_pair(
    sdl_root: Path, source_id: str, source_artifact_id: str
) -> dict[str, Any]:
    """A confirmed production-style pair: no fixture_extracted_items;
    carries the slug source_id field plus a per-run source_artifact_id.
    Returns the pair dict so the test can inspect it."""
    pair = {
        "pair_id": str(uuid.uuid4()),
        "status": "confirmed",
        "source_artifact_id": source_artifact_id,
        "minutes_artifact_id": str(uuid.uuid4()),
        "source_id": source_id,
    }
    _write_pair(sdl_root, pair)
    return pair


def _seed_source_record(
    data_lake: Path, source_id: str, source_artifact_id: str
) -> None:
    """Write a processed-tree source_record.json so the eval gate's
    canonical resolution can find the *current* source_artifact_id."""
    target = data_lake / "store" / "processed" / "meetings" / source_id
    target.mkdir(parents=True, exist_ok=True)
    (target / "source_record.json").write_text(
        json.dumps(
            {
                "artifact_id": source_artifact_id,
                "artifact_type": "source_record",
                "payload": {"source_id": source_id},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def test_set_baseline_allows_stale_pair_sa_ids_when_extraction_present(
    tmp_path: Path,
) -> None:
    """Regression: PR #114 / fix-eval-ground-truth-gate.

    Reproduces ``partial_run_warning_blocks_set_baseline: expected=10
    actual=8 missing=[slug, slug]`` — every ``run-pipeline --force``
    mints a fresh ``source_record.artifact_id``; ground_truth_pairs
    generated against older extractions freeze the older
    ``source_artifact_id``. The eval gate must still recognise the
    current on-disk extraction (matched via the source_record's current
    artifact_id) and must not double-count the same source_id slug in
    the missing list.
    """
    data_lake = tmp_path
    sdl = data_lake / "store" / "artifacts"
    sdl.mkdir(parents=True)
    (sdl / "extractions").mkdir()

    source_id = "7-ghz-downlink-tig-meeting-kickoff---transcript-20251218"
    current_sa = str(uuid.uuid4())
    _seed_source_record(data_lake, source_id, current_sa)
    _seed_meeting_extraction(sdl, current_sa)

    # 10 pairs, each frozen to a different older source_artifact_id;
    # only 8 of those legacy sa_ids still have an extraction file on
    # disk (the other 2 sa_ids were superseded by re-runs).
    legacy_sa_ids = [str(uuid.uuid4()) for _ in range(10)]
    for i, sa in enumerate(legacy_sa_ids):
        _write_production_pair(sdl, source_id, sa)
        if i < 8:
            _seed_meeting_extraction(sdl, sa)

    runner = EvalRunner(
        data_lake_path=str(data_lake),
        sdl_root=str(sdl),
        pipeline_run_id="run-stale-sa-ids",
    )
    result = runner.run(set_baseline=True, source_id_filter=source_id)
    assert result["exit_code"] == 0, result
    summary = result["summary"]
    assert summary["partial_run_warning"] is False
    detail = summary["partial_run_detail"]
    assert detail["expected"] == 10
    assert detail["actual"] == 10
    assert detail["missing_source_ids"] == []


def test_missing_source_ids_is_deduplicated_per_source(tmp_path: Path) -> None:
    """When a source has multiple pairs and zero extractions, the slug
    must appear at most once in ``missing_source_ids``."""
    data_lake = tmp_path
    sdl = data_lake / "store" / "artifacts"
    sdl.mkdir(parents=True)
    (sdl / "extractions").mkdir()
    source_id = "src-with-no-extraction"
    # No source_record, no extraction file. Three pairs all sharing
    # the same source_id slug.
    for _ in range(3):
        _write_production_pair(sdl, source_id, str(uuid.uuid4()))
    runner = EvalRunner(
        data_lake_path=str(data_lake),
        sdl_root=str(sdl),
        pipeline_run_id="run-dedup",
    )
    result = runner.run(set_baseline=True, source_id_filter=source_id)
    assert result["exit_code"] == 1
    detail = result["partial_run_detail"]
    # Three pairs all share the slug — but the slug must appear once.
    assert detail["expected"] == 3
    assert detail["actual"] == 0
    assert detail["missing_source_ids"] == [source_id]
