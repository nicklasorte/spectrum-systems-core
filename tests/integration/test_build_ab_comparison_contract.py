"""Phase 5 — integration contract for ``scripts/build_ab_comparison.py``.

The CLAUDE.md integration-test rule binds: any script that reads a
pipeline artifact MUST have an integration test that:

  1. Produces artifacts via the real writer (``fixtures.py``
     factories), not hand-rolled dicts.
  2. Writes them to a real temp directory.
  3. Calls the script via ``subprocess.run`` against that directory.
  4. Asserts the on-disk output.

This test creates two promoted ``meeting_minutes`` artifacts (one
"baseline" and one "variant_a") via the real Phase-1
``make_promoted_meeting_minutes_artifact`` factory, places a hand-
authored ``comparison_result`` for each (the only Phase-5-specific
shape the aggregator reads), runs ``build_ab_comparison.py``, and
asserts the produced ``ab_comparison`` JSON carries both rows, the
expected metric values, and the correct winner.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# scripts/ + tests/ on sys.path so fixtures and the validator import.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / "scripts"
_TESTS = _REPO_ROOT / "tests"
for p in (_SCRIPTS, _TESTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from integration.fixtures import (  # type: ignore  # noqa: E402
    make_promoted_meeting_minutes_artifact,
)


def _write_comparison(
    meeting_dir: Path,
    *,
    haiku_artifact_id: str,
    haiku_run_id: str,
    f1: float,
    precision: float,
    recall: float,
    timestamp: str,
) -> Path:
    """Write a minimal comparison_result that the aggregator can read.

    Mirrors the field set ``build_ab_comparison.py`` reaches for
    (``artifact_type``, ``summary`` with the three haiku_* keys,
    ``haiku_artifact_id``, ``haiku_run_id``). Adding more fields would
    over-couple this test to the full comparison schema; the
    aggregator only needs these.
    """
    comparisons_dir = meeting_dir / "comparisons"
    comparisons_dir.mkdir(parents=True, exist_ok=True)
    artifact = {
        "artifact_type": "comparison_result",
        "haiku_artifact_id": haiku_artifact_id,
        "haiku_run_id": haiku_run_id,
        "summary": {
            "haiku_f1_vs_opus": f1,
            "haiku_precision_vs_opus": precision,
            "haiku_recall_vs_opus": recall,
        },
    }
    path = comparisons_dir / f"haiku_vs_opus_{timestamp}.json"
    path.write_text(
        json.dumps(artifact, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _run_script(
    *,
    data_lake: Path,
    source_id: str,
    run_id_baseline: str,
    run_id_variant_a: str,
    run_id_variant_b: str = "",
    run_id_variant_c: str = "",
    out_path: Path,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        str(_SCRIPTS / "build_ab_comparison.py"),
        "--data-lake",
        str(data_lake),
        "--source-id",
        source_id,
        "--run-id-baseline",
        run_id_baseline,
        "--run-id-variant-a",
        run_id_variant_a,
        "--run-id-variant-b",
        run_id_variant_b,
        "--run-id-variant-c",
        run_id_variant_c,
        "--out",
        str(out_path),
    ]
    env = os.environ.copy()
    return subprocess.run(
        cmd, capture_output=True, text=True, env=env, check=False
    )


@pytest.fixture
def lake_root(tmp_path: Path) -> Path:
    """Return the data-lake root that the script expects.

    The CLI takes ``--data-lake <root>``; the script resolves
    artifacts under ``<root>/store/processed/...`` (matching
    compare_opus_haiku and the production layout). The
    ``make_promoted_meeting_minutes_artifact`` factory writes to
    ``<lake_root>/processed/...``, so we point ``lake_root`` at
    ``<root>/store`` and pass ``<root>`` as ``--data-lake``.
    """
    (tmp_path / "store").mkdir()
    return tmp_path


def test_aggregator_combines_baseline_and_variant_a(
    lake_root: Path,
) -> None:
    """End-to-end: real promoted minutes + hand-authored comparisons →
    ab_comparison artifact with both rows populated."""
    source_id = "ab-test-source"
    # Real Phase-1 factory promotes a meeting_minutes artifact for each
    # variant. The factory writes through the actual contract writer,
    # so the trace_id / artifact_id stamping matches production.
    baseline = make_promoted_meeting_minutes_artifact(
        lake_root=lake_root / "store",
        source_id=source_id,
        decisions=["The group approved a baseline decision X."],
    )
    variant_a = make_promoted_meeting_minutes_artifact(
        lake_root=lake_root / "store",
        source_id=source_id,
        decisions=[
            "The group approved a Variant A decision Y.",
            "The group approved a Variant A decision Z.",
        ],
    )
    baseline_artifact = json.loads(Path(baseline).read_text(encoding="utf-8"))
    variant_a_artifact = json.loads(
        Path(variant_a).read_text(encoding="utf-8")
    )
    baseline_run_id = baseline_artifact["trace_id"]
    variant_a_run_id = variant_a_artifact["trace_id"]

    meeting_dir = lake_root / "store" / "processed" / "meetings" / source_id

    # Hand-authored comparison_result per variant. The aggregator reads
    # only the three summary fields plus the haiku_artifact_id /
    # haiku_run_id references.
    _write_comparison(
        meeting_dir,
        haiku_artifact_id=baseline_artifact["artifact_id"],
        haiku_run_id=baseline_run_id,
        f1=0.30,
        precision=0.20,
        recall=0.80,
        timestamp="20250101T000000Z",
    )
    _write_comparison(
        meeting_dir,
        haiku_artifact_id=variant_a_artifact["artifact_id"],
        haiku_run_id=variant_a_run_id,
        f1=0.50,
        precision=0.45,
        recall=0.60,
        timestamp="20250102T000000Z",
    )

    out_path = lake_root / "ab.json"
    cp = _run_script(
        data_lake=lake_root,
        source_id=source_id,
        run_id_baseline=baseline_run_id,
        run_id_variant_a=variant_a_run_id,
        out_path=out_path,
    )
    assert cp.returncode == 0, cp.stderr

    ab = json.loads(out_path.read_text(encoding="utf-8"))
    assert ab["artifact_type"] == "ab_comparison"
    assert ab["source_id"] == source_id
    # Both real-factory variants are populated; B and C left null.
    assert ab["variants"]["baseline"] is not None
    assert ab["variants"]["variant_a"] is not None
    assert ab["variants"]["variant_b"] is None
    assert ab["variants"]["variant_c"] is None
    # Metrics flow through from the comparison_result.
    assert ab["variants"]["baseline"]["f1_vs_opus"] == pytest.approx(0.30)
    assert ab["variants"]["variant_a"]["f1_vs_opus"] == pytest.approx(0.50)
    # Winners — variant_a beats baseline on F1 and precision; baseline
    # still wins on recall.
    assert ab["winner"]["by_f1_vs_opus"] == "variant_a"
    assert ab["winner"]["by_precision"] == "variant_a"
    assert ab["winner"]["by_recall"] == "baseline"
    # No variant carried f1_vs_human; winner picker returns None.
    assert ab["winner"]["by_f1_vs_human"] is None


def test_aggregator_handles_missing_variant_gracefully(lake_root: Path) -> None:
    """If a run_id has no minutes artifact on disk, the row is null."""
    source_id = "ab-test-missing"
    baseline = make_promoted_meeting_minutes_artifact(
        lake_root=lake_root / "store",
        source_id=source_id,
    )
    baseline_artifact = json.loads(Path(baseline).read_text(encoding="utf-8"))
    baseline_run_id = baseline_artifact["trace_id"]
    meeting_dir = lake_root / "store" / "processed" / "meetings" / source_id
    _write_comparison(
        meeting_dir,
        haiku_artifact_id=baseline_artifact["artifact_id"],
        haiku_run_id=baseline_run_id,
        f1=0.30,
        precision=0.20,
        recall=0.80,
        timestamp="20250101T000000Z",
    )
    out_path = lake_root / "ab.json"
    cp = _run_script(
        data_lake=lake_root,
        source_id=source_id,
        run_id_baseline=baseline_run_id,
        run_id_variant_a="run-that-does-not-exist",
        out_path=out_path,
    )
    assert cp.returncode == 0, cp.stderr
    ab = json.loads(out_path.read_text(encoding="utf-8"))
    assert ab["variants"]["baseline"] is not None
    # Missing variant row resolves to null, not a synthetic zero row.
    assert ab["variants"]["variant_a"] is None
    # Winner picker skipped the null row.
    assert ab["winner"]["by_f1_vs_opus"] == "baseline"


def test_aggregator_rejects_bad_data_lake(tmp_path: Path) -> None:
    """The script MUST halt fail-closed when --data-lake doesn't exist.

    Silent acceptance would write an artifact with every variant null
    and an "all variants missing" winner block — a false-success
    output the operator can't distinguish from a real all-missing run.
    """
    out_path = tmp_path / "ab.json"
    cp = _run_script(
        data_lake=tmp_path / "nonexistent",
        source_id="x",
        run_id_baseline="",
        run_id_variant_a="",
        out_path=out_path,
    )
    assert cp.returncode == 2
    assert "not a directory" in cp.stderr
    assert not out_path.exists()
