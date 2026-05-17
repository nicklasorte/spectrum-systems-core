"""Tests for FewShotLoader (Phase M.4)."""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from spectrum_systems_core.evals.m4 import (
    load_few_shot_examples,
)
from spectrum_systems_core.evals.m4.few_shot import format_examples_for_prompt

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "schemas" / "eval"
SEED_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "eval"
    / "seeds"
    / "extraction_few_shot_v1.json"
)


def _load_schema(name: str) -> dict:
    return json.loads(
        (CONTRACT_DIR / f"{name}.schema.json").read_text(encoding="utf-8")
    )


def test_few_shot_schema_validates() -> None:
    """The shipped seed JSON validates against its schema."""
    schema = _load_schema("few_shot_examples")
    artifact = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(artifact)


def test_few_shot_injected_on_version_match() -> None:
    """Version match -> loader returns artifact, status ok, prompt renders."""
    artifact, status = load_few_shot_examples(
        prompt_schema_version="1.0.0",
        seed_path=str(SEED_PATH),
    )
    assert status == "ok"
    assert artifact is not None
    assert artifact["prompt_schema_version"] == "1.0.0"
    rendered = format_examples_for_prompt(artifact)
    assert "EXPECTED OUTPUT" in rendered, (
        "Prompt-ready string must include the structured expected_output "
        "blocks for the model to imitate."
    )


def test_few_shot_injection_skipped_on_version_mismatch(caplog) -> None:
    """Version mismatch -> (None, version_mismatch), no exception, warning logged."""
    import logging
    caplog.set_level(logging.WARNING)
    artifact, status = load_few_shot_examples(
        prompt_schema_version="2.0.0",  # current schema differs from seed
        seed_path=str(SEED_PATH),
    )
    assert artifact is None
    assert status == "version_mismatch"
    assert any(
        "few_shot_version_mismatch" in rec.getMessage()
        for rec in caplog.records
    )


def test_few_shot_missing_file_does_not_fail_closed(tmp_path, caplog) -> None:
    """Missing seed -> (None, missing). Extraction must continue without injection.

    This is a deliberate departure from fail-closed-by-default: few-shot is
    an optimization, not a correctness gate.
    """
    import logging
    caplog.set_level(logging.WARNING)
    missing_path = tmp_path / "does_not_exist.json"
    artifact, status = load_few_shot_examples(
        prompt_schema_version="1.0.0",
        seed_path=str(missing_path),
    )
    assert artifact is None
    assert status == "missing"
    assert any(
        "few_shot_missing" in rec.getMessage() for rec in caplog.records
    )


def test_few_shot_unreadable_file_does_not_raise(tmp_path) -> None:
    """Malformed JSON -> (None, unreadable), no exception."""
    bad = tmp_path / "bad.json"
    bad.write_text("not json {", encoding="utf-8")
    artifact, status = load_few_shot_examples(
        prompt_schema_version="1.0.0",
        seed_path=str(bad),
    )
    assert artifact is None
    assert status == "unreadable"


def test_few_shot_schema_invalid_does_not_raise(tmp_path) -> None:
    """JSON but invalid against schema -> (None, schema_invalid)."""
    bad_seed = {
        "few_shot_id": "not-a-uuid",
        "artifact_type": "few_shot_examples",
        "schema_version": "1.0.0",
        "prompt_schema_version": "1.0.0",
        "source_meeting": "fixture",
        "created_at": "2026-05-11T00:00:00+00:00",
        "examples": [],
    }
    path = tmp_path / "schema_invalid.json"
    path.write_text(json.dumps(bad_seed), encoding="utf-8")
    artifact, status = load_few_shot_examples(
        prompt_schema_version="1.0.0",
        seed_path=str(path),
    )
    assert artifact is None
    assert status == "schema_invalid"


def test_format_examples_for_prompt_filters_by_type() -> None:
    """example_type filter narrows the rendered prompt fragment."""
    artifact, _ = load_few_shot_examples(
        prompt_schema_version="1.0.0",
        seed_path=str(SEED_PATH),
    )
    assert artifact is not None
    decision_only = format_examples_for_prompt(artifact, example_type="decision")
    assert "decision" in decision_only.lower()
    assert "action_item" not in decision_only.lower(), (
        "Type-filtered rendering must exclude action_item examples."
    )
