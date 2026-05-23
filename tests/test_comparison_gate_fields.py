"""Tests for the Phase 4.A comparison-script wiring follow-up.

PR #237 added the schema fields but the comparison script never
populated them. These tests defend the wiring: every field is
computed when its source artifact is present; every field gracefully
returns ``None`` when the source artifact is absent (so a comparison
on a source whose gate has not run yet still produces a valid
comparison_result artifact).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import compare_opus_haiku as cmp  # noqa: E402


# --------------------------------------------------------------------------
# Fixture builders — small enough to keep the test logic readable.
# --------------------------------------------------------------------------
def _baseline_rows(decision_texts: list[str]) -> list[dict]:
    """Build a fake Opus baseline JSONL row list with N decisions."""
    return [
        {
            "extraction_type": "decisions",
            "ground_truth_text": text,
            "pair_id": f"p-{i}",
            "source_id": "src",
            "source_artifact_id": "00000000-0000-0000-0000-000000000000",
            "schema_version": "1.4.0",
            "chunking_strategy_version": "speaker_turn_v1",
        }
        for i, text in enumerate(decision_texts)
    ]


def _raw_extraction_artifact(decision_texts: list[str]) -> dict:
    """Build a fake meeting_minutes__*.json envelope with N decisions."""
    return {
        "artifact_type": "meeting_minutes",
        "schema_version": "1.4.0",
        "payload": {
            "title": "x",
            "summary": "",
            "decisions": [{"text": t} for t in decision_texts],
            "action_items": [],
            "open_questions": [],
            "provenance": {
                "produced_by": "meeting_minutes_llm",
                "model_id": "claude-haiku-4-5",
            },
        },
    }


def _grounded_items_artifact(decision_texts: list[str]) -> dict:
    """Build a fake grounded_items__*.json artifact with N decisions."""
    return {
        "artifact_type": "grounded_meeting_minutes",
        "schema_version": "1.5.0",
        "source_id": "src",
        "run_id": "r1",
        "gate_passed": True,
        "payload": {
            "title": "x",
            "summary": "",
            "decisions": [{"text": t} for t in decision_texts],
            "action_items": [],
            "open_questions": [],
            "provenance": {
                "produced_by": "meeting_minutes_llm",
                "model_id": "claude-haiku-4-5",
            },
        },
    }


def _gate_result_artifact(
    *,
    total: int,
    grounded: int,
    ungrounded: int,
    drop_rate: float,
    legacy_exempt: int = 0,
) -> dict:
    """Build a fake grounding_gate_result__*.json artifact."""
    return {
        "artifact_type": "grounding_gate_result",
        "schema_version": "1.0.0",
        "source_id": "src",
        "run_id": "r1",
        "trace_id": None,
        "extraction_artifact_path": "x",
        "passed": ungrounded == 0,
        "total_items": total,
        "grounded_count": grounded,
        "ungrounded_count": ungrounded,
        "gate_drop_rate": drop_rate,
        "legacy_exempt_count": legacy_exempt,
        "failures": [],
        "warnings": [],
    }


def _write_meeting_artifacts(
    meeting_dir: Path,
    *,
    raw: dict | None = None,
    grounded: dict | None = None,
    gate_result: dict | None = None,
) -> None:
    """Write each provided artifact under ``meeting_dir`` with the
    canonical filename prefix the script's globs look for."""
    meeting_dir.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        (meeting_dir / "meeting_minutes__abc.json").write_text(
            json.dumps(raw, sort_keys=True), encoding="utf-8"
        )
    if grounded is not None:
        (meeting_dir / "grounded_items__r1.json").write_text(
            json.dumps(grounded, sort_keys=True), encoding="utf-8"
        )
    if gate_result is not None:
        (meeting_dir / "grounding_gate_result__r1.json").write_text(
            json.dumps(gate_result, sort_keys=True), encoding="utf-8"
        )


def _data_lake_root(tmp_path: Path, source_id: str = "src") -> tuple[Path, Path]:
    data_lake = tmp_path / "data-lake"
    meeting_dir = (
        data_lake / "store" / "processed" / "meetings" / source_id
    )
    meeting_dir.mkdir(parents=True, exist_ok=True)
    return data_lake, meeting_dir


# --------------------------------------------------------------------------
# _find_latest_artifact
# --------------------------------------------------------------------------
def test_find_latest_artifact_returns_most_recent(tmp_path: Path) -> None:
    """Two matching files: the one with the newer mtime wins."""
    older = tmp_path / "meeting_minutes__old.json"
    newer = tmp_path / "meeting_minutes__new.json"
    older.write_text("{}", encoding="utf-8")
    newer.write_text("{}", encoding="utf-8")
    # Force the mtimes apart so the test does not depend on
    # filesystem-dependent ordering of two files written in the same
    # tick.
    import os, time
    os.utime(older, (time.time() - 100, time.time() - 100))
    os.utime(newer, (time.time(), time.time()))
    result = cmp._find_latest_artifact(tmp_path, "meeting_minutes__*.json")
    assert result == newer


