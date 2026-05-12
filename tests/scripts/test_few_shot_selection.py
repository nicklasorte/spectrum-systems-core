"""Phase X2.2 — few-shot example selection + verification tests."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

# Make scripts/ importable without packaging it.
_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import select_few_shot_examples as selector  # noqa: E402
import verify_example as verifier  # noqa: E402

from spectrum_systems_core.validation import validate_artifact


def _make_decision(
    *,
    outcome: str,
    confidence: float,
    decision_text: str,
    turn_ids: List[str],
    speaker: str = "Chair",
) -> Dict[str, Any]:
    return {
        "decision_id": f"d_{outcome}_{int(confidence*100)}",
        "decision_text": decision_text,
        "decision_outcome": outcome,
        "regulatory_verb": outcome,
        "speaker": speaker,
        "confidence": confidence,
        "grounding_verified": True,
        "source_turn_ids": turn_ids,
        "source_text": decision_text,
    }


def _write_extraction(data_lake: Path, source_id: str, decisions: List[Dict[str, Any]]) -> None:
    target = data_lake / "store" / "artifacts" / "extractions"
    target.mkdir(parents=True, exist_ok=True)
    artifact = {
        "artifact_type": "meeting_extraction",
        "schema_version": "1.0.0",
        "source_id": source_id,
        "decisions": decisions,
    }
    (target / f"{source_id}.json").write_text(
        json.dumps(artifact, indent=2), encoding="utf-8"
    )


# ----- selection --------------------------------------------------


def test_selects_one_decision_per_target_outcome(tmp_path: Path) -> None:
    decisions = [
        _make_decision(outcome="approval", confidence=0.7, decision_text="A low", turn_ids=["t1"]),
        _make_decision(outcome="approval", confidence=0.95, decision_text="A high", turn_ids=["t2"]),
        _make_decision(outcome="deferral", confidence=0.8, decision_text="D high", turn_ids=["t3"]),
        _make_decision(outcome="action_required", confidence=0.85, decision_text="AR high", turn_ids=["t4"]),
        # noise:
        _make_decision(outcome="noted", confidence=0.99, decision_text="N", turn_ids=["t5"]),
    ]
    _write_extraction(tmp_path, "src1", decisions)
    artifact = tmp_path / "few_shot.json"
    rc = selector.main([
        "--source-id", "src1",
        "--data-lake", str(tmp_path),
        "--artifact-path", str(artifact),
    ])
    assert rc == 0
    doc = json.loads(artifact.read_text(encoding="utf-8"))
    outcomes = sorted(ex["expected_output"]["decision_outcome"] for ex in doc["examples"])
    assert outcomes == ["action_required", "approval", "deferral"]
    # Within approval bucket, the highest-confidence one is chosen.
    approval = next(
        ex for ex in doc["examples"]
        if ex["expected_output"]["decision_outcome"] == "approval"
    )
    assert "A high" in approval["expected_output"]["decision_text"]


def test_all_selected_examples_are_unverified(tmp_path: Path) -> None:
    decisions = [_make_decision(outcome="approval", confidence=0.9, decision_text="A", turn_ids=["t1"])]
    _write_extraction(tmp_path, "src2", decisions)
    artifact = tmp_path / "few_shot.json"
    selector.main([
        "--source-id", "src2",
        "--data-lake", str(tmp_path),
        "--artifact-path", str(artifact),
    ])
    doc = json.loads(artifact.read_text(encoding="utf-8"))
    assert doc["verified"] is False
    assert all(ex["verified"] is False for ex in doc["examples"])
    assert all(ex.get("verified_by") is None for ex in doc["examples"])


def test_review_checklist_written(tmp_path: Path) -> None:
    decisions = [_make_decision(outcome="approval", confidence=0.9, decision_text="A", turn_ids=["t1"])]
    _write_extraction(tmp_path, "src3", decisions)
    artifact = tmp_path / "few_shot.json"
    selector.main([
        "--source-id", "src3",
        "--data-lake", str(tmp_path),
        "--artifact-path", str(artifact),
    ])
    # When --artifact-path is provided, checklist lands next to it.
    checklist = tmp_path / "REVIEW_CHECKLIST.md"
    assert checklist.is_file()
    body = checklist.read_text(encoding="utf-8")
    assert "Review Checklist" in body
    assert "src3" in body
    assert "scripts/verify_example.py" in body


def test_selection_schema_validates(tmp_path: Path) -> None:
    decisions = [
        _make_decision(outcome="approval", confidence=0.9, decision_text="A", turn_ids=["t1"]),
    ]
    _write_extraction(tmp_path, "src4", decisions)
    artifact = tmp_path / "few_shot.json"
    selector.main([
        "--source-id", "src4",
        "--data-lake", str(tmp_path),
        "--artifact-path", str(artifact),
    ])
    doc = json.loads(artifact.read_text(encoding="utf-8"))
    # Strip the audit_log + extra fields the loader doesn't read, just
    # validate the core shape.
    validate_artifact(doc, "decision_few_shot_examples")


def test_selection_fails_when_no_extraction(tmp_path: Path) -> None:
    rc = selector.main([
        "--source-id", "missing",
        "--data-lake", str(tmp_path),
        "--artifact-path", str(tmp_path / "x.json"),
    ])
    assert rc != 0


def test_selection_fails_when_no_target_outcomes(tmp_path: Path) -> None:
    _write_extraction(tmp_path, "src5", [
        _make_decision(outcome="noted", confidence=0.99, decision_text="N", turn_ids=["t1"])
    ])
    rc = selector.main([
        "--source-id", "src5",
        "--data-lake", str(tmp_path),
        "--artifact-path", str(tmp_path / "x.json"),
    ])
    assert rc == 2


# ----- verification ----------------------------------------------


def _seed_unverified_artifact(path: Path, example_id: str = "ex1") -> Dict[str, Any]:
    doc = {
        "artifact_type": "decision_few_shot_examples",
        "schema_version": "1.0.0",
        "examples_version": "1",
        "extraction_type": "decision",
        "verified": False,
        "examples": [
            {
                "example_id": example_id,
                "source_meeting_id": "test",
                "source_turn_ids": ["t1"],
                "input_text": "txt",
                "expected_output": {"decision_outcome": "approval"},
                "verified": False,
                "verified_by": None,
            }
        ],
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    return doc


def test_verify_sets_verified_true_with_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    artifact = tmp_path / "few_shot.json"
    _seed_unverified_artifact(artifact, "ex1")
    rc = verifier.main([
        "--example-id", "ex1",
        "--reviewer-id", "alice",
        "--artifact-path", str(artifact),
    ])
    assert rc == 0
    doc = json.loads(artifact.read_text(encoding="utf-8"))
    ex = doc["examples"][0]
    assert ex["verified"] is True
    assert ex["verified_by"] == "alice"
    assert ex["verified_at"] is not None
    # Artifact-level verified flag rolls up.
    assert doc["verified"] is True
    # Audit trail entry.
    assert any(
        a["action"] == "verified" and a["example_id"] == "ex1"
        for a in doc["audit_log"]
    )


def test_verify_refuses_already_verified_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    artifact = tmp_path / "few_shot.json"
    _seed_unverified_artifact(artifact, "ex1")
    verifier.main([
        "--example-id", "ex1", "--reviewer-id", "alice",
        "--artifact-path", str(artifact),
    ])
    rc = verifier.main([
        "--example-id", "ex1", "--reviewer-id", "bob",
        "--artifact-path", str(artifact),
    ])
    assert rc == 3
    doc = json.loads(artifact.read_text(encoding="utf-8"))
    # Original verifier preserved.
    assert doc["examples"][0]["verified_by"] == "alice"


def test_verify_force_overrides_existing_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    artifact = tmp_path / "few_shot.json"
    _seed_unverified_artifact(artifact, "ex1")
    verifier.main([
        "--example-id", "ex1", "--reviewer-id", "alice",
        "--artifact-path", str(artifact),
    ])
    rc = verifier.main([
        "--example-id", "ex1", "--reviewer-id", "bob",
        "--force", "--artifact-path", str(artifact),
    ])
    assert rc == 0
    doc = json.loads(artifact.read_text(encoding="utf-8"))
    assert doc["examples"][0]["verified_by"] == "bob"
    # Audit trail records the force-verify.
    assert any(a["action"] == "force-verified" for a in doc["audit_log"])


def test_verify_refuses_when_ci_env_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    artifact = tmp_path / "few_shot.json"
    _seed_unverified_artifact(artifact, "ex1")
    rc = verifier.main([
        "--example-id", "ex1", "--reviewer-id", "alice",
        "--artifact-path", str(artifact),
    ])
    assert rc == 4
    doc = json.loads(artifact.read_text(encoding="utf-8"))
    assert doc["examples"][0]["verified"] is False


def test_verify_unknown_example_returns_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    artifact = tmp_path / "few_shot.json"
    _seed_unverified_artifact(artifact, "ex1")
    rc = verifier.main([
        "--example-id", "doesnotexist", "--reviewer-id", "alice",
        "--artifact-path", str(artifact),
    ])
    assert rc == 2
