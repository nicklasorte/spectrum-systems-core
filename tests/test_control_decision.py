from spectrum_systems_core.artifacts import ArtifactStore, new_artifact
from spectrum_systems_core.control import decide_control
from spectrum_systems_core.evals import run_required_evals
from spectrum_systems_core.promotion import promote_if_allowed


def _good_minutes():
    return new_artifact(
        "meeting_minutes",
        {
            "title": "Sync",
            "summary": "Discussed roadmap.",
            "decisions": ["ship v1"],
            "action_items": ["write docs"],
            "open_questions": [],
        },
        trace_id="t-good",
    )


def test_empty_payload_fails_non_empty_eval():
    artifact = new_artifact("meeting_minutes", {}, trace_id="t")
    results = run_required_evals(artifact)
    by_type = {r.payload["eval_type"]: r for r in results}
    assert by_type["non_empty_payload"].payload["status"] == "fail"


def test_missing_eval_results_block_control():
    artifact = _good_minutes()
    decision = decide_control(artifact, [])
    assert decision.payload["decision"] == "block"
    assert "missing_required_evals" in decision.payload["reason_codes"]


def test_failed_eval_blocks_control():
    artifact = new_artifact("meeting_minutes", {"title": "x"}, trace_id="t")
    results = run_required_evals(artifact)
    decision = decide_control(artifact, results)
    assert decision.payload["decision"] == "block"
    assert any(rc.startswith("failed:") for rc in decision.payload["reason_codes"])


def test_passing_evals_allow_control():
    artifact = _good_minutes()
    results = run_required_evals(artifact)
    decision = decide_control(artifact, results)
    assert decision.payload["decision"] == "allow"
    assert decision.payload["reason_codes"] == []
    assert decision.payload["eval_result_refs"] == [r.artifact_id for r in results]


def test_promotion_only_after_allow():
    store = ArtifactStore()
    artifact = _good_minutes()
    store.put(artifact)
    results = run_required_evals(artifact)
    for r in results:
        store.put(r)
    decision = decide_control(artifact, results)
    store.put(decision)

    promote_if_allowed(store, artifact, decision)
    assert store.get(artifact.artifact_id).status == "promoted"


def test_block_decision_causes_rejected_status():
    store = ArtifactStore()
    artifact = new_artifact("meeting_minutes", {}, trace_id="t")
    store.put(artifact)
    results = run_required_evals(artifact)
    for r in results:
        store.put(r)
    decision = decide_control(artifact, results)
    store.put(decision)

    promote_if_allowed(store, artifact, decision)
    assert store.get(artifact.artifact_id).status == "rejected"


def test_promotion_rejects_when_no_evals_provided():
    store = ArtifactStore()
    artifact = _good_minutes()
    store.put(artifact)
    decision = decide_control(artifact, [])
    store.put(decision)
    promote_if_allowed(store, artifact, decision)
    assert store.get(artifact.artifact_id).status == "rejected"
