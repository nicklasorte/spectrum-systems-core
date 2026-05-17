"""Phase X2.4 — eval-ground-truth CLI: --specific-source-id, baseline_scope,
baseline_set finding, baseline_requires_successful_run guard."""
from __future__ import annotations

import io
import json
import shutil
from pathlib import Path
from typing import Any

from spectrum_systems_core.cli import eval_ground_truth

FIXTURE_DIR = (
    Path(__file__).resolve().parents[1] / "fixtures" / "eval" / "ground_truth"
)


def _stage_pairs(sdl_root: Path) -> None:
    target = sdl_root / "ground_truth"
    target.mkdir(parents=True, exist_ok=True)
    for path in FIXTURE_DIR.glob("*.json"):
        shutil.copy(path, target / path.name)


def _make_data_lake(tmp_path: Path) -> Path:
    """Build a data_lake-shaped temp dir with store/artifacts/ as sdl_root."""
    sdl_root = tmp_path / "store" / "artifacts"
    sdl_root.mkdir(parents=True, exist_ok=True)
    _stage_pairs(sdl_root)
    return tmp_path


def _read_gate_decision(sdl_root: Path, run_id: str) -> dict[str, Any]:
    return json.loads(
        (sdl_root / "evals" / f"gate_decision_{run_id}.json").read_text(encoding="utf-8")
    )


def _read_summary(sdl_root: Path, run_id: str) -> dict[str, Any]:
    return json.loads(
        (sdl_root / "evals" / f"eval_summary_{run_id}.json").read_text(encoding="utf-8")
    )


def _findings(data_lake: Path) -> list[dict[str, Any]]:
    findings_dir = data_lake / "store" / "artifacts" / "health"
    if not findings_dir.is_dir():
        return []
    out = []
    for path in findings_dir.glob("*.json"):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


# ---- single-source baseline path ----------------------------------


def test_set_baseline_single_transcript_scope(tmp_path: Path) -> None:
    data_lake = _make_data_lake(tmp_path)
    sdl_root = data_lake / "store" / "artifacts"
    rc = eval_ground_truth(
        data_lake=str(data_lake),
        pipeline_run_id="run-X2-1",
        set_baseline=True,
        specific_source_id="fixture-meeting-001",
        out_stream=io.StringIO(),
    )
    assert rc == 0
    summary = _read_summary(sdl_root, "run-X2-1")
    gate = _read_gate_decision(sdl_root, "run-X2-1")
    assert summary["is_baseline"] is True
    assert summary["baseline_scope"] == "single_transcript"
    assert gate["baseline_scope"] == "single_transcript"
    assert gate["baseline_type"] == "development"


def test_set_baseline_full_corpus_when_no_source_filter(tmp_path: Path) -> None:
    data_lake = _make_data_lake(tmp_path)
    sdl_root = data_lake / "store" / "artifacts"
    rc = eval_ground_truth(
        data_lake=str(data_lake),
        pipeline_run_id="run-X2-2",
        set_baseline=True,
        out_stream=io.StringIO(),
    )
    assert rc == 0
    summary = _read_summary(sdl_root, "run-X2-2")
    gate = _read_gate_decision(sdl_root, "run-X2-2")
    assert summary["baseline_scope"] == "full_corpus"
    assert gate["baseline_type"] == "production"


def test_baseline_set_finding_emitted_with_metrics(tmp_path: Path) -> None:
    data_lake = _make_data_lake(tmp_path)
    rc = eval_ground_truth(
        data_lake=str(data_lake),
        pipeline_run_id="run-X2-3",
        set_baseline=True,
        specific_source_id="fixture-meeting-001",
        out_stream=io.StringIO(),
    )
    assert rc == 0
    codes = [f.get("finding_code") for f in _findings(data_lake)]
    assert "baseline_set" in codes
    # The finding must carry coverage/precision/baseline_scope/pairs_count
    # as structured fields -- not prose.
    baseline_finding = next(
        f for f in _findings(data_lake) if f.get("finding_code") == "baseline_set"
    )
    ctx = baseline_finding.get("context", {})
    assert "coverage" in ctx
    assert "precision" in ctx
    assert "baseline_scope" in ctx
    assert ctx["baseline_scope"] == "single_transcript"
    assert "pairs_count" in ctx