def test_find_latest_artifact_returns_none_when_no_match(
    tmp_path: Path,
) -> None:
    """No matching file → None, not a halt."""
    assert (
        cmp._find_latest_artifact(tmp_path, "meeting_minutes__*.json")
        is None
    )


def test_find_latest_artifact_returns_none_when_base_dir_missing(
    tmp_path: Path,
) -> None:
    """Non-existent base dir → None (graceful degrade)."""
    missing = tmp_path / "does-not-exist"
    assert (
        cmp._find_latest_artifact(missing, "meeting_minutes__*.json")
        is None
    )


# --------------------------------------------------------------------------
# _compute_phase_4a_fields — pre-gate fields
# --------------------------------------------------------------------------
def test_pre_gate_fields_populated_when_raw_extraction_present(
    tmp_path: Path,
) -> None:
    """A raw extraction on disk produces pre_gate_* numbers."""
    data_lake, meeting_dir = _data_lake_root(tmp_path)
    _write_meeting_artifacts(
        meeting_dir,
        raw=_raw_extraction_artifact(
            ["alpha decision", "beta decision", "gamma decision"]
        ),
    )
    baseline = _baseline_rows(["alpha decision", "beta decision"])
    fields = cmp._compute_phase_4a_fields(
        data_lake=data_lake,
        source_id="src",
        baseline_rows=baseline,
        gt_pairs=None,
        types=cmp.extraction_types(),
    )
    assert fields["pre_gate_haiku_count"] == 3
    # 2 matched / 3 candidate items → precision = 2/3
    assert fields["pre_gate_haiku_precision"] == pytest.approx(2 / 3)
    # 2 matched / 2 opus → recall = 1.0
    assert fields["pre_gate_haiku_recall"] == pytest.approx(1.0)
    assert fields["pre_gate_haiku_f1"] is not None
    assert fields["pre_gate_haiku_f1"] > 0.0


def test_pre_gate_fields_none_when_raw_extraction_absent(
    tmp_path: Path,
) -> None:
    """No raw extraction on disk → pre_gate_* stays None."""
    data_lake, _ = _data_lake_root(tmp_path)
    fields = cmp._compute_phase_4a_fields(
        data_lake=data_lake,
        source_id="src",
        baseline_rows=_baseline_rows(["alpha"]),
        gt_pairs=None,
        types=cmp.extraction_types(),
    )
    assert fields["pre_gate_haiku_count"] is None
    assert fields["pre_gate_haiku_f1"] is None
    assert fields["pre_gate_haiku_precision"] is None
    assert fields["pre_gate_haiku_recall"] is None


# --------------------------------------------------------------------------
# _compute_phase_4a_fields — post-gate fields
# --------------------------------------------------------------------------
def test_post_gate_fields_use_grounded_artifact_when_present(
    tmp_path: Path,
) -> None:
    """The grounded artifact (smaller, gated) drives post_gate_*."""
    data_lake, meeting_dir = _data_lake_root(tmp_path)
    _write_meeting_artifacts(
        meeting_dir,
        raw=_raw_extraction_artifact(
            ["alpha", "beta", "gamma", "delta"]
        ),
        grounded=_grounded_items_artifact(["alpha", "beta"]),
    )
    baseline = _baseline_rows(["alpha", "beta"])
    fields = cmp._compute_phase_4a_fields(
        data_lake=data_lake,
        source_id="src",
        baseline_rows=baseline,
        gt_pairs=None,
        types=cmp.extraction_types(),
    )
    # Pre-gate sees 4 items; post-gate sees the 2 grounded ones.
    assert fields["pre_gate_haiku_count"] == 4
    assert fields["post_gate_haiku_count"] == 2
    # Post-gate precision = 2/2 = 1.0; recall = 2/2 = 1.0
    assert fields["post_gate_haiku_precision"] == pytest.approx(1.0)
    assert fields["post_gate_haiku_recall"] == pytest.approx(1.0)
    assert fields["post_gate_haiku_f1"] == pytest.approx(1.0)


def test_post_gate_fields_none_when_grounded_absent(
    tmp_path: Path,
) -> None:
    """No grounded_items artifact → post_gate_* stays None."""
    data_lake, meeting_dir = _data_lake_root(tmp_path)
    _write_meeting_artifacts(
        meeting_dir, raw=_raw_extraction_artifact(["alpha"])
    )
    fields = cmp._compute_phase_4a_fields(
        data_lake=data_lake,
        source_id="src",
        baseline_rows=_baseline_rows(["alpha"]),
        gt_pairs=None,
        types=cmp.extraction_types(),
    )
    assert fields["post_gate_haiku_count"] is None
    assert fields["post_gate_haiku_f1"] is None
    assert fields["recall_collapse_warning"] is None


