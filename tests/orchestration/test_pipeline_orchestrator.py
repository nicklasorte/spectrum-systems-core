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

    assert record["schema_version"] == "1.3.0"
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


# ---------------------------------------------------------------------------
# Fix B: filter "minutes" filenames out of transcript scan
# ---------------------------------------------------------------------------


def test_minutes_files_filtered_from_transcript_scan(tmp_path):
    """A .docx whose name contains 'minutes' (case-insensitive) must be
    routed to filtered_from_transcripts — never appear in unprocessed
    or already_processed."""
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "regular-meeting.txt")
    _drop_docx(
        root, "Meeting Minutes 20260115.docx", ["Body"]
    )
    _drop_txt(root, "Meeting MINUTES 20260116.txt")

    result = PipelineOrchestrator().scan(str(root))

    assert result["status"] == "success"
    filtered = result.get("filtered_from_transcripts", [])
    filtered_names = sorted(f["filename"] for f in filtered)
    assert filtered_names == [
        "Meeting MINUTES 20260116.txt",
        "Meeting Minutes 20260115.docx",
    ]
    for f in filtered:
        assert "minutes" in f["reason"].lower()
    # The non-minutes transcript is still picked up.
    assert result["total_unprocessed"] == 1
    assert result["unprocessed"][0]["filename"] == "regular-meeting.txt"
    # Filtered files do NOT appear in unprocessed or already_processed.
    all_seen = [
        e["filename"]
        for e in result["unprocessed"] + result["already_processed"]
    ]
    assert "Meeting Minutes 20260115.docx" not in all_seen
    assert "Meeting MINUTES 20260116.txt" not in all_seen


def test_filtered_files_not_counted_as_failures(tmp_path):
    """run() must NEVER turn a filtered file into a failure. The
    orchestration record exposes them via filtered_from_transcripts and
    failed_this_run stays empty when only filtered files exist."""
    root = _make_data_lake(tmp_path)
    _drop_docx(
        root, "Working Group MINUTES 5Mar2026.docx", ["Body"]
    )

    calls: List[Dict[str, Any]] = []
    orchestrator = PipelineOrchestrator(
        transcript_runner=_success_runner_factory(calls)
    )
    result = orchestrator.run(str(root), dry_run=False)

    assert calls == []  # runner never invoked on filtered files
    assert result["total_failed"] == 0
    assert result["failed_this_run"] == []
    assert result["total_attempted"] == 0
    assert len(result["filtered_from_transcripts"]) == 1
    assert (
        result["filtered_from_transcripts"][0]["filename"]
        == "Working Group MINUTES 5Mar2026.docx"
    )

    # The on-disk run record carries filtered_from_transcripts and
    # has a `filtered` results-row.
    record = json.loads(
        Path(result["orchestration_record_path"]).read_text(encoding="utf-8")
    )
    assert len(record["filtered_from_transcripts"]) == 1
    filtered_rows = [r for r in record["results"] if r["status"] == "filtered"]
    assert len(filtered_rows) == 1
    assert filtered_rows[0]["eval_status"] == "not_run"


def test_transcript_files_not_filtered(tmp_path):
    """Files without 'minutes' in the filename are NOT filtered, even
    if they contain other meeting-related words."""
    root = _make_data_lake(tmp_path)
    _drop_docx(
        root, "Meeting Transcript 20260115.docx", ["Body"]
    )
    _drop_txt(root, "weekly-sync-2026-02-01.txt")

    result = PipelineOrchestrator().scan(str(root))

    assert result["filtered_from_transcripts"] == []
    assert result["total_unprocessed"] == 2
    assert result["total_raw"] == 2


