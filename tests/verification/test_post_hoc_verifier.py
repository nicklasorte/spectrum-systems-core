"""Phase V — PostHocVerifier tests."""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from spectrum_systems_core.verification.post_hoc_verifier import (
    EARLY_HALT_SAMPLE_SIZE,
    PostHocVerifier,
)

# --------------------------------------------------------------------- #
# Tiny stand-ins for the real ModelRegistry so tests don't need files.
# --------------------------------------------------------------------- #


class _Registry:
    """Records every ``get`` call and returns the configured spec."""

    def __init__(self, spec=None):
        self.calls: list[str] = []
        self._spec = spec or {"model": "claude-sonnet-4-6", "version": "test"}

    def get(self, task_type: str) -> dict[str, str]:
        self.calls.append(task_type)
        return dict(self._spec)


def _chunks(turn_text: dict[str, str]) -> dict[str, dict[str, Any]]:
    return {
        tid: {
            "chunk_id": tid,
            "text": text,
            "speaker": "Alice",
            "timestamp": "00:00:00",
        }
        for tid, text in turn_text.items()
    }


def _meeting_extraction(*, decisions=None, claims=None, action_items=None) -> dict[str, Any]:
    return {
        "meeting_extraction_id": str(uuid.uuid4()),
        "source_artifact_id": str(uuid.uuid4()),
        "artifact_type": "meeting_extraction",
        "schema_version": "1.1.0",
        "decisions": decisions or [],
        "claims": claims or [],
        "action_items": action_items or [],
    }


def _claim(text: str, turn_ids):
    return {
        "claim_text": text,
        "claim_type": "regulatory",
        "speaker": "Alice",
        "source_turn_ids": list(turn_ids),
        "source_turn_validation": "verified",
        "confidence": 0.9,
    }


# --------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------- #


def test_uses_generation_task_type_not_extraction():
    reg = _Registry()
    verifier = PostHocVerifier(reg, sdl_root="/x", api_caller=lambda p: {
        "verification_status": "verified",
        "supporting_text_excerpts": ["x"],
        "verifier_confidence": 0.9,
        "verifier_rationale": "ok",
    })
    # Constructor must have requested generation, not extraction.
    assert reg.calls == ["generation"]

    # Drive an end-to-end verify so we also prove the verify path
    # does not silently downgrade to "extraction" via a future bug.
    chunks = _chunks({"t-1": "x"})
    artifact = _meeting_extraction(claims=[_claim("c", ["t-1"])])
    verifier.verify_extraction(artifact, chunks, str(uuid.uuid4()))
    assert "extraction" not in reg.calls
    # Constructor + verify combined should not re-request another task type.
    assert set(reg.calls) == {"generation"}


def test_verify_supported_claim_returns_verified():
    chunks = _chunks({
        "t-1": "The DoD agreed to provide updated COA data by Q3.",
    })
    artifact = _meeting_extraction(claims=[
        _claim("DoD agreed to provide updated COA data by Q3.", ["t-1"]),
    ])
    caller = lambda prompt: {  # noqa: E731
        "verification_status": "verified",
        "supporting_text_excerpts": [
            "The DoD agreed to provide updated COA data by Q3."
        ],
        "verifier_confidence": 0.97,
        "verifier_rationale": "verbatim match.",
    }
    verifier = PostHocVerifier(_Registry(), sdl_root="/x", api_caller=caller)
    result = verifier.verify_extraction(artifact, chunks, str(uuid.uuid4()))

    assert len(result["item_verifications"]) == 1
    entry = result["item_verifications"][0]
    assert entry["verification_status"] == "verified"
    assert entry["supporting_text_excerpts"]
    assert result["summary"]["verified_count"] == 1
    assert result["summary"]["status"] == "complete"


def test_verify_unsupported_claim_returns_unsupported():
    chunks = _chunks({"t-1": "We then discussed the agenda."})
    artifact = _meeting_extraction(claims=[
        _claim("DoD agreed to provide COA data by Q3.", ["t-1"]),
    ])
    caller = lambda prompt: {  # noqa: E731
        "verification_status": "unsupported",
        "supporting_text_excerpts": [],
        "verifier_confidence": 0.9,
        "verifier_rationale": "no mention of DoD or COA data.",
    }
    verifier = PostHocVerifier(_Registry(), sdl_root="/x", api_caller=caller)
    result = verifier.verify_extraction(artifact, chunks, str(uuid.uuid4()))

    entry = result["item_verifications"][0]
    assert entry["verification_status"] == "unsupported"
    assert entry["supporting_text_excerpts"] == []
    assert result["summary"]["spurious_add_rate"] == 1.0