# --------------------------------------------------------------------------
# Gate accounting fields
# --------------------------------------------------------------------------
def test_gate_drop_rate_computed_from_gate_result_artifact(
    tmp_path: Path,
) -> None:
    """grounded_count / ungrounded_count / gate_drop_rate are read
    from grounding_gate_result__*.json."""
    data_lake, meeting_dir = _data_lake_root(tmp_path)
    _write_meeting_artifacts(
        meeting_dir,
        gate_result=_gate_result_artifact(
            total=10, grounded=7, ungrounded=3, drop_rate=0.30
        ),
    )
    fields = cmp._compute_phase_4a_fields(
        data_lake=data_lake,
        source_id="src",
        baseline_rows=[],
        gt_pairs=None,
        types=cmp.extraction_types(),
    )
    assert fields["grounded_count"] == 7
    assert fields["ungrounded_count"] == 3
    assert fields["gate_drop_rate"] == pytest.approx(0.30)
    assert fields["legacy_exempt_count"] == 0


def test_gate_accounting_none_when_gate_result_absent(
    tmp_path: Path,
) -> None:
    """No grounding_gate_result__*.json → fields stay None."""
    data_lake, _ = _data_lake_root(tmp_path)
    fields = cmp._compute_phase_4a_fields(
        data_lake=data_lake,
        source_id="src",
        baseline_rows=[],
        gt_pairs=None,
        types=cmp.extraction_types(),
    )
    assert fields["grounded_count"] is None
    assert fields["ungrounded_count"] is None
    assert fields["gate_drop_rate"] is None
    assert fields["legacy_exempt_count"] is None


# --------------------------------------------------------------------------
# recall_collapse_warning thresholding
# --------------------------------------------------------------------------
def _post_gate_recall_setup(
    tmp_path: Path, baseline_n: int, grounded_n: int
) -> dict[str, Any]:
    """Force a specific post-gate recall by sizing the inputs."""
    data_lake, meeting_dir = _data_lake_root(tmp_path)
    baseline_texts = [f"item-{i}" for i in range(baseline_n)]
    grounded_texts = [f"item-{i}" for i in range(grounded_n)]
    _write_meeting_artifacts(
        meeting_dir,
        raw=_raw_extraction_artifact(baseline_texts),
        grounded=_grounded_items_artifact(grounded_texts),
    )
    return cmp._compute_phase_4a_fields(
        data_lake=data_lake,
        source_id="src",
        baseline_rows=_baseline_rows(baseline_texts),
        gt_pairs=None,
        types=cmp.extraction_types(),
    )


def test_recall_collapse_warning_true_when_recall_below_0_5(
    tmp_path: Path,
) -> None:
    """post_gate_haiku_recall < 0.50 → recall_collapse_warning True."""
    # 10 baseline items, 4 grounded → recall = 4/10 = 0.40
    fields = _post_gate_recall_setup(tmp_path, baseline_n=10, grounded_n=4)
    assert fields["post_gate_haiku_recall"] == pytest.approx(0.40)
    assert fields["recall_collapse_warning"] is True


def test_recall_collapse_warning_false_when_recall_above_0_5(
    tmp_path: Path,
) -> None:
    """post_gate_haiku_recall >= 0.50 → recall_collapse_warning False."""
    # 10 baseline items, 7 grounded → recall = 7/10 = 0.70
    fields = _post_gate_recall_setup(tmp_path, baseline_n=10, grounded_n=7)
    assert fields["post_gate_haiku_recall"] == pytest.approx(0.70)
    assert fields["recall_collapse_warning"] is False


def test_recall_collapse_warning_none_when_post_gate_not_computable(
    tmp_path: Path,
) -> None:
    """No grounded artifact → recall_collapse_warning None (not False)."""
    data_lake, meeting_dir = _data_lake_root(tmp_path)
    _write_meeting_artifacts(
        meeting_dir, raw=_raw_extraction_artifact(["alpha"])
    )
    fields = cmp._compute_phase_4a_fields(
        data_lake=data_lake,
        source_id="src",
        baseline_rows=_baseline_rows(["alpha"]),
        gt_pairs=None,
        types=cmp.extraction_types(),
    )
    assert fields["recall_collapse_warning"] is None


