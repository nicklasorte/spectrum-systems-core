"""Integration contract test for ``scripts/create_cross_meeting_synthesis.py``.

CLAUDE.md non-negotiable: a script that reads a pipeline artifact (the
promoted ``meeting_minutes`` envelope) and calls ``validate_artifact``
must have an integration test that

  1. Uses ``tests/integration/fixtures.py`` factories — never a
     hand-rolled dict — to produce the artifact via the REAL writer
     (here: the real LLM governed loop + ``write_promoted_artifact``,
     through ``make_promoted_meeting_minutes_artifact``).
  2. Writes artifacts to a real temp directory (not mocked).
  3. Calls the script via ``subprocess.run`` against the temp dir.
  4. Asserts the correct output on disk (not just the return code).

This catches writer/reader field drift at the fixture-factory level
before the script logic runs — the exact bug class CLAUDE.md cites.
The Opus transport is the explicit offline env-var seam
(``CROSS_MEETING_SYNTHESIS_STUB_RESPONSE``) so CI needs no API key.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.integration.fixtures import make_promoted_meeting_minutes_artifact

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "create_cross_meeting_synthesis.py"
MODEL = "claude-opus-stub-model"

SID_A = "tig-kickoff-transcript-20251101"
SID_B = "tig-followup-transcript-20251218"

DECISIONS_A = ["The group approved the 7 GHz downlink threshold."]
ACTIONS_A = ["DoD will submit revised ERP values before the next session."]
QUESTIONS_A = ["What is the coordination distance for federal incumbents?"]

DECISIONS_B = ["The group deferred the aggregate interference methodology."]
ACTIONS_B = ["NTIA will circulate the revised methodology."]
QUESTIONS_B = ["What aggregate interference limit applies in band?"]

NARRATIVE = (
    "The TIG opened with a kickoff that approved a provisional 7 GHz "
    "downlink threshold and then, in the follow-up session, deferred "
    "the aggregate interference methodology. The decision trajectory "
    "is convergence on the downlink rule while the methodology remains "
    "the dominant open question carried forward across the corpus."
)


def _stub_response() -> str:
    return json.dumps(
        {
            "decision_threads": [
                {
                    "topic": "7 GHz downlink threshold",
                    "summary": "Threshold approved then methodology deferred.",
                    "decisions": [
                        {
                            "source_id": SID_A,
                            "text": DECISIONS_A[0],
                            "regulatory_verb": "approved",
                            "status": "active",
                        },
                        {
                            "source_id": SID_B,
                            "text": DECISIONS_B[0],
                            "regulatory_verb": "deferred",
                            "status": "deferred",
                        },
                    ],
                }
            ],
            "open_actions": [
                {
                    "text": ACTIONS_A[0],
                    "owner": "DoD",
                    "assigned_meeting": SID_A,
                    "closed_meeting": None,
                },
                {
                    "text": ACTIONS_B[0],
                    "owner": "NTIA",
                    "assigned_meeting": SID_B,
                    "closed_meeting": SID_B,
                },
            ],
            "claim_drift": [
                {
                    "topic": "coordination distance",
                    "drift_detected": False,
                    "drift_summary": None,
                    "instances": [
                        {
                            "source_id": SID_A,
                            "text": "Coordination distance is large.",
                            "speaker": "NTIA Lead",
                        }
                    ],
                }
            ],
            "unresolved_questions": [
                {
                    "text": QUESTIONS_B[0],
                    "raised_meeting": SID_B,
                    "resolution": None,
                    "resolved": False,
                }
            ],
            "narrative_summary": NARRATIVE,
        }
    )


def _seed(tmp_path: Path) -> Path:
    dl = tmp_path / "data-lake"
    store = dl / "store"
    a = make_promoted_meeting_minutes_artifact(
        lake_root=store,
        source_id=SID_A,
        decisions=DECISIONS_A,
        action_items=ACTIONS_A,
        open_questions=QUESTIONS_A,
    )
    b = make_promoted_meeting_minutes_artifact(
        lake_root=store,
        source_id=SID_B,
        decisions=DECISIONS_B,
        action_items=ACTIONS_B,
        open_questions=QUESTIONS_B,
    )
    assert a.name.startswith("meeting_minutes__") and a.is_file()
    assert b.name.startswith("meeting_minutes__") and b.is_file()
    return dl


def _run(args: list[str], *, stub: str | None):
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # prove no live model path
    if stub is not None:
        env["CROSS_MEETING_SYNTHESIS_STUB_RESPONSE"] = stub
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )


def _synthesis_files(dl: Path) -> list[Path]:
    sdir = dl / "store" / "artifacts" / "synthesis"
    return sorted(sdir.glob("cross_meeting_synthesis_*.json"))


def test_subprocess_writes_validated_synthesis(tmp_path: Path) -> None:
    dl = _seed(tmp_path)
    result = _run(
        ["--data-lake", str(dl), "--model", MODEL, "--min-meetings", "2"],
        stub=_stub_response(),
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )

    files = _synthesis_files(dl)
    assert len(files) == 1, "synthesis artifact not written at contract path"
    art = json.loads(files[0].read_text(encoding="utf-8"))

    assert art["artifact_type"] == "cross_meeting_synthesis"
    assert "artifact_kind" not in json.dumps(art)
    assert art["schema_version"] == "1.0.0"
    assert art["model_id"] == MODEL
    assert art["provenance"]["produced_by"] == (
        "cross_meeting_synthesis_workflow"
    )
    assert sorted(art["source_ids"]) == sorted([SID_A, SID_B])
    assert art["corpus_span"]["total_meetings"] == 2
    assert art["corpus_span"]["earliest_meeting"] == "2025-11-01"
    assert art["corpus_span"]["latest_meeting"] == "2025-12-18"
    assert isinstance(art["decision_threads"], list)
    assert len(art["narrative_summary"]) >= 100

    # open_actions status recomputed: A has no closure -> open; B was
    # closed in SID_B (a corpus meeting) -> closed (NOT open).
    by_text = {a["text"]: a for a in art["open_actions"]}
    assert by_text[ACTIONS_A[0]]["status"] == "open"
    assert by_text[ACTIONS_A[0]]["closed_meeting"] is None
    assert by_text[ACTIONS_B[0]]["status"] == "closed"
    assert by_text[ACTIONS_B[0]]["closed_meeting"] == SID_B
    assert by_text[ACTIONS_B[0]]["closed_date"] == "2025-12-18"

    # Every id is a re-stamped UUID5.
    import uuid as _uuid

    for t in art["decision_threads"]:
        assert _uuid.UUID(t["thread_id"]).version == 5
    for a in art["open_actions"]:
        assert _uuid.UUID(a["action_id"]).version == 5

    # provenance.input_artifact_ids ties back to the promoted envelopes.
    assert len(art["provenance"]["input_artifact_ids"]) == 2

    summary = json.loads(result.stdout)
    assert summary["meetings_synthesized"] == 2
    assert summary["status"] == "success"


def test_subprocess_dry_run_writes_nothing(tmp_path: Path) -> None:
    dl = _seed(tmp_path)
    result = _run(
        [
            "--data-lake",
            str(dl),
            "--model",
            MODEL,
            "--min-meetings",
            "2",
            "--dry-run",
        ],
        stub=_stub_response(),
    )
    assert result.returncode == 0, result.stderr
    assert not _synthesis_files(dl), "dry-run must not write the artifact"
    summary = json.loads(result.stdout)
    assert summary["dry_run"] is True
    assert summary["model"] == MODEL


def test_subprocess_insufficient_corpus_halts(tmp_path: Path) -> None:
    dl = tmp_path / "data-lake"
    store = dl / "store"
    make_promoted_meeting_minutes_artifact(
        lake_root=store,
        source_id=SID_A,
        decisions=DECISIONS_A,
        action_items=ACTIONS_A,
        open_questions=QUESTIONS_A,
    )
    result = _run(
        ["--data-lake", str(dl), "--model", MODEL, "--min-meetings", "2"],
        stub=_stub_response(),
    )
    assert result.returncode != 0
    assert "insufficient_corpus" in result.stdout
    assert not _synthesis_files(dl)


def test_subprocess_missing_model_halts(tmp_path: Path) -> None:
    dl = _seed(tmp_path)
    result = _run(
        ["--data-lake", str(dl), "--model", "", "--min-meetings", "2"],
        stub=_stub_response(),
    )
    assert result.returncode != 0
    assert "missing_model" in result.stdout
    assert not _synthesis_files(dl)
