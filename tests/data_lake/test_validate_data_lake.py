"""Tests for ``scripts/validate_data_lake.py``.

These cover the bug classes the validator exists to catch:

  * verified field wrong type (string instead of bool, None, etc.).
  * audit_log action outside the schema enum.
  * glossary aggregate missing or empty.
  * malformed JSON in any artifact under store/artifacts/.
  * orchestration_result missing glossary_injection_summary when
    glossary terms exist.

The test scaffolding builds a minimal valid data-lake on disk, then
mutates one field at a time and asserts the validator catches it.
This is the test pattern that would have caught the original PR #77 /
#78 / #79 bug class at PR-review time.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from typing import Any, Dict, List

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "validate_data_lake.py"

# Make the script importable for direct in-process calls. Avoids the
# subprocess overhead for the common-case unit tests.
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import validate_data_lake as vdl  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _valid_few_shot_doc() -> Dict[str, Any]:
    return {
        "artifact_type": "decision_few_shot_examples",
        "schema_version": "1.0.0",
        "examples_version": "1",
        "extraction_type": "decision",
        "verified": True,
        "examples": [
            {
                "example_id": "ex-1",
                "source_meeting_id": "m-1",
                "input_text": "alpha decision",
                "expected_output": {"decision_outcome": "approval"},
                "verified": True,
                "verified_by": "operator",
                "verified_at": "2026-05-13T00:00:00+00:00",
                "selected_at": "2026-05-12T00:00:00+00:00",
                "selection_reason": "seed",
            }
        ],
        "audit_log": [
            {
                "action": "selected",
                "example_id": "ex-1",
                "at": "2026-05-12T00:00:00+00:00",
                "actor": "operator",
                "notes": None,
            },
            {
                "action": "verified",
                "example_id": "ex-1",
                "at": "2026-05-13T00:00:00+00:00",
                "actor": "operator",
                "notes": None,
            },
        ],
    }


def _valid_glossary_doc() -> Dict[str, Any]:
    return {
        "artifact_type": "spectrum_glossary",
        "schema_version": "1.0.0",
        "glossary_version": "v1",
        "term_count": 1,
        "content_hash": "sha256:" + "0" * 64,
        "terms": [
            {
                "term_id": "tdd",
                "term": "Time Division Duplex",
                "abbreviation": "TDD",
                "definition": "A duplexing scheme that uses time slots.",
                "short_definition": "Time-slot duplexing.",
                "authoritative_source": "ITU",
                "domain_scope": "wireless",
                "related_term_ids": [],
            }
        ],
    }


def _build_valid_lake(root: pathlib.Path) -> pathlib.Path:
    artifacts = root / "store" / "artifacts"
    (artifacts / "evals" / "few_shot").mkdir(parents=True)
    (artifacts / "glossary").mkdir(parents=True)
    (artifacts / "evals" / "few_shot" / "decision_examples_v1.json").write_text(
        json.dumps(_valid_few_shot_doc(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (artifacts / "glossary" / "spectrum_glossary_v1.json").write_text(
        json.dumps(_valid_glossary_doc(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return root


def _run_in_process(data_lake: pathlib.Path) -> List[vdl.CheckResult]:
    return vdl.run_checks(data_lake)


def _result_by_name(
    results: List[vdl.CheckResult], name: str
) -> vdl.CheckResult:
    for r in results:
        if r.name == name:
            return r
    raise AssertionError(
        f"no check named {name!r} in results "
        f"(have: {[r.name for r in results]})"
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_valid_data_lake_passes_all_checks(tmp_path: pathlib.Path) -> None:
    lake = _build_valid_lake(tmp_path)
    results = _run_in_process(lake)
    for r in results:
        assert r.passed, f"unexpected fail {r.name}: {r.detail} / {r.failures}"


# ---------------------------------------------------------------------------
# verified field type checks
# ---------------------------------------------------------------------------

def test_missing_verified_field_fails(tmp_path: pathlib.Path) -> None:
    lake = _build_valid_lake(tmp_path)
    path = lake / "store" / "artifacts" / "evals" / "few_shot" / (
        "decision_examples_v1.json"
    )
    doc = json.loads(path.read_text(encoding="utf-8"))
    del doc["examples"][0]["verified"]
    path.write_text(json.dumps(doc), encoding="utf-8")

    results = _run_in_process(lake)
    fs = _result_by_name(results, "decision_few_shot_examples")
    assert not fs.passed
    assert any("verified" in f for f in fs.failures), fs.failures
    wiring = _result_by_name(
        results, "wiring_signal:few_shot_present_with_verified"
    )
    assert not wiring.passed


def test_verified_string_true_fails(tmp_path: pathlib.Path) -> None:
    lake = _build_valid_lake(tmp_path)
    path = lake / "store" / "artifacts" / "evals" / "few_shot" / (
        "decision_examples_v1.json"
    )
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["examples"][0]["verified"] = "true"  # noqa: WPS425 — intentional bad value
    path.write_text(json.dumps(doc), encoding="utf-8")

    results = _run_in_process(lake)
    fs = _result_by_name(results, "decision_few_shot_examples")
    assert not fs.passed
    assert any(
        "verified" in f and "bool" in f.lower() for f in fs.failures
    ), fs.failures
    wiring = _result_by_name(
        results, "wiring_signal:few_shot_present_with_verified"
    )
    assert not wiring.passed, "string 'true' must NOT satisfy `is True`"


def test_verified_bool_true_passes(tmp_path: pathlib.Path) -> None:
    lake = _build_valid_lake(tmp_path)
    results = _run_in_process(lake)
    fs = _result_by_name(results, "decision_few_shot_examples")
    wiring = _result_by_name(
        results, "wiring_signal:few_shot_present_with_verified"
    )
    assert fs.passed
    assert wiring.passed


def test_verified_none_fails(tmp_path: pathlib.Path) -> None:
    lake = _build_valid_lake(tmp_path)
    path = lake / "store" / "artifacts" / "evals" / "few_shot" / (
        "decision_examples_v1.json"
    )
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["examples"][0]["verified"] = None
    path.write_text(json.dumps(doc), encoding="utf-8")

    fs = _result_by_name(
        _run_in_process(lake), "decision_few_shot_examples"
    )
    assert not fs.passed


def test_top_level_verified_string_fails(tmp_path: pathlib.Path) -> None:
    lake = _build_valid_lake(tmp_path)
    path = lake / "store" / "artifacts" / "evals" / "few_shot" / (
        "decision_examples_v1.json"
    )
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["verified"] = "true"
    path.write_text(json.dumps(doc), encoding="utf-8")

    fs = _result_by_name(
        _run_in_process(lake), "decision_few_shot_examples"
    )
    assert not fs.passed


# ---------------------------------------------------------------------------
# audit_log action enum
# ---------------------------------------------------------------------------

def test_audit_log_rejected_action_fails(tmp_path: pathlib.Path) -> None:
    lake = _build_valid_lake(tmp_path)
    path = lake / "store" / "artifacts" / "evals" / "few_shot" / (
        "decision_examples_v1.json"
    )
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["audit_log"].append(
        {
            "action": "rejected",  # NOT in the schema enum
            "example_id": "ex-1",
            "at": "2026-05-14T00:00:00+00:00",
            "actor": "operator",
            "notes": None,
        }
    )
    path.write_text(json.dumps(doc), encoding="utf-8")

    fs = _result_by_name(
        _run_in_process(lake), "decision_few_shot_examples"
    )
    assert not fs.passed
    assert any("rejected" in f for f in fs.failures), fs.failures


def test_audit_log_all_legal_actions_pass(tmp_path: pathlib.Path) -> None:
    lake = _build_valid_lake(tmp_path)
    path = lake / "store" / "artifacts" / "evals" / "few_shot" / (
        "decision_examples_v1.json"
    )
    doc = json.loads(path.read_text(encoding="utf-8"))
    # All four schema-enum values must validate.
    doc["audit_log"] = [
        {
            "action": action,
            "example_id": "ex-1",
            "at": "2026-05-13T00:00:00+00:00",
            "actor": "op",
            "notes": None,
        }
        for action in ("selected", "verified", "unverified", "force-verified")
    ]
    path.write_text(json.dumps(doc), encoding="utf-8")

    fs = _result_by_name(
        _run_in_process(lake), "decision_few_shot_examples"
    )
    assert fs.passed, fs.failures


# ---------------------------------------------------------------------------
# Glossary aggregate
# ---------------------------------------------------------------------------

def test_missing_glossary_aggregate_fails(tmp_path: pathlib.Path) -> None:
    lake = _build_valid_lake(tmp_path)
    (
        lake
        / "store"
        / "artifacts"
        / "glossary"
        / "spectrum_glossary_v1.json"
    ).unlink()

    results = _run_in_process(lake)
    gloss = _result_by_name(results, "spectrum_glossary")
    wiring = _result_by_name(
        results, "wiring_signal:glossary_aggregate_nonempty"
    )
    assert not gloss.passed
    assert not wiring.passed


def test_empty_glossary_terms_fails(tmp_path: pathlib.Path) -> None:
    lake = _build_valid_lake(tmp_path)
    path = lake / "store" / "artifacts" / "glossary" / "spectrum_glossary_v1.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["terms"] = []
    path.write_text(json.dumps(doc), encoding="utf-8")

    results = _run_in_process(lake)
    gloss = _result_by_name(results, "spectrum_glossary")
    wiring = _result_by_name(
        results, "wiring_signal:glossary_aggregate_nonempty"
    )
    assert not gloss.passed
    assert not wiring.passed


# ---------------------------------------------------------------------------
# Malformed JSON
# ---------------------------------------------------------------------------

def test_malformed_json_anywhere_fails(tmp_path: pathlib.Path) -> None:
    lake = _build_valid_lake(tmp_path)
    bad = (
        lake / "store" / "artifacts" / "evals" / "garbage.json"
    )
    bad.write_text("{not valid json", encoding="utf-8")
    results = _run_in_process(lake)
    jv = _result_by_name(results, "json_validity")
    assert not jv.passed
    assert any("garbage.json" in f for f in jv.failures), jv.failures


# ---------------------------------------------------------------------------
# Orchestration result glossary_injection_summary
# ---------------------------------------------------------------------------

def _write_orchestration_result(
    lake: pathlib.Path,
    *,
    include_summary: bool,
    summary_value: Any = None,
) -> pathlib.Path:
    orch = lake / "store" / "artifacts" / "orchestration"
    orch.mkdir(parents=True, exist_ok=True)
    doc: Dict[str, Any] = {
        "artifact_type": "orchestration_result",
        "schema_version": "1.0.0",
        "run_id": "r-1",
        "source_id": "s-1",
        "chunks_attempted": 1,
        "chunks_succeeded": 1,
        "chunks_blocked": 0,
        "block_reasons": {
            "rate_limit_exhausted": 0,
            "empty_response": 0,
            "parse_error": 0,
            "other": 0,
        },
        "stage_status": "ok",
    }
    if include_summary:
        doc["glossary_injection_summary"] = (
            summary_value
            if summary_value is not None
            else {
                "chunks_with_matches": 1,
                "chunks_with_no_matches": 0,
                "total_term_injections": 3,
                "most_injected_terms": ["TDD"],
                "stale_records_count": 0,
            }
        )
    path = orch / "r-1_extraction.json"
    path.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    return path


def test_orchestration_missing_summary_fails_when_glossary_has_terms(
    tmp_path: pathlib.Path,
) -> None:
    lake = _build_valid_lake(tmp_path)
    _write_orchestration_result(lake, include_summary=False)
    results = _run_in_process(lake)
    orch = _result_by_name(results, "orchestration_result")
    assert not orch.passed


def test_orchestration_non_dict_summary_fails(tmp_path: pathlib.Path) -> None:
    lake = _build_valid_lake(tmp_path)
    _write_orchestration_result(
        lake, include_summary=True, summary_value="not-a-dict"
    )
    results = _run_in_process(lake)
    orch = _result_by_name(results, "orchestration_result")
    assert not orch.passed


def test_orchestration_with_summary_passes(tmp_path: pathlib.Path) -> None:
    lake = _build_valid_lake(tmp_path)
    _write_orchestration_result(lake, include_summary=True)
    results = _run_in_process(lake)
    orch = _result_by_name(results, "orchestration_result")
    assert orch.passed, orch.failures


# ---------------------------------------------------------------------------
# CLI surface (subprocess) — confirms exit codes and stdout report.
# ---------------------------------------------------------------------------

def test_cli_exit_code_passes_on_clean_lake(tmp_path: pathlib.Path) -> None:
    lake = _build_valid_lake(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--data-lake", str(lake)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SUMMARY:" in proc.stdout
    assert "0 FAIL" in proc.stdout


def test_cli_exit_code_fails_on_bad_lake(tmp_path: pathlib.Path) -> None:
    lake = _build_valid_lake(tmp_path)
    # Break ONE thing: stringified verified.
    path = lake / "store" / "artifacts" / "evals" / "few_shot" / (
        "decision_examples_v1.json"
    )
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["examples"][0]["verified"] = "true"
    path.write_text(json.dumps(doc), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--data-lake", str(lake)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "FAIL" in proc.stdout


def test_cli_missing_data_lake_fails(tmp_path: pathlib.Path) -> None:
    nonexistent = tmp_path / "does-not-exist"
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--data-lake", str(nonexistent)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "FAIL" in proc.stdout
