"""Phase Z.5: full pipeline validation on the gm-001 gold fixture.

This is a controlled "real-structure" test — gm-001 is a synthetic
spectrum-policy transcript, not an actual government proceeding,
but it exercises the same code path a real meeting would.

Asserts:

  1. All four workflows promote.
  2. ``extraction_precision`` and ``regulatory_verb`` evals pass for
     ``meeting_minutes``.
  3. The debug artifact carries no ``inspect_next`` hints (no
     pending operator follow-up).
  4. Every gold decision text matches at least one extracted decision
     in the promoted artifact within LCS >= 0.7.
"""
from __future__ import annotations

import difflib
import json
import shutil
from pathlib import Path

from spectrum_systems_core.data_lake import run_transcript_pipeline
from spectrum_systems_core.evals import (
    EXTRACTION_PRECISION_EVAL_TYPE,
    LCS_THRESHOLD,
    REGULATORY_VERB_EVAL_TYPE,
)

FIXTURE = (
    Path(__file__).parent.parent / "fixtures"
    / "golden_meetings" / "gm-001-spectrum-planning"
)


def _seed(tmp_path: Path, meeting_id: str) -> None:
    dst = tmp_path / "raw" / "meetings" / meeting_id
    dst.mkdir(parents=True)
    shutil.copy(FIXTURE / "transcript.txt", dst / "transcript.txt")
    shutil.copy(FIXTURE / "metadata.json", dst / "metadata.json")


def _matches_gold(extracted_text: str, expected_text: str) -> bool:
    """Substring match wins; otherwise fall back to LCS >= 0.7.

    Same algorithm as ``extraction_precision`` — keeps the gold-match
    semantics consistent across the eval and the integration test."""
    if expected_text.lower() in extracted_text.lower():
        return True
    ratio = difflib.SequenceMatcher(
        None,
        extracted_text.lower(),
        expected_text.lower(),
    ).ratio()
    return ratio >= LCS_THRESHOLD


def _eval_status(result, eval_type: str) -> str | None:
    for r in result.eval_results:
        if r.payload.get("eval_type") == eval_type:
            return r.payload.get("status")
    return None


def test_full_pipeline_on_golden_fixture(tmp_path):
    meeting_id = "gm-001-spectrum-planning"
    _seed(tmp_path, meeting_id)
    expected = json.loads(
        (FIXTURE / "expected.json").read_text(encoding="utf-8")
    )

    # ---- 1. All four workflows promote --------------------------------
    workflow_results = {}
    for workflow in (
        "meeting_minutes",
        "decision_brief",
        "agency_question_summary",
        "meeting_action_log",
    ):
        # Use a fresh subdir per workflow so writes don't collide on the
        # shared processed/ path.
        wf_root = tmp_path / workflow
        _seed(wf_root, meeting_id)
        result = run_transcript_pipeline(
            lake_root=wf_root, meeting_id=meeting_id, workflow_name=workflow
        )
        workflow_results[workflow] = result
        assert result.promoted is True, (
            f"workflow {workflow} did not promote: "
            f"decision={result.control_decision.payload.get('decision')} "
            f"reasons={result.control_decision.payload.get('reason_codes')}"
        )

    meeting_minutes_result = workflow_results["meeting_minutes"]

    # ---- 2. Precision + verb evals pass on meeting_minutes ------------
    assert (
        _eval_status(meeting_minutes_result, EXTRACTION_PRECISION_EVAL_TYPE)
        == "pass"
    ), (
        f"extraction_precision did not pass: "
        f"{[r.payload for r in meeting_minutes_result.eval_results]}"
    )
    assert (
        _eval_status(meeting_minutes_result, REGULATORY_VERB_EVAL_TYPE)
        == "pass"
    ), (
        f"regulatory_verb did not pass: "
        f"{[r.payload for r in meeting_minutes_result.eval_results]}"
    )

    # ---- 3. debug artifact has no inspect_next hints ------------------
    debug = meeting_minutes_result.debug_report
    assert debug is not None
    inspect_next = debug.get("inspect_next", [])
    assert inspect_next == [], (
        f"promoted run unexpectedly carries inspect_next hints: "
        f"{inspect_next}"
    )

    # ---- 4. Gold-set match: every expected decision shows up ----------
    extracted_decisions = meeting_minutes_result.target.payload.get(
        "decisions", []
    )
    # decisions are strings in the legacy extractor; coerce for match.
    extracted_texts = [
        d if isinstance(d, str) else d.get("text", "")
        for d in extracted_decisions
    ]
    missing: list[str] = []
    for gold in expected["decisions"]:
        gold_text = gold["text"]
        if not any(_matches_gold(et, gold_text) for et in extracted_texts):
            missing.append(gold_text)
    assert not missing, (
        f"Gold decisions not found in extracted output: {missing} "
        f"(extracted: {extracted_texts})"
    )


def test_gold_actions_match_within_lcs_threshold(tmp_path):
    """Same gold-set match logic applied to action items, so a
    regression that drops one expected action surfaces here too."""
    meeting_id = "gm-001-spectrum-planning"
    _seed(tmp_path, meeting_id)
    expected = json.loads(
        (FIXTURE / "expected.json").read_text(encoding="utf-8")
    )
    result = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id=meeting_id,
        workflow_name="meeting_minutes",
    )
    extracted_actions = result.target.payload.get("action_items", [])
    extracted_texts = [
        a if isinstance(a, str) else a.get("action", a.get("text", ""))
        for a in extracted_actions
    ]
    missing: list[str] = []
    for gold in expected["actions"]:
        gold_text = gold["text"]
        if not any(_matches_gold(et, gold_text) for et in extracted_texts):
            missing.append(gold_text)
    assert not missing, (
        f"Gold actions not found in extracted output: {missing} "
        f"(extracted: {extracted_texts})"
    )


def test_promoted_artifact_carries_agenda_metadata_in_source_record(tmp_path):
    """Z.4 sanity: agenda metadata survives the full pipeline run."""
    meeting_id = "gm-001-spectrum-planning"
    _seed(tmp_path, meeting_id)
    result = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id=meeting_id,
        workflow_name="meeting_minutes",
    )
    sr = json.loads(
        Path(result.source_record_path).read_text(encoding="utf-8")
    )
    chunks = sr["payload"]["chunks"]
    assert any(c.get("agenda_item_id") == "item-1" for c in chunks)
    assert any(c.get("agenda_item_id") == "item-2" for c in chunks)
