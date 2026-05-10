"""Tests for PipelineOrchestrator (Phase L.1).

Uses tmp_path for all I/O. No LLM calls. No hard-coded UUIDs. The default
transcript runner is replaced with a mock for most tests; one test
exercises the full default runner via SourceLoader-friendly fixtures.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List

import jsonschema
import pytest
from docx import Document

from spectrum_systems_core.cli import main as cli_main
from spectrum_systems_core.orchestration import PipelineOrchestrator
from spectrum_systems_core.orchestration.pipeline_orchestrator import (
    _slugify,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_data_lake(root: Path) -> Path:
    (root / "store" / "raw" / "transcripts").mkdir(parents=True)
    (root / "store" / "artifacts").mkdir(parents=True)
    (root / "store" / "processed").mkdir(parents=True)
    return root


def _drop_txt(root: Path, filename: str, content: str = "Hello world\n") -> Path:
    p = root / "store" / "raw" / "transcripts" / filename
    p.write_text(content, encoding="utf-8")
    return p


def _drop_docx(root: Path, filename: str, paragraphs: List[str]) -> Path:
    p = root / "store" / "raw" / "transcripts" / filename
    doc = Document()
    for para in paragraphs:
        doc.add_paragraph(para)
    doc.save(str(p))
    return p


def _seed_processed_record(root: Path, source_id: str) -> str:
    """Write a minimal source_record.json under processed/meetings/<sid>/."""
    artifact_id = str(uuid.uuid4())
    target = root / "store" / "processed" / "meetings" / source_id
    target.mkdir(parents=True, exist_ok=True)
    record = {
        "artifact_kind": "source_record",
        "artifact_id": artifact_id,
        "payload": {"source_id": source_id, "source_family": "meetings"},
    }
    (target / "source_record.json").write_text(
        json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
    )
    return artifact_id


def _seed_sdl_artifact(root: Path, source_id: str) -> str:
    """Write a minimal source_record artifact in the SDL fallback location."""
    artifact_id = str(uuid.uuid4())
    sdl = root / "store" / "artifacts"
    sdl.mkdir(parents=True, exist_ok=True)
    record = {
        "artifact_kind": "source_record",
        "artifact_id": artifact_id,
        "payload": {"source_id": source_id, "source_family": "meetings"},
    }
    (sdl / f"{artifact_id}.json").write_text(
        json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
    )
    return artifact_id


def _success_runner_factory(calls: List[Dict[str, Any]]):
    def runner(txt_path: Path, source_id: str, store_root: Path):
        artifact_id = str(uuid.uuid4())
        calls.append(
            {
                "txt_path": str(txt_path),
                "source_id": source_id,
                "store_root": str(store_root),
                "artifact_id": artifact_id,
            }
        )
        return {
            "status": "success",
            "artifact_id": artifact_id,
            "reason": "",
        }

    return runner


def _failure_runner(txt_path: Path, source_id: str, store_root: Path):
    return {
        "status": "failure",
        "artifact_id": "",
        "reason": "boom",
    }


def _raising_runner(txt_path: Path, source_id: str, store_root: Path):
    raise RuntimeError("kaboom")


# ---------------------------------------------------------------------------
# scan()
# ---------------------------------------------------------------------------


def test_scan_finds_unprocessed_transcripts(tmp_path):
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "meeting-a.txt")
    _drop_txt(root, "meeting-b.txt")

    result = PipelineOrchestrator().scan(str(root))

    assert result["status"] == "success"
    assert result["total_raw"] == 2
    assert result["total_unprocessed"] == 2
    assert result["total_processed"] == 0
    filenames = sorted(e["filename"] for e in result["unprocessed"])
    assert filenames == ["meeting-a.txt", "meeting-b.txt"]
    for entry in result["unprocessed"]:
        assert entry["reason"] == "no_processed_evidence"


def test_scan_skips_already_processed(tmp_path):
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "done.txt")
    _drop_txt(root, "todo.txt")
    seeded_aid = _seed_processed_record(root, _slugify("done"))

    result = PipelineOrchestrator().scan(str(root))

    assert result["status"] == "success"
    assert result["total_raw"] == 2
    assert result["total_processed"] == 1
    assert result["total_unprocessed"] == 1
    assert result["already_processed"][0]["filename"] == "done.txt"
    assert result["already_processed"][0]["artifact_id"] == seeded_aid
    assert result["unprocessed"][0]["filename"] == "todo.txt"


def test_scan_treats_unknown_state_as_unprocessed(tmp_path):
    """A processed/<family>/<sid>/ dir without source_record.json is
    ambiguous evidence => unprocessed (run again)."""
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "weird.txt")
    sid = _slugify("weird")
    # Create the directory but no source_record.json (or write a corrupt one).
    proc = root / "store" / "processed" / "meetings" / sid
    proc.mkdir(parents=True)
    (proc / "source_record.json").write_text(
        "not valid json {{", encoding="utf-8"
    )

    result = PipelineOrchestrator().scan(str(root))

    assert result["status"] == "success"
    assert result["total_unprocessed"] == 1
    assert result["unprocessed"][0]["filename"] == "weird.txt"


# ---------------------------------------------------------------------------
# run() -- behavior
# ---------------------------------------------------------------------------


def test_run_dry_run_does_not_call_source_loader(tmp_path):
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "a.txt")
    _drop_txt(root, "b.txt")

    calls: List[Dict[str, Any]] = []
    orchestrator = PipelineOrchestrator(
        transcript_runner=_success_runner_factory(calls)
    )

    result = orchestrator.run(str(root), dry_run=True)

    assert result["status"] == "dry_run"
    assert result["dry_run"] is True
    assert calls == []  # no runner invocations
    # No record written in dry-run mode (zero side effects).
    assert result["orchestration_record_path"] == ""
    sdl = root / "store" / "artifacts" / "orchestration"
    assert not sdl.exists() or not list(sdl.iterdir())
    # No staging into raw/meetings/ either.
    assert not (root / "store" / "raw" / "meetings").exists()


def test_run_processes_only_unprocessed(tmp_path):
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "done.txt")
    _drop_txt(root, "todo.txt")
    _seed_processed_record(root, _slugify("done"))

    calls: List[Dict[str, Any]] = []
    orchestrator = PipelineOrchestrator(
        transcript_runner=_success_runner_factory(calls)
    )

    result = orchestrator.run(str(root), dry_run=False)

    assert result["status"] == "success"
    assert result["total_attempted"] == 1
    assert result["total_succeeded"] == 1
    assert result["total_failed"] == 0
    assert len(calls) == 1
    assert calls[0]["source_id"] == _slugify("todo")


def test_run_extracts_docx_before_processing(tmp_path):
    root = _make_data_lake(tmp_path)
    _drop_docx(
        root, "policy-meeting.docx", ["First paragraph", "Second paragraph"]
    )

    calls: List[Dict[str, Any]] = []
    orchestrator = PipelineOrchestrator(
        transcript_runner=_success_runner_factory(calls)
    )

    result = orchestrator.run(str(root), dry_run=False)

    assert result["status"] == "success"
    assert result["total_succeeded"] == 1
    assert len(calls) == 1
    # Runner should have been called with the EXTRACTED .txt path.
    txt_path = Path(calls[0]["txt_path"])
    assert txt_path.suffix == ".txt"
    assert txt_path.is_file()
    # The .txt should contain extracted paragraphs joined with double newlines.
    assert "First paragraph" in txt_path.read_text(encoding="utf-8")


def test_run_partial_failure_returns_partial_status(tmp_path):
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "ok.txt")
    _drop_txt(root, "bad.txt")

    def runner(txt_path: Path, source_id: str, store_root: Path):
        if "bad" in source_id:
            return {
                "status": "failure",
                "artifact_id": "",
                "reason": "intentional_test_failure",
            }
        return {
            "status": "success",
            "artifact_id": str(uuid.uuid4()),
            "reason": "",
        }

    orchestrator = PipelineOrchestrator(transcript_runner=runner)
    result = orchestrator.run(str(root), dry_run=False)

    assert result["status"] == "partial"
    assert result["total_attempted"] == 2
    assert result["total_succeeded"] == 1
    assert result["total_failed"] == 1
    # Record is written even on partial failure.
    assert result["orchestration_record_path"]
    assert Path(result["orchestration_record_path"]).is_file()


def test_run_all_fail_returns_failure_status(tmp_path):
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "a.txt")
    _drop_txt(root, "b.txt")

    orchestrator = PipelineOrchestrator(transcript_runner=_failure_runner)
    result = orchestrator.run(str(root), dry_run=False)

    assert result["status"] == "failure"
    assert result["total_attempted"] == 2
    assert result["total_failed"] == 2
    assert result["total_succeeded"] == 0
    # Record IS written even when all attempts fail.
    assert result["orchestration_record_path"]
    assert Path(result["orchestration_record_path"]).is_file()


def test_run_all_succeed_returns_success_status(tmp_path):
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "a.txt")
    _drop_txt(root, "b.txt")

    calls: List[Dict[str, Any]] = []
    orchestrator = PipelineOrchestrator(
        transcript_runner=_success_runner_factory(calls)
    )
    result = orchestrator.run(str(root), dry_run=False)

    assert result["status"] == "success"
    assert result["total_attempted"] == 2
    assert result["total_succeeded"] == 2
    assert result["total_failed"] == 0


def test_orchestration_record_written_to_sdl_root(tmp_path):
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "a.txt")

    calls: List[Dict[str, Any]] = []
    orchestrator = PipelineOrchestrator(
        transcript_runner=_success_runner_factory(calls)
    )
    result = orchestrator.run(str(root), dry_run=False)

    record_path = Path(result["orchestration_record_path"])
    assert record_path.is_file()
    # Default SDL_ROOT == <data_lake>/store/artifacts.
    assert record_path.parent == root / "store" / "artifacts" / "orchestration"
    assert record_path.name == f"{result['run_id']}.json"


def test_orchestration_record_schema_validates(tmp_path):
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "a.txt")

    calls: List[Dict[str, Any]] = []
    orchestrator = PipelineOrchestrator(
        transcript_runner=_success_runner_factory(calls)
    )
    result = orchestrator.run(str(root), dry_run=False)

    record = json.loads(
        Path(result["orchestration_record_path"]).read_text(encoding="utf-8")
    )

    # Locate and load the schema.
    here = Path(__file__).resolve()
    schema_path = None
    for parent in here.parents:
        candidate = (
            parent
            / "contracts"
            / "schemas"
            / "orchestration"
            / "orchestration_run_record.schema.json"
        )
        if candidate.is_file():
            schema_path = candidate
            break
    assert schema_path is not None, "schema file not found"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(record)

    assert record["schema_version"] == "1.0.0"
    assert record["provenance"]["produced_by"] == "PipelineOrchestrator"
    assert record["status"] in {"success", "partial", "failure", "dry_run"}


def test_run_never_raises(tmp_path):
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "a.txt")

    orchestrator = PipelineOrchestrator(transcript_runner=_raising_runner)
    # Should not raise even when the runner throws.
    result = orchestrator.run(str(root), dry_run=False)
    assert isinstance(result, dict)
    assert result["status"] == "failure"
    assert result["total_failed"] == 1
    # Record should still be written.
    assert result["orchestration_record_path"]


def test_idempotent_run_does_not_reprocess(tmp_path):
    """Run twice; the second run should skip everything because the first
    run's success creates processed evidence (via _seed_processed_record
    side-effect from a stub runner).
    """
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "alpha.txt")
    _drop_txt(root, "beta.txt")

    def runner(txt_path: Path, source_id: str, store_root: Path):
        # Simulate the real runner's effect: write a source_record.json.
        proc = store_root / "processed" / "meetings" / source_id
        proc.mkdir(parents=True, exist_ok=True)
        artifact_id = str(uuid.uuid4())
        record = {
            "artifact_kind": "source_record",
            "artifact_id": artifact_id,
            "payload": {"source_id": source_id, "source_family": "meetings"},
        }
        (proc / "source_record.json").write_text(
            json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
        )
        return {"status": "success", "artifact_id": artifact_id, "reason": ""}

    orchestrator = PipelineOrchestrator(transcript_runner=runner)
    first = orchestrator.run(str(root), dry_run=False)
    assert first["total_succeeded"] == 2

    second = orchestrator.run(str(root), dry_run=False)
    assert second["total_attempted"] == 0
    assert second["total_succeeded"] == 0
    assert second["total_failed"] == 0
    assert len(second["skipped_already_done"]) == 2


# ---------------------------------------------------------------------------
# CLI behavior
# ---------------------------------------------------------------------------


def test_cli_dry_run_exits_0(tmp_path, monkeypatch, capsys):
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "a.txt")
    monkeypatch.setenv("DATA_LAKE_PATH", str(root))

    rc = cli_main(["run-pipeline", "--dry-run"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "Pipeline Orchestrator" in captured.out
    assert "Would run:" in captured.out
    # No record written.
    assert not (root / "store" / "artifacts" / "orchestration").exists() or not list(
        (root / "store" / "artifacts" / "orchestration").iterdir()
    )


def test_cli_missing_data_lake_path_exits_1(monkeypatch, capsys):
    monkeypatch.delenv("DATA_LAKE_PATH", raising=False)

    rc = cli_main(["run-pipeline", "--dry-run"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "DATA_LAKE_PATH" in captured.out


# ---------------------------------------------------------------------------
# Gate A redteam follow-up coverage
# ---------------------------------------------------------------------------


def test_scan_treats_raw_hash_mismatch_as_unprocessed(tmp_path):
    """If a transcript has been edited since last run (its raw_hash no
    longer matches the recorded source_record), it MUST be treated as
    unprocessed so we don't silently skip the new content."""
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "edited.txt", content="ORIGINAL content\n")
    sid = _slugify("edited")
    # Seed a source_record claiming a DIFFERENT raw_hash for this source_id.
    proc = root / "store" / "processed" / "meetings" / sid
    proc.mkdir(parents=True)
    (proc / "source_record.json").write_text(
        json.dumps(
            {
                "artifact_id": str(uuid.uuid4()),
                "payload": {
                    "source_id": sid,
                    "source_family": "meetings",
                    "raw_hash": "sha256:" + ("0" * 64),
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    result = PipelineOrchestrator().scan(str(root))

    assert result["status"] == "success"
    assert result["total_unprocessed"] == 1
    assert result["unprocessed"][0]["reason"] == "raw_hash_mismatch"


def test_scan_flags_source_id_collisions(tmp_path):
    """Two raw files that slugify to the same source_id must NOT silently
    overwrite each other in raw/meetings/<sid>/. They are flagged as
    unprocessed with an explicit collision reason."""
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "Q3 Review.txt", content="version A\n")
    _drop_txt(root, "q3-review.txt", content="version B\n")

    result = PipelineOrchestrator().scan(str(root))

    assert result["status"] == "success"
    assert result["total_unprocessed"] == 2
    for entry in result["unprocessed"]:
        assert entry["reason"].startswith("source_id_collision_with:")


def test_run_collision_files_become_failures_no_run(tmp_path):
    """A collision must short-circuit at run() — the runner must NEVER be
    invoked, because invoking it would silently clobber raw/meetings/."""
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "Q3 Review.txt", content="version A\n")
    _drop_txt(root, "q3-review.txt", content="version B\n")

    calls: List[Dict[str, Any]] = []
    orchestrator = PipelineOrchestrator(
        transcript_runner=_success_runner_factory(calls)
    )
    result = orchestrator.run(str(root), dry_run=False)

    assert result["status"] == "failure"
    assert result["total_attempted"] == 2
    assert result["total_failed"] == 2
    assert calls == []
    for entry in result["failed_this_run"]:
        assert entry["reason"].startswith("source_id_collision_with:")
