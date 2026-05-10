"""Phase K (GOV-10) — GOV10CertificationStep tests.

17 tests cover all 7 checks plus contractual properties (no override
parameter, schemas validate, projection banner, never-raises).

Determinism: fresh tmp_path per test, no LLM calls, no hard-coded UUIDs.
"""
from __future__ import annotations

import builtins
import datetime
import hashlib
import inspect
import json
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List

import jsonschema
import pytest

from spectrum_systems_core.governance import gov10_certification
from spectrum_systems_core.governance.gov10_certification import (
    CERTIFICATION_COST_CEILING_USD,
    GOV10CertificationStep,
)
from spectrum_systems_core.paper.publication_formatter import PublicationFormatter


_REPO_ROOT = Path(__file__).resolve().parents[2]
_FAMILY = "working_papers"
_DUMMY_HASH = "sha256:" + ("a" * 64)


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _stage_contracts(repo_root: Path) -> None:
    contracts_src = _REPO_ROOT / "contracts"
    shutil.copytree(contracts_src, repo_root / "contracts", dirs_exist_ok=True)
    pyproject = _REPO_ROOT / "pyproject.toml"
    if pyproject.is_file():
        shutil.copy(pyproject, repo_root / "pyproject.toml")


def _make_source_record(repo_root: Path, source_id: str) -> Dict[str, Any]:
    record = {
        "artifact_kind": "source_record",
        "artifact_id": str(uuid.uuid4()),
        "created_at": _now_iso(),
        "schema_ref": {
            "name": "source_record",
            "version": "1.0.0",
            "digest": _DUMMY_HASH,
        },
        "trace": {
            "trace_id": uuid.uuid4().hex,
            "span_id": uuid.uuid4().hex[:16],
            "parent_span_id": None,
        },
        "provenance": {
            "produced_by": {"component": "test_fixture", "version": "1.0.0"},
            "input_artifact_ids": [],
            "execution_fingerprint_hash": _DUMMY_HASH,
        },
        "payload": {
            "source_id": source_id,
            "source_family": _FAMILY,
            "source_type": "working_paper",
            "title": "Test Working Paper",
            "metadata": {"author": "tester"},
            "raw_path": f"raw/{_FAMILY}/{source_id}/paper.txt",
            "raw_hash": _DUMMY_HASH,
            "text_unit_count": 1,
            "processed_path": f"processed/{_FAMILY}/{source_id}",
        },
    }
    target = repo_root / "processed" / _FAMILY / source_id / "source_record.json"
    _write_json(target, record)
    return record


def _make_revised_draft(repo_root: Path, source_id: str) -> Dict[str, Any]:
    record = {
        "schema_version": "1.0.0",
        "source_id": source_id,
        "generated_at": _now_iso(),
        "revised_sections": {
            "Introduction": (
                "This paper describes spectrum coordination governance "
                f"[source: {source_id}]."
            ),
            "Conclusion": "Governance properties hold under deterministic replay.",
        },
        "applied_instruction_ids": [],
    }
    target = (
        repo_root / "processed" / _FAMILY / source_id / "paper" / "revised_draft.json"
    )
    _write_json(target, record)
    return record


def _make_paper_metadata(repo_root: Path, source_id: str) -> None:
    target = (
        repo_root / "processed" / _FAMILY / source_id / "paper" / "paper_metadata.json"
    )
    _write_json(
        target,
        {
            "title": "A Test Paper on Spectrum Coordination Governance",
            "authors": ["Alice Tester"],
            "abstract": (
                "This paper examines deterministic replay properties of "
                "the spectrum-systems-core publication pipeline under "
                "Phase K terminal certification."
            ),
        },
    )


def _required_chain_eval_cases(repo_root: Path) -> List[Dict[str, Any]]:
    """Return required eval cases targeting chain types (FPA / revised_draft /
    source_record) — the cases CHECK-5 must find result files for."""
    targets = {"formatted_paper_artifact", "revised_draft", "source_record"}
    out: List[Dict[str, Any]] = []
    for path in sorted((repo_root / "contracts" / "evals").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, list):
            continue
        for case in data:
            if not isinstance(case, dict):
                continue
            if case.get("required") and case.get("target_artifact_type") in targets:
                out.append(case)
    return out


