"""Tests for spectrum_systems_core.ai.memory_context_builder."""
from __future__ import annotations

import json

from spectrum_systems_core.ai.memory_context_builder import MemoryContextBuilder
from spectrum_systems_core.ai.prompt_registry import PromptRegistry

from ._fixtures import seed_promoted_artifacts, setup_phase_h_repo


def test_build_returns_context_text_with_citations(tmp_path):
    setup_phase_h_repo(tmp_path)
    PromptRegistry().load(str(tmp_path))
    seeds = seed_promoted_artifacts(tmp_path)

    result = MemoryContextBuilder().build("memory_query", "Q1?", str(tmp_path))
    assert result["status"] == "success", result["reason"]
    text = result["context_text"]
    assert "[source:" in text
    # At least one of the seeded artifact_ids must be present.
    seeded_ids = (
        seeds["story_ids"] + seeds["claim_ids"] + seeds["theme_ids"]
    )
    assert any(aid in text for aid in seeded_ids)


def test_candidate_artifact_blocked_by_bundle_eval(tmp_path):
    """FINDING-H-002: a non-promoted item must not reach the AI context.

    BundleAssembler enforces promoted_only=True at the source — we
    confirm here that injecting a candidate story manually into a bundle
    causes BundleEval to block. MemoryContextBuilder propagates that
    block as status="blocked".
    """
    from spectrum_systems_core.ai.memory_context_builder import (
        MemoryContextBuilder as _MCB,
    )
    from spectrum_systems_core.synthesis import BundleAssembler

    setup_phase_h_repo(tmp_path)
    PromptRegistry().load(str(tmp_path))
    seed_promoted_artifacts(tmp_path)

    real_assemble = BundleAssembler.assemble

    def _bad_assemble(self, run_id, recipe_id, audience, purpose, repo_root):
        result = real_assemble(self, run_id, recipe_id, audience, purpose, repo_root)
        if result["status"] == "success" and result["bundle"]["items"]:
            result["bundle"]["items"][0]["promoted_status"] = "candidate"
        return result

    BundleAssembler.assemble = _bad_assemble
    try:
        result = _MCB().build("memory_query", "Q1?", str(tmp_path))
    finally:
        BundleAssembler.assemble = real_assemble

    assert result["status"] == "blocked"
    assert "EVAL-CTX-002" in result["reason"]


def test_token_budget_enforced(tmp_path):
    """The context_text length should respect max_bundle_tokens * 4 chars."""
    setup_phase_h_repo(tmp_path)
    PromptRegistry().load(str(tmp_path))
    seed_promoted_artifacts(tmp_path)

    result = MemoryContextBuilder().build("story_fit", "Q1?", str(tmp_path))
    assert result["status"] == "success", result["reason"]
    # story_fit max_bundle_tokens=2000 -> 8000 char budget
    assert len(result["context_text"]) <= 8000


def test_empty_retrieval_returns_failure(tmp_path):
    """No promoted artifacts -> bundle assembly fails -> builder reports failure."""
    setup_phase_h_repo(tmp_path)
    PromptRegistry().load(str(tmp_path))
    # Intentionally seed nothing.
    result = MemoryContextBuilder().build("memory_query", "Q1?", str(tmp_path))
    assert result["status"] == "failure"
    assert "no_eligible_artifacts" in result["reason"]


def test_context_text_includes_source_annotations(tmp_path):
    """Each item must appear as `<artifact_type> [source: <id>]:` in context."""
    setup_phase_h_repo(tmp_path)
    PromptRegistry().load(str(tmp_path))
    seed_promoted_artifacts(tmp_path)
    result = MemoryContextBuilder().build("memory_query", "Q1?", str(tmp_path))
    assert result["status"] == "success"
    assert "[source:" in result["context_text"]
    # A type label must appear before the [source: ...] marker.
    text = result["context_text"]
    candidates = ("technical_claim", "story_candidate", "theme_record")
    assert any(c in text for c in candidates)


def test_unregistered_task_type_returns_failure(tmp_path):
    """RT2-001: builder reports failure for unknown task before any work."""
    setup_phase_h_repo(tmp_path)
    PromptRegistry().load(str(tmp_path))
    result = MemoryContextBuilder().build("unknown_task", "Q1?", str(tmp_path))
    assert result["status"] == "failure"
    assert "unregistered_task_type" in result["reason"]
