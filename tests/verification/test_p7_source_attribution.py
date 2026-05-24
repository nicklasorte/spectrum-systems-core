"""Phase P7 — source-attribution foundation, end-to-end evidence.

Scope note (constitution-compliant interpretation): P7's intent is to
make a fabricated extracted item detectable. The Phase Y/V architecture
already provides the deterministic turn chunker, the ``grounding`` /
``source_turns`` contract at schema_version 1.1.0, and the
``source_turn_validity`` eval wired through the one ``decide_control``
gate — but ONLY on the data-lake pipeline path. The live-LLM workflow
(PR #121/#124) bypassed all of it. P7 closes that gap by extending the
existing architecture to the LLM path (no second chunker, no forked
schema, no second control authority).

These tests defend the trust properties, with a happy path AND a
fail-closed rejection path for every gate:

- P7-A  chunker replay determinism; chunker_version; recursive_512
        fallback never claims a speaker turn.
- P7-B  schema additivity — legacy 1.0.0 (no grounding) still valid;
        grounded 1.1.0 valid.
- P7-C  a fabricated turn_id blocks promotion through the SAME control
        gate; the metric is non-zero on hallucination and zero on the
        happy path (never always-zero); the verifier makes ZERO LLM
        calls (static assertion).
"""
from __future__ import annotations

import pathlib

from spectrum_systems_core.data_lake.chunker import (
    CHUNKER_VERSION_RECURSIVE,
    CHUNKER_VERSION_SPEAKER_TURN,
    chunk_transcript,
)
from spectrum_systems_core.evals import (
    SOURCE_TURN_UNRESOLVED_PREFIX,
    run_source_turn_validity_eval_from_chunks,
)
from spectrum_systems_core.evals.source_turn_validity import (
    GROUNDING_MISSING_FOR_CONTENT,
)
from spectrum_systems_core.validation import validate_artifact
from spectrum_systems_core.workflows import run_meeting_minutes_llm_workflow
from tests.llm_stub import (
    DEC18_ACTION_ITEMS,
    DEC18_DECISIONS,
    DEC18_OPEN_QUESTIONS,
    DEC18_TECHNICAL_PARAMETERS,
    json_stub,
)

DEC18 = (
    pathlib.Path(__file__).resolve().parents[1]
    / "fixtures"
    / "llm_extraction"
    / "dec18_transcript.txt"
).read_text(encoding="utf-8")

SPEAKER_TRANSCRIPT = (
    "ALICE: We approved the 7 GHz downlink threshold.\n"
    "BOB: I will submit the revised values before the next session.\n"
    "ALICE: What is the coordination distance for federal incumbents?\n"
)

# No ALL-CAPS "NAME:" speaker labels, no blank-line paragraph
# boundaries, > 512 words -> the terminal recursive_512 fallback.
NO_STRUCTURE_TRANSCRIPT = "\n".join(["lorem ipsum dolor sit amet"] * 300)


def _eval(result, eval_type):
    matches = [
        e
        for e in result.eval_results
        if e.payload.get("eval_type") == eval_type
    ]
    assert len(matches) == 1, f"expected exactly one {eval_type} eval_result"
    return matches[0].payload


def _decision(result) -> str:
    return result.control_decision.payload["decision"]


def _failed_turn_item_count(stv_payload: dict) -> int:
    """The fail-closed metric: number of grounding items whose
    source_turns did not resolve to a real chunk. This is the live-LLM
    path's analogue of verification_result.items_failed /
    orchestration_result.spurious_add_count."""
    return sum(
        1
        for rc in stv_payload["reason_codes"]
        if rc.startswith(SOURCE_TURN_UNRESOLVED_PREFIX)
    )


# ----------------------------- P7-A ------------------------------------