def _write_eval_result_stubs(
    repo_root: Path, run_id: str, cases: List[Dict[str, Any]]
) -> Path:
    out_dir = repo_root / "synthesis" / run_id / "evals"
    out_dir.mkdir(parents=True, exist_ok=True)
    for case in cases:
        case_id = case.get("id") or case.get("name")
        if not case_id:
            continue
        (out_dir / f"{case_id}.json").write_text(
            json.dumps(
                {"eval_case_id": case_id, "status": "pass"},
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return out_dir


def _write_cost_record(repo_root: Path, run_id: str, cost_usd: float) -> None:
    target = repo_root / "synthesis" / run_id / "cost.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "cost_id": str(uuid.uuid4()),
        "run_id": run_id,
        "call_purpose": "test_fixture",
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost_usd": float(cost_usd),
        "model": "claude-test",
        "recorded_at": _now_iso(),
    }
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


def _make_passing_chain(tmp_path: Path) -> Dict[str, Any]:
    """Set up a complete fixture chain that passes all 7 checks."""
    _stage_contracts(tmp_path)
    source_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    _make_source_record(tmp_path, source_id)
    _make_revised_draft(tmp_path, source_id)
    _make_paper_metadata(tmp_path, source_id)

    result = PublicationFormatter().format(
        revised_draft_id=source_id, repo_root=str(tmp_path)
    )
    assert result["status"] == "success", result
    paper_id = result["artifact"]["paper_id"]

    _write_cost_record(tmp_path, run_id, 0.01)
    cases = _required_chain_eval_cases(tmp_path)
    _write_eval_result_stubs(tmp_path, run_id, cases)
    return {
        "source_id": source_id,
        "run_id": run_id,
        "paper_id": paper_id,
        "formatted_artifact": result["artifact"],
    }


# ----------------------------- tests ---------------------------------


def test_all_checks_pass_for_valid_pipeline(tmp_path: Path) -> None:
    ctx = _make_passing_chain(tmp_path)
    out = GOV10CertificationStep().certify(
        paper_id=ctx["paper_id"], run_id=ctx["run_id"], repo_root=str(tmp_path)
    )
    assert out["status"] == "PASSED", out["record"]["check_results"]
    assert out["record"]["passed_checks"] == 7
    assert out["record"]["failed_checks"] == 0
    assert isinstance(out["release_artifact"], dict)


def test_broken_lineage_fails_check2(tmp_path: Path) -> None:
    ctx = _make_passing_chain(tmp_path)
    # Wipe revised_draft so CHECK-2 can't resolve the referenced input.
    revised_path = (
        tmp_path
        / "processed"
        / _FAMILY
        / ctx["source_id"]
        / "paper"
        / "revised_draft.json"
    )
    revised_path.unlink()
    out = GOV10CertificationStep().certify(
        paper_id=ctx["paper_id"], run_id=ctx["run_id"], repo_root=str(tmp_path)
    )
    assert out["status"] == "FAILED"
    reasons = out["record"]["failure_reasons"]
    assert any(r.startswith("broken_lineage:") for r in reasons), reasons


def test_replay_hash_mismatch_fails_check3(tmp_path: Path) -> None:
    ctx = _make_passing_chain(tmp_path)
    # Mutate the formatted artifact's content_hash so replay can't match.
    formatted_path = (
        tmp_path
        / "processed"
        / _FAMILY
        / ctx["source_id"]
        / "paper"
        / "formatted"
        / f"{ctx['paper_id']}.json"
    )
    payload = json.loads(formatted_path.read_text(encoding="utf-8"))
    payload["content_hash"] = "sha256:" + ("0" * 64)
    formatted_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    out = GOV10CertificationStep().certify(
        paper_id=ctx["paper_id"], run_id=ctx["run_id"], repo_root=str(tmp_path)
    )
    assert out["status"] == "FAILED"
    reasons = out["record"]["failure_reasons"]
    assert any(r.startswith("replay_hash_mismatch:") for r in reasons), reasons


def test_missing_schema_fails_check4(tmp_path: Path) -> None:
    ctx = _make_passing_chain(tmp_path)
    schema_path = (
        tmp_path / "contracts" / "schemas" / "paper" / "revised_draft.schema.json"
    )
    schema_path.unlink()
    out = GOV10CertificationStep().certify(
        paper_id=ctx["paper_id"], run_id=ctx["run_id"], repo_root=str(tmp_path)
    )
    assert out["status"] == "FAILED"
    reasons = out["record"]["failure_reasons"]
    assert any(
        r.startswith("contract_mismatch:") or r.startswith("schema_invalid:")
        for r in reasons
    ), reasons


def test_missing_eval_result_fails_check5(tmp_path: Path) -> None:
    ctx = _make_passing_chain(tmp_path)
    # Remove one required eval-result stub.
    cases = _required_chain_eval_cases(tmp_path)
    assert cases, "expected at least one required chain eval case"
    case_id = cases[0]["id"]
    stub_path = tmp_path / "synthesis" / ctx["run_id"] / "evals" / f"{case_id}.json"
    stub_path.unlink()
    out = GOV10CertificationStep().certify(
        paper_id=ctx["paper_id"], run_id=ctx["run_id"], repo_root=str(tmp_path)
    )
    assert out["status"] == "FAILED"
    reasons = out["record"]["failure_reasons"]
    assert any(
        r.startswith("missing_eval_result:") and case_id in r for r in reasons
    ), reasons


def test_uncovered_artifact_type_fails_check6(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_passing_chain(tmp_path)

    def fake_scan(self: Any, repo_root: Any) -> Dict[str, Any]:  # noqa: ARG001
        return {
            "flagged_items": [
                {
                    "item_type": "uncovered_artifact_type",
                    "item_id": "formatted_paper_artifact",
                    "detail": "test fixture forced an uncovered type",
                    "severity": "high",
                    "recommended_action": "—",
                }
            ]
        }

    monkeypatch.setattr(
        gov10_certification.EvalCoverageScanner, "scan", fake_scan, raising=False
    )
    out = GOV10CertificationStep().certify(
        paper_id=ctx["paper_id"], run_id=ctx["run_id"], repo_root=str(tmp_path)
    )
    assert out["status"] == "FAILED"
    reasons = out["record"]["failure_reasons"]
    assert any(
        r == "uncovered_artifact_type:formatted_paper_artifact" for r in reasons
    ), reasons


def test_cost_overrun_fails_check7(tmp_path: Path) -> None:
    ctx = _make_passing_chain(tmp_path)
    # Append a cost record that pushes total above the $5.00 ceiling.
    _write_cost_record(tmp_path, ctx["run_id"], CERTIFICATION_COST_CEILING_USD + 1.00)
    out = GOV10CertificationStep().certify(
        paper_id=ctx["paper_id"], run_id=ctx["run_id"], repo_root=str(tmp_path)
    )
    assert out["status"] == "FAILED"
    reasons = out["record"]["failure_reasons"]
    assert any(r.startswith("cost_overrun:") for r in reasons), reasons


def test_cost_records_missing_fails_not_passes(tmp_path: Path) -> None:
    ctx = _make_passing_chain(tmp_path)
    cost_path = tmp_path / "synthesis" / ctx["run_id"] / "cost.jsonl"
    cost_path.unlink()
    out = GOV10CertificationStep().certify(
        paper_id=ctx["paper_id"], run_id=ctx["run_id"], repo_root=str(tmp_path)
    )
    assert out["status"] == "FAILED"
    reasons = out["record"]["failure_reasons"]
    assert any(r == "cost_records_missing" for r in reasons), reasons


def test_all_7_failures_collected_before_returning(tmp_path: Path) -> None:
    """When the formatted artifact is missing, all 7 checks must FAIL."""
    _stage_contracts(tmp_path)
    out = GOV10CertificationStep().certify(
        paper_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        repo_root=str(tmp_path),
    )
    assert out["status"] == "FAILED"
    record = out["record"]
    assert record["failed_checks"] == 7
    assert len(record["check_results"]) == 7
    statuses = [c["status"] for c in record["check_results"]]
    assert statuses == ["FAILED"] * 7
    assert len(record["failure_reasons"]) == 7


def test_failed_certification_has_non_empty_reasons(tmp_path: Path) -> None:
    _stage_contracts(tmp_path)
    out = GOV10CertificationStep().certify(
        paper_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        repo_root=str(tmp_path),
    )
    assert out["status"] == "FAILED"
    assert out["record"]["failure_reasons"], "FAILED record must list at least one reason"
    assert all(r for r in out["record"]["failure_reasons"])


def test_no_release_artifact_on_failed_cert(tmp_path: Path) -> None:
    _stage_contracts(tmp_path)
    paper_id = str(uuid.uuid4())
    out = GOV10CertificationStep().certify(
        paper_id=paper_id, run_id=str(uuid.uuid4()), repo_root=str(tmp_path)
    )
    assert out["status"] == "FAILED"
    assert out["release_artifact"] is None
    release_path = tmp_path / "paper" / "released" / f"{paper_id}.json"
    assert not release_path.exists()


def test_release_artifact_written_on_passed_cert(tmp_path: Path) -> None:
    ctx = _make_passing_chain(tmp_path)
    out = GOV10CertificationStep().certify(
        paper_id=ctx["paper_id"], run_id=ctx["run_id"], repo_root=str(tmp_path)
    )
    assert out["status"] == "PASSED"
    release_path = tmp_path / "paper" / "released" / f"{ctx['paper_id']}.json"
    assert release_path.is_file()
    payload = json.loads(release_path.read_text(encoding="utf-8"))
    assert payload["paper_id"] == ctx["paper_id"]
    assert payload["formatted_paper_artifact_id"] == ctx["paper_id"]


def test_release_artifact_schema_validates(tmp_path: Path) -> None:
    ctx = _make_passing_chain(tmp_path)
    out = GOV10CertificationStep().certify(
        paper_id=ctx["paper_id"], run_id=ctx["run_id"], repo_root=str(tmp_path)
    )
    assert out["status"] == "PASSED"
    schema = json.loads(
        (
            tmp_path
            / "contracts"
            / "schemas"
            / "certification"
            / "release_artifact.schema.json"
        ).read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(schema).validate(out["release_artifact"])


def test_done_certification_record_schema_validates(tmp_path: Path) -> None:
    ctx = _make_passing_chain(tmp_path)
    out = GOV10CertificationStep().certify(
        paper_id=ctx["paper_id"], run_id=ctx["run_id"], repo_root=str(tmp_path)
    )
    schema = json.loads(
        (
            tmp_path
            / "contracts"
            / "schemas"
            / "certification"
            / "done_certification_record.schema.json"
        ).read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(schema).validate(out["record"])


def test_no_override_parameter_in_signature() -> None:
    sig = inspect.signature(GOV10CertificationStep.certify)
    forbidden = {"override", "bypass", "force", "human_override"}
    actual = set(sig.parameters.keys())
    assert not (forbidden & actual), (
        f"certify() must not accept override/bypass; got {sorted(actual)}"
    )


def test_view_only_banner_first_line_of_projection(tmp_path: Path) -> None:
    ctx = _make_passing_chain(tmp_path)
    vault_root = tmp_path / "vault"
    out = GOV10CertificationStep().certify(
        paper_id=ctx["paper_id"],
        run_id=ctx["run_id"],
        repo_root=str(tmp_path),
        vault_root=str(vault_root),
    )
    assert out["status"] == "PASSED"
    md_path = vault_root / "Certifications" / f"{out['certification_id']}.md"
    assert md_path.is_file()
    first_line = md_path.read_text(encoding="utf-8").splitlines()[0]
    assert first_line == (
        "<!-- VIEW ONLY — generated by GOV10CertificationStep — do not edit -->"
    )


def test_certify_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stage_contracts(tmp_path)

    real_open = builtins.open

    def _exploding_open(*args: Any, **kwargs: Any) -> Any:
        raise OSError("synthetic IO failure")

    # Have certify call into PublicationFormatter, which uses pathlib.read_text.
    # We patch Path.read_text to throw, forcing the certifier through every
    # safe-read branch.
    real_read_text = Path.read_text

    def _exploding_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        raise OSError("synthetic IO failure")

    monkeypatch.setattr(Path, "read_text", _exploding_read_text)
    try:
        out = GOV10CertificationStep().certify(
            paper_id=str(uuid.uuid4()),
            run_id=str(uuid.uuid4()),
            repo_root=str(tmp_path),
        )
    finally:
        monkeypatch.setattr(Path, "read_text", real_read_text)
    assert isinstance(out, dict)
    assert out["status"] == "FAILED"
    assert out["release_artifact"] is None