def test_verify_contradicted_claim_returns_contradicted():
    chunks = _chunks({
        "t-1": "DoD explicitly rejected providing COA data.",
    })
    artifact = _meeting_extraction(claims=[
        _claim("DoD agreed to provide COA data.", ["t-1"]),
    ])
    caller = lambda prompt: {  # noqa: E731
        "verification_status": "contradicted",
        "supporting_text_excerpts": [],
        "verifier_confidence": 0.85,
        "verifier_rationale": "cited turn says the opposite.",
    }
    verifier = PostHocVerifier(_Registry(), sdl_root="/x", api_caller=caller)
    result = verifier.verify_extraction(artifact, chunks, str(uuid.uuid4()))

    assert result["item_verifications"][0]["verification_status"] == "contradicted"
    assert result["summary"]["spurious_add_rate"] == 1.0


def test_verify_empty_excerpts_forces_insufficient_evidence():
    """Defense against silent self-grading collapse."""
    chunks = _chunks({"t-1": "We then discussed the agenda."})
    artifact = _meeting_extraction(claims=[
        _claim("DoD agreed.", ["t-1"]),
    ])
    caller = lambda prompt: {  # noqa: E731
        "verification_status": "verified",
        "supporting_text_excerpts": [],  # empty
        "verifier_confidence": 0.99,
        "verifier_rationale": "trust me.",
    }
    verifier = PostHocVerifier(_Registry(), sdl_root="/x", api_caller=caller)
    result = verifier.verify_extraction(artifact, chunks, str(uuid.uuid4()))

    entry = result["item_verifications"][0]
    assert entry["verification_status"] == "insufficient_evidence"
    assert "downgraded" in entry["verifier_rationale"]


def test_verify_api_failure_returns_verification_failed():
    chunks = _chunks({"t-1": "DoD agreed."})
    artifact = _meeting_extraction(claims=[
        _claim("DoD agreed.", ["t-1"]),
    ])

    def _boom(prompt: str) -> dict[str, Any]:
        raise RuntimeError("api on fire")

    verifier = PostHocVerifier(_Registry(), sdl_root="/x", api_caller=_boom)
    # Must not raise.
    result = verifier.verify_extraction(artifact, chunks, str(uuid.uuid4()))

    entry = result["item_verifications"][0]
    assert entry["verification_status"] == "verification_failed"
    assert "api on fire" in entry["verifier_rationale"]
    assert result["summary"]["verification_failed_count"] == 1


def test_early_halt_on_high_unsupported_rate():
    """The verifier must stop processing after the EARLY_HALT_SAMPLE_SIZE
    threshold is hit, NOT keep churning through every item. We assert
    that fewer than the total number of items got a verification entry
    -- proving it actually halted.
    """
    chunks = _chunks({f"t-{i}": "<unrelated>" for i in range(1, 11)})
    claims = [_claim(f"claim {i}", [f"t-{i}"]) for i in range(1, 11)]
    artifact = _meeting_extraction(claims=claims)

    # Always return "unsupported" -> trips the early-halt sanity check.
    caller = lambda p: {  # noqa: E731
        "verification_status": "unsupported",
        "supporting_text_excerpts": [],
        "verifier_confidence": 0.9,
        "verifier_rationale": "no overlap.",
    }
    verifier = PostHocVerifier(_Registry(), sdl_root="/x", api_caller=caller)
    result = verifier.verify_extraction(artifact, chunks, str(uuid.uuid4()))

    assert result["summary"]["status"] == "halted_sanity_check"
    # Should have processed exactly EARLY_HALT_SAMPLE_SIZE items, not all 10.
    assert len(result["item_verifications"]) == EARLY_HALT_SAMPLE_SIZE
    assert len(result["item_verifications"]) < len(claims)


def test_no_cited_turns_returns_unsupported_without_call():
    chunks = _chunks({})
    artifact = _meeting_extraction(claims=[
        {
            "claim_text": "Some claim",
            "claim_type": "opinion",
            "speaker": "Alice",
            "source_turn_ids": [],
            "source_turn_validation": "missing",
            "confidence": 0.5,
        },
    ])
    calls: list[str] = []

    def caller(prompt: str) -> dict[str, Any]:
        calls.append(prompt)
        return {}

    # Make schema-required min set satisfied by skipping the validator
    # path; the verifier still accepts the input shape.
    verifier = PostHocVerifier(_Registry(), sdl_root="/x", api_caller=caller)
    result = verifier.verify_extraction(artifact, chunks, str(uuid.uuid4()))

    entry = result["item_verifications"][0]
    assert entry["verification_status"] == "unsupported"
    assert calls == []  # never called the model


