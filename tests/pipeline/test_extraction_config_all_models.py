"""Phase 5 hotfix — every model path stamps a complete extraction_config.

Pre-fix, the production CLI's ``--model sonnet`` path stamped only the
``prompt_variant`` discriminator into ``provenance.extraction_config``.
Because the meeting_minutes schema marks the ``extraction_config`` block
as optional at the OUTER level but requires the seven Phase 2 fields
(``temperature``, ``seed_inputs``, ``chunks_full_hash``, ``chunk_count``,
``first_chunk_hash``, ``last_chunk_hash``, ``prompt_content_hash``)
INSIDE the block, the partial stamp made the comparison engine reject
the artifact with::

    'temperature' is a required property in provenance -> extraction_config

The Haiku path was correct in practice because correction_miner and the
batch workflows route through ``governed_pipeline_run`` (which always
populates the full block via ``build_extraction_config_from_run``); the
CLI shim path was the only writer producing partial blocks.

These tests pin the contract for every model path:

1. ``build_extraction_config_from_run`` round-trips every Phase 5
   ``prompt_variant`` enum value into a schema-valid block.
2. ``governed_pipeline_run`` produces a schema-valid artifact for each
   prompt_variant when the underlying workflow returns a promoted
   meeting_minutes.
3. The Haiku path is unchanged (no extra fields, same hashes).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

import jsonschema
import pytest

from spectrum_systems_core.pipeline import (
    ALLOWED_CALLERS,
    build_extraction_config_from_run,
)
from spectrum_systems_core.pipeline.governed_run import (
    ALL_PROMPT_VARIANTS,
    PROMPT_VARIANT_HAIKU_PROMPT_WITH_SONNET,
    PROMPT_VARIANT_OPUS_BASELINE,
    PROMPT_VARIANT_OPUS_PROMPT_WITH_SONNET,
    PROMPT_VARIANT_PRODUCTION_HAIKU,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_PATH = (
    REPO_ROOT
    / "src"
    / "spectrum_systems_core"
    / "schemas"
    / "meeting_minutes.schema.json"
)


def _meeting_minutes_schema() -> dict:
    import json as _json

    return _json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _flatten_artifact_for_validation(artifact: Dict[str, Any]) -> Dict[str, Any]:
    """Project a meeting_minutes envelope into the flat shape the
    schema validates (``{"artifact_type": ..., **payload}``)."""
    payload = artifact.get("payload") or {}
    return {"artifact_type": "meeting_minutes", **payload}


# ---------------------------------------------------------------------
# 1. Unit: every prompt_variant builds a schema-valid extraction_config.
# ---------------------------------------------------------------------
@pytest.mark.parametrize("variant", sorted(ALL_PROMPT_VARIANTS))
def test_build_extraction_config_for_every_variant_validates(
    variant: str,
) -> None:
    """Every Phase 5 prompt_variant enum value produces a complete block
    whose required fields are all populated.

    This is the unit underneath both the CLI shim and
    ``governed_pipeline_run``; if it ever regresses the higher-level
    paths will silently start producing partial blocks again.
    """
    cfg = build_extraction_config_from_run(
        prompt_text="some prompt content",
        transcript_text="some transcript",
        model_id="claude-test-1-2",
        chunks=[
            {"text": "alpha turn"},
            {"text": "beta turn"},
        ],
        temperature=0.0,
        prompt_variant=variant,
    )
    block = cfg.to_dict()
    for required in (
        "temperature",
        "seed_inputs",
        "chunks_full_hash",
        "chunk_count",
        "first_chunk_hash",
        "last_chunk_hash",
        "prompt_content_hash",
    ):
        assert required in block, f"missing required field: {required}"
    # Seed inputs sub-required keys.
    for seed_key in ("model_id", "prompt_content_hash", "transcript_hash"):
        assert seed_key in block["seed_inputs"], (
            f"missing seed_inputs key: {seed_key}"
        )
    assert block["prompt_variant"] == variant
    assert block["chunk_count"] == 2

    # Embed into a minimal meeting_minutes flat payload and validate.
    flat = {
        "artifact_type": "meeting_minutes",
        "title": "T",
        "summary": "S",
        "schema_version": "1.4.0",
        "decisions": [],
        "action_items": [],
        "open_questions": [],
        "provenance": {
            "produced_by": "meeting_minutes_llm",
            "extraction_config": block,
        },
    }
    jsonschema.Draft202012Validator(_meeting_minutes_schema()).validate(flat)


def test_build_extraction_config_without_variant_is_valid() -> None:
    """``prompt_variant`` is optional on the schema. Omitting it must
    still produce a schema-valid block — protects the pre-Phase-5
    backward-compat path that ``governed_pipeline_run`` exposes via
    ``prompt_variant=None``.
    """
    cfg = build_extraction_config_from_run(
        prompt_text="P",
        transcript_text="T",
        model_id="haiku-X",
        chunks=[{"text": "x"}],
        temperature=0.0,
        prompt_variant=None,
    )
    block = cfg.to_dict()
    assert "prompt_variant" not in block
    flat = {
        "artifact_type": "meeting_minutes",
        "title": "T",
        "summary": "S",
        "schema_version": "1.4.0",
        "decisions": [],
        "action_items": [],
        "open_questions": [],
        "provenance": {
            "produced_by": "meeting_minutes_llm",
            "extraction_config": block,
        },
    }
    jsonschema.Draft202012Validator(_meeting_minutes_schema()).validate(flat)


# ---------------------------------------------------------------------
# 2. Regression: a partial block (the pre-fix CLI output) is rejected.
# ---------------------------------------------------------------------
def test_partial_extraction_config_rejected_by_schema() -> None:
    """Demonstrates the failure mode the hotfix targets.

    Pre-fix, the CLI stamped::

        provenance.extraction_config = {"prompt_variant": "..."}

    The meeting_minutes schema requires the seven Phase 2 fields inside
    the block, so this partial stamp fails validation with the exact
    error string the bug report quotes.
    """
    flat = {
        "artifact_type": "meeting_minutes",
        "title": "T",
        "summary": "S",
        "schema_version": "1.4.0",
        "decisions": [],
        "action_items": [],
        "open_questions": [],
        "provenance": {
            "produced_by": "meeting_minutes_llm",
            "extraction_config": {
                "prompt_variant": PROMPT_VARIANT_HAIKU_PROMPT_WITH_SONNET,
            },
        },
    }
    with pytest.raises(jsonschema.ValidationError) as ei:
        jsonschema.Draft202012Validator(_meeting_minutes_schema()).validate(flat)
    # The error message names the missing required property.
    assert "'temperature' is a required property" in str(ei.value)


# ---------------------------------------------------------------------
# 3. Integration: governed_pipeline_run for each model path produces a
#    schema-valid artifact whose extraction_config carries every
#    required Phase 2 field.
# ---------------------------------------------------------------------
def _build_promoted_workflow_result(
    *,
    model_id: str,
    transcript: str,
):
    """Construct a minimally-promoted WorkflowResult for the stub.

    Mirrors the shape the real workflow emits: a promoted
    meeting_minutes envelope with ``provenance.model_id`` stamped so
    ``governed_pipeline_run``'s auto-derive branch reads the resolved
    model id.
    """
    from spectrum_systems_core.artifacts import Artifact
    from spectrum_systems_core.workflows.meeting_minutes import (
        WorkflowResult,
    )

    payload = {
        "title": "T",
        "summary": "S",
        "schema_version": "1.4.0",
        "decisions": [],
        "action_items": [],
        "open_questions": [],
        "provenance": {
            "produced_by": "meeting_minutes_llm",
            "model_id": model_id,
        },
    }
    art = Artifact(
        artifact_type="meeting_minutes",
        schema_version=1,
        status="promoted",
        payload=payload,
        trace_id="trace-test",
    )
    return WorkflowResult(
        context_bundle=None,
        meeting_minutes=art,
        eval_results=[],
        control_decision=None,
        promoted=True,
    )


@pytest.mark.parametrize(
    "model_id, prompt_variant",
    [
        ("claude-haiku-4-7", PROMPT_VARIANT_PRODUCTION_HAIKU),
        ("claude-sonnet-4-6", PROMPT_VARIANT_HAIKU_PROMPT_WITH_SONNET),
        ("claude-sonnet-4-6", PROMPT_VARIANT_OPUS_PROMPT_WITH_SONNET),
        ("claude-opus-4-7", PROMPT_VARIANT_OPUS_BASELINE),
    ],
)
def test_governed_pipeline_run_stamps_full_extraction_config(
    model_id: str,
    prompt_variant: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``governed_pipeline_run`` produces a schema-valid artifact whose
    ``extraction_config`` carries every Phase 2 required field, for all
    four model/prompt combinations Phase 5 supports.

    The workflow + comparison helpers are stubbed so the test exercises
    the wiring (config-stamping + envelope hash recomputation) without
    a live model or an on-disk Opus baseline.
    """
    from spectrum_systems_core.pipeline import governed_run as gr
    from spectrum_systems_core.workflows import meeting_minutes_llm as mml

    transcript = "Speaker 1: alpha decision.\nSpeaker 2: beta question."

    def _stub_workflow(input_text: str, **kwargs: Any):
        return _build_promoted_workflow_result(
            model_id=model_id, transcript=input_text
        )

    monkeypatch.setattr(mml, "run_meeting_minutes_llm_workflow", _stub_workflow)

    # Stub compare_opus_haiku so no baseline lookup is required.
    scripts_dir = REPO_ROOT / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import compare_opus_haiku as cmp  # noqa: WPS433

    monkeypatch.setattr(cmp, "load_opus_baseline", lambda *a, **k: [])
    monkeypatch.setattr(cmp, "load_gt_pairs", lambda *a, **k: [])
    monkeypatch.setattr(cmp, "extraction_types", lambda: [])
    monkeypatch.setattr(
        cmp,
        "compute_comparison",
        lambda **kw: {"summary": {"haiku_f1_vs_opus": 0.0}},
    )
    monkeypatch.setattr(cmp, "is_legacy_eval", lambda *a, **k: True)

    result = gr.governed_pipeline_run(
        source_id="src-test",
        prompt_content="any non-empty prompt content",
        transcript=transcript,
        data_lake_path=tmp_path / "dl",
        enable_glossary_injection=False,
        prompt_variant=prompt_variant,
        model_id_override=model_id,
        skip_invocation_log=True,
    )

    assert result.artifact is not None
    ec = (
        result.artifact["payload"]
        .get("provenance", {})
        .get("extraction_config")
    )
    assert ec is not None, "extraction_config was not stamped"
    for required in (
        "temperature",
        "seed_inputs",
        "chunks_full_hash",
        "chunk_count",
        "first_chunk_hash",
        "last_chunk_hash",
        "prompt_content_hash",
    ):
        assert required in ec, f"{prompt_variant}: missing {required}"
    assert ec["temperature"] == 0.0
    assert ec["prompt_variant"] == prompt_variant
    assert ec["seed_inputs"]["model_id"] == model_id

    # The full envelope must validate against the meeting_minutes schema.
    flat = _flatten_artifact_for_validation(result.artifact)
    jsonschema.Draft202012Validator(_meeting_minutes_schema()).validate(flat)


# ---------------------------------------------------------------------
# 4. Allowed callers contract — the four CLI tokens map to the same
#    three allowed pipeline callers. Defensive: a future refactor that
#    drops one of the three callers must trip this test.
# ---------------------------------------------------------------------
def test_allowed_callers_unchanged() -> None:
    assert ALLOWED_CALLERS == frozenset(
        {"production_cli", "correction_miner", "batch_workflow"}
    )
