"""Unit tests for ``scripts/create_cross_meeting_synthesis.py``.

These are in-process tests of the pure synthesis logic with an
INJECTED stub Opus transport (the same ``client`` seam
``workflows/llm_client.py`` defines) — no API key, no network. The
subprocess + real-writer-factory contract is covered separately by
``tests/integration/test_create_cross_meeting_synthesis_contract.py``
(CLAUDE.md integration-test non-negotiable).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import create_cross_meeting_synthesis as cms  # noqa: E402

SCHEMA = (
    REPO_ROOT
    / "src"
    / "spectrum_systems_core"
    / "schemas"
    / "cross_meeting_synthesis.schema.json"
)


def _write_minutes(
    data_lake: Path,
    source_id: str,
    *,
    decisions: Optional[List[Any]] = None,
    action_items: Optional[List[Any]] = None,
    open_questions: Optional[List[Any]] = None,
    claims: Optional[List[Dict[str, Any]]] = None,
    produced_by: str = "meeting_minutes_llm",
) -> None:
    """Write one promoted meeting_minutes envelope at the contract path.

    The on-disk shape mirrors ``write_promoted_artifact``: an Artifact
    envelope (`schema_version` int) whose `payload` is the flat
    meeting_minutes content (`schema_version` string). The integration
    contract test uses the REAL factory; this unit helper writes the
    same envelope shape directly so the pure logic can be exercised in
    isolation.
    """
    payload: Dict[str, Any] = {
        "title": f"Meeting {source_id}",
        "summary": f"Summary for {source_id}",
        "schema_version": "1.0.0",
        "provenance": {"produced_by": produced_by},
        "meeting_id": source_id,
        "decisions": decisions if decisions is not None else [],
        "action_items": action_items if action_items is not None else [],
        "open_questions": (
            open_questions if open_questions is not None else []
        ),
    }
    if claims is not None:
        payload["schema_version"] = "1.2.0"
        payload["claims"] = claims
    envelope = {
        "artifact_id": f"art-{source_id}",
        "artifact_type": "meeting_minutes",
        "schema_version": 1,
        "status": "promoted",
        "created_at": "1970-01-01T00:00:00+00:00",
        "trace_id": f"trace-{source_id}",
        "input_refs": [],
        "content_hash": "deadbeef",
        "payload": payload,
    }
    mdir = data_lake / "store" / "processed" / "meetings" / source_id
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / f"meeting_minutes__{source_id}.json").write_text(
        json.dumps(envelope), encoding="utf-8"
    )


def _synthesis_payload(
    source_ids: List[str],
    *,
    narrative: str = (
        "The TIG opened with a kickoff on the 7 GHz downlink band and "
        "progressively narrowed the technical envelope. Early meetings "
        "approved a provisional threshold; later sessions deferred the "
        "aggregate interference methodology, which remains the central "
        "open question. The trajectory is convergence on the downlink "
        "rule with the methodology still unresolved."
    ),
    closed_meeting: Optional[str] = None,
    decision_status: str = "active",
) -> Dict[str, Any]:
    first = source_ids[0]
    last = source_ids[-1]
    return {
        "decision_threads": [
            {
                "topic": "7 GHz downlink threshold",
                "summary": "Thread tracking the downlink threshold.",
                "decisions": [
                    {
                        "source_id": first,
                        "text": "Approved the 7 GHz downlink threshold.",
                        "regulatory_verb": "approved",
                        "status": decision_status,
                    }
                ],
            }
        ],
        "open_actions": [
            {
                "text": "DoD to submit revised ERP values.",
                "owner": "DoD",
                "assigned_meeting": first,
                "closed_meeting": closed_meeting,
            }
        ],
        "claim_drift": [
            {
                "topic": "coordination distance",
                "drift_detected": True,
                "drift_summary": "Distance estimate grew across meetings.",
                "instances": [
                    {
                        "source_id": first,
                        "text": "Coordination distance is about 50 km.",
                        "speaker": "NTIA Lead",
                    },
                    {
                        "source_id": last,
                        "text": "Coordination distance is about 80 km.",
                        "speaker": "NTIA Lead",
                    },
                ],
            }
        ],
        "unresolved_questions": [
            {
                "text": "What is the aggregate interference methodology?",
                "raised_meeting": last,
                "resolution": None,
                "resolved": False,
            }
        ],
        "narrative_summary": narrative,
    }


def _stub(payload: Dict[str, Any]):
    def _client(*, system: str, user: str) -> str:  # noqa: ARG001
        return json.dumps(payload)

    return _client


def _two_meeting_lake(tmp_path: Path) -> Path:
    dl = tmp_path / "data-lake"
    _write_minutes(
        dl,
        "tig-kickoff-transcript-20251101",
        decisions=["Approved the 7 GHz downlink threshold."],
        action_items=["DoD to submit revised ERP values."],
        open_questions=["What is the coordination distance?"],
        claims=[
            {
                "claim_id": "c1",
                "claim_text": "Coordination distance is about 50 km.",
                "speaker": "NTIA Lead",
            }
        ],
    )
    _write_minutes(
        dl,
        "tig-followup-transcript-20251218",
        decisions=[
            {"text": "Deferred the aggregate methodology.", "verb": "deferred"}
        ],
        action_items=[{"action": "Circulate revised methodology."}],
        open_questions=[
            {
                "question_id": "q1",
                "question_text": "Aggregate interference methodology?",
            }
        ],
        claims=[
            {
                "claim_id": "c2",
                "claim_text": "Coordination distance is about 80 km.",
                "speaker": "NTIA Lead",
            }
        ],
    )
    return dl


def _source_ids() -> List[str]:
    return [
        "tig-kickoff-transcript-20251101",
        "tig-followup-transcript-20251218",
    ]


def test_insufficient_corpus_halts_below_min(tmp_path: Path) -> None:
    dl = tmp_path / "data-lake"
    _write_minutes(dl, "only-one-transcript-20251101", decisions=["x"])
    with pytest.raises(cms.SynthesisError) as exc:
        cms.run_synthesis(
            data_lake=dl,
            dry_run=True,
            model="claude-opus-stub",
            min_meetings=2,
            client=_stub(_synthesis_payload(["only-one-transcript-20251101"])),
        )
    assert exc.value.reason == "insufficient_corpus"


def test_min_meetings_floor_is_two_even_when_one_requested(
    tmp_path: Path,
) -> None:
    """The constraint floor is always >= 2 regardless of --min-meetings."""
    dl = tmp_path / "data-lake"
    _write_minutes(dl, "solo-transcript-20251101", decisions=["x"])
    with pytest.raises(cms.SynthesisError) as exc:
        cms.run_synthesis(
            data_lake=dl,
            dry_run=True,
            model="claude-opus-stub",
            min_meetings=1,
            client=_stub(_synthesis_payload(["solo-transcript-20251101"])),
        )
    assert exc.value.reason == "insufficient_corpus"


def test_valid_corpus_validates_against_schema(tmp_path: Path) -> None:
    import jsonschema

    dl = _two_meeting_lake(tmp_path)
    sids = _source_ids()
    # Drive the full assemble path and capture the written artifact.
    cms.run_synthesis(
        data_lake=dl,
        dry_run=False,
        model="claude-opus-stub",
        min_meetings=2,
        client=_stub(_synthesis_payload(sids)),
    )
    out = sorted(
        (dl / "store" / "artifacts" / "synthesis").glob("*.json")
    )
    assert len(out) == 1
    artifact = json.loads(out[0].read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema.validate(artifact, schema)
    assert artifact["artifact_type"] == "cross_meeting_synthesis"
    assert artifact["schema_version"] == "1.0.0"
    assert artifact["provenance"]["produced_by"] == (
        "cross_meeting_synthesis_workflow"
    )
    assert isinstance(artifact["decision_threads"], list)
    assert isinstance(artifact["narrative_summary"], str)
    assert len(artifact["narrative_summary"]) >= 100
    assert artifact["corpus_span"]["total_meetings"] == 2
    assert artifact["corpus_span"]["earliest_meeting"] == "2025-11-01"
    assert artifact["corpus_span"]["latest_meeting"] == "2025-12-18"
    assert sorted(artifact["source_ids"]) == sorted(sids)
    assert artifact["provenance"]["input_artifact_ids"] == sorted(
        [f"art-{s}" for s in sids]
    )


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    dl = _two_meeting_lake(tmp_path)
    summary = cms.run_synthesis(
        data_lake=dl,
        dry_run=True,
        model="claude-opus-stub",
        min_meetings=2,
        client=_stub(_synthesis_payload(_source_ids())),
    )
    assert summary["dry_run"] is True
    assert not (dl / "store" / "artifacts" / "synthesis").exists()


def test_open_action_without_closed_meeting_is_open(
    tmp_path: Path,
) -> None:
    dl = _two_meeting_lake(tmp_path)
    cms.run_synthesis(
        data_lake=dl,
        dry_run=False,
        model="claude-opus-stub",
        min_meetings=2,
        client=_stub(_synthesis_payload(_source_ids(), closed_meeting=None)),
    )
    artifact = json.loads(
        next(
            (dl / "store" / "artifacts" / "synthesis").glob("*.json")
        ).read_text(encoding="utf-8")
    )
    act = artifact["open_actions"][0]
    assert act["status"] == "open"
    assert act["closed_meeting"] is None
    assert act["closed_date"] is None


def test_closure_in_a_corpus_meeting_marks_closed_not_open(
    tmp_path: Path,
) -> None:
    """Red-team: a closure recorded in ANY meeting in the corpus must
    mark the action closed — never open just because the closure was in
    a different (later) meeting. The whole corpus is read in one pass so
    the closure is always visible."""
    dl = _two_meeting_lake(tmp_path)
    sids = _source_ids()
    cms.run_synthesis(
        data_lake=dl,
        dry_run=False,
        model="claude-opus-stub",
        min_meetings=2,
        client=_stub(
            _synthesis_payload(sids, closed_meeting=sids[1])
        ),
    )
    artifact = json.loads(
        next(
            (dl / "store" / "artifacts" / "synthesis").glob("*.json")
        ).read_text(encoding="utf-8")
    )
    act = artifact["open_actions"][0]
    assert act["status"] == "closed"
    assert act["closed_meeting"] == sids[1]
    assert act["closed_date"] == "2025-12-18"


def test_closure_in_non_corpus_meeting_is_unclear(tmp_path: Path) -> None:
    dl = _two_meeting_lake(tmp_path)
    cms.run_synthesis(
        data_lake=dl,
        dry_run=False,
        model="claude-opus-stub",
        min_meetings=2,
        client=_stub(
            _synthesis_payload(
                _source_ids(), closed_meeting="not-in-corpus-20990101"
            )
        ),
    )
    artifact = json.loads(
        next(
            (dl / "store" / "artifacts" / "synthesis").glob("*.json")
        ).read_text(encoding="utf-8")
    )
    act = artifact["open_actions"][0]
    assert act["status"] == "unclear"
    assert act["closed_date"] is None


def test_thread_open_is_recomputed_from_decision_status(
    tmp_path: Path,
) -> None:
    dl = _two_meeting_lake(tmp_path)
    # All decisions resolved -> thread.open must be False even if the
    # model had said open.
    payload = _synthesis_payload(_source_ids(), decision_status="resolved")
    cms.run_synthesis(
        data_lake=dl,
        dry_run=False,
        model="claude-opus-stub",
        min_meetings=2,
        client=_stub(payload),
    )
    artifact = json.loads(
        next(
            (dl / "store" / "artifacts" / "synthesis").glob("*.json")
        ).read_text(encoding="utf-8")
    )
    assert artifact["decision_threads"][0]["open"] is False

    # Re-run with an active decision -> open True.
    payload2 = _synthesis_payload(_source_ids(), decision_status="active")
    cms.run_synthesis(
        data_lake=dl,
        dry_run=False,
        model="claude-opus-stub",
        min_meetings=2,
        client=_stub(payload2),
    )
    artifacts = sorted(
        (dl / "store" / "artifacts" / "synthesis").glob("*.json")
    )
    latest = json.loads(artifacts[-1].read_text(encoding="utf-8"))
    assert latest["decision_threads"][0]["open"] is True


def test_sparse_corpus_empty_arrays_still_valid(tmp_path: Path) -> None:
    """A sparse corpus may yield empty decision_threads / open_actions /
    claim_drift / unresolved_questions — the artifact must still
    validate as long as the narrative meets the floor."""
    import jsonschema

    dl = _two_meeting_lake(tmp_path)
    sparse = {
        "decision_threads": [],
        "open_actions": [],
        "claim_drift": [],
        "unresolved_questions": [],
        "narrative_summary": (
            "Across the two meetings in this corpus the TIG made no "
            "tracked decisions and surfaced no carried-forward actions; "
            "the process is still in an exploratory phase with the "
            "central methodology question not yet formally raised."
        ),
    }
    cms.run_synthesis(
        data_lake=dl,
        dry_run=False,
        model="claude-opus-stub",
        min_meetings=2,
        client=_stub(sparse),
    )
    artifact = json.loads(
        next(
            (dl / "store" / "artifacts" / "synthesis").glob("*.json")
        ).read_text(encoding="utf-8")
    )
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema.validate(artifact, schema)
    assert artifact["decision_threads"] == []
    assert artifact["open_actions"] == []
    assert artifact["corpus_span"]["total_meetings"] == 2


def test_narrative_too_short_halts(tmp_path: Path) -> None:
    dl = _two_meeting_lake(tmp_path)
    payload = _synthesis_payload(_source_ids(), narrative="too short")
    with pytest.raises(cms.SynthesisError) as exc:
        cms.run_synthesis(
            data_lake=dl,
            dry_run=True,
            model="claude-opus-stub",
            min_meetings=2,
            client=_stub(payload),
        )
    assert exc.value.reason == "narrative_too_short"


def test_unknown_source_id_halts(tmp_path: Path) -> None:
    """Attribution must tie back to a promoted artifact actually read."""
    dl = _two_meeting_lake(tmp_path)
    bad = _synthesis_payload(["totally-made-up-meeting-20990101"])
    with pytest.raises(cms.SynthesisError) as exc:
        cms.run_synthesis(
            data_lake=dl,
            dry_run=True,
            model="claude-opus-stub",
            min_meetings=2,
            client=_stub(bad),
        )
    assert exc.value.reason == "malformed_synthesis_response"


def test_non_json_response_halts(tmp_path: Path) -> None:
    dl = _two_meeting_lake(tmp_path)

    def _bad(*, system: str, user: str) -> str:  # noqa: ARG001
        return "I cannot produce JSON for this corpus."

    with pytest.raises(cms.SynthesisError) as exc:
        cms.run_synthesis(
            data_lake=dl,
            dry_run=True,
            model="claude-opus-stub",
            min_meetings=2,
            client=_bad,
        )
    assert exc.value.reason == "malformed_synthesis_response"


def test_fenced_response_is_recovered(tmp_path: Path) -> None:
    dl = _two_meeting_lake(tmp_path)
    payload = _synthesis_payload(_source_ids())

    def _fenced(*, system: str, user: str) -> str:  # noqa: ARG001
        return "```json\n" + json.dumps(payload) + "\n```"

    summary = cms.run_synthesis(
        data_lake=dl,
        dry_run=True,
        model="claude-opus-stub",
        min_meetings=2,
        client=_fenced,
    )
    assert summary["status"] == "success"


def test_missing_model_halts(tmp_path: Path) -> None:
    dl = _two_meeting_lake(tmp_path)
    with pytest.raises(cms.SynthesisError) as exc:
        cms.run_synthesis(
            data_lake=dl,
            dry_run=True,
            model="   ",
            min_meetings=2,
            client=_stub(_synthesis_payload(_source_ids())),
        )
    assert exc.value.reason == "missing_model"


def test_context_window_guard_summarizes_above_500_items(
    tmp_path: Path,
) -> None:
    """Red-team: > 500 corpus items must flip the context to summarized
    so a large corpus cannot truncate the Opus response."""
    dl = tmp_path / "data-lake"
    # 2 meetings x 300 decisions = 600 items > 500.
    for sid in (
        "big-a-transcript-20251101",
        "big-b-transcript-20251218",
    ):
        _write_minutes(
            dl,
            sid,
            decisions=[f"Decision number {i}" for i in range(300)],
        )
    summary = cms.run_synthesis(
        data_lake=dl,
        dry_run=True,
        model="claude-opus-stub",
        min_meetings=2,
        client=_stub(
            _synthesis_payload(
                ["big-a-transcript-20251101", "big-b-transcript-20251218"]
            )
        ),
    )
    assert summary["summarized_context"] is True
    assert summary["total_items_read"] == 600


def test_small_corpus_is_not_summarized(tmp_path: Path) -> None:
    dl = _two_meeting_lake(tmp_path)
    summary = cms.run_synthesis(
        data_lake=dl,
        dry_run=True,
        model="claude-opus-stub",
        min_meetings=2,
        client=_stub(_synthesis_payload(_source_ids())),
    )
    assert summary["summarized_context"] is False


def test_script_never_hardcodes_a_model_string() -> None:
    """Static guard mirroring tests/ci/test_no_deprecated_model_strings:
    the script must resolve the model from --model, never embed a
    claude-<tier>-<n> literal."""
    import re

    src = (SCRIPTS_DIR / "create_cross_meeting_synthesis.py").read_text(
        encoding="utf-8"
    )
    pattern = re.compile(r"claude-(?:opus|sonnet|haiku)-\d[\w-]*")
    assert not pattern.search(src), (
        "create_cross_meeting_synthesis.py must not hardcode a model "
        "string; it comes from --model (the registry)."
    )


def test_synthesis_never_reads_raw_tree(tmp_path: Path) -> None:
    """The synthesis must read promoted artifacts only. A misleading
    raw transcript present on disk must be completely ignored — the
    output attributes only to promoted source_ids."""
    dl = _two_meeting_lake(tmp_path)
    # Plant a raw transcript that, if (wrongly) read, would inject a
    # bogus decision. The synthesis must never touch raw/.
    for sid in _source_ids():
        raw = dl / "store" / "raw" / "transcripts"
        raw.mkdir(parents=True, exist_ok=True)
        (raw / f"{sid}.txt").write_text(
            "RAW ONLY: this must never appear in the synthesis.",
            encoding="utf-8",
        )
    cms.run_synthesis(
        data_lake=dl,
        dry_run=False,
        model="claude-opus-stub",
        min_meetings=2,
        client=_stub(_synthesis_payload(_source_ids())),
    )
    artifact = json.loads(
        next(
            (dl / "store" / "artifacts" / "synthesis").glob("*.json")
        ).read_text(encoding="utf-8")
    )
    blob = json.dumps(artifact)
    assert "RAW ONLY" not in blob
    assert set(artifact["source_ids"]) == set(_source_ids())