def test_invalid_status_from_llm_treated_as_verification_failed():
    chunks = _chunks({"t-1": "<something>"})
    artifact = _meeting_extraction(claims=[
        _claim("c", ["t-1"]),
    ])
    caller = lambda p: {  # noqa: E731
        "verification_status": "OK",  # not in enum
        "supporting_text_excerpts": [],
        "verifier_confidence": 0.5,
    }
    verifier = PostHocVerifier(_Registry(), sdl_root="/x", api_caller=caller)
    result = verifier.verify_extraction(artifact, chunks, str(uuid.uuid4()))

    assert result["item_verifications"][0]["verification_status"] == "verification_failed"


def test_summary_counts_all_status_buckets():
    chunks = _chunks({
        "t-v": "agreed.", "t-u": "off topic.", "t-c": "rejected.",
        "t-i": "vague.", "t-f": "x.",
    })
    artifact = _meeting_extraction(claims=[
        _claim("verify", ["t-v"]),
        _claim("unsup", ["t-u"]),
        _claim("contr", ["t-c"]),
        _claim("insuf", ["t-i"]),
        _claim("fail",  ["t-f"]),
    ])
    sequence = iter([
        {"verification_status": "verified",
         "supporting_text_excerpts": ["agreed."],
         "verifier_confidence": 0.9,
         "verifier_rationale": "ok"},
        {"verification_status": "unsupported",
         "supporting_text_excerpts": [],
         "verifier_confidence": 0.9,
         "verifier_rationale": "no"},
        {"verification_status": "contradicted",
         "supporting_text_excerpts": [],
         "verifier_confidence": 0.9,
         "verifier_rationale": "no"},
        {"verification_status": "insufficient_evidence",
         "supporting_text_excerpts": [],
         "verifier_confidence": 0.4,
         "verifier_rationale": "vague"},
        # Triggers api-failure path:
    ])

    def caller(prompt: str) -> dict[str, Any]:
        try:
            return next(sequence)
        except StopIteration:
            raise RuntimeError("kaboom")

    verifier = PostHocVerifier(_Registry(), sdl_root="/x", api_caller=caller)
    result = verifier.verify_extraction(artifact, chunks, str(uuid.uuid4()))

    s = result["summary"]
    assert s["verified_count"] == 1
    assert s["unsupported_count"] == 1
    assert s["contradicted_count"] == 1
    assert s["insufficient_evidence_count"] == 1
    assert s["verification_failed_count"] == 1
    # spurious = (1 unsupported + 1 contradicted) / 5
    assert s["spurious_add_rate"] == pytest.approx(0.4)


def test_decisions_claims_action_items_all_verified():
    chunks = _chunks({"t-1": "agreed"})
    artifact = _meeting_extraction(
        decisions=[{
            "decision_text": "d", "decision_type": "approved",
            "stakeholders": [], "rationale": None,
            "source_turn_ids": ["t-1"], "source_turn_validation": "verified",
            "confidence": 0.9,
        }],
        claims=[_claim("c", ["t-1"])],
        action_items=[{
            "action": "a", "owner": "o", "due": None,
            "source_turn_ids": ["t-1"], "source_turn_validation": "verified",
            "confidence": 0.9,
        }],
    )
    caller = lambda p: {  # noqa: E731
        "verification_status": "verified",
        "supporting_text_excerpts": ["agreed"],
        "verifier_confidence": 0.9,
        "verifier_rationale": "ok",
    }
    verifier = PostHocVerifier(_Registry(), sdl_root="/x", api_caller=caller)
    result = verifier.verify_extraction(artifact, chunks, str(uuid.uuid4()))

    types = {v["item_type"] for v in result["item_verifications"]}
    assert types == {"decision", "claim", "action_item"}


def test_verifier_model_version_recorded():
    chunks = _chunks({"t-1": "x"})
    artifact = _meeting_extraction(claims=[_claim("c", ["t-1"])])
    caller = lambda p: {  # noqa: E731
        "verification_status": "verified",
        "supporting_text_excerpts": ["x"],
        "verifier_confidence": 0.9,
        "verifier_rationale": "ok",
    }
    verifier = PostHocVerifier(
        _Registry(spec={"model": "claude-sonnet-x", "version": "9.9"}),
        sdl_root="/x",
        api_caller=caller,
    )
    result = verifier.verify_extraction(artifact, chunks, str(uuid.uuid4()))
    assert result["item_verifications"][0]["verifier_model_version"].startswith(
        "claude-sonnet-x@"
    )
