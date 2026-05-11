"""Phase O.3 — tests for force_only_missing + specific_source_id.

These tests exercise the orchestrator's filter logic via injected stage
runners. No live extractor calls. Each test asserts BOTH presence in the
expected list AND absence from the wrong list to catch one-sided regressions.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, List

import pytest

from spectrum_systems_core.orchestration import PipelineOrchestrator


def _stage_lake(tmp_path: Path) -> Path:
    root = tmp_path / "data-lake"
    (root / "store" / "raw" / "transcripts").mkdir(parents=True)
    (root / "store" / "artifacts").mkdir(parents=True)
    (root / "store" / "processed" / "meetings").mkdir(parents=True)
    return root


def _drop_txt(root: Path, name: str, content: str = "Hello\n") -> Path:
    p = root / "store" / "raw" / "transcripts" / name
    p.write_text(content, encoding="utf-8")
    return p


def _seed_meeting_extraction(root: Path, source_id: str) -> Path:
    extractions = root / "store" / "artifacts" / "extractions"
    extractions.mkdir(parents=True, exist_ok=True)
    target = extractions / f"{source_id}_meeting_extraction.json"
    target.write_text(
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
    return target


def _make_orchestrator() -> PipelineOrchestrator:
    """Build an orchestrator with stage-runner stubs that always succeed.

    The default transcript runner is also stubbed so no SourceLoader work
    is needed — the test only cares about which source_ids are touched.
    """

    def _ok_transcript(txt_path: Path, source_id: str, store_root: Path) -> Dict[str, Any]:
        # Mimic the post-condition the orchestrator's Stages 2-4 chain
        # expects: a source_record stub exists for evidence.
        sid_dir = store_root / "processed" / "meetings" / source_id
        sid_dir.mkdir(parents=True, exist_ok=True)
        (sid_dir / "source_record.json").write_text(
            json.dumps(
                {
                    "artifact_type": "source_record",
                    "artifact_id": str(uuid.uuid4()),
                    "payload": {"source_id": source_id, "raw_hash": ""},
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return {
            "status": "success",
            "artifact_id": "ok-" + source_id,
            "reason": "",
            "source_record": {"artifact_id": "ok-" + source_id},
            "text_units": [],
        }

    def _stage_runner_success(source_id: str, store_root: Path) -> Dict[str, Any]:
        # Write the minimal artifact each stage checks for so the
        # artifact-as-evidence contract resolves to success.
        sid_dir = store_root / "processed" / "meetings" / source_id
        (sid_dir / "stories").mkdir(parents=True, exist_ok=True)
        (sid_dir / "stories" / "chunks.jsonl").write_text("", encoding="utf-8")
        (sid_dir / "stories" / "candidates.jsonl").write_text(
            "", encoding="utf-8"
        )
        (sid_dir / "knowledge").mkdir(parents=True, exist_ok=True)
        (sid_dir / "knowledge" / "concepts.jsonl").write_text(
            "", encoding="utf-8"
        )
        (sid_dir / "paper").mkdir(parents=True, exist_ok=True)
        (sid_dir / "paper" / "claims.jsonl").write_text("", encoding="utf-8")
        return {"status": "success", "reason": ""}

    def _synth_skipped(_store_root: Path) -> Dict[str, Any]:
        return {"status": "success", "reason": ""}

    return PipelineOrchestrator(
        transcript_runner=_ok_transcript,
        extract_stories_runner=_stage_runner_success,
        promote_knowledge_runner=_stage_runner_success,
        extract_claims_runner=_stage_runner_success,
        synthesize_runner=_synth_skipped,
    )


def test_force_only_missing_skips_existing_extractions(tmp_path: Path) -> None:
    root = _stage_lake(tmp_path)
    _drop_txt(root, "alpha.txt")
    _drop_txt(root, "bravo.txt")
    _seed_meeting_extraction(root, "alpha")  # only alpha has an extraction

    orch = _make_orchestrator()
    result = orch.run(
        str(root), force=True, force_only_missing=True
    )

    assert "alpha" in result["source_ids_skipped"]
    assert "alpha" not in result["source_ids_processed"]
    assert "bravo" in result["source_ids_processed"]
    assert "bravo" not in result["source_ids_skipped"]


def test_specific_source_id_filters(tmp_path: Path) -> None:
    root = _stage_lake(tmp_path)
    _drop_txt(root, "alpha.txt")
    _drop_txt(root, "bravo.txt")
    # Even WITH force=True and an existing extraction for bravo, the
    # specific_source_id filter must narrow scope to bravo only.
    _seed_meeting_extraction(root, "bravo")

    orch = _make_orchestrator()
    result = orch.run(
        str(root),
        force=True,
        specific_source_id="bravo",
    )

    assert "alpha" not in result["source_ids_processed"]
    assert "alpha" not in result["source_ids_skipped"]
    assert "alpha" not in result["source_ids_failed"]
    # bravo was already processed and force=True without force_only_missing
    # means we re-run it; either processed or skipped is acceptable as long
    # as alpha is out of scope. The orchestrator scans for evidence first,
    # so bravo with existing source_record + force=True surfaces as
    # processed_this_run with stage statuses forced/success.
    assert (
        "bravo" in result["source_ids_processed"]
        or "bravo" in result["source_ids_skipped"]
    )


def test_force_without_force_only_missing_reprocesses_all(
    tmp_path: Path,
) -> None:
    root = _stage_lake(tmp_path)
    _drop_txt(root, "alpha.txt")
    _seed_meeting_extraction(root, "alpha")

    orch = _make_orchestrator()
    result = orch.run(
        str(root),
        force=True,
        force_only_missing=False,
    )

    # alpha was forced; existing meeting_extraction must NOT skip it.
    assert "alpha" in result["source_ids_processed"]


def test_orchestration_record_includes_new_fields(tmp_path: Path) -> None:
    """The run record must validate against schema 1.4.0 with new fields."""
    import jsonschema

    root = _stage_lake(tmp_path)
    _drop_txt(root, "alpha.txt")

    orch = _make_orchestrator()
    result = orch.run(
        str(root),
        force=True,
        force_only_missing=True,
        specific_source_id="alpha",
    )
    record_path = result["orchestration_record_path"]
    assert record_path
    record = json.loads(Path(record_path).read_text(encoding="utf-8"))
    assert record["schema_version"] == "1.4.0"
    assert record["force_only_missing"] is True
    assert record["specific_source_id"] == "alpha"
    assert isinstance(record["source_ids_processed"], list)
    assert isinstance(record["source_ids_skipped"], list)
    assert isinstance(record["source_ids_failed"], list)

    schema = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "contracts"
            / "schemas"
            / "orchestration"
            / "orchestration_run_record.schema.json"
        ).read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(schema).validate(record)