def test_orchestration_record_with_filtered_validates_against_schema(tmp_path):
    """Verifies the schema bump (1.2.0) accepts filtered_from_transcripts
    plus a results-row with status='filtered'."""
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "ok.txt")
    _drop_docx(root, "Project MINUTES 20260101.docx", ["Body"])

    calls: List[Dict[str, Any]] = []
    orchestrator = PipelineOrchestrator(
        transcript_runner=_success_runner_factory(calls)
    )
    result = orchestrator.run(str(root), dry_run=False)
    record = json.loads(
        Path(result["orchestration_record_path"]).read_text(encoding="utf-8")
    )

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
    assert schema_path is not None
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(record)
    assert record["schema_version"] == "1.3.0"


# ---------------------------------------------------------------------------
# Phase L.3 — full pipeline orchestration + --force flag
# ---------------------------------------------------------------------------


def _stage_runner_factory(calls: List[Dict[str, Any]], stage: str):
    """Build a stage runner that records its invocations and succeeds."""
    def runner(source_id: str, store_root: Path) -> Dict[str, Any]:
        calls.append({"stage": stage, "source_id": source_id})
        # Write the marker file so subsequent idempotency checks pass.
        processed_dir = store_root / "processed" / "meetings" / source_id
        if stage == "extract_stories":
            (processed_dir / "stories").mkdir(parents=True, exist_ok=True)
            # chunks.jsonl is the artifact-evidence file (Chunker output);
            # candidates.jsonl is the idempotency marker (full-run output).
            (processed_dir / "stories" / "chunks.jsonl").write_text(
                "", encoding="utf-8"
            )
            (processed_dir / "stories" / "candidates.jsonl").write_text(
                "", encoding="utf-8"
            )
        elif stage == "promote_knowledge":
            (processed_dir / "knowledge").mkdir(parents=True, exist_ok=True)
            (processed_dir / "knowledge" / "concepts.jsonl").write_text(
                "", encoding="utf-8"
            )
        elif stage == "extract_claims":
            (processed_dir / "paper").mkdir(parents=True, exist_ok=True)
            (processed_dir / "paper" / "claims.jsonl").write_text(
                "", encoding="utf-8"
            )
        return {"status": "success", "reason": ""}
    return runner


def _stage_failure_runner(reason: str = "stage_boom"):
    def runner(source_id: str, store_root: Path) -> Dict[str, Any]:
        return {"status": "failure", "reason": reason}
    return runner


def _synthesize_runner_factory(calls: List[str]):
    def runner(store_root: Path) -> Dict[str, Any]:
        calls.append(str(store_root))
        return {"status": "success", "reason": ""}
    return runner


def _full_pipeline_orchestrator(
    *,
    transcript_runner=None,
    stage_calls: List[Dict[str, Any]] | None = None,
    synth_calls: List[str] | None = None,
    extract_stories_runner=None,
    promote_knowledge_runner=None,
    extract_claims_runner=None,
    synthesize_runner=None,
):
    """Orchestrator with all stage runners injected (no real AI calls)."""
    if stage_calls is None:
        stage_calls = []
    if synth_calls is None:
        synth_calls = []
    return PipelineOrchestrator(
        transcript_runner=transcript_runner
        or _success_runner_factory([]),
        extract_stories_runner=extract_stories_runner
        or _stage_runner_factory(stage_calls, "extract_stories"),
        promote_knowledge_runner=promote_knowledge_runner
        or _stage_runner_factory(stage_calls, "promote_knowledge"),
        extract_claims_runner=extract_claims_runner
        or _stage_runner_factory(stage_calls, "extract_claims"),
        synthesize_runner=synthesize_runner
        or _synthesize_runner_factory(synth_calls),
    )


def _make_processed_for_runner(store_root: Path, source_id: str) -> None:
    """Create the bare processed/<family>/<sid>/ dir so stage runners can
    write their marker files."""
    (store_root / "processed" / "meetings" / source_id).mkdir(
        parents=True, exist_ok=True
    )


def _success_runner_with_processed(calls: List[Dict[str, Any]]):
    """Stage 1 runner that ALSO creates the processed dir."""
    def runner(txt_path: Path, source_id: str, store_root: Path):
        artifact_id = str(uuid.uuid4())
        calls.append(
            {
                "txt_path": str(txt_path),
                "source_id": source_id,
                "artifact_id": artifact_id,
            }
        )
        _make_processed_for_runner(store_root, source_id)
        return {"status": "success", "artifact_id": artifact_id, "reason": ""}
    return runner


