"""Tests for ``scripts/repair_few_shot_verified.py``.

The Phase W wiring signal ``few_shot_present_with_verified`` uses the
strict identity predicate
``any(isinstance(ex, dict) and ex.get("verified") is True for ex in examples)``
on ``decision_examples_v1.json``. These tests pin BOTH:

  * the signal predicate (positive + four negative cases), and
  * the repair script's evidence-gated normalization behavior.

They guard against a future refactor that weakens the predicate (e.g.
to ``ex.get("verified")``, which would silently accept truthy strings
or ``1``) or that loosens the repair script's governance check.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import repair_few_shot_verified as repair_mod  # noqa: E402

SCHEMA_BASE: dict[str, Any] = {
    "artifact_type": "decision_few_shot_examples",
    "schema_version": "1.0.0",
    "examples_version": "1",
    "extraction_type": "decision",
}


def _signal_predicate(doc: dict[str, Any]) -> bool:
    """Exact predicate from .github/workflows/validate-and-baseline.yml."""
    examples = doc.get("examples") or []
    return any(
        isinstance(ex, dict) and ex.get("verified") is True for ex in examples
    )


def _make_example(
    *, example_id: str, verified: Any, verified_by: Any = None,
) -> dict[str, Any]:
    return {
        "example_id": example_id,
        "source_meeting_id": "m-1",
        "input_text": "decision text body",
        "expected_output": {"decision_outcome": "approval"},
        "verified": verified,
        "verified_by": verified_by,
        "verified_at": None,
        "selected_at": "2026-05-13T00:00:00+00:00",
        "selection_reason": "test",
    }


# ----------------------------------------------------------------------
# Signal-predicate pinning tests.
# ----------------------------------------------------------------------


def test_signal_predicate_passes_on_boolean_true() -> None:
    doc = dict(SCHEMA_BASE)
    doc["examples"] = [_make_example(example_id="a", verified=True)]
    assert _signal_predicate(doc) is True


def test_signal_predicate_fails_on_boolean_false() -> None:
    doc = dict(SCHEMA_BASE)
    doc["examples"] = [_make_example(example_id="a", verified=False)]
    assert _signal_predicate(doc) is False


def test_signal_predicate_fails_on_missing_field() -> None:
    doc = dict(SCHEMA_BASE)
    ex = _make_example(example_id="a", verified=False)
    ex.pop("verified")
    doc["examples"] = [ex]
    assert _signal_predicate(doc) is False


def test_signal_predicate_fails_on_null_field() -> None:
    doc = dict(SCHEMA_BASE)
    doc["examples"] = [_make_example(example_id="a", verified=None)]
    assert _signal_predicate(doc) is False


def test_signal_predicate_fails_on_truthy_string_true() -> None:
    doc = dict(SCHEMA_BASE)
    doc["examples"] = [_make_example(example_id="a", verified="true")]
    assert _signal_predicate(doc) is False


def test_signal_predicate_fails_on_integer_one() -> None:
    doc = dict(SCHEMA_BASE)
    doc["examples"] = [_make_example(example_id="a", verified=1)]
    assert _signal_predicate(doc) is False


# ----------------------------------------------------------------------
# Repair-script behavior.
# ----------------------------------------------------------------------


def _write_artifact(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_artifact(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_repair_promotes_example_with_audit_log_evidence(tmp_path: Path) -> None:
    path = tmp_path / "decision_examples_v1.json"
    doc = dict(SCHEMA_BASE)
    doc["examples"] = [
        _make_example(example_id="ex-a", verified="true"),  # string -> wrong type
    ]
    doc["audit_log"] = [
        {
            "action": "verified",
            "example_id": "ex-a",
            "at": "2026-05-01T00:00:00+00:00",
            "actor": "alice",
            "notes": None,
        }
    ]
    _write_artifact(path, doc)

    code, summary = repair_mod.repair(path)
    assert code == 0
    assert summary["examples_repaired"] == 1

    repaired = _read_artifact(path)
    assert _signal_predicate(repaired) is True
    assert repaired["examples"][0]["verified"] is True
    assert repaired["verified"] is True  # artifact-level


def test_repair_promotes_example_with_verified_by_evidence(tmp_path: Path) -> None:
    path = tmp_path / "decision_examples_v1.json"
    doc = dict(SCHEMA_BASE)
    # Example has verified_by set (residue of prior verification) but
    # the verified field itself is null. Repair should fix the field.
    doc["examples"] = [
        _make_example(
            example_id="ex-b",
            verified=None,
            verified_by="alice",
        ),
    ]
    _write_artifact(path, doc)

    code, summary = repair_mod.repair(path)
    assert code == 0
    assert summary["examples_repaired"] == 1
    repaired = _read_artifact(path)
    assert _signal_predicate(repaired) is True


def test_repair_does_not_promote_examples_without_evidence(tmp_path: Path) -> None:
    path = tmp_path / "decision_examples_v1.json"
    doc = dict(SCHEMA_BASE)
    doc["examples"] = [
        _make_example(example_id="ex-c", verified=False),
    ]
    doc["audit_log"] = []
    _write_artifact(path, doc)

    code, summary = repair_mod.repair(path)
    assert code == 0
    assert summary["examples_repaired"] == 0
    # Critical governance assertion: the script must NOT auto-promote
    # examples without evidence. Otherwise it becomes a self-grading
    # bypass of verify-few-shot-example.
    repaired = _read_artifact(path)
    assert _signal_predicate(repaired) is False
    assert repaired["examples"][0]["verified"] is False


def test_repair_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "decision_examples_v1.json"
    doc = dict(SCHEMA_BASE)
    doc["examples"] = [
        _make_example(example_id="ex-a", verified=True, verified_by="alice"),
    ]
    _write_artifact(path, doc)

    code1, summary1 = repair_mod.repair(path)
    bytes_after_first = path.read_bytes()
    code2, summary2 = repair_mod.repair(path)
    bytes_after_second = path.read_bytes()

    assert code1 == 0 and code2 == 0
    assert summary1["examples_repaired"] == 0
    assert summary2["examples_repaired"] == 0
    assert bytes_after_first == bytes_after_second


def test_repair_dry_run_does_not_write(tmp_path: Path) -> None:
    path = tmp_path / "decision_examples_v1.json"
    doc = dict(SCHEMA_BASE)
    doc["examples"] = [
        _make_example(example_id="ex-a", verified=None, verified_by="alice"),
    ]
    _write_artifact(path, doc)
    before = path.read_bytes()

    code, summary = repair_mod.repair(path, dry_run=True)
    after = path.read_bytes()

    assert code == 0
    assert summary["examples_repaired"] == 1
    assert before == after  # dry-run must not mutate the file


def test_repair_artifact_level_verified_recomputed(tmp_path: Path) -> None:
    path = tmp_path / "decision_examples_v1.json"
    doc = dict(SCHEMA_BASE)
    doc["examples"] = [
        _make_example(example_id="ex-a", verified=None, verified_by="alice"),
        _make_example(example_id="ex-b", verified=False),  # no evidence
    ]
    _write_artifact(path, doc)

    code, _summary = repair_mod.repair(path)
    assert code == 0
    repaired = _read_artifact(path)
    # ex-b stays unverified, so artifact-level verified must be false.
    assert repaired["verified"] is False
    # But the signal predicate must still pass because ex-a is now True.
    assert _signal_predicate(repaired) is True


def test_repair_refuses_in_ci_without_force(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    path = tmp_path / "x.json"
    rc = repair_mod.main(["--artifact-path", str(path)])
    assert rc == 4  # refusal exit code