def test_filter_narrows_evaluated_pairs(tmp_path: Path) -> None:
    data_lake = _make_data_lake(tmp_path)
    rc = eval_ground_truth(
        data_lake=str(data_lake),
        pipeline_run_id="run-X2-4",
        specific_source_id="fixture-meeting-001",
        out_stream=io.StringIO(),
    )
    assert rc == 0
    sdl_root = data_lake / "store" / "artifacts"
    summary = _read_summary(sdl_root, "run-X2-4")
    # Filtered to one pair (the one fixture pair that resolves to
    # fixture-meeting-001 via fixture_source_id).
    assert summary["pairs_evaluated"] == 1


def test_unknown_source_filter_evaluates_zero(tmp_path: Path) -> None:
    data_lake = _make_data_lake(tmp_path)
    rc = eval_ground_truth(
        data_lake=str(data_lake),
        pipeline_run_id="run-X2-5",
        specific_source_id="not-a-real-source",
        out_stream=io.StringIO(),
    )
    assert rc == 0
    sdl_root = data_lake / "store" / "artifacts"
    summary = _read_summary(sdl_root, "run-X2-5")
    assert summary["pairs_evaluated"] == 0


# ---- failed-run guard ----------------------------------------------


def test_set_baseline_refused_on_failed_orchestration(tmp_path: Path) -> None:
    data_lake = _make_data_lake(tmp_path)
    sdl_root = data_lake / "store" / "artifacts"
    # Plant an orchestration_result with stage_status=failed for the
    # filtered source.
    orch_dir = sdl_root / "orchestration"
    orch_dir.mkdir(parents=True, exist_ok=True)
    (orch_dir / "failed.json").write_text(json.dumps({
        "artifact_type": "orchestration_result",
        "schema_version": "1.0.0",
        "run_id": "prior-run",
        "source_id": "fixture-meeting-001",
        "chunks_attempted": 1,
        "chunks_succeeded": 0,
        "chunks_blocked": 1,
        "block_reasons": {
            "rate_limit_exhausted": 1,
            "empty_response": 0,
            "parse_error": 0,
            "other": 0,
        },
        "stage_status": "failed",
    }), encoding="utf-8")

    rc = eval_ground_truth(
        data_lake=str(data_lake),
        pipeline_run_id="run-X2-6",
        set_baseline=True,
        specific_source_id="fixture-meeting-001",
        out_stream=io.StringIO(),
    )
    assert rc == 1
    codes = [f.get("finding_code") for f in _findings(data_lake)]
    assert "baseline_requires_successful_run" in codes
    # No eval_summary written.
    assert not (sdl_root / "evals" / "eval_summary_run-X2-6.json").is_file()


def test_set_baseline_allows_when_orchestration_ok(tmp_path: Path) -> None:
    data_lake = _make_data_lake(tmp_path)
    sdl_root = data_lake / "store" / "artifacts"
    orch_dir = sdl_root / "orchestration"
    orch_dir.mkdir(parents=True, exist_ok=True)
    (orch_dir / "ok.json").write_text(json.dumps({
        "artifact_type": "orchestration_result",
        "schema_version": "1.0.0",
        "run_id": "prior-ok",
        "source_id": "fixture-meeting-001",
        "chunks_attempted": 1,
        "chunks_succeeded": 1,
        "chunks_blocked": 0,
        "block_reasons": {
            "rate_limit_exhausted": 0,
            "empty_response": 0,
            "parse_error": 0,
            "other": 0,
        },
        "stage_status": "ok",
    }), encoding="utf-8")
    rc = eval_ground_truth(
        data_lake=str(data_lake),
        pipeline_run_id="run-X2-7",
        set_baseline=True,
        specific_source_id="fixture-meeting-001",
        out_stream=io.StringIO(),
    )
    assert rc == 0


# ---- overwrite ---------------------------------------------------