def test_force_flag_reruns_already_processed_transcripts(tmp_path):
    """source_record exists; with force=True the Stage 1 runner is re-invoked."""
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "done.txt")
    _seed_processed_record(root, _slugify("done"))

    s1_calls: List[Dict[str, Any]] = []
    stage_calls: List[Dict[str, Any]] = []
    synth_calls: List[str] = []
    orch = _full_pipeline_orchestrator(
        transcript_runner=_success_runner_with_processed(s1_calls),
        stage_calls=stage_calls,
        synth_calls=synth_calls,
    )

    # Without force: skip Stage 1.
    res = orch.run(str(root), force=False)
    assert s1_calls == []
    assert res["total_attempted"] == 0
    assert len(res["skipped_already_done"]) == 1

    # With force: Stage 1 re-runs.
    res2 = orch.run(str(root), force=True)
    assert len(s1_calls) == 1
    assert s1_calls[0]["source_id"] == _slugify("done")
    assert res2["force"] is True
    assert res2["total_attempted"] == 1
    # Per-result pipeline_stages records "forced" (re-ran due to force).
    forced_results = [
        r for r in res2["results"]
        if r.get("pipeline_stages", {}).get("process_source") == "forced"
    ]
    assert len(forced_results) == 1


def test_force_flag_reruns_story_extraction(tmp_path):
    """Stage 2 marker exists; with force=True extract-stories runs again."""
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "alpha.txt")
    sid = _slugify("alpha")
    _make_processed_for_runner(root / "store", sid)
    # Pre-existing Stage 2 marker.
    (root / "store" / "processed" / "meetings" / sid / "stories").mkdir(
        parents=True, exist_ok=True
    )
    (
        root / "store" / "processed" / "meetings" / sid / "stories" / "candidates.jsonl"
    ).write_text("", encoding="utf-8")

    s1_calls: List[Dict[str, Any]] = []
    stage_calls: List[Dict[str, Any]] = []
    orch = _full_pipeline_orchestrator(
        transcript_runner=_success_runner_with_processed(s1_calls),
        stage_calls=stage_calls,
    )

    # Without force: extract_stories is SKIPPED.
    res1 = orch.run(str(root), force=False)
    stories_calls_1 = [c for c in stage_calls if c["stage"] == "extract_stories"]
    assert stories_calls_1 == []
    s2_results_1 = [r["pipeline_stages"]["extract_stories"] for r in res1["results"]]
    assert "skipped" in s2_results_1

    # With force: extract_stories IS invoked again.
    stage_calls.clear()
    res2 = orch.run(str(root), force=True)
    stories_calls_2 = [c for c in stage_calls if c["stage"] == "extract_stories"]
    assert len(stories_calls_2) == 1
    assert stories_calls_2[0]["source_id"] == sid
    s2_results_2 = [r["pipeline_stages"]["extract_stories"] for r in res2["results"]]
    assert "forced" in s2_results_2


def test_stage2_failure_skips_stages_3_and_4(tmp_path):
    """extract-stories fails → promote-knowledge and extract-claims not attempted."""
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "alpha.txt")

    s1_calls: List[Dict[str, Any]] = []
    stage_calls: List[Dict[str, Any]] = []
    synth_calls: List[str] = []
    orch = _full_pipeline_orchestrator(
        transcript_runner=_success_runner_with_processed(s1_calls),
        stage_calls=stage_calls,
        synth_calls=synth_calls,
        extract_stories_runner=_stage_failure_runner("stories_boom"),
    )

    res = orch.run(str(root), force=False)
    assert len(s1_calls) == 1
    # Stage 3 and 4 runners NEVER called for this transcript.
    assert [c for c in stage_calls if c["stage"] == "promote_knowledge"] == []
    assert [c for c in stage_calls if c["stage"] == "extract_claims"] == []
    # Per-result pipeline_stages reflects failure + not_run.
    pr = [r for r in res["results"] if r["filename"] == "alpha.txt"][0]
    assert pr["pipeline_stages"]["extract_stories"] == "failure"
    assert pr["pipeline_stages"]["promote_knowledge"] == "not_run"
    assert pr["pipeline_stages"]["extract_claims"] == "not_run"
    # Stage 5 NEVER ran (no transcript reached Stage 4 success).
    assert synth_calls == []
    assert res["synthesize_status"] == "skipped"


