"""Hash-helper + invocation-log tests for governed_pipeline_run.

Phase 2 Step 2.2 + Step 2.10. These tests exercise the pure hash
helpers and the on-disk invocation-log writer WITHOUT invoking the
LLM workflow (which requires a transport + chunks). The integration
tests under tests/integration/ cover the end-to-end loop.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from spectrum_systems_core.pipeline import (
    CALLER_BATCH_WORKFLOW,
    CALLER_CORRECTION_MINER,
    CALLER_PRODUCTION_CLI,
    ExtractionConfig,
    PIPELINE_INVOCATION_LOG_ARTIFACT_TYPE,
    PIPELINE_INVOCATION_LOG_SCHEMA_VERSION,
    PipelineRunError,
    build_extraction_config_from_run,
    extraction_config_hash,
    prompt_content_hash,
    transcript_hash,
    write_pipeline_invocation_log,
)


# ---------------- pure-hash helpers ----------------
def test_prompt_content_hash_is_deterministic() -> None:
    a = prompt_content_hash("hello")
    b = prompt_content_hash("hello")
    assert a == b
    assert len(a) == 64


def test_prompt_content_hash_changes_on_whitespace_drift() -> None:
    """Step 2.6 / Pass 2: a single whitespace char produces a new hash."""
    a = prompt_content_hash("hello world")
    b = prompt_content_hash("hello world ")  # trailing space
    assert a != b


def test_extraction_config_hash_is_deterministic() -> None:
    cfg = ExtractionConfig(
        temperature=0.0,
        seed_inputs={
            "model_id": "haiku",
            "prompt_content_hash": "p",
            "transcript_hash": "t",
        },
        chunks_full_hash="x",
        chunk_count=2,
        first_chunk_hash="a",
        last_chunk_hash="b",
        prompt_content_hash="p",
    )
    assert extraction_config_hash(cfg) == extraction_config_hash(cfg.to_dict())


def test_extraction_config_to_dict_requires_seed_keys() -> None:
    bad = ExtractionConfig(
        temperature=0.0,
        seed_inputs={"model_id": "m"},  # missing prompt+transcript hashes
        chunks_full_hash="x",
        chunk_count=1,
        first_chunk_hash="a",
        last_chunk_hash="a",
        prompt_content_hash="p",
    )
    with pytest.raises(PipelineRunError) as ei:
        bad.to_dict()
    assert ei.value.reason_code == "extraction_config_seed_missing"


def test_build_extraction_config_round_trips() -> None:
    chunks = [
        {"text": "alpha"},
        {"text": "beta"},
        {"text": "gamma"},
    ]
    cfg = build_extraction_config_from_run(
        prompt_text="P",
        transcript_text="T",
        model_id="haiku-X",
        chunks=chunks,
        temperature=0.0,
    )
    assert cfg.chunk_count == 3
    assert cfg.prompt_content_hash == prompt_content_hash("P")
    assert cfg.seed_inputs["transcript_hash"] == transcript_hash("T")
    assert cfg.seed_inputs["model_id"] == "haiku-X"


# ---------------- invocation log on-disk ----------------
def _cfg() -> ExtractionConfig:
    return ExtractionConfig(
        temperature=0.0,
        seed_inputs={
            "model_id": "haiku-Z",
            "prompt_content_hash": "p" * 64,
            "transcript_hash": "t" * 64,
        },
        chunks_full_hash="c" * 64,
        chunk_count=1,
        first_chunk_hash="a" * 64,
        last_chunk_hash="a" * 64,
        prompt_content_hash="p" * 64,
    )


def test_write_invocation_log_emits_valid_artifact(tmp_path: Path) -> None:
    log = write_pipeline_invocation_log(
        data_lake_path=tmp_path,
        source_id="mtg-1",
        invocation_id="inv-abc",
        started_at="2026-05-20T00:00:00+00:00",
        completed_at="2026-05-20T00:00:01+00:00",
        caller=CALLER_PRODUCTION_CLI,
        extraction_config=_cfg(),
        comparison_artifact_path="some/path.json",
    )
    assert log["artifact_type"] == PIPELINE_INVOCATION_LOG_ARTIFACT_TYPE
    assert log["schema_version"] == PIPELINE_INVOCATION_LOG_SCHEMA_VERSION
    out = (
        tmp_path
        / "store"
        / "processed"
        / "meetings"
        / "mtg-1"
        / "diagnostics"
        / "pipeline_invocation_log__inv-abc.json"
    )
    assert out.is_file()
    written = json.loads(out.read_text(encoding="utf-8"))
    # Pass 1 reproduction check: an engineer must be able to
    # reconstruct the run from these fields alone.
    for required in (
        "source_id",
        "invocation_id",
        "started_at",
        "completed_at",
        "caller",
        "extraction_config_hash",
        "prompt_content_hash",
        "transcript_hash",
        "comparison_artifact_path",
        "ttl_expires_at",
    ):
        assert written[required], f"required field {required} is empty"


def test_invocation_log_rejects_unknown_caller(tmp_path: Path) -> None:
    with pytest.raises(PipelineRunError) as ei:
        write_pipeline_invocation_log(
            data_lake_path=tmp_path,
            source_id="mtg-1",
            invocation_id="inv-abc",
            started_at="2026-05-20T00:00:00+00:00",
            completed_at="2026-05-20T00:00:01+00:00",
            caller="rogue_caller",
            extraction_config=_cfg(),
            comparison_artifact_path=None,
        )
    assert ei.value.reason_code == "invocation_log_invalid_caller"


def test_invocation_log_accepts_three_known_callers(tmp_path: Path) -> None:
    for caller in (
        CALLER_PRODUCTION_CLI,
        CALLER_CORRECTION_MINER,
        CALLER_BATCH_WORKFLOW,
    ):
        write_pipeline_invocation_log(
            data_lake_path=tmp_path,
            source_id=f"mtg-{caller}",
            invocation_id=f"inv-{caller}",
            started_at="2026-05-20T00:00:00+00:00",
            completed_at="2026-05-20T00:00:01+00:00",
            caller=caller,
            extraction_config=_cfg(),
            comparison_artifact_path=None,
        )


def test_invocation_log_ttl_30_days_after_started(tmp_path: Path) -> None:
    log = write_pipeline_invocation_log(
        data_lake_path=tmp_path,
        source_id="mtg-1",
        invocation_id="inv-ttl",
        started_at="2026-05-20T00:00:00+00:00",
        completed_at="2026-05-20T00:00:01+00:00",
        caller=CALLER_PRODUCTION_CLI,
        extraction_config=_cfg(),
        comparison_artifact_path=None,
    )
    assert log["ttl_expires_at"] == "2026-06-19T00:00:00+00:00"
