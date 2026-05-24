"""Phase 4.C — integration contract for ``scripts/run_cascade_filter.py``.

Drives the script as a real subprocess against a temp data-lake. No
network: a stub ``ANTHROPIC_API_KEY`` is set and the script is
exercised either via ``--disable-cascade`` (no api call) or by
overriding the cascade api_client through a stub script invocation.

Asserts the four-artifact contract from the task spec:

* the cascade writes ``cascade_filtered__<run_id>.json`` (kept items)
* ``cascade_audit__<run_id>.jsonl`` (one line per item decision)
* ``cascade_filter_result__<run_id>.json`` (counts + drop rate)
* ``cascade_bypass_record__<run_id>.json`` only on ``--disable-cascade``

Plus the precondition checks: missing grounded artifact → exit 2,
missing grounding_gate_result → exit 2 (race-condition guard).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "run_cascade_filter.py"

SOURCE_ID = "fixture-cascade-source-id"
RUN_ID = "run-abc"


def _write_chunks_jsonl(path: Path, chunks: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"chunk_id": cid, "text": text}, sort_keys=True)
        for cid, text in sorted(chunks.items())
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_data_lake(
    tmp_path: Path,
    *,
    grounded_items: dict,
    transcript: str = "we will adopt the new propagation method as the baseline",
    chunks: dict[str, str] | None = None,
    write_gate_result: bool = True,
    run_id: str = RUN_ID,
) -> Path:
    data_lake = tmp_path / "data-lake"
    processed = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    raw = data_lake / "store" / "raw" / "meetings" / SOURCE_ID
    processed.mkdir(parents=True)
    raw.mkdir(parents=True)
    (raw / "source.txt").write_text(transcript, encoding="utf-8")
    if chunks is None:
        chunks = {"c1": transcript}
    _write_chunks_jsonl(raw / "chunks.jsonl", chunks)

    grounded_envelope = {
        "artifact_type": "grounded_meeting_minutes",
        "schema_version": "1.5.0",
        "source_id": SOURCE_ID,
        "run_id": run_id,
        "source_extraction_artifact": "store/processed/.../meeting_minutes__abc.json",
        "gate_passed": True,
        "payload": grounded_items,
    }
    (processed / f"grounded_items__{run_id}.json").write_text(
        json.dumps(grounded_envelope, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    if write_gate_result:
        gate_result = {
            "artifact_type": "grounding_gate_result",
            "schema_version": "1.0.0",
            "source_id": SOURCE_ID,
            "run_id": run_id,
            "passed": True,
            "total_items": 1,
            "grounded_count": 1,
            "ungrounded_count": 0,
            "gate_drop_rate": 0.0,
            "failures": [],
            "warnings": [],
        }
        (processed / f"grounding_gate_result__{run_id}.json").write_text(
            json.dumps(gate_result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return data_lake


def _run(
    *argv: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    full_env["PYTHONPATH"] = (
        str(REPO_ROOT / "src") + os.pathsep + full_env.get("PYTHONPATH", "")
    )
    return subprocess.run(
        [sys.executable, str(SCRIPT), *argv],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env=full_env,
    )


# --------------------------------------------------------------------------
# Bypass path — covers the disable-cascade flag end-to-end without a
# real api_client.
# --------------------------------------------------------------------------


def test_script_writes_four_artifacts_on_bypass(tmp_path: Path) -> None:
    data_lake = _build_data_lake(
        tmp_path,
        grounded_items={
            "decisions": [
                {
                    "text": "we will adopt the new propagation method as the baseline",
                    "source_quote": "we will adopt the new propagation method as the baseline",
                    "source_chunk_id": "c1",
                    "grounding_mode": "verbatim",
                    "reason": "explicit decision",
                }
            ]
        },
    )
    result = _run(
        "--source-id", SOURCE_ID,
        "--data-lake", str(data_lake),
        "--run-id", RUN_ID,
        "--operator", "tester",
        "--disable-cascade",
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    processed = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    assert (processed / f"cascade_filtered__{RUN_ID}.json").is_file()
    assert (processed / f"cascade_audit__{RUN_ID}.jsonl").is_file()
    assert (processed / f"cascade_filter_result__{RUN_ID}.json").is_file()
    assert (processed / f"cascade_bypass_record__{RUN_ID}.json").is_file()

    bypass = json.loads(
        (processed / f"cascade_bypass_record__{RUN_ID}.json").read_text()
    )
    assert bypass["artifact_type"] == "cascade_bypass_record"
    assert bypass["operator"] == "tester"
    assert bypass["source_id"] == SOURCE_ID
    assert "filter_model" in bypass


def test_script_does_not_write_bypass_when_cascade_runs(tmp_path: Path) -> None:
    # We exercise the non-bypass path by mocking ANTHROPIC_API_KEY out
    # (script returns a halt-on-call stub). The cascade has zero items
    # to adjudicate, so the api_client is never invoked → cascade runs
    # cleanly without needing a real model.
    data_lake = _build_data_lake(
        tmp_path,
        grounded_items={"decisions": []},  # empty
    )
    env = {"ANTHROPIC_API_KEY": ""}
    result = _run(
        "--source-id", SOURCE_ID,
        "--data-lake", str(data_lake),
        "--run-id", RUN_ID,
        "--operator", "tester",
        env=env,
    )
    assert result.returncode == 0, result.stderr
    processed = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    assert not (processed / f"cascade_bypass_record__{RUN_ID}.json").is_file()
    # The three product artifacts ARE written even for an empty cascade.
    assert (processed / f"cascade_filtered__{RUN_ID}.json").is_file()
    assert (processed / f"cascade_audit__{RUN_ID}.jsonl").is_file()
    assert (processed / f"cascade_filter_result__{RUN_ID}.json").is_file()


# --------------------------------------------------------------------------
# Precondition guards
# --------------------------------------------------------------------------


def test_script_exits_2_when_grounded_artifact_missing(tmp_path: Path) -> None:
    data_lake = tmp_path / "data-lake"
    processed = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    raw = data_lake / "store" / "raw" / "meetings" / SOURCE_ID
    processed.mkdir(parents=True)
    raw.mkdir(parents=True)
    (raw / "source.txt").write_text("hi", encoding="utf-8")
    result = _run(
        "--source-id", SOURCE_ID,
        "--data-lake", str(data_lake),
        "--operator", "tester",
        "--disable-cascade",
    )
    assert result.returncode == 2
    assert "no grounded_items" in result.stderr


def test_script_exits_2_when_gate_result_missing(tmp_path: Path) -> None:
    data_lake = _build_data_lake(
        tmp_path,
        grounded_items={"decisions": []},
        write_gate_result=False,
    )
    result = _run(
        "--source-id", SOURCE_ID,
        "--data-lake", str(data_lake),
        "--run-id", RUN_ID,
        "--operator", "tester",
        "--disable-cascade",
    )
    assert result.returncode == 2
    assert "grounding_gate_result" in result.stderr


def test_script_exits_2_when_data_lake_missing(tmp_path: Path) -> None:
    result = _run(
        "--source-id", SOURCE_ID,
        "--data-lake", str(tmp_path / "no-such-dir"),
        "--operator", "tester",
        "--disable-cascade",
    )
    assert result.returncode == 2


# --------------------------------------------------------------------------
# Selector + run_id behaviour
# --------------------------------------------------------------------------


def test_script_run_id_flag_targets_specific_artifact(tmp_path: Path) -> None:
    data_lake = _build_data_lake(
        tmp_path,
        grounded_items={"decisions": []},
        run_id="targeted-run",
    )
    # Write a second grounded artifact under a different run_id so the
    # content-aware selector would pick the wrong one without --run-id.
    other_envelope = {
        "artifact_type": "grounded_meeting_minutes",
        "schema_version": "1.5.0",
        "source_id": SOURCE_ID,
        "run_id": "other-run",
        "source_extraction_artifact": "store/processed/.../meeting_minutes__other.json",
        "gate_passed": True,
        "payload": {"decisions": []},
    }
    processed = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    (processed / "grounded_items__other-run.json").write_text(
        json.dumps(other_envelope, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # Add the matching gate result so the script does not 2-out.
    (processed / "grounding_gate_result__other-run.json").write_text(
        json.dumps(
            {
                "artifact_type": "grounding_gate_result",
                "schema_version": "1.0.0",
                "source_id": SOURCE_ID,
                "run_id": "other-run",
                "passed": True,
                "total_items": 0,
                "grounded_count": 0,
                "ungrounded_count": 0,
                "gate_drop_rate": 0.0,
                "failures": [],
                "warnings": [],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    result = _run(
        "--source-id", SOURCE_ID,
        "--data-lake", str(data_lake),
        "--run-id", "targeted-run",
        "--operator", "tester",
        "--disable-cascade",
    )
    assert result.returncode == 0, result.stderr
    # The cascade artifacts under "targeted-run" must exist; the
    # "other-run" artifacts must NOT, since we passed --run-id.
    assert (processed / "cascade_filtered__targeted-run.json").is_file()
    assert not (processed / "cascade_filtered__other-run.json").is_file()


def test_script_picks_latest_grounded_via_content_aware_selector(
    tmp_path: Path,
) -> None:
    """When --run-id is omitted the script uses the content-aware selector."""
    data_lake = _build_data_lake(
        tmp_path,
        grounded_items={"decisions": []},
        run_id="older-run",
    )
    # Newer artifact lands second; the selector tiebreaks on name desc
    # when schema_versions and mtimes tie.
    processed = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    newer = {
        "artifact_type": "grounded_meeting_minutes",
        "schema_version": "1.5.0",
        "source_id": SOURCE_ID,
        "run_id": "zzzz-newer-run",
        "source_extraction_artifact": "store/processed/.../meeting_minutes__z.json",
        "gate_passed": True,
        "payload": {"decisions": []},
    }
    (processed / "grounded_items__zzzz-newer-run.json").write_text(
        json.dumps(newer, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (processed / "grounding_gate_result__zzzz-newer-run.json").write_text(
        json.dumps(
            {
                "artifact_type": "grounding_gate_result",
                "schema_version": "1.0.0",
                "source_id": SOURCE_ID,
                "run_id": "zzzz-newer-run",
                "passed": True,
                "total_items": 0,
                "grounded_count": 0,
                "ungrounded_count": 0,
                "gate_drop_rate": 0.0,
                "failures": [],
                "warnings": [],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    result = _run(
        "--source-id", SOURCE_ID,
        "--data-lake", str(data_lake),
        "--operator", "tester",
        "--disable-cascade",
    )
    assert result.returncode == 0, result.stderr
    # The 'zzzz' run sorts after 'older-run' alphabetically, so the
    # selector picks it.
    assert (processed / "cascade_filtered__zzzz-newer-run.json").is_file()


# --------------------------------------------------------------------------
# Audit log integrity
# --------------------------------------------------------------------------


def test_audit_jsonl_has_one_line_per_item(tmp_path: Path) -> None:
    items = [
        {
            "text": f"we will adopt the new propagation method as the baseline {i}",
            "source_quote": f"we will adopt the new propagation method as the baseline {i}",
            "source_chunk_id": "c1",
            "grounding_mode": "verbatim",
            "reason": "fixture",
        }
        for i in range(3)
    ]
    data_lake = _build_data_lake(
        tmp_path,
        grounded_items={"decisions": items},
        chunks={
            "c1": (
                "we will adopt the new propagation method as the baseline 0 "
                "we will adopt the new propagation method as the baseline 1 "
                "we will adopt the new propagation method as the baseline 2"
            )
        },
    )
    result = _run(
        "--source-id", SOURCE_ID,
        "--data-lake", str(data_lake),
        "--run-id", RUN_ID,
        "--operator", "tester",
        "--disable-cascade",
    )
    assert result.returncode == 0
    processed = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    audit = (processed / f"cascade_audit__{RUN_ID}.jsonl").read_text()
    lines = [ln for ln in audit.splitlines() if ln.strip()]
    assert len(lines) == 3
    parsed = [json.loads(ln) for ln in lines]
    for record in parsed:
        assert set(record.keys()) >= {
            "item_index", "extraction_type", "decision", "reason", "original_item"
        }
        assert record["decision"] in {"keep", "drop", "modify"}