def test_stage5_synthesize_runs_after_all_transcripts(tmp_path):
    """3 transcripts processed → synthesize runs once at end."""
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "a.txt")
    _drop_txt(root, "b.txt")
    _drop_txt(root, "c.txt")

    s1_calls: List[Dict[str, Any]] = []
    stage_calls: List[Dict[str, Any]] = []
    synth_calls: List[str] = []
    orch = _full_pipeline_orchestrator(
        transcript_runner=_success_runner_with_processed(s1_calls),
        stage_calls=stage_calls,
        synth_calls=synth_calls,
    )

    res = orch.run(str(root), force=False)
    assert len(s1_calls) == 3
    # Each transcript runs Stages 2, 3, 4 once.
    for stage in ("extract_stories", "promote_knowledge", "extract_claims"):
        assert len([c for c in stage_calls if c["stage"] == stage]) == 3
    # Synthesize ran exactly ONCE.
    assert len(synth_calls) == 1
    assert res["synthesize_status"] == "success"


def test_synthesize_skipped_if_no_new_artifacts(tmp_path):
    """All stages skipped (already-done evidence) → synthesize skipped."""
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "done.txt")
    sid = _slugify("done")
    # Seed Stage 1 evidence + every Stage marker file.
    _seed_processed_record(root, sid)
    pd = root / "store" / "processed" / "meetings" / sid
    (pd / "stories").mkdir(parents=True, exist_ok=True)
    (pd / "stories" / "candidates.jsonl").write_text("", encoding="utf-8")
    (pd / "knowledge").mkdir(parents=True, exist_ok=True)
    (pd / "knowledge" / "concepts.jsonl").write_text("", encoding="utf-8")
    (pd / "paper").mkdir(parents=True, exist_ok=True)
    (pd / "paper" / "claims.jsonl").write_text("", encoding="utf-8")

    s1_calls: List[Dict[str, Any]] = []
    stage_calls: List[Dict[str, Any]] = []
    synth_calls: List[str] = []
    orch = _full_pipeline_orchestrator(
        transcript_runner=_success_runner_with_processed(s1_calls),
        stage_calls=stage_calls,
        synth_calls=synth_calls,
    )

    res = orch.run(str(root), force=False)
    # Nothing actually ran.
    assert s1_calls == []
    assert stage_calls == []
    assert synth_calls == []
    assert res["synthesize_status"] == "skipped"
    # All stages SKIPPED for this transcript.
    pr = [r for r in res["results"] if r["filename"] == "done.txt"][0]
    for stage in (
        "process_source",
        "extract_stories",
        "promote_knowledge",
        "extract_claims",
    ):
        assert pr["pipeline_stages"][stage] == "skipped"


def test_orchestration_record_includes_pipeline_stages(tmp_path):
    """Each result row in the on-disk record includes a pipeline_stages dict."""
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "alpha.txt")

    s1_calls: List[Dict[str, Any]] = []
    stage_calls: List[Dict[str, Any]] = []
    synth_calls: List[str] = []
    orch = _full_pipeline_orchestrator(
        transcript_runner=_success_runner_with_processed(s1_calls),
        stage_calls=stage_calls,
        synth_calls=synth_calls,
    )

    res = orch.run(str(root), force=False)
    record = json.loads(
        Path(res["orchestration_record_path"]).read_text(encoding="utf-8")
    )

    assert "force" in record and record["force"] is False
    assert record["synthesize_status"] == "success"
    assert "total_stages_completed" in record
    assert "total_stages_failed" in record
    for r in record["results"]:
        ps = r.get("pipeline_stages", {})
        assert set(ps.keys()) == {
            "process_source",
            "extract_stories",
            "promote_knowledge",
            "extract_claims",
        }
        for v in ps.values():
            assert v in ("success", "skipped", "failure", "forced", "not_run")