def test_p7a_chunker_replay_is_byte_identical():
    """Same transcript -> identical chunks (turn_id, turn_index,
    chunker_version, word_count) on two independent calls. Any UUID4 /
    clock / randomness would fail this."""
    first = chunk_transcript(SPEAKER_TRANSCRIPT)
    second = chunk_transcript(SPEAKER_TRANSCRIPT)
    assert first == second
    assert [c["turn_id"] for c in first] == [
        f"t{i:04d}" for i in range(len(first))
    ]
    assert [c["turn_index"] for c in first] == list(range(len(first)))
    assert all(
        c["chunker_version"] == CHUNKER_VERSION_SPEAKER_TURN for c in first
    )
    assert first[0]["word_count"] == len(first[0]["text"].split())


def test_p7a_recursive_fallback_never_claims_a_speaker_turn():
    """No speaker structure -> recursive_512 windows. They still carry a
    turn_id (the binding source_record contract requires a string
    turn_id on every chunk) but speaker is None and chunker_version
    marks the boundary as positional, not a real speaker turn."""
    chunks = chunk_transcript(NO_STRUCTURE_TRANSCRIPT)
    assert len(chunks) > 1  # 1500 words / 512 -> several windows
    assert all(
        c["chunker_version"] == CHUNKER_VERSION_RECURSIVE for c in chunks
    )
    assert all(c["speaker"] is None for c in chunks)
    assert all(
        isinstance(c["turn_id"], str) and c["turn_id"].startswith("t")
        for c in chunks
    )
    assert chunk_transcript(NO_STRUCTURE_TRANSCRIPT) == chunks  # replay


# ----------------------------- P7-B ------------------------------------


def test_p7b_legacy_1_0_0_without_grounding_still_validates():
    """Additivity: a legacy LLM-shaped payload at 1.0.0 with NO
    grounding (pre-attribution) is still schema-valid."""
    legacy = {
        "artifact_type": "meeting_minutes",
        "schema_version": "1.0.0",
        "title": "Legacy meeting",
        "summary": "No source attribution recorded.",
        "decisions": ["Approve the roadmap."],
        "action_items": [{"action": "Draft the scope."}],
        "open_questions": ["Do we need a follow-up?"],
        "provenance": {"produced_by": "meeting_minutes_llm"},
    }
    validate_artifact(legacy, "meeting_minutes")


def test_p7b_grounded_1_1_0_with_source_turns_validates():
    grounded = {
        "artifact_type": "meeting_minutes",
        "schema_version": "1.1.0",
        "title": "Grounded meeting",
        "summary": "Attributed to transcript turns.",
        "decisions": ["Approve the roadmap."],
        "action_items": [],
        "open_questions": [],
        "grounding": [
            {
                "kind": "decision",
                "text": "Approve the roadmap.",
                "source_turns": ["t0000"],
            }
        ],
        "provenance": {"produced_by": "meeting_minutes_llm"},
    }
    validate_artifact(grounded, "meeting_minutes")


# ----------------------------- P7-C ------------------------------------


def test_p7c_happy_real_turn_ids_promote_and_metric_is_zero():
    """A well-behaved model cites real turn_ids -> source_turn_validity
    passes, decision is allow, artifact promotes, failed-item metric is
    exactly 0 (proving the metric is not always-non-zero)."""
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            decisions=DEC18_DECISIONS,
            action_items=DEC18_ACTION_ITEMS,
            open_questions=DEC18_OPEN_QUESTIONS,
            technical_parameters=DEC18_TECHNICAL_PARAMETERS,
        ),
        meeting_id="m-p7c-happy",
    )
    stv = _eval(result, "source_turn_validity")
    assert stv["status"] == "pass"
    assert _failed_turn_item_count(stv) == 0
    assert _eval(result, "grounding_coverage")["status"] == "pass"
    assert result.meeting_minutes.payload["schema_version"] == "1.1.0"
    assert _decision(result) == "allow"
    assert result.promoted is True


