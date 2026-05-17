"""SSC-034 — Debug Report Upgrade.

A blocked workflow's debug report must let a new engineer answer:
- Input loaded?
- Workflow run?
- Artifact produced?
- Which eval failed?
- Which control decision?
- Was JSON written?
- What should the operator inspect next?
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from spectrum_systems_core.data_lake import process_meeting

FIXTURES = Path(__file__).parent / "fixtures" / "golden_meetings"


def _seed(lake_root: Path, meeting_id: str) -> None:
    src = FIXTURES / meeting_id
    dst = lake_root / "raw" / "meetings" / meeting_id
    dst.mkdir(parents=True)
    shutil.copy(src / "transcript.txt", dst / "transcript.txt")
    shutil.copy(src / "metadata.json", dst / "metadata.json")


def _debug_for(lake_root: Path, meeting_id: str, workflow: str) -> dict:
    processed = lake_root / "processed" / "meetings" / meeting_id
    debug_files = sorted(processed.glob("debug__*.json"))
    for p in debug_files:
        body = json.loads(p.read_text(encoding="utf-8"))
        if body.get("workflow_name") == workflow:
            return body
    raise AssertionError(f"no debug report for workflow {workflow!r}")


def test_blocked_decision_brief_debug_explains_missing_signal(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    rep = _debug_for(tmp_path, meeting_id, "decision_brief")
    assert rep["outcome"] == "rejected"
    assert rep["control"]["decision"] == "block"
    fp = rep["failure_path"]
    assert fp["input_loaded"] is True
    assert fp["workflow_ran"] is True
    assert fp["artifact_produced"] is True
    assert fp["control_decision"] == "block"
    assert fp["json_written"] is False
    inspect_next = rep.get("inspect_next") or []
    assert inspect_next, "blocked debug must list inspect_next hints"
    joined = " ".join(inspect_next)
    # The block on this fixture is failed:transcript_evidence (or a
    # missing/empty required field). Either way the hint must mention
    # the transcript or the missing field.
    assert "transcript" in joined or "field" in joined


def test_blocked_agency_question_summary_debug_names_empty_field(tmp_path):
    """The 'good' golden has no AGENCY: line, so empty_required_field:agency."""
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    rep = _debug_for(tmp_path, meeting_id, "agency_question_summary")
    assert rep["outcome"] == "rejected"
    # The granular `empty_required_field:agency` reason originates on the
    # eval, not on the control decision (which compacts it to
    # `failed:non_empty_payload`). The debug report must still surface
    # the granular code somewhere a reader can find it.
    failed_reason_codes = [
        rc
        for ev in rep["evals"]["failed"]
        for rc in ev.get("reason_codes", [])
    ]
    assert any(
        rc.startswith("empty_required_field:") for rc in failed_reason_codes
    ), failed_reason_codes
    inspect_next = " ".join(rep.get("inspect_next") or [])
    # SSC-034: the inspection hint must surface the empty field name.
    assert "agency" in inspect_next or "empty value" in inspect_next


def test_promoted_debug_lists_passing_evals(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    rep = _debug_for(tmp_path, meeting_id, "meeting_minutes")
    assert rep["outcome"] == "promoted"
    assert rep["failure_path"]["json_written"] is True
    eval_types = [e["eval_type"] for e in rep["evals"]["passed"]]
    for required in ("non_empty_payload", "source_grounding", "transcript_evidence"):
        assert required in eval_types


def test_debug_report_is_byte_deterministic_across_runs(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)

    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    processed = tmp_path / "processed" / "meetings" / meeting_id
    first = {
        p.name: p.read_bytes() for p in sorted(processed.glob("debug__*.json"))
    }

    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    second = {
        p.name: p.read_bytes() for p in sorted(processed.glob("debug__*.json"))
    }
    assert first == second


def test_debug_report_text_remains_plain_json(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    processed = tmp_path / "processed" / "meetings" / meeting_id
    for p in processed.glob("debug__*.json"):
        body = p.read_text(encoding="utf-8")
        # Plain JSON (single trailing newline allowed).
        json.loads(body)
        assert body.endswith("\n")