def test_force_false_skips_existing_artifacts(tmp_path):
    """Existing artifacts → all stages skipped, no stage runners invoked."""
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "done.txt")
    sid = _slugify("done")
    _seed_processed_record(root, sid)
    pd = root / "store" / "processed" / "meetings" / sid
    (pd / "stories").mkdir(parents=True, exist_ok=True)
    (pd / "stories" / "candidates.jsonl").write_text("", encoding="utf-8")
    (pd / "knowledge").mkdir(parents=True, exist_ok=True)
    (pd / "knowledge" / "themes.jsonl").write_text("", encoding="utf-8")
    (pd / "paper").mkdir(parents=True, exist_ok=True)
    (pd / "paper" / "claims.jsonl").write_text("", encoding="utf-8")

    s1_calls: List[Dict[str, Any]] = []
    stage_calls: List[Dict[str, Any]] = []
    synth_calls: List[str] = []
    orch = _full_pipeline_orchestrator(
        transcript_runner=_success_runner_with_processed(s1_calls),
        stage_calls=stage_calls,
        synth_calls=synth_calls,
    )

    res = orch.run(str(root), force=False)
    assert s1_calls == []  # Stage 1 never invoked
    assert stage_calls == []  # Stages 2-4 never invoked
    assert synth_calls == []  # Stage 5 never invoked
    assert res["synthesize_status"] == "skipped"


def test_force_never_deletes_existing_artifacts(tmp_path):
    """force=True must not delete any pre-existing artifact file."""
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "done.txt")
    sid = _slugify("done")
    prior_aid = _seed_processed_record(root, sid)
    prior_record_path = (
        root / "store" / "processed" / "meetings" / sid / "source_record.json"
    )
    prior_content = prior_record_path.read_text(encoding="utf-8")
    pd = root / "store" / "processed" / "meetings" / sid
    (pd / "stories").mkdir(parents=True, exist_ok=True)
    candidates_path = pd / "stories" / "candidates.jsonl"
    candidates_path.write_text(
        '{"old":"candidate"}\n', encoding="utf-8"
    )

    s1_calls: List[Dict[str, Any]] = []
    stage_calls: List[Dict[str, Any]] = []
    synth_calls: List[str] = []
    # Stage 1 runner that does NOT touch existing source_record.json
    # (real Promoter is content-addressed; new artifact_ids only on
    # changed content). Stage 2 runner overwrites candidates.jsonl
    # by design (existing module behavior, not a delete by orchestrator).
    def s1_no_overwrite(txt_path, source_id, store_root):
        artifact_id = str(uuid.uuid4())
        s1_calls.append({"source_id": source_id, "artifact_id": artifact_id})
        return {"status": "success", "artifact_id": artifact_id, "reason": ""}

    orch = _full_pipeline_orchestrator(
        transcript_runner=s1_no_overwrite,
        stage_calls=stage_calls,
        synth_calls=synth_calls,
    )

    orch.run(str(root), force=True)
    # Prior source_record.json untouched (orchestrator never deletes).
    assert prior_record_path.is_file()
    assert prior_record_path.read_text(encoding="utf-8") == prior_content