def test_p7c_synthetic_hallucination_blocks_and_metric_fires():
    """Inject a grounding entry citing a turn_id that does not exist in
    the chunked transcript. The deterministic eval must fail, the SAME
    decide_control gate must block, the artifact must NOT promote, and
    the failed-item metric must be > 0 (never always-zero)."""
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            decisions=[DEC18_DECISIONS[0]],
            grounding=[
                {
                    "kind": "decision",
                    "text": DEC18_DECISIONS[0],
                    "source_turns": ["t9999"],  # no such chunk
                }
            ],
        ),
        meeting_id="m-p7c-halluc",
    )
    stv = _eval(result, "source_turn_validity")
    assert stv["status"] == "fail"
    assert any(
        rc.startswith(SOURCE_TURN_UNRESOLVED_PREFIX) and rc.endswith("t9999")
        for rc in stv["reason_codes"]
    )
    assert _failed_turn_item_count(stv) == 1
    assert _decision(result) == "block"
    assert result.promoted is False
    assert result.meeting_minutes.status == "rejected"


def test_p7c_content_without_grounding_is_blocked_fail_closed():
    """The coverage floor: a model that extracts a decision but emits
    grounding: [] cannot promote (the runner's per-item check passes
    vacuously on an empty list — this eval is what closes that hole)."""
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(decisions=[DEC18_DECISIONS[0]], grounding=[]),
        meeting_id="m-p7c-nocover",
    )
    coverage = _eval(result, "grounding_coverage")
    assert coverage["status"] == "fail"
    assert GROUNDING_MISSING_FOR_CONTENT in coverage["reason_codes"]
    assert _decision(result) == "block"
    assert result.promoted is False


def test_p7c_pre_attribution_legacy_items_do_not_block():
    """Fail-closed must not over-fire: an artifact with NO grounding and
    NO items (the pre-attribution / empty case) passes the in-memory
    validity eval rather than blocking."""
    from spectrum_systems_core.artifacts import new_artifact

    art = new_artifact(
        artifact_type="meeting_minutes",
        payload={
            "title": "x",
            "summary": "y",
            "decisions": [],
            "action_items": [],
            "open_questions": [],
            "schema_version": "1.0.0",
        },
        trace_id="trace-preattr",
        status="draft",
    )
    res = run_source_turn_validity_eval_from_chunks(
        art, [{"turn_id": "t0000", "text": "anything"}]
    )
    assert res.payload["status"] == "pass"
    assert res.payload["reason_codes"] == []


def test_p7c_eval_is_deterministic():
    """Same artifact + same chunks -> identical eval status and reason
    codes on two independent calls (no clock, no randomness)."""
    from spectrum_systems_core.artifacts import new_artifact

    art = new_artifact(
        artifact_type="meeting_minutes",
        payload={
            "schema_version": "1.1.0",
            "grounding": [{"kind": "decision", "source_turns": ["t0000"]}],
        },
        trace_id="trace-determinism",
        status="draft",
    )
    chunks = [{"turn_id": "t0000", "text": "x"}]
    a = run_source_turn_validity_eval_from_chunks(art, chunks)
    b = run_source_turn_validity_eval_from_chunks(art, chunks)
    assert a.payload["status"] == b.payload["status"] == "pass"
    assert a.payload["reason_codes"] == b.payload["reason_codes"] == []


def test_p7c_verifier_makes_zero_llm_calls_static():
    """Static guarantee: the deterministic source-turn verifier module
    references no Anthropic SDK / LLM client. If a future edit imports
    one, this fails pre-PR rather than after a costly run."""
    src = (
        pathlib.Path(__file__).resolve().parents[2]
        / "src"
        / "spectrum_systems_core"
        / "evals"
        / "source_turn_validity.py"
    ).read_text(encoding="utf-8")
    lowered = src.lower()
    for forbidden in (
        "anthropic",
        "llm_client",
        "ai.adapter",
        "api_caller",
        "openai",
    ):
        assert forbidden not in lowered, (
            f"source_turn_validity.py references '{forbidden}' — the "
            "deterministic-verifier (zero-LLM-call) invariant is broken"
        )