def test_second_set_baseline_overwrites_first(tmp_path: Path) -> None:
    data_lake = _make_data_lake(tmp_path)
    sdl_root = data_lake / "store" / "artifacts"
    rc1 = eval_ground_truth(
        data_lake=str(data_lake),
        pipeline_run_id="run-X2-8a",
        set_baseline=True,
        out_stream=io.StringIO(),
    )
    rc2 = eval_ground_truth(
        data_lake=str(data_lake),
        pipeline_run_id="run-X2-8b",
        set_baseline=True,
        out_stream=io.StringIO(),
    )
    assert rc1 == 0 and rc2 == 0
    baseline = json.loads(
        (sdl_root / "evals" / "baseline_eval_summary.json").read_text(encoding="utf-8")
    )
    # The most recent run's summary is the baseline now.
    assert baseline["pipeline_run_id"] == "run-X2-8b"


# ---- Codex P2: baseline_type preserved across non-baseline runs ----


def test_baseline_type_null_before_any_baseline(tmp_path: Path) -> None:
    """Before any baseline is installed, a non-baseline run should
    report ``baseline_type: null`` -- there is no baseline to label."""
    data_lake = _make_data_lake(tmp_path)
    # Wipe out any auto-installed baseline by NOT running set_baseline
    # and ensuring no baseline file exists yet. The first run installs
    # an implicit baseline (run_count==1), so we ratchet run_count by
    # writing a non-zero count up front.
    sdl_root = data_lake / "store" / "artifacts"
    (sdl_root / "evals").mkdir(parents=True, exist_ok=True)
    (sdl_root / "evals" / "eval_run_count.json").write_text(
        json.dumps({"count": 5}), encoding="utf-8"
    )
    rc = eval_ground_truth(
        data_lake=str(data_lake),
        pipeline_run_id="run-X2-9",
        out_stream=io.StringIO(),
    )
    assert rc == 0
    gate = _read_gate_decision(sdl_root, "run-X2-9")
    assert gate.get("baseline_type") is None


def test_development_baseline_type_preserved_on_followup_runs(
    tmp_path: Path,
) -> None:
    """After --set-baseline --specific-source-id installs a development
    baseline, a subsequent non-baseline run must continue to report
    ``baseline_type: development`` so an operator reading gate_decision
    alone can answer 'what baseline are we comparing against?'.
    Regression test for the Codex P2 finding."""
    data_lake = _make_data_lake(tmp_path)
    sdl_root = data_lake / "store" / "artifacts"
    rc1 = eval_ground_truth(
        data_lake=str(data_lake),
        pipeline_run_id="run-X2-10a",
        set_baseline=True,
        specific_source_id="fixture-meeting-001",
        out_stream=io.StringIO(),
    )
    rc2 = eval_ground_truth(
        data_lake=str(data_lake),
        pipeline_run_id="run-X2-10b",
        out_stream=io.StringIO(),
    )
    assert rc1 == 0 and rc2 == 0
    gate1 = _read_gate_decision(sdl_root, "run-X2-10a")
    gate2 = _read_gate_decision(sdl_root, "run-X2-10b")
    assert gate1["baseline_type"] == "development"
    # The follow-up run must preserve the development label.
    assert gate2["baseline_type"] == "development"


def test_production_baseline_type_preserved_on_followup_runs(
    tmp_path: Path,
) -> None:
    """After a full-corpus baseline is set, follow-up runs must report
    ``baseline_type: production``."""
    data_lake = _make_data_lake(tmp_path)
    sdl_root = data_lake / "store" / "artifacts"
    eval_ground_truth(
        data_lake=str(data_lake),
        pipeline_run_id="run-X2-11a",
        set_baseline=True,
        out_stream=io.StringIO(),
    )
    rc = eval_ground_truth(
        data_lake=str(data_lake),
        pipeline_run_id="run-X2-11b",
        out_stream=io.StringIO(),
    )
    assert rc == 0
    gate = _read_gate_decision(sdl_root, "run-X2-11b")
    assert gate["baseline_type"] == "production"