def test_stage_4_failure_does_not_skip_other_transcripts(tmp_path):
    """One transcript's Stage 4 failure must not block other transcripts."""
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "good.txt")
    _drop_txt(root, "bad.txt")

    s1_calls: List[Dict[str, Any]] = []
    stage_calls: List[Dict[str, Any]] = []
    synth_calls: List[str] = []

    def claims_runner(source_id, store_root):
        if source_id == _slugify("bad"):
            return {"status": "failure", "reason": "claims_boom"}
        # Good source: write the marker.
        pd = store_root / "processed" / "meetings" / source_id / "paper"
        pd.mkdir(parents=True, exist_ok=True)
        (pd / "claims.jsonl").write_text("", encoding="utf-8")
        stage_calls.append({"stage": "extract_claims", "source_id": source_id})
        return {"status": "success", "reason": ""}

    orch = _full_pipeline_orchestrator(
        transcript_runner=_success_runner_with_processed(s1_calls),
        stage_calls=stage_calls,
        synth_calls=synth_calls,
        extract_claims_runner=claims_runner,
    )

    res = orch.run(str(root), force=False)
    # Both transcripts had Stage 1 invoked.
    assert len(s1_calls) == 2
    # Synthesize ran (the "good" transcript reached Stage 4 success).
    assert len(synth_calls) == 1
    assert res["synthesize_status"] == "success"
    # Per-transcript pipeline_stages: bad shows failure for extract_claims.
    bad = [r for r in res["results"] if r["filename"] == "bad.txt"][0]
    good = [r for r in res["results"] if r["filename"] == "good.txt"][0]
    assert bad["pipeline_stages"]["extract_claims"] == "failure"
    assert good["pipeline_stages"]["extract_claims"] == "success"
    assert res["total_stages_failed"] >= 1


def test_synthesize_skipped_when_all_transcripts_fail_at_stage_2(tmp_path):
    """Sev-1 hazard guard: if every transcript fails at Stage 2, Stage 5
    must NOT run — there is nothing to synthesize."""
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "a.txt")
    _drop_txt(root, "b.txt")

    s1_calls: List[Dict[str, Any]] = []
    stage_calls: List[Dict[str, Any]] = []
    synth_calls: List[str] = []
    orch = _full_pipeline_orchestrator(
        transcript_runner=_success_runner_with_processed(s1_calls),
        stage_calls=stage_calls,
        synth_calls=synth_calls,
        extract_stories_runner=_stage_failure_runner("stories_boom"),
    )

    res = orch.run(str(root), force=False)
    assert synth_calls == []
    assert res["synthesize_status"] == "skipped"


def test_force_synthesize_not_run_when_zero_stage4_success(tmp_path):
    """Sev-1 hazard guard: force=True alone does NOT trigger synthesize
    if no transcript reached Stage 4 success."""
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "a.txt")

    s1_calls: List[Dict[str, Any]] = []
    stage_calls: List[Dict[str, Any]] = []
    synth_calls: List[str] = []
    orch = _full_pipeline_orchestrator(
        transcript_runner=_success_runner_with_processed(s1_calls),
        stage_calls=stage_calls,
        synth_calls=synth_calls,
        extract_stories_runner=_stage_failure_runner("stories_boom"),
    )

    res = orch.run(str(root), force=True)
    assert synth_calls == []
    assert res["synthesize_status"] == "skipped"


