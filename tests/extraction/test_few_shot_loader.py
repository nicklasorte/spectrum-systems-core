"""Few-shot wiring at the EXTRACTOR level.

The loader itself is exercised in ``tests/eval/test_few_shot.py``. These
tests cover the integration: do the three typed extractors actually
inject few-shot examples into their prompts, record the right metadata,
and degrade gracefully on version mismatch / missing seed?
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from spectrum_systems_core.extraction.action_item_extractor import (
    ActionItemExtractor,
)
from spectrum_systems_core.extraction.claim_extractor import ClaimExtractor
from spectrum_systems_core.extraction.decision_extractor import (
    DecisionExtractor,
)

SEED_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "eval"
    / "seeds"
    / "extraction_few_shot_v1.json"
)


def _capture_prompt() -> tuple[list[str], Callable[[str], dict[str, Any]]]:
    """Return ``(captured, api_caller)``: the caller records the prompt."""
    captured: list[str] = []

    def caller(prompt: str) -> dict[str, Any]:
        captured.append(prompt)
        return {"items": []}

    return captured, caller


def _decision_chunk() -> dict[str, Any]:
    return {"chunk_id": "c1", "speaker": "S", "text": "Group approves plan A."}


def _claim_chunk() -> dict[str, Any]:
    return {"chunk_id": "c2", "speaker": "S", "text": "Plan A reduces I/N by 3 dB."}


def _action_chunk() -> dict[str, Any]:
    return {"chunk_id": "c3", "speaker": "S", "text": "Alice will draft the memo."}


# ---------------------------------------------------------------------------
# Decision extractor

def test_decision_extractor_injects_few_shot_examples_into_prompt() -> None:
    captured, caller = _capture_prompt()
    ex = DecisionExtractor(api_caller=caller, few_shot_path=str(SEED_PATH))
    ex.extract([_decision_chunk()], "", available_turn_ids={"c1"})
    assert len(captured) == 1
    prompt = captured[0]
    # The format_examples_for_prompt block has a recognisable header.
    assert "Here are examples of valid extraction:" in prompt
    # The decision-type example body should be present (filtered by type).
    assert "decision_text" in prompt
    # Action-item-only and claim-only example bodies must NOT appear in a
    # decision-extractor prompt: the filter is by example_type.
    assert "claim_text" not in prompt
    assert "\"action\":" not in prompt and "\"owner\":" not in prompt


def test_decision_extractor_metadata_on_version_match() -> None:
    _, caller = _capture_prompt()
    ex = DecisionExtractor(api_caller=caller, few_shot_path=str(SEED_PATH))
    ex.extract([_decision_chunk()], "", available_turn_ids={"c1"})
    md = ex.last_run_metadata
    assert md["few_shot_injected"] is True
    assert md["few_shot_version"] == "1.0.0"
    assert md["few_shot_example_count"] >= 1  # seed has one decision example
    assert md["omit_instruction_present"] is True


def test_decision_extractor_skips_injection_on_version_mismatch(tmp_path) -> None:
    """Seed prompt_schema_version != extractor PROMPT_SCHEMA_VERSION -> no injection."""
    seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    seed["prompt_schema_version"] = "9.9.9"  # mismatch
    p = tmp_path / "mismatch.json"
    p.write_text(json.dumps(seed), encoding="utf-8")

    captured, caller = _capture_prompt()
    ex = DecisionExtractor(api_caller=caller, few_shot_path=str(p))
    ex.extract([_decision_chunk()], "", available_turn_ids={"c1"})
    md = ex.last_run_metadata
    assert md["few_shot_injected"] is False
    assert md["few_shot_version"] is None
    assert md["few_shot_example_count"] == 0
    prompt = captured[0]
    assert "Here are examples of valid extraction:" not in prompt


def test_decision_extractor_handles_missing_seed_file(tmp_path) -> None:
    captured, caller = _capture_prompt()
    missing = tmp_path / "nope.json"
    ex = DecisionExtractor(api_caller=caller, few_shot_path=str(missing))
    # Must not raise even though seed is absent.
    items = ex.extract([_decision_chunk()], "", available_turn_ids={"c1"})
    assert items == []
    assert ex.last_run_metadata["few_shot_injected"] is False
    assert "Here are examples of valid extraction:" not in captured[0]


# ---------------------------------------------------------------------------
# Claim extractor

def test_claim_extractor_injects_only_claim_examples() -> None:
    captured, caller = _capture_prompt()
    ex = ClaimExtractor(api_caller=caller, few_shot_path=str(SEED_PATH))
    ex.extract([_claim_chunk()], "", available_turn_ids={"c2"})
    prompt = captured[0]
    assert "Here are examples of valid extraction:" in prompt
    assert "claim_text" in prompt
    assert ex.last_run_metadata["few_shot_injected"] is True
    assert ex.last_run_metadata["few_shot_example_count"] >= 1


# ---------------------------------------------------------------------------
# Action item extractor

def test_action_item_extractor_injects_only_action_examples() -> None:
    captured, caller = _capture_prompt()
    ex = ActionItemExtractor(api_caller=caller, few_shot_path=str(SEED_PATH))
    ex.extract([_action_chunk()], "", available_turn_ids={"c3"})
    prompt = captured[0]
    assert "Here are examples of valid extraction:" in prompt
    # action_item example uses "action": and "owner": keys.
    assert "\"action\":" in prompt
    assert "\"owner\":" in prompt
    assert ex.last_run_metadata["few_shot_injected"] is True


# ---------------------------------------------------------------------------
# Run-record propagation: metadata flows extractor -> merger -> artifact.

def test_meeting_extraction_records_few_shot_injected_flag() -> None:
    """End-to-end inside one process: merger emits few_shot_* fields from extractor metadata."""
    from spectrum_systems_core.extraction.extraction_merger import (
        ExtractionMerger,
    )

    _, caller = _capture_prompt()
    decision_x = DecisionExtractor(
        api_caller=caller, few_shot_path=str(SEED_PATH)
    )
    decision_x.extract([_decision_chunk()], "", available_turn_ids={"c1"})

    # Match version OK case: metadata says injected=True.
    artifact = ExtractionMerger().merge(
        source_artifact_id="00000000-0000-0000-0000-000000000000",
        extraction_run_id="run-1",
        classifications=[],
        decisions=[],
        claims=[],
        action_items=[],
        run_metadata=[decision_x.last_run_metadata],
    )
    assert artifact["few_shot_injected"] is True
    assert artifact["few_shot_version"] == "1.0.0"
    assert artifact["few_shot_example_count"] >= 1


def test_no_vacuous_injection_when_zero_examples_of_type(tmp_path) -> None:
    """Seed exists and version matches but has zero examples of the requested type.

    The previous behaviour would have set ``few_shot_injected=True`` with
    ``example_count=0`` because format_examples_for_prompt returns a
    header+footer block even when no examples match. Guard against
    regression.
    """
    seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    # Keep only the action_item example so a decision-extractor run finds
    # zero matches.
    seed["examples"] = [
        ex for ex in seed["examples"] if ex["example_type"] == "action_item"
    ]
    p = tmp_path / "decision_empty.json"
    p.write_text(json.dumps(seed), encoding="utf-8")

    captured, caller = _capture_prompt()
    ex = DecisionExtractor(api_caller=caller, few_shot_path=str(p))
    ex.extract([_decision_chunk()], "", available_turn_ids={"c1"})
    md = ex.last_run_metadata
    assert md["few_shot_injected"] is False, (
        "Zero matching examples must report injected=False, not True with count=0"
    )
    assert md["few_shot_example_count"] == 0
    # And the rendered prompt should NOT carry an empty stub block.
    assert "Here are examples of valid extraction:" not in captured[0]


def test_meeting_extraction_records_no_injection_on_mismatch(tmp_path) -> None:
    """When all three extractors saw a version mismatch, artifact reflects False."""
    from spectrum_systems_core.extraction.extraction_merger import (
        ExtractionMerger,
    )

    seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    seed["prompt_schema_version"] = "9.9.9"
    p = tmp_path / "mismatch.json"
    p.write_text(json.dumps(seed), encoding="utf-8")

    _, caller = _capture_prompt()
    decision_x = DecisionExtractor(api_caller=caller, few_shot_path=str(p))
    decision_x.extract([_decision_chunk()], "", available_turn_ids={"c1"})

    artifact = ExtractionMerger().merge(
        source_artifact_id="00000000-0000-0000-0000-000000000000",
        extraction_run_id="run-1",
        classifications=[],
        decisions=[],
        claims=[],
        action_items=[],
        run_metadata=[decision_x.last_run_metadata],
    )
    assert artifact["few_shot_injected"] is False
    assert artifact["few_shot_version"] is None
    assert artifact["few_shot_example_count"] == 0
