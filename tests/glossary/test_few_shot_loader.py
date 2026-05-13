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


def test_shipped_artifact_lifecycle_invariants() -> None:
    """The shipped ``decision_examples_v1.json`` artifact MUST be in a
    self-consistent lifecycle state.

    Replaces the pre-Phase-X2.2 static-placeholder check, which asserted
    ``artifact.verified is False`` and broke the moment an operator ran
    the verify-few-shot-example mobile workflow on main (commits land
    with ``[skip ci]`` so the regression hid on main until the next
    non-skip PR triggered pytest — PR #93 was that PR).

    Invariants asserted here are EVERY contract ``verify_example.py``
    promises about the artifact, so this test fails whenever a future
    writer (or hand-edit) breaks any of:

    1. ``artifact["verified"]`` equals ``all(ex["verified"])`` —
       ``verify_example.py:154-159`` derives the artifact-level flag
       from the example-level flags after every transition. A drift in
       either direction breaks downstream consumers that branch on
       ``artifact["verified"]``.
    2. Every example whose ``verified`` is true MUST carry a
       non-empty ``verified_by`` AND ``verified_at`` — the Phase X2.2
       reviewer-attribution requirement (a verified example with no
       reviewer is unauditable and must never reach the prompt).
    3. The ``audit_log`` (when present) must include a
       ``verified`` (or ``force-verified``) entry for every example
       with ``verified: true`` — append-only trace promised by Phase
       X2.2. An example flipped to true with no audit entry is the
       silent-tampering failure mode this invariant catches.

    The schema-validity check is kept in the sibling
    ``test_shipped_artifact_passes_schema`` test and is intentionally
    not duplicated here.
    """
    artifact = json.loads(FEW_SHOT_PATH.read_text(encoding="utf-8"))

    examples = artifact.get("examples") or []
    assert isinstance(examples, list) and examples, (
        "shipped few-shot artifact must contain at least one example "
        "(an empty examples list would silently produce zero injection "
        "without any health finding)"
    )

    # Invariant 1: artifact-level verified mirrors example-level verified.
    artifact_verified = artifact.get("verified")
    all_examples_verified = all(
        bool(ex.get("verified")) for ex in examples if isinstance(ex, dict)
    )
    assert artifact_verified is all_examples_verified, (
        f"artifact.verified ({artifact_verified!r}) must equal "
        f"all(examples.verified) ({all_examples_verified!r}). "
        f"verify_example.py:154-159 enforces this on every write; "
        f"a mismatch means the artifact was edited outside the writer."
    )

    # Invariant 2: every verified example carries reviewer attribution.
    for ex in examples:
        if not bool(ex.get("verified")):
            continue
        eid = ex.get("example_id")
        assert (
            isinstance(ex.get("verified_by"), str)
            and ex["verified_by"].strip()
        ), (
            f"example {eid!r} has verified=true but no verified_by — "
            f"Phase X2.2 reviewer attribution missing"
        )
        assert (
            isinstance(ex.get("verified_at"), str)
            and ex["verified_at"].strip()
        ), (
            f"example {eid!r} has verified=true but no verified_at — "
            f"Phase X2.2 timestamp missing"
        )

    # Invariant 3: audit_log integrity. ``audit_log`` is optional in
    # the schema but, when present on a verified artifact, MUST cover
    # every verified example. Phase X2.2 promises an append-only trace.
    audit_log = artifact.get("audit_log") or []
    if isinstance(audit_log, list) and audit_log:
        verified_in_audit = {
            entry.get("example_id")
            for entry in audit_log
            if isinstance(entry, dict)
            and entry.get("action") in ("verified", "force-verified")
        }
        for ex in examples:
            if not bool(ex.get("verified")):
                continue
            eid = ex.get("example_id")
            assert eid in verified_in_audit, (
                f"example {eid!r} has verified=true but no matching "
                f"'verified' (or 'force-verified') entry in audit_log — "
                f"Phase X2.2 promises every verification appends one."
            )


def test_writer_drives_lifecycle_consistently(tmp_path: Path) -> None:
    """End-to-end trace test: drive ``verify_example.verify_example``
    on a tmp clone of the shipped artifact through every lifecycle
    state and assert the lifecycle invariants hold at each step.

    This is the "writer-side" half of the contract; the sibling test
    above is the "shipped-artifact-side" half. They MUST agree, and
    if a future change to ``verify_example.py`` introduces a third
    state (e.g. an artifact-level ``partial`` flag), this test
    surfaces the drift before the shipped artifact does — strengthens
    the earlier-detection path the user's governance loop requires.
    """
    import sys

    scripts_dir = REPO_ROOT / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import verify_example  # noqa: WPS433

    # Seed a tmp data-lake with three unverified placeholder examples.
    artifact_path = _write_artifact(
        tmp_path,
        verified=False,
        examples=[
            _example("e1", verified=False),
            _example("e2", verified=False),
            _example("e3", verified=False),
        ],
    )
    doc = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert doc["verified"] is False
    assert all(not ex["verified"] for ex in doc["examples"])

    # Transition 1: verify one example. Lifecycle state = mid-lifecycle.
    # The writer's invariant: artifact.verified stays False because
    # not every example is verified yet.
    rc = verify_example.verify_example(
        artifact_path=artifact_path,
        example_id="e1",
        reviewer_id="reviewer-alice",
        notes=None,
        force=False,
    )
    assert rc == 0, f"writer returned non-zero exit code {rc}"
    doc = json.loads(artifact_path.read_text(encoding="utf-8"))
    e1 = next(ex for ex in doc["examples"] if ex["example_id"] == "e1")
    assert e1["verified"] is True
    assert e1["verified_by"] == "reviewer-alice"
    assert isinstance(e1["verified_at"], str) and e1["verified_at"]
    assert doc["verified"] is False, (
        "Mid-lifecycle: artifact-level flag must stay False until "
        "every example is verified."
    )
    # Audit-log invariant from Invariant 3 holds even mid-lifecycle.
    actions = {
        entry["example_id"]
        for entry in doc.get("audit_log", [])
        if entry.get("action") in ("verified", "force-verified")
    }
    assert "e1" in actions

    # Transition 2: verify remaining examples. Lifecycle state = verified.
    for eid, reviewer in (("e2", "reviewer-bob"), ("e3", "reviewer-carol")):
        verify_example.verify_example(
            artifact_path=artifact_path,
            example_id=eid,
            reviewer_id=reviewer,
            notes=None,
            force=False,
        )
    doc = json.loads(artifact_path.read_text(encoding="utf-8"))
    # Invariant 1: artifact-level flag now mirrors examples.
    assert doc["verified"] is True
    assert all(ex["verified"] is True for ex in doc["examples"])
    # Invariant 2: every verified example carries reviewer attribution.
    for ex in doc["examples"]:
        assert ex["verified_by"] and ex["verified_at"]
    # Invariant 3: audit_log covers every verified example_id.
    audit_ids = {
        entry["example_id"]
        for entry in doc.get("audit_log", [])
        if entry.get("action") in ("verified", "force-verified")
    }
    assert audit_ids >= {"e1", "e2", "e3"}