def test_cli_force_flag_emits_force_prefix(tmp_path, monkeypatch, capsys):
    """`--force` on the CLI prints the FORCE RE-PROCESS banner."""
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "a.txt")
    monkeypatch.setenv("DATA_LAKE_PATH", str(root))

    rc = cli_main(["run-pipeline", "--dry-run", "--force"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "FORCE RE-PROCESS" in captured.out


# ---------------------------------------------------------------------------
# Artifact-existence verification (fix/orchestrator-stage-status)
# ---------------------------------------------------------------------------


def test_story_extraction_success_detected_by_artifact_existence(tmp_path, capsys):
    """CLI runner returns failure but chunks.jsonl exists → stage = success.

    Mirrors the production bug: Chunker writes chunks.jsonl, StoryExtractor
    fails (API error). The artifact is the evidence, so the stage is
    recorded as success and a `cli_failure_artifact_produced` warning is
    printed.
    """
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "alpha.txt")

    def chunks_then_fail_runner(source_id, store_root):
        # Chunker wrote chunks.jsonl before StoryExtractor failed.
        pd = store_root / "processed" / "meetings" / source_id / "stories"
        pd.mkdir(parents=True, exist_ok=True)
        (pd / "chunks.jsonl").write_text(
            '{"chunk_id": "c1"}\n', encoding="utf-8"
        )
        return {"status": "failure", "reason": "extractor_failed:api_error"}

    s1_calls: List[Dict[str, Any]] = []
    stage_calls: List[Dict[str, Any]] = []
    synth_calls: List[str] = []
    orch = _full_pipeline_orchestrator(
        transcript_runner=_success_runner_with_processed(s1_calls),
        stage_calls=stage_calls,
        synth_calls=synth_calls,
        extract_stories_runner=chunks_then_fail_runner,
    )

    res = orch.run(str(root), force=False)

    sid = _slugify("alpha")
    pr = [r for r in res["results"] if r["filename"] == "alpha.txt"][0]
    assert pr["pipeline_stages"]["extract_stories"] == "success"

    out = capsys.readouterr().out
    assert "extract_stories_artifact_present_despite_cli_failure" in out
    assert sid in out

    # Stage 2 success this run → synthesize was attempted.
    assert len(synth_calls) == 1


def test_story_extraction_failure_when_no_artifact(tmp_path, capsys):
    """CLI runner returns success but chunks.jsonl missing → stage = failure
    with `cli_success_artifact_missing` warning."""
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "alpha.txt")

    def lying_success_runner(source_id, store_root):
        # Runner claims success but writes nothing.
        return {"status": "success", "reason": ""}

    s1_calls: List[Dict[str, Any]] = []
    stage_calls: List[Dict[str, Any]] = []
    synth_calls: List[str] = []
    orch = _full_pipeline_orchestrator(
        transcript_runner=_success_runner_with_processed(s1_calls),
        stage_calls=stage_calls,
        synth_calls=synth_calls,
        extract_stories_runner=lying_success_runner,
    )

    res = orch.run(str(root), force=False)

    sid = _slugify("alpha")
    pr = [r for r in res["results"] if r["filename"] == "alpha.txt"][0]
    assert pr["pipeline_stages"]["extract_stories"] == "failure"
    # Sev-1 guard: Stages 3+4 not attempted because Stage 2 reported failure.
    assert pr["pipeline_stages"]["promote_knowledge"] == "not_run"
    assert pr["pipeline_stages"]["extract_claims"] == "not_run"

    out = capsys.readouterr().out
    assert "extract_stories_artifact_missing_despite_cli_success" in out
    assert sid in out

    # No Stage 2 success this run → synthesize skipped.
    assert synth_calls == []
    assert res["synthesize_status"] == "skipped"


def test_synthesize_runs_when_stories_exist(tmp_path):
    """Stories produced (chunks.jsonl present after Stage 2) → synthesize
    is attempted, even if every downstream stage (3, 4) fails — because
    Stage 2 is the gating signal for synthesize per the new design."""
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "alpha.txt")

    def chunks_only_runner(source_id, store_root):
        pd = store_root / "processed" / "meetings" / source_id / "stories"
        pd.mkdir(parents=True, exist_ok=True)
        (pd / "chunks.jsonl").write_text(
            '{"chunk_id": "c1"}\n', encoding="utf-8"
        )
        return {"status": "success", "reason": ""}

    s1_calls: List[Dict[str, Any]] = []
    stage_calls: List[Dict[str, Any]] = []
    synth_calls: List[str] = []
    orch = _full_pipeline_orchestrator(
        transcript_runner=_success_runner_with_processed(s1_calls),
        stage_calls=stage_calls,
        synth_calls=synth_calls,
        extract_stories_runner=chunks_only_runner,
        promote_knowledge_runner=_stage_failure_runner("knowledge_boom"),
        extract_claims_runner=_stage_failure_runner("claims_boom"),
    )

    res = orch.run(str(root), force=False)

    pr = [r for r in res["results"] if r["filename"] == "alpha.txt"][0]
    assert pr["pipeline_stages"]["extract_stories"] == "success"
    # Synthesize ran on the strength of Stage 2 evidence, even with
    # Stages 3+4 failing.
    assert len(synth_calls) == 1
    assert res["synthesize_status"] == "success"


