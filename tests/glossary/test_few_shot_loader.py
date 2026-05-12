"""Phase V.3 tests: few-shot examples loader."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from spectrum_systems_core.glossary.few_shot_loader import (
    FEW_SHOT_ARTIFACT_FILENAME,
    build_few_shot_block,
    load_few_shot_examples,
)
from spectrum_systems_core.validation import validate_artifact


REPO_ROOT = Path(__file__).resolve().parents[2]
FEW_SHOT_DIR = REPO_ROOT / "data-lake" / "store" / "artifacts" / "evals" / "few_shot"
FEW_SHOT_PATH = FEW_SHOT_DIR / FEW_SHOT_ARTIFACT_FILENAME


def _write_artifact(tmp_path: Path, *, examples: list, verified: bool) -> Path:
    artifact_dir = tmp_path / "evals" / "few_shot"
    artifact_dir.mkdir(parents=True)
    path = artifact_dir / FEW_SHOT_ARTIFACT_FILENAME
    artifact = {
        "artifact_type": "decision_few_shot_examples",
        "schema_version": "1.0.0",
        "examples_version": "1",
        "extraction_type": "decision",
        "verified": verified,
        "examples": examples,
    }
    path.write_text(json.dumps(artifact), encoding="utf-8")
    return path


def _example(eid: str, *, verified: bool) -> dict:
    return {
        "example_id": eid,
        "source_meeting_id": "m1",
        "input_text": f"input for {eid}",
        "expected_output": {"decision_text": "..."},
        "verified": verified,
        "verified_by": None,
    }


def test_two_verified_examples_returns_two(tmp_path: Path) -> None:
    _write_artifact(
        tmp_path,
        examples=[_example("a", verified=True), _example("b", verified=True)],
        verified=True,
    )
    result = load_few_shot_examples(tmp_path)
    assert len(result.examples) == 2
    assert result.finding_code is None


def test_mixed_verified_and_unverified(tmp_path: Path) -> None:
    _write_artifact(
        tmp_path,
        examples=[_example("a", verified=True), _example("b", verified=False)],
        verified=True,
    )
    result = load_few_shot_examples(tmp_path)
    assert len(result.examples) == 1
    assert result.examples[0]["example_id"] == "a"


def test_zero_verified_emits_info_finding(tmp_path: Path) -> None:
    _write_artifact(
        tmp_path,
        examples=[_example("a", verified=False)],
        verified=False,
    )
    result = load_few_shot_examples(tmp_path)
    assert result.examples == []
    assert result.finding_code == "few_shot_no_verified_examples"
    assert result.severity == "info"
    assert result.remediation


def test_missing_artifact_default_info_finding(tmp_path: Path) -> None:
    result = load_few_shot_examples(tmp_path)
    assert result.examples == []
    assert result.finding_code == "few_shot_artifact_missing"
    assert result.severity == "info"


def test_missing_artifact_with_required_halt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FEW_SHOT_REQUIRED", "true")
    result = load_few_shot_examples(tmp_path)
    assert result.examples == []
    assert result.finding_code == "few_shot_artifact_missing"
    assert result.severity == "halt"


def test_malformed_json_treated_as_missing(tmp_path: Path) -> None:
    d = tmp_path / "evals" / "few_shot"
    d.mkdir(parents=True)
    (d / FEW_SHOT_ARTIFACT_FILENAME).write_text("{not json", encoding="utf-8")
    result = load_few_shot_examples(tmp_path)
    assert result.examples == []
    assert result.finding_code == "few_shot_artifact_missing"


def test_build_few_shot_block_with_examples() -> None:
    examples = [{
        "input_text": "Chair: 'Approved.'",
        "expected_output": {"decision_outcome": "approval"},
    }]
    block = build_few_shot_block(examples)
    assert "FEW-SHOT EXAMPLES" in block
    assert "Chair: 'Approved.'" in block
    assert "approval" in block


def test_build_few_shot_block_empty_returns_empty_string() -> None:
    assert build_few_shot_block([]) == ""


def test_shipped_artifact_passes_schema() -> None:
    """The decision_examples_v1.json artifact shipped with the repo
    must pass schema validation."""
    artifact = json.loads(FEW_SHOT_PATH.read_text(encoding="utf-8"))
    validate_artifact(artifact, "decision_few_shot_examples")


def test_shipped_artifact_is_placeholder() -> None:
    """The shipped artifact ships verified=false so no examples are
    injected by default. Operator must verify before use."""
    artifact = json.loads(FEW_SHOT_PATH.read_text(encoding="utf-8"))
    assert artifact["verified"] is False
    for example in artifact["examples"]:
        assert example["verified"] is False
