"""Tests for ``scripts/wipe_eval_artifacts.py``.

The bug this script defends against: ``reset_stale_baseline.py`` only
scans a fixed list of subdirectories, so eval artifacts written under
other paths (e.g. ``evals/baseline_history/``) survive the reset and
the next ``eval-ground-truth`` run still trips
``partial_run_warning_blocks_set_baseline``.

These tests reproduce the failure shape on disk by writing JSON files
across a mix of nested subdirectories AND files whose only link to the
source is a substring of ``pair_id`` (the existing reset uses ``==``
on ``source_id`` and misses these). The wipe must remove every one.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SOURCE_ID = "7-ghz-downlink-tig-meeting-kickoff---transcript-20251218"
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "wipe_eval_artifacts.py"


def _write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")


def _seed_lake(root: Path) -> dict[str, Path]:
    """Lay out files mimicking the post-reset state described in the task.

    Includes:
      - Global singletons at the top of evals/.
      - Per-run artifacts with the source_id in the document.
      - A nested 'baseline_history/' subdir the legacy reset never looked at.
      - A pair_id substring match (legacy reset uses == on source_id and
        misses this).
      - An unrelated source's files, which MUST survive the wipe.
    """
    evals = root / "store" / "artifacts" / "evals"

    paths = {
        "baseline": evals / "baseline_eval_summary.json",
        "run_count": evals / "eval_run_count.json",
        "summary_match": evals / "eval_summary_run-001.json",
        "gate_match": evals / "gate_decision_run-001.json",
        "result_match": evals / "results" / "result-1.json",
        "alignment_match": evals / "alignment" / "align-1.json",
        # Nested subdirectory the legacy reset never scanned.
        "nested_match": evals / "baseline_history" / "history-001.json",
        # pair_id substring carries the source_id even though source_id
        # is absent on the doc.
        "pair_substring_match": evals / "results" / "result-pair-only.json",
        # Unrelated source -- must survive the wipe.
        "unrelated": evals / "results" / "result-other.json",
        # A nested baseline file that the script must also wipe by name.
        "nested_baseline": evals / "archive" / "baseline_eval_summary.json",
    }

    _write_json(paths["baseline"], {"run_count": 10, "source_id": SOURCE_ID})
    _write_json(paths["run_count"], {"count": 10})
    _write_json(paths["summary_match"], {"source_id": SOURCE_ID})
    _write_json(paths["gate_match"], {"source_id": SOURCE_ID})
    _write_json(paths["result_match"], {"source_id": SOURCE_ID})
    _write_json(paths["alignment_match"], {"source_id": SOURCE_ID})
    _write_json(paths["nested_match"], {"source_id": SOURCE_ID})
    _write_json(
        paths["pair_substring_match"],
        {"pair_id": f"pair--{SOURCE_ID}--candidate-3"},
    )
    _write_json(
        paths["unrelated"],
        {"source_id": "some-other-meeting-20260101"},
    )
    _write_json(
        paths["nested_baseline"],
        {"run_count": 7, "source_id": SOURCE_ID},
    )
    return paths


def _run_script(data_lake: Path, *extra: str) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable,
        str(SCRIPT),
        "--data-lake",
        str(data_lake),
        "--source-id",
        SOURCE_ID,
        *extra,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def test_recursive_wipe_removes_nested_and_substring_matches(tmp_path: Path) -> None:
    paths = _seed_lake(tmp_path)

    result = _run_script(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr

    must_be_gone = [
        "baseline",
        "run_count",
        "summary_match",
        "gate_match",
        "result_match",
        "alignment_match",
        "nested_match",
        "pair_substring_match",
        "nested_baseline",
    ]
    for key in must_be_gone:
        assert not paths[key].exists(), f"{key} should be wiped: {paths[key]}"

    assert paths["unrelated"].exists(), (
        "unrelated source's eval artifacts must survive the wipe"
    )


def test_dry_run_deletes_nothing(tmp_path: Path) -> None:
    paths = _seed_lake(tmp_path)

    result = _run_script(tmp_path, "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "(dry-run; no files deleted)" in result.stdout
    for path in paths.values():
        assert path.exists(), f"dry-run must not delete: {path}"


def test_verify_passes_after_wipe(tmp_path: Path) -> None:
    _seed_lake(tmp_path)
    result = _run_script(tmp_path)

    assert result.returncode == 0
    assert "OK: ready for fresh baseline" in result.stdout
    assert "baseline_eval_summary.json: False (should be False)" in result.stdout
    assert "eval_run_count.json: False (should be False)" in result.stdout


def test_missing_data_lake_returns_2(tmp_path: Path) -> None:
    result = _run_script(tmp_path / "does-not-exist")
    assert result.returncode == 2
    assert "store/artifacts not found" in result.stderr


def test_no_evals_dir_is_clean(tmp_path: Path) -> None:
    # store/artifacts exists, but no evals/ subdir yet. Should be a clean no-op.
    (tmp_path / "store" / "artifacts").mkdir(parents=True)
    result = _run_script(tmp_path)
    assert result.returncode == 0
    assert "OK: ready for fresh baseline" in result.stdout
