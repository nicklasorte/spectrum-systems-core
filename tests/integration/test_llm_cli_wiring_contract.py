"""Contract: the ``process-meeting --llm`` CLI wiring.

These tests pin the *wiring only* — no live model call is made. The
two properties that matter for "ready to run the moment credentials
exist":

  1. Fail-closed (the realistic no-key CI state): ``--llm`` with no
     ``ANTHROPIC_API_KEY`` halts pre-run with
     ``reason_code=config_error``, exits non-zero, and writes NOTHING
     to disk. It must NOT silently fall back to the regex extractor.
     Exercised through ``subprocess`` against a real temp lake (the
     CLAUDE.md integration-test requirement).

  2. Success path (key + deterministic stub, no network): the SAME
     code path promotes a ``meeting_minutes`` artifact whose
     ``provenance.produced_by == "meeting_minutes_llm"``, rebuilds the
     artifact index, and writes the LLM ``eval_history.jsonl``
     projection (carrying the GT-coverage row). The stub is the same
     injection seam the existing LLM integration tests use, so the
     real workflow / governed loop runs — only the transport is fixed.

  3. Regression guard: with ``--llm`` absent the regex
     ``process_meeting`` path is taken unchanged and the LLM-only
     ``store/.../eval_history.jsonl`` is never written.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from spectrum_systems_core.data_lake.cli import process_meeting_llm
from tests.llm_stub import (
    DEC18_ACTION_ITEMS,
    DEC18_DECISIONS,
    DEC18_OPEN_QUESTIONS,
    json_stub,
    load_fixture,
)

MEETING_ID = "7-ghz-downlink-tig-meeting-kickoff---transcript-20251218"
DEC18 = load_fixture("dec18_transcript.txt")


def _seed_lake(tmp_path: Path) -> Path:
    """Write a valid raw meeting so ``load_meeting`` succeeds; the
    fail-closed gate must fire AFTER a clean load, not because of it."""
    lake = tmp_path / "lake"
    raw = lake / "raw" / "meetings" / MEETING_ID
    raw.mkdir(parents=True)
    (raw / "transcript.txt").write_text(DEC18, encoding="utf-8")
    (raw / "metadata.json").write_text(
        json.dumps(
            {
                "meeting_id": MEETING_ID,
                "title": "7 GHz Downlink TIG Meeting Kickoff",
                "date": "2025-12-18",
                "source_type": "transcript",
            }
        ),
        encoding="utf-8",
    )
    return lake


def _eval_history_path(lake: Path, source_id: str) -> Path:
    return (
        lake
        / "store"
        / "processed"
        / "meetings"
        / source_id
        / "eval_history.jsonl"
    )


def _promoted_jsons(lake: Path) -> list[Path]:
    proc = lake / "processed" / "meetings" / MEETING_ID
    if not proc.is_dir():
        return []
    return sorted(proc.glob("meeting_minutes__*.json"))


def test_llm_flag_fail_closed_no_key_writes_nothing(tmp_path):
    """``--llm`` + no ANTHROPIC_API_KEY -> rc!=0, config_error, and the
    lake is byte-empty of any product (no regex fallback)."""
    lake = _seed_lake(tmp_path)

    import os

    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "spectrum_systems_core.data_lake.cli",
            "process-meeting",
            "--lake",
            str(lake),
            "--meeting-id",
            MEETING_ID,
            "--llm",
        ],
        capture_output=True,
        text=True,
        env=env,
    )

    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "reason_code=config_error" in proc.stdout, proc.stdout
    # Fail-closed: nothing produced, and NOT a regex artifact either.
    assert _promoted_jsons(lake) == []
    assert not (lake / "indexes" / "meetings" / "artifact_index.jsonl").exists()
    assert not _eval_history_path(lake, MEETING_ID).exists()


def test_llm_flag_success_path_with_stub_persists(tmp_path):
    """Key present + deterministic stub: same wiring promotes the LLM
    artifact, rebuilds the index, and writes the eval_history with the
    GT-coverage row. No network, no real key."""
    lake = _seed_lake(tmp_path)

    rc = process_meeting_llm(
        lake_root=lake,
        meeting_id=MEETING_ID,
        source_id=MEETING_ID,
        client=json_stub(
            decisions=DEC18_DECISIONS,
            action_items=DEC18_ACTION_ITEMS,
            open_questions=DEC18_OPEN_QUESTIONS,
        ),
        env={"ANTHROPIC_API_KEY": "sk-test"},
    )

    assert rc == 0

    promoted = _promoted_jsons(lake)
    assert len(promoted) == 1, promoted
    body = json.loads(promoted[0].read_text(encoding="utf-8"))
    assert body["payload"]["provenance"]["produced_by"] == "meeting_minutes_llm"
    assert body["payload"]["decisions"] == DEC18_DECISIONS
    assert body["payload"]["action_items"] == DEC18_ACTION_ITEMS
    assert body["payload"]["open_questions"] == DEC18_OPEN_QUESTIONS

    assert (lake / "indexes" / "meetings" / "artifact_index.jsonl").exists()

    eh = _eval_history_path(lake, MEETING_ID)
    assert eh.exists()
    rows = [
        json.loads(ln)
        for ln in eh.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    cov = [
        r
        for r in rows
        if r["eval_type"] == "extraction_vs_human_minutes_coverage"
    ]
    assert len(cov) == 1
    assert "coverage_threshold:0.0" in cov[0]["reason_codes"]
    assert all(r["workflow_name"] == "meeting_minutes_llm" for r in rows)


def test_llm_flag_off_leaves_regex_path_unchanged(tmp_path):
    """No ``--llm`` and no governed flag artifact -> the regex
    ``process_meeting`` path runs; the LLM-only eval_history is never
    written."""
    lake = _seed_lake(tmp_path)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "spectrum_systems_core.data_lake.cli",
            "process-meeting",
            "--lake",
            str(lake),
            "--meeting-id",
            MEETING_ID,
        ],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    # The regex process_meeting path writes harness-memory
    # run_history.jsonl under processed/meetings/<id>/; the LLM path
    # never does. The LLM-only eval_history under store/ must be absent.
    # (Promotion of this synthetic fixture is fixture-dependent and not
    # what this guard is about — the path taken is.)
    assert (
        lake / "processed" / "meetings" / MEETING_ID / "run_history.jsonl"
    ).exists()
    assert not _eval_history_path(lake, MEETING_ID).exists()
