"""Reconciler tests (Phase 2 Step 2.10 + Red Team Pass 2).

The reconciler is documented to:

* Find comparison_result artifacts without a matching invocation log
  and write them to ``reconciliation_gaps.jsonl``.
* Never block — exit code is always 0.
* Tolerate a missing data lake (forked PR / fresh checkout).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import reconcile_invocation_logs as rec  # noqa: E402


def _seed_comparison(dl: Path, sid: str, name: str) -> Path:
    d = dl / "store" / "processed" / "meetings" / sid
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"comparison_result__{name}.json"
    p.write_text(
        json.dumps(
            {
                "artifact_type": "comparison_result",
                "schema_version": "1.0.0",
                "source_id": sid,
                "summary": {},
            }
        ),
        encoding="utf-8",
    )
    return p


def _seed_invocation_log(dl: Path, sid: str, name: str) -> Path:
    d = dl / "store" / "processed" / "meetings" / sid / "diagnostics"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"pipeline_invocation_log__{name}.json"
    p.write_text(
        json.dumps(
            {
                "artifact_type": "pipeline_invocation_log",
                "schema_version": "1.0.0",
                "source_id": sid,
                "invocation_id": name,
            }
        ),
        encoding="utf-8",
    )
    return p


def test_reconciler_returns_empty_for_clean_lake(tmp_path: Path) -> None:
    sid = "mtg-1"
    _seed_comparison(tmp_path, sid, "x")
    _seed_invocation_log(tmp_path, sid, "log-1")
    gaps = rec.reconcile(tmp_path)
    assert gaps == []


def test_reconciler_flags_missing_invocation_log(tmp_path: Path) -> None:
    """Pass-2 gate: synthetically delete a log and assert the gap surfaces."""
    sid = "mtg-2"
    cmp_path = _seed_comparison(tmp_path, sid, "y")
    # No invocation log seeded. The reconciler must surface a gap.
    gaps = rec.reconcile(tmp_path)
    assert len(gaps) == 1
    assert gaps[0]["kind"] == "missing_invocation_log"
    assert gaps[0]["source_id"] == sid
    assert gaps[0]["comparison_artifact_path"] == str(cmp_path)


def test_reconciler_tolerates_missing_data_lake(tmp_path: Path) -> None:
    nonexistent = tmp_path / "no_lake"
    assert rec.reconcile(nonexistent) == []


def test_reconciler_writes_jsonl(tmp_path: Path) -> None:
    sid = "mtg-3"
    _seed_comparison(tmp_path, sid, "z")
    gaps = rec.reconcile(tmp_path)
    out = rec.write_gaps(tmp_path, gaps)
    assert out.is_file()
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["source_id"] == sid


def test_reconciler_cli_exit_code_is_always_zero(tmp_path: Path) -> None:
    """The reconciler does NOT block. Exit code is 0 even when gaps exist."""
    sid = "mtg-4"
    _seed_comparison(tmp_path, sid, "w")
    rc = rec.main(["--data-lake", str(tmp_path)])
    assert rc == 0