# --------------------------------------------------------------------------
# Graceful degradation — comparison still emits a valid artifact
# --------------------------------------------------------------------------
def test_build_comparison_artifact_with_no_phase_4a_fields_validates() -> None:
    """No gate has ever run for this source → the artifact still
    validates against the comparison_result schema."""
    haiku_artifact = _raw_extraction_artifact(["alpha"])
    baseline_rows = _baseline_rows(["alpha"])
    metrics = cmp.compute_comparison(
        baseline_rows=baseline_rows,
        haiku_payload=haiku_artifact["payload"],
        gt_pairs=None,
        types=cmp.extraction_types(),
    )
    artifact = cmp.build_comparison_artifact(
        source_id="src",
        haiku_artifact=haiku_artifact,
        baseline_rows=baseline_rows,
        metrics=metrics,
        compared_at="2026-05-23T00:00:00+00:00",
        phase_4a_fields=None,
    )
    # No gate fields should have been stamped on.
    for key in (
        "pre_gate_haiku_count",
        "post_gate_haiku_count",
        "grounded_count",
        "ungrounded_count",
    ):
        assert key not in artifact

    import jsonschema
    schema = json.loads(
        (
            REPO_ROOT
            / "src"
            / "spectrum_systems_core"
            / "schemas"
            / "comparison_result.schema.json"
        ).read_text(encoding="utf-8")
    )
    jsonschema.validate(artifact, schema)


def test_build_comparison_artifact_with_all_phase_4a_fields_validates() -> None:
    """Every Phase 4.A field set → artifact validates."""
    haiku_artifact = _raw_extraction_artifact(["alpha"])
    baseline_rows = _baseline_rows(["alpha"])
    metrics = cmp.compute_comparison(
        baseline_rows=baseline_rows,
        haiku_payload=haiku_artifact["payload"],
        gt_pairs=None,
        types=cmp.extraction_types(),
    )
    fields = {
        "pre_gate_haiku_count": 50,
        "pre_gate_haiku_f1": 0.55,
        "pre_gate_haiku_precision": 0.50,
        "pre_gate_haiku_recall": 0.60,
        "post_gate_haiku_count": 38,
        "post_gate_haiku_f1": 0.68,
        "post_gate_haiku_precision": 0.75,
        "post_gate_haiku_recall": 0.62,
        "grounded_count": 38,
        "ungrounded_count": 12,
        "gate_drop_rate": 0.24,
        "legacy_exempt_count": 0,
        "recall_collapse_warning": False,
    }
    artifact = cmp.build_comparison_artifact(
        source_id="src",
        haiku_artifact=haiku_artifact,
        baseline_rows=baseline_rows,
        metrics=metrics,
        compared_at="2026-05-23T00:00:00+00:00",
        phase_4a_fields=fields,
    )
    for k, v in fields.items():
        assert artifact[k] == v

    import jsonschema
    schema = json.loads(
        (
            REPO_ROOT
            / "src"
            / "spectrum_systems_core"
            / "schemas"
            / "comparison_result.schema.json"
        ).read_text(encoding="utf-8")
    )
    jsonschema.validate(artifact, schema)


# --------------------------------------------------------------------------
# Byte-equal invariant — _find_latest_artifact source string identical
# --------------------------------------------------------------------------
def test_find_latest_artifact_source_byte_identical_between_scripts() -> None:
    """Extends PR #233's byte-equal invariant:
    ``_find_latest_artifact`` MUST have identical source text in
    ``compare_opus_haiku.py`` and ``create_opus_reference_baselines.py``.
    A drift would let the comparison script and the baseline producer
    resolve "the latest artifact" differently, re-introducing the same
    class of asymmetric-reader bugs the per-type maps already guard
    against.
    """
    import inspect

    import create_opus_reference_baselines as crb  # noqa: WPS433

    src1 = inspect.getsource(cmp._find_latest_artifact)
    src2 = inspect.getsource(crb._find_latest_artifact)
    assert src1 == src2, (
        "_find_latest_artifact source diverged between scripts:\n"
        f"compare_opus_haiku.py:\n{src1}\n\n"
        f"create_opus_reference_baselines.py:\n{src2}"
    )


def test_find_latest_artifact_behaviour_byte_identical_between_scripts(
    tmp_path: Path,
) -> None:
    """The two helpers must agree at run time for an identical input.

    Asserted at module import time AND at call time so a future
    refactor that moves the function to a shared utility (renaming
    one of the copies) still has its semantics pinned.
    """
    import create_opus_reference_baselines as crb  # noqa: WPS433

    (tmp_path / "meeting_minutes__a.json").write_text("{}", encoding="utf-8")
    (tmp_path / "meeting_minutes__b.json").write_text("{}", encoding="utf-8")
    (tmp_path / "other__x.json").write_text("{}", encoding="utf-8")
    assert cmp._find_latest_artifact(
        tmp_path, "meeting_minutes__*.json"
    ) == crb._find_latest_artifact(tmp_path, "meeting_minutes__*.json")
    assert cmp._find_latest_artifact(
        tmp_path, "missing__*.json"
    ) == crb._find_latest_artifact(tmp_path, "missing__*.json")