def test_summary_shows_correct_success_count(tmp_path):
    """13 transcripts each producing chunks.jsonl → succeeded-this-run = 13
    and synthesize fires once. Mirrors the production scenario the fix
    targets."""
    root = _make_data_lake(tmp_path)
    for i in range(13):
        _drop_txt(root, f"transcript-{i:02d}.txt")

    def chunks_then_fail_runner(source_id, store_root):
        # Realistic production failure: chunks written, candidates not.
        pd = store_root / "processed" / "meetings" / source_id / "stories"
        pd.mkdir(parents=True, exist_ok=True)
        (pd / "chunks.jsonl").write_text(
            '{"chunk_id": "c1"}\n', encoding="utf-8"
        )
        return {"status": "failure", "reason": "extractor_failed:api_error"}

    s1_calls: List[Dict[str, Any]] = []
    stage_calls: List[Dict[str, Any]] = []
    synth_calls: List[str] = []
    orch = _full_pipeline_orchestrator(
        transcript_runner=_success_runner_with_processed(s1_calls),
        stage_calls=stage_calls,
        synth_calls=synth_calls,
        extract_stories_runner=chunks_then_fail_runner,
    )

    res = orch.run(str(root), force=False)

    # All 13 attempted, all reach Stage 2 success via artifact-existence.
    assert res["total_attempted"] == 13
    assert res["total_succeeded"] == 13
    assert res["total_failed"] == 0
    stage2_results = [
        r["pipeline_stages"]["extract_stories"] for r in res["results"]
    ]
    assert stage2_results.count("success") == 13
    # Synthesize ran exactly once for the whole batch.
    assert len(synth_calls) == 1
    assert res["synthesize_status"] == "success"


def test_stale_artifact_does_not_mask_runner_failure(tmp_path, capsys):
    """Sev-1 guard: a pre-existing artifact from a prior run does NOT
    count as new evidence when the current run's runner fails."""
    root = _make_data_lake(tmp_path)
    _drop_txt(root, "alpha.txt")
    sid = _slugify("alpha")
    # Seed Stage 1 evidence but NOT the candidates.jsonl idempotency
    # marker — so the orchestrator will re-attempt Stage 2.
    _seed_processed_record(root, sid)
    pd = root / "store" / "processed" / "meetings" / sid / "stories"
    pd.mkdir(parents=True, exist_ok=True)
    # Pre-existing chunks.jsonl from an earlier (incomplete) run.
    (pd / "chunks.jsonl").write_text(
        '{"chunk_id": "stale"}\n', encoding="utf-8"
    )

    def failing_runner_no_write(source_id, store_root):
        # Runner fails without touching chunks.jsonl.
        return {"status": "failure", "reason": "extractor_failed:api_error"}

    s1_calls: List[Dict[str, Any]] = []
    stage_calls: List[Dict[str, Any]] = []
    synth_calls: List[str] = []
    orch = _full_pipeline_orchestrator(
        transcript_runner=_success_runner_with_processed(s1_calls),
        stage_calls=stage_calls,
        synth_calls=synth_calls,
        extract_stories_runner=failing_runner_no_write,
    )

    res = orch.run(str(root), force=False)

    pr = [r for r in res["results"] if r["filename"] == "alpha.txt"][0]
    # Stale chunks.jsonl is NOT enough — Stage 2 still reports failure.
    assert pr["pipeline_stages"]["extract_stories"] == "failure"

    out = capsys.readouterr().out
    assert "extract_stories_stale_artifact_not_treated_as_success" in out

    # No Stage 2 success this run → synthesize skipped.
    assert synth_calls == []
    assert res["synthesize_status"] == "skipped"
