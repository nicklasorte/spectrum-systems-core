"""Phase AA — integration & contract tests.

Per CLAUDE.md: artifacts come from the fixture factories (which call
the REAL AA.4/AA.5/AA.7 producers), are written to a real temp dir,
and the new artifact-touching script
(``scripts/check_harness_code_pr_eligibility.py``) is exercised via
subprocess against that directory. No mocked gates.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from spectrum_systems_core.data_lake.serialize import artifact_to_dict
from tests.integration.fixtures import (
    make_harness_code_candidate_artifact,
    make_harness_code_candidate_evaluation_artifact,
    make_harness_search_result_artifact,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"


def _run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


def _validate(payload: dict, expected_type: str) -> None:
    sys.path.insert(0, str(SCRIPTS))
    from _artifact_validator import validate_artifact  # noqa: WPS433

    validate_artifact(payload, expected_type)


@pytest.mark.parametrize(
    "factory,expected_type",
    [
        (make_harness_code_candidate_artifact, "harness_code_candidate"),
        (
            make_harness_code_candidate_evaluation_artifact,
            "harness_code_candidate_evaluation",
        ),
        (make_harness_search_result_artifact, "harness_search_result"),
    ],
)
def test_factory_artifacts_validate_against_schema(factory, expected_type):
    art = factory()
    assert art.payload["artifact_type"] == expected_type
    _validate(art.payload, expected_type)


def test_check_harness_code_pr_eligibility_eligible(tmp_path):
    art = make_harness_code_candidate_evaluation_artifact(eligible=True)
    path = tmp_path / "eval.json"
    path.write_text(
        json.dumps(artifact_to_dict(art)), encoding="utf-8"
    )
    res = _run("check_harness_code_pr_eligibility.py", str(path))
    assert res.returncode == 0, res.stdout + res.stderr
    out = json.loads(res.stdout)
    assert out["eligible"] is True
    assert out["reason"] == "all conditions met"


def test_check_harness_code_pr_eligibility_ineligible(tmp_path):
    art = make_harness_code_candidate_evaluation_artifact(eligible=False)
    path = tmp_path / "eval.json"
    path.write_text(
        json.dumps(artifact_to_dict(art)), encoding="utf-8"
    )
    res = _run("check_harness_code_pr_eligibility.py", str(path))
    assert res.returncode == 1, res.stdout + res.stderr
    out = json.loads(res.stdout)
    assert out["eligible"] is False
    assert out["reason"] != "all conditions met"


def test_check_harness_code_pr_eligibility_missing_artifact(tmp_path):
    res = _run(
        "check_harness_code_pr_eligibility.py",
        str(tmp_path / "nope.json"),
    )
    assert res.returncode == 2
    out = json.loads(res.stdout)
    assert out["eligible"] is False


def test_check_harness_code_pr_eligibility_flat_payload(tmp_path):
    # The script must also accept the bare evaluation payload (not just
    # the full envelope).
    art = make_harness_code_candidate_evaluation_artifact(eligible=True)
    path = tmp_path / "eval_flat.json"
    path.write_text(json.dumps(art.payload), encoding="utf-8")
    res = _run("check_harness_code_pr_eligibility.py", str(path))
    assert res.returncode == 0, res.stdout + res.stderr
    assert json.loads(res.stdout)["eligible"] is True
