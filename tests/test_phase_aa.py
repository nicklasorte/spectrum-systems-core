"""Phase AA — Meta-Harness outer loop: gate-table + reproduction tests.

Every rejection test feeds a REAL input through the REAL gate (no
mocked validators, no mocked eligibility). The convergence test drives
the REAL outer-loop driver for 3 iterations rather than asserting a
function return. Filesystem assertions check what landed on disk, not
just return codes.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from spectrum_systems_core.data_lake import process_meeting
from spectrum_systems_core.harness.code_candidate_evaluator import (
    CodeCandidateEvaluatorError,
    evaluate_code_candidate,
)
from spectrum_systems_core.harness.code_pr_eligibility import (
    evaluate_code_eligibility,
)
from spectrum_systems_core.harness.harness_mutation_validator import (
    validate_diff,
)
from spectrum_systems_core.harness.harness_search import (
    finalize_code_proposal,
    run_harness_search,
)
from spectrum_systems_core.harness.pareto_frontier import (
    load_pareto_frontier,
    update_pareto_frontier,
)
from spectrum_systems_core.harness.proposer import (
    ProposerContext,
    ProposerProposal,
    propose,
)
from spectrum_systems_core.harness.score_summary_writer import (
    ScoreSummaryError,
    write_score_summary,
)
from spectrum_systems_core.harness.trace_capture import (
    build_chunk_experience_rows,
    harness_snapshot_dirname,
    trace_capture_enabled,
    write_harness_snapshot,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).parent / "fixtures" / "golden_meetings"
TID = "m-2025-12-18-7ghz-downlink-tig-kickoff"
HOLDOUT = "m-2025-11-20-ntia-coordination-session"

_ALLOWED_DIFF = (
    "diff --git a/src/spectrum_systems_core/extraction/chunker.py "
    "b/src/spectrum_systems_core/extraction/chunker.py\n"
    "--- a/src/spectrum_systems_core/extraction/chunker.py\n"
    "+++ b/src/spectrum_systems_core/extraction/chunker.py\n"
    "@@ -1 +1 @@\n-# old\n+# new\n"
)
_FORBIDDEN_DIFF = (
    "diff --git a/src/spectrum_systems_core/control/decision.py "
    "b/src/spectrum_systems_core/control/decision.py\n"
    "--- a/src/spectrum_systems_core/control/decision.py\n"
    "+++ b/src/spectrum_systems_core/control/decision.py\n"
    "@@ -1 +1 @@\n-x\n+y\n"
)
_CLAUDE_MD_DIFF = (
    "diff --git a/CLAUDE.md b/CLAUDE.md\n"
    "--- a/CLAUDE.md\n+++ b/CLAUDE.md\n@@ -1 +1 @@\n-a\n+b\n"
)
_MIXED_DIFF = _ALLOWED_DIFF + _FORBIDDEN_DIFF


def _seed(lake_root: Path, meeting_id: str = "m-golden-good") -> str:
    dst = lake_root / "raw" / "meetings" / meeting_id
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy(GOLDEN / meeting_id / "transcript.txt", dst / "transcript.txt")
    shutil.copy(GOLDEN / meeting_id / "metadata.json", dst / "metadata.json")
    return meeting_id


# ======================================================================
# AA.1 — execution trace capture
# ======================================================================
def test_aa1_trace_capture_disabled_omits_new_fields(tmp_path):
    mid = _seed(tmp_path)
    result = process_meeting(lake_root=tmp_path, meeting_id=mid)
    r = result.pipeline_results[0]

    enabled = build_chunk_experience_rows(
        r,
        chunk_traces=[{"chunk_id": "c1", "extraction_result": "extracted"}],
        trace_enabled=True,
    )
    assert any(row.get("chunk_id") for row in enabled)

    disabled = build_chunk_experience_rows(
        r,
        chunk_traces=[{"chunk_id": "c1", "extraction_result": "extracted"}],
        trace_enabled=False,
    )
    assert len(disabled) == 1
    assert "chunk_id" not in disabled[0]
    assert "extraction_result" not in disabled[0]
    # Existing fields still present.
    assert disabled[0]["workflow_name"] == r.workflow_name


def test_aa1_zero_chunks_produces_empty_jsonl_not_missing(tmp_path):
    mid = _seed(tmp_path)
    result = process_meeting(lake_root=tmp_path, meeting_id=mid)
    r = result.pipeline_results[0]

    rows = build_chunk_experience_rows(
        r, chunk_traces=None, trace_enabled=True
    )
    # Zero chunk traces -> exactly the base row, never zero rows.
    assert len(rows) == 1
    assert "chunk_id" not in rows[0]

    # And the on-disk file the pipeline wrote exists and is non-empty.
    exp = (
        tmp_path / "processed" / "meetings" / mid
        / "experience_history.jsonl"
    )
    assert exp.is_file()
    assert exp.read_text(encoding="utf-8").strip() != ""


def test_aa1_per_chunk_rows_have_nonnull_chunk_id_and_unique_ids(tmp_path):
    mid = _seed(tmp_path)
    r = process_meeting(
        lake_root=tmp_path, meeting_id=mid
    ).pipeline_results[0]
    traces = [
        {"chunk_id": "chunk-1", "extraction_result": "extracted"},
        {"chunk_id": "chunk-2", "extraction_result": "empty"},
    ]
    rows = build_chunk_experience_rows(
        r, chunk_traces=traces, trace_enabled=True
    )
    assert len(rows) == 2
    assert all(row["chunk_id"] for row in rows)
    assert rows[0]["experience_id"] != rows[1]["experience_id"]


def test_aa1_snapshot_written_with_commit_sha(tmp_path):
    snap = write_harness_snapshot(
        processed_dir=tmp_path,
        trial_id="trial-x",
        repo_root=REPO_ROOT,
    )
    sha_file = (
        tmp_path / harness_snapshot_dirname("trial-x") / "commit_sha.txt"
    )
    assert sha_file.is_file()
    assert sha_file.read_text(encoding="utf-8").strip() != ""
    assert "chunker.py" in snap.copied_files


def test_aa1_snapshot_written_even_on_blocked_run(tmp_path):
    # m-golden-malformed blocks all workflows; the snapshot must still
    # be written so the proposer can read the failed trial.
    mid = _seed(tmp_path, "m-golden-weak")
    result = process_meeting(lake_root=tmp_path, meeting_id=mid)
    assert all(not r.promoted for r in result.pipeline_results)
    snaps = list(
        (tmp_path / "processed" / "meetings" / mid).glob(
            "harness_snapshot__*"
        )
    )
    assert len(snaps) == 1
    assert (snaps[0] / "commit_sha.txt").is_file()


def test_aa1_env_rollback_disables_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACE_CAPTURE_ENABLED", "false")
    assert trace_capture_enabled() is False
    mid = _seed(tmp_path)
    process_meeting(lake_root=tmp_path, meeting_id=mid)
    snaps = list(
        (tmp_path / "processed" / "meetings" / mid).glob(
            "harness_snapshot__*"
        )
    )
    assert snaps == []
    exp = (
        tmp_path / "processed" / "meetings" / mid
        / "experience_history.jsonl"
    )
    assert exp.is_file()
    for line in exp.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        assert "chunk_id" not in row
        assert "workflow_name" in row


# ======================================================================
# AA.2 — score summary
# ======================================================================
def _snapshot(tmp_path, sha="abc123", trial="trial-1"):
    write_harness_snapshot(
        processed_dir=tmp_path,
        trial_id=trial,
        repo_root=REPO_ROOT,
        commit_sha=sha,
    )


def test_aa2_score_summary_valid_with_required_fields(tmp_path):
    _snapshot(tmp_path, "abc123", "trial-1")
    out = write_score_summary(
        processed_dir=tmp_path,
        trial_id="trial-1",
        transcript_id=TID,
        expected_commit_sha="abc123",
        total_f1=0.71,
        per_type_f1={"decision": 0.7},
        false_negative_count=3,
        false_positive_count=1,
        clock=lambda: "1970-01-01T00:00:00+00:00",
    )
    doc = json.loads(out.read_text(encoding="utf-8"))
    from spectrum_systems_core.harness.score_summary_writer import (
        REQUIRED_FIELDS,
    )

    for f in REQUIRED_FIELDS:
        assert f in doc
    assert doc["harness_snapshot_commit_sha"] == "abc123"


def test_aa2_commit_sha_mismatch_halts(tmp_path):
    _snapshot(tmp_path, "abc123", "trial-1")
    with pytest.raises(ScoreSummaryError) as exc:
        write_score_summary(
            processed_dir=tmp_path,
            trial_id="trial-1",
            transcript_id=TID,
            expected_commit_sha="DIFFERENT",
            total_f1=0.5,
        )
    assert exc.value.reason_code == "commit_sha_mismatch"
    assert not list(tmp_path.glob("score_summary__*.json"))


def test_aa2_missing_snapshot_sha_halts(tmp_path):
    with pytest.raises(ScoreSummaryError) as exc:
        write_score_summary(
            processed_dir=tmp_path,
            trial_id="no-snapshot",
            transcript_id=TID,
            expected_commit_sha="abc123",
            total_f1=0.5,
        )
    assert exc.value.reason_code == "commit_sha_unavailable"


def test_aa2_grep_total_f1_across_three_trials(tmp_path):
    processed = tmp_path / "processed"
    processed.mkdir()
    for i in range(3):
        t = f"trial-{i}"
        _snapshot(processed, f"sha{i}", t)
        write_score_summary(
            processed_dir=processed,
            trial_id=t,
            transcript_id=TID,
            expected_commit_sha=f"sha{i}",
            total_f1=0.6 + i * 0.05,
            clock=lambda: "1970-01-01T00:00:00+00:00",
        )
    hits = [
        p
        for p in processed.glob("score_summary__*.json")
        if "total_f1" in p.read_text(encoding="utf-8")
    ]
    assert len(hits) == 3


# ======================================================================
# AA.3 — allowlist validator (real diffs through the real validator)
# ======================================================================
def test_aa3_allowed_file_valid():
    assert validate_diff(_ALLOWED_DIFF).valid is True


def test_aa3_forbidden_file_rejected():
    r = validate_diff(_FORBIDDEN_DIFF)
    assert r.valid is False
    assert "src/spectrum_systems_core/control/decision.py" in r.rejected_paths


def test_aa3_claude_md_rejected():
    assert validate_diff(_CLAUDE_MD_DIFF).valid is False


def test_aa3_mixed_diff_rejected():
    r = validate_diff(_MIXED_DIFF)
    assert r.valid is False
    assert "src/spectrum_systems_core/control/decision.py" in r.rejected_paths


def test_aa3_missing_contract_rejects_by_default(tmp_path):
    missing = tmp_path / "nope.md"
    r = validate_diff(_ALLOWED_DIFF, contract_path=missing)
    assert r.valid is False
    assert r.reason == "allowlist_unavailable"


def test_aa3_empty_diff_rejected():
    r = validate_diff("")
    assert r.valid is False
    assert r.reason == "no_paths_in_diff"


# ======================================================================
# AA.4 — proposer
# ======================================================================
def _ctx():
    return ProposerContext(
        transcript_id=TID,
        current_trial_id="trial-cur",
        score_summaries=[
            {"trial_id": "trial-a", "total_f1": 0.60},
            {"trial_id": "trial-b", "total_f1": 0.65},
        ],
    )


def test_aa4_proposer_emits_candidate_with_trial_ids_read():
    def fake_opus(_sys, _ctx):
        return {
            "candidate_type": "B",
            "trial_ids_read": ["trial-a"],
            "proposer_reasoning": "r",
            "hypothesis": "h",
            "predicted_improvement": "p",
            "proposed_diff": _ALLOWED_DIFF,
        }

    p = propose(_ctx(), opus_call=fake_opus)
    assert p.candidate_type == "B"
    assert "trial-a" in p.trial_ids_read
    assert "trial-b" in p.trial_ids_read  # union with context


def test_aa4_invalid_diff_candidate_not_written():
    proposal = ProposerProposal(
        candidate_type="B",
        trial_ids_read=["trial-a", "trial-b"],
        proposer_reasoning="r",
        hypothesis="h",
        predicted_improvement="p",
        proposed_diff=_FORBIDDEN_DIFF,
    )
    outcome = finalize_code_proposal(proposal, _ctx())
    assert outcome.candidate is None
    assert outcome.finding == "proposer_rejected_invalid_diff"
    assert outcome.validation.valid is False


def test_aa4_valid_diff_candidate_written():
    proposal = ProposerProposal(
        candidate_type="B",
        trial_ids_read=["trial-a", "trial-b"],
        proposer_reasoning="r",
        hypothesis="h",
        predicted_improvement="p",
        proposed_diff=_ALLOWED_DIFF,
    )
    outcome = finalize_code_proposal(proposal, _ctx())
    assert outcome.candidate is not None
    pl = outcome.candidate.payload
    assert pl["artifact_type"] == "harness_code_candidate"
    assert pl["allowlist_validation_result"]["valid"] is True


def test_aa4_frontier_exhausted_statement():
    def fake_opus(_sys, _ctx):
        return {
            "candidate_type": "none",
            "trial_ids_read": ["trial-a"],
            "frontier_statement": "every improvement is already on the "
            "frontier at 0.85",
        }

    p = propose(_ctx(), opus_call=fake_opus)
    assert p.candidate_type == "none"
    assert "frontier" in (p.frontier_statement or "")


def test_aa4_proposer_does_not_call_validator():
    # Architecture enforcement: the proposer must not import or call the
    # allowlist validator. Parse the AST so a docstring that *names* the
    # validator (to explain WHY it is not imported) does not false-fail.
    import ast

    import spectrum_systems_core.harness.proposer as prop

    tree = ast.parse(Path(prop.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                assert "harness_mutation_validator" not in n.name
        if isinstance(node, ast.ImportFrom):
            assert "harness_mutation_validator" not in (node.module or "")
        if isinstance(node, ast.Call):
            fn = node.func
            name = (
                fn.id
                if isinstance(fn, ast.Name)
                else fn.attr
                if isinstance(fn, ast.Attribute)
                else ""
            )
            assert name != "validate_diff"


# ======================================================================
# AA.5 — code candidate evaluator + eligibility
# ======================================================================
def _ceiling_loader(tid):
    from tests.integration.fixtures import (
        _ceiling_item,
        make_opus_ceiling_artifact,
    )

    return make_opus_ceiling_artifact(
        transcript_id=tid,
        items=[
            _ceiling_item("c-1", "decision", ["t1"], "alpha decision"),
            _ceiling_item("c-2", "decision", ["t2"], "beta decision"),
        ],
    )


def _haiku(f1, tid):
    from tests.integration.fixtures import (
        _ceiling_item,
        make_opus_ceiling_artifact,
    )

    if f1 >= 0.99:
        items = [
            _ceiling_item("h-1", "decision", ["t1"], "alpha decision"),
            _ceiling_item("h-2", "decision", ["t2"], "beta decision"),
        ]
    else:
        items = [_ceiling_item("h-x", "decision", ["t9"], "noise")]
    return make_opus_ceiling_artifact(transcript_id=tid, items=items)


def test_aa5_all_conditions_eligible():
    from tests.integration.fixtures import (
        make_harness_code_candidate_artifact,
    )

    cand = make_harness_code_candidate_artifact(transcript_id=TID)
    ev = evaluate_code_candidate(
        candidate=cand,
        target_transcript_id=TID,
        ceiling_loader=_ceiling_loader,
        baseline_loader=lambda t: _haiku(0.0, t),
        patched_runner=lambda t, _d: _haiku(1.0, t),
        holdout_transcript_id=HOLDOUT,
        apply_diff=lambda _x, _y: None,
    )
    assert ev.payload["auto_pr_eligible"] is True
    assert ev.payload["eligibility_reason"] == "all conditions met"


def test_aa5_allowlist_recheck_false_ineligible():
    r = evaluate_code_eligibility(
        {
            "target_delta_f1": 0.2,
            "holdout_delta_f1": 0.0,
            "per_type_regressions": [],
            "allowlist_recheck_passed": False,
        }
    )
    assert r.eligible is False
    assert "allowlist_recheck_failed" in r.reason


def test_aa5_holdout_regression_ineligible():
    r = evaluate_code_eligibility(
        {
            "target_delta_f1": 0.2,
            "holdout_delta_f1": -0.01,
            "per_type_regressions": [],
            "allowlist_recheck_passed": True,
        }
    )
    assert r.eligible is False
    assert "holdout_regression" in r.reason


def test_aa5_tampered_diff_caught_by_recheck():
    # Candidate claims valid==True but its actual proposed_diff touches
    # control/decision.py. The defense-in-depth recheck must halt
    # BEFORE evaluation; the seams raise if ever reached.
    tampered = {
        "artifact_type": "harness_code_candidate",
        "schema_version": "1.0.0",
        "candidate_id": "tampered-001",
        "produced_at": "1970-01-01T00:00:00+00:00",
        "transcript_id": TID,
        "trial_ids_read": ["trial-a"],
        "proposed_diff": _FORBIDDEN_DIFF,
        "proposer_reasoning": "r",
        "hypothesis": "h",
        "predicted_improvement": "p",
        "allowlist_validation_result": {"valid": True},
    }

    def _boom(*_a, **_k):
        raise AssertionError("evaluation reached on a tampered diff")

    with pytest.raises(CodeCandidateEvaluatorError) as exc:
        evaluate_code_candidate(
            candidate=tampered,
            target_transcript_id=TID,
            ceiling_loader=_boom,
            baseline_loader=_boom,
            patched_runner=_boom,
            holdout_transcript_id=HOLDOUT,
            apply_diff=_boom,
        )
    assert exc.value.reason_code == "allowlist_recheck_failed"


# ======================================================================
# AA.6 — Pareto frontier
# ======================================================================
def _write_summary(processed, trial, f1, tokens, clock="1970-01-01T00:00:00+00:00"):
    write_harness_snapshot(
        processed_dir=processed,
        trial_id=trial,
        repo_root=REPO_ROOT,
        commit_sha=f"sha-{trial}",
    )
    write_score_summary(
        processed_dir=processed,
        trial_id=trial,
        transcript_id=TID,
        expected_commit_sha=f"sha-{trial}",
        total_f1=f1,
        context_tokens_used=tokens,
        clock=lambda: clock,
    )


def test_aa6_dominated_removed(tmp_path):
    _write_summary(tmp_path, "t1", 0.60, 100)
    _write_summary(tmp_path, "t2", 0.70, 120)
    _write_summary(tmp_path, "t3", 0.65, 90)
    doc = update_pareto_frontier(
        processed_dir=tmp_path, transcript_id=TID,
        clock=lambda: "1970-01-01T00:00:00+00:00",
    )
    pts = {(e["total_f1"], e["context_tokens_used"]) for e in doc["frontier"]}
    assert pts == {(0.70, 120), (0.65, 90)}


def test_aa6_rederive_after_delete(tmp_path):
    _write_summary(tmp_path, "t1", 0.60, 100)
    _write_summary(tmp_path, "t2", 0.70, 120)
    _write_summary(tmp_path, "t3", 0.65, 90)
    first = update_pareto_frontier(
        processed_dir=tmp_path, transcript_id=TID,
        clock=lambda: "1970-01-01T00:00:00+00:00",
    )
    (tmp_path / "pareto_frontier.json").unlink()
    assert load_pareto_frontier(tmp_path) == []
    second = update_pareto_frontier(
        processed_dir=tmp_path, transcript_id=TID,
        clock=lambda: "1970-01-01T00:00:00+00:00",
    )
    assert first["frontier"] == second["frontier"]


def test_aa6_displacement(tmp_path):
    _write_summary(tmp_path, "t1", 0.60, 100)
    _write_summary(tmp_path, "t2", 0.70, 120)
    _write_summary(tmp_path, "t3", 0.65, 90)
    _write_summary(tmp_path, "t4", 0.70, 100)
    doc = update_pareto_frontier(
        processed_dir=tmp_path, transcript_id=TID,
        clock=lambda: "1970-01-01T00:00:00+00:00",
    )
    pts = {(e["total_f1"], e["context_tokens_used"]) for e in doc["frontier"]}
    assert (0.70, 100) in pts
    assert (0.70, 120) not in pts
    assert (0.65, 90) in pts


def test_aa6_null_tokens_never_dominate_non_null(tmp_path):
    # A high-F1 null-token point must not evict a known-cost point.
    _write_summary(tmp_path, "t1", 0.90, None)
    _write_summary(tmp_path, "t2", 0.70, 50)
    doc = update_pareto_frontier(
        processed_dir=tmp_path, transcript_id=TID,
        clock=lambda: "1970-01-01T00:00:00+00:00",
    )
    pts = {(e["total_f1"], e["context_tokens_used"]) for e in doc["frontier"]}
    assert (0.70, 50) in pts  # survived: null-token 0.90 cannot dominate
    assert (0.90, None) in pts


# ======================================================================
# AA.7 — outer loop driver
# ======================================================================
def _never(*_a, **_k):
    raise AssertionError("should not be reached")


def test_aa7_preflight_no_trace_data_halts():
    art = run_harness_search(
        transcript_id=TID,
        iterations=3,
        preflight=lambda: (False, "no_trace_data_available"),
        propose=_never,
        context_for=_never,
        evaluate_code=_never,
        route_prompt=_never,
        trigger_pr=_never,
        update_frontier=_never,
        search_id="s1",
        clock=lambda: "1970-01-01T00:00:00+00:00",
    )
    p = art.payload
    assert p["halt_reason"] == "preflight_failed"
    assert p["iterations_completed"] == 0
    assert p["per_iteration"] == []


def test_aa7_preflight_open_pr_halts():
    art = run_harness_search(
        transcript_id=TID,
        iterations=2,
        preflight=lambda: (False, "prior_open_harness_pr"),
        propose=_never,
        context_for=_never,
        evaluate_code=_never,
        route_prompt=_never,
        trigger_pr=_never,
        update_frontier=_never,
    )
    assert art.payload["halt_reason"] == "preflight_failed"
    assert art.payload["preflight_halt_detail"] == "prior_open_harness_pr"
    assert art.payload["iterations_completed"] == 0


def test_aa7_convergence_halt_after_3_flat_iterations():
    # Three iterations, flat F1 -> convergence_halt with 3 flat iters.
    proposal = ProposerProposal(
        candidate_type="B",
        trial_ids_read=["trial-a"],
        proposer_reasoning="r",
        hypothesis="h",
        predicted_improvement="p",
        proposed_diff=_ALLOWED_DIFF,
    )

    def _evaluate(cand):
        from spectrum_systems_core.artifacts import new_artifact

        return new_artifact(
            artifact_type="harness_code_candidate_evaluation",
            payload={
                "candidate_target_f1": 0.50,
                "target_delta_f1": 0.0,
                "auto_pr_eligible": False,
            },
            trace_id="t",
            status="draft",
        )

    propose_calls = []

    def _propose(i):
        propose_calls.append(i)
        return proposal

    art = run_harness_search(
        transcript_id=TID,
        iterations=10,
        preflight=lambda: (True, None),
        propose=_propose,
        context_for=lambda _i: _ctx(),
        evaluate_code=_evaluate,
        route_prompt=_never,
        trigger_pr=_never,
        update_frontier=lambda: [],
        clock=lambda: "1970-01-01T00:00:00+00:00",
    )
    p = art.payload
    assert p["halt_reason"] == "convergence_halt"
    assert p["convergence_detail"]["consecutive_flat_iterations"] == 3
    assert p["iterations_completed"] == 3
    # Proof the driver actually ran 3 real iterations (not a function
    # return): propose() was invoked exactly 3 times before the halt.
    assert propose_calls == [0, 1, 2]
    assert len(p["per_iteration"]) == 3


def test_aa7_evaluator_error_does_not_crash_loop():
    # Red-Team Pass-2 #1: a real evaluator exception must be logged as
    # the iteration outcome and the loop must continue, never crash.
    proposal = ProposerProposal(
        candidate_type="B",
        trial_ids_read=["trial-a"],
        proposer_reasoning="r",
        hypothesis="h",
        predicted_improvement="p",
        proposed_diff=_ALLOWED_DIFF,
    )

    def _boom_eval(_cand):
        raise CodeCandidateEvaluatorError(
            "git apply failed", reason_code="diff_apply_failed"
        )

    art = run_harness_search(
        transcript_id=TID,
        iterations=2,
        preflight=lambda: (True, None),
        propose=lambda _i: proposal,
        context_for=lambda _i: _ctx(),
        evaluate_code=_boom_eval,
        route_prompt=_never,
        trigger_pr=_never,
        update_frontier=lambda: [],
        clock=lambda: "1970-01-01T00:00:00+00:00",
    )
    p = art.payload
    assert p["iterations_completed"] == 2  # loop continued, no crash
    assert p["per_iteration"][0]["candidate_type"] == "code"
    assert p["per_iteration"][0]["outcome"].startswith(
        "code_evaluation_failed:"
    )


def test_aa7_valid_type_b_pr_opened():
    proposal = ProposerProposal(
        candidate_type="B",
        trial_ids_read=["trial-a"],
        proposer_reasoning="r",
        hypothesis="h",
        predicted_improvement="p",
        proposed_diff=_ALLOWED_DIFF,
    )
    triggered = []

    def _evaluate(cand):
        from spectrum_systems_core.artifacts import new_artifact

        return new_artifact(
            artifact_type="harness_code_candidate_evaluation",
            payload={
                "candidate_target_f1": 0.80,
                "target_delta_f1": 0.20,
                "auto_pr_eligible": True,
            },
            trace_id="t",
            status="draft",
        )

    art = run_harness_search(
        transcript_id=TID,
        iterations=1,
        preflight=lambda: (True, None),
        propose=lambda _i: proposal,
        context_for=lambda _i: _ctx(),
        evaluate_code=_evaluate,
        route_prompt=_never,
        trigger_pr=lambda cid: triggered.append(cid),
        update_frontier=lambda: [],
    )
    it0 = art.payload["per_iteration"][0]
    assert it0["pr_opened"] is True
    assert it0["candidate_type"] == "code"
    assert len(triggered) == 1


def test_aa7_invalid_diff_rejected_loop_continues():
    bad = ProposerProposal(
        candidate_type="B",
        trial_ids_read=["trial-a"],
        proposer_reasoning="r",
        hypothesis="h",
        predicted_improvement="p",
        proposed_diff=_FORBIDDEN_DIFF,
    )
    art = run_harness_search(
        transcript_id=TID,
        iterations=2,
        preflight=lambda: (True, None),
        propose=lambda _i: bad,
        context_for=lambda _i: _ctx(),
        evaluate_code=_never,
        route_prompt=_never,
        trigger_pr=_never,
        update_frontier=lambda: [],
        clock=lambda: "1970-01-01T00:00:00+00:00",
    )
    p = art.payload
    assert p["iterations_completed"] == 2  # loop continued past rejection
    assert p["per_iteration"][0]["candidate_type"] == "rejected"
    assert (
        p["per_iteration"][0]["outcome"]
        == "proposer_rejected_invalid_diff"
    )
