"""Tests for spectrum_systems_core.ai.adapter."""
from __future__ import annotations

import json
import re

import pytest

from spectrum_systems_core.ai.adapter import AIAdapter
from spectrum_systems_core.ai.prompt_registry import PromptRegistry

from ._fixtures import (
    CountingAPICaller,
    FakeDataLakeChecker,
    load_fixture,
    seed_promoted_artifacts,
    setup_phase_h_repo,
)


# Each fixture's first expected_citation also seeds a story_id in the bundle
# so the BundleAssembler/BundleEval pipeline has something concrete to work
# with. The remaining ids are added to the FakeDataLakeChecker so the
# adapter's verification step succeeds.


def _adapter_with_fixture(tmp_path, fixture_name):
    setup_phase_h_repo(tmp_path)
    PromptRegistry().load(str(tmp_path))
    fixture = load_fixture(fixture_name)
    primary_id = fixture["expected_citations"][0]
    seeds = seed_promoted_artifacts(tmp_path, story_id=primary_id)

    checker = FakeDataLakeChecker(fixture["mock_data_lake_exists"])
    # Also mark every seeded id as known so any incidental citation is OK.
    checker.add_many(
        seeds["story_ids"] + seeds["claim_ids"] + seeds["theme_ids"]
    )
    api = CountingAPICaller(fixture["canonical_response"])
    adapter = AIAdapter(api_caller=api, data_lake_checker=checker)
    return adapter, fixture, api, checker


def _run_fixture_query(tmp_path, fixture_name):
    adapter, fixture, api, checker = _adapter_with_fixture(tmp_path, fixture_name)
    return adapter.query(
        task_type=fixture["task_type"],
        question=fixture["mock_question"],
        repo_root=str(tmp_path),
    ), fixture, api


def test_memory_query_success(tmp_path):
    result, fixture, api = _run_fixture_query(tmp_path, "memory_query")
    assert result["status"] == "success", result.get("reason")
    out = result["output"]
    assert out["ai_advisory"] is True
    assert out["requires_human_review"] is True
    assert fixture["expected_citations"][0] in out["citations"]
    assert api.call_count == 1


def test_claim_check_success(tmp_path):
    result, fixture, _ = _run_fixture_query(tmp_path, "claim_check")
    assert result["status"] == "success", result.get("reason")
    assert fixture["expected_citations"][0] in result["output"]["citations"]


def test_objection_check_success(tmp_path):
    result, fixture, _ = _run_fixture_query(tmp_path, "objection_check")
    assert result["status"] == "success", result.get("reason")
    assert fixture["expected_citations"][0] in result["output"]["citations"]


def test_story_fit_success(tmp_path):
    result, fixture, _ = _run_fixture_query(tmp_path, "story_fit")
    assert result["status"] == "success", result.get("reason")
    assert fixture["expected_citations"][0] in result["output"]["citations"]


def test_unregistered_task_fails_before_api_call(tmp_path):
    """FINDING-H-001 / RT2-001: zero API calls for unknown task."""
    setup_phase_h_repo(tmp_path)
    PromptRegistry().load(str(tmp_path))
    seed_promoted_artifacts(tmp_path)
    api = CountingAPICaller("{}")
    adapter = AIAdapter(api_caller=api, data_lake_checker=FakeDataLakeChecker())
    result = adapter.query(
        task_type="totally_made_up",
        question="Q1?",
        repo_root=str(tmp_path),
    )
    assert result["status"] == "failure"
    assert "unregistered_task_type" in result["reason"]
    assert api.call_count == 0


def test_vault_path_citation_blocked(tmp_path):
    """FINDING-H-003 / RT2-003: vault paths must be rejected as non-uuid."""
    setup_phase_h_repo(tmp_path)
    PromptRegistry().load(str(tmp_path))
    seed_promoted_artifacts(tmp_path)
    bad_response = json.dumps(
        {
            "answer": "x [source: /vault/Inbox/note.md]",
            "citations": ["/vault/Inbox/note.md"],
            "confidence": "high",
            "gaps": "",
        }
    )
    api = CountingAPICaller(bad_response)
    adapter = AIAdapter(api_caller=api, data_lake_checker=FakeDataLakeChecker())
    result = adapter.query(
        task_type="memory_query",
        question="Q1?",
        repo_root=str(tmp_path),
    )
    assert result["status"] == "blocked"
    assert result["failure"]["failure_type"] == "non_uuid_citation"


def test_fabricated_uuid_blocked(tmp_path):
    """FINDING-H-003 / RT2-004: a uuid not in DataLake -> blocked."""
    setup_phase_h_repo(tmp_path)
    PromptRegistry().load(str(tmp_path))
    seed_promoted_artifacts(tmp_path)
    fabricated = "00000000-0000-4000-8000-000000000000"
    response = json.dumps(
        {
            "answer": f"x [source: {fabricated}]",
            "citations": [fabricated],
            "confidence": "high",
            "gaps": "",
        }
    )
    api = CountingAPICaller(response)
    checker = FakeDataLakeChecker({fabricated: False})
    adapter = AIAdapter(api_caller=api, data_lake_checker=checker)
    result = adapter.query(
        task_type="memory_query",
        question="Q1?",
        repo_root=str(tmp_path),
    )
    assert result["status"] == "blocked"
    assert result["failure"]["failure_type"] == "fabricated_citation"


def test_ai_advisory_false_blocked(tmp_path):
    """FINDING-H-005 / RT2-005: ai_advisory cannot be False at any layer.

    The adapter sets ai_advisory=True itself, so we exercise the eval
    directly here.
    """
    from spectrum_systems_core.ai.grounding_eval import AIGroundingEval

    setup_phase_h_repo(tmp_path)
    fake_output = {
        "output_id": "11111111-1111-4111-8111-111111111111",
        "query_id": "22222222-2222-4222-8222-222222222222",
        "task_type": "memory_query",
        "raw_response": {"answer": "x"},
        "citations": [],
        "verified_citations": [],
        "unverified_citations": [],
        "grounded": False,
        "ai_advisory": False,
        "requires_human_review": True,
        "confidence": "low",
        "created_at": "2026-05-09T00:00:00+00:00",
        "provenance": {
            "produced_by": {"component": "ai_adapter", "version": "1.0.0"},
            "bundle_id": "33333333-3333-4333-8333-333333333333",
            "bundle_hash": "sha256:" + ("a" * 64),
            "model": "claude-haiku-4-5-20251001",
            "temperature": 0,
        },
    }
    eval_result = AIGroundingEval().run(
        fake_output, fake_output["query_id"], str(tmp_path)
    )
    assert eval_result["decision"] == "block"
    # Schema rejects ai_advisory=false (const: true), so decision is block.
    assert any(
        "schema_conformance" in code or "advisory_flag_present" in code
        for code in eval_result["reason_codes"]
    )


def test_json_parse_failure_blocked(tmp_path):
    """RT3-003: model returns non-JSON text -> blocked, not crashed."""
    setup_phase_h_repo(tmp_path)
    PromptRegistry().load(str(tmp_path))
    seed_promoted_artifacts(tmp_path)
    api = CountingAPICaller("This is not JSON at all.")
    adapter = AIAdapter(api_caller=api, data_lake_checker=FakeDataLakeChecker())
    result = adapter.query(
        task_type="memory_query",
        question="Q1?",
        repo_root=str(tmp_path),
    )
    assert result["status"] == "blocked"
    assert result["failure"]["failure_type"] == "schema_violation"


def test_cost_record_written(tmp_path):
    """FINDING-H-006: every query writes a cost record."""
    result, fixture, _ = _run_fixture_query(tmp_path, "memory_query")
    assert result["status"] == "success"
    cost_dir = tmp_path / "ai" / "costs"
    cost_files = [p for p in cost_dir.glob("*.json") if p.name != "monthly.json"]
    assert len(cost_files) == 1
    record = json.loads(cost_files[0].read_text(encoding="utf-8"))
    assert record["query_id"] == result["output"]["query_id"]
    assert record["estimated_cost_usd"] >= 0


def test_monthly_cost_accumulates(tmp_path):
    """RT4-005: monthly.json must accumulate across queries."""
    adapter, fixture, _, _ = _adapter_with_fixture(tmp_path, "memory_query")
    adapter.query(
        task_type=fixture["task_type"],
        question=fixture["mock_question"],
        repo_root=str(tmp_path),
    )
    adapter.query(
        task_type=fixture["task_type"],
        question=fixture["mock_question"],
        repo_root=str(tmp_path),
    )
    monthly = json.loads(
        (tmp_path / "ai" / "costs" / "monthly.json").read_text(encoding="utf-8")
    )
    assert monthly["query_count"] == 2
    assert monthly["total_cost_usd"] > 0


def test_query_record_written(tmp_path):
    result, _, _ = _run_fixture_query(tmp_path, "memory_query")
    assert result["status"] == "success"
    queries_dir = tmp_path / "ai" / "queries"
    files = list(queries_dir.glob("*.json"))
    assert len(files) >= 1
    record = json.loads(files[0].read_text(encoding="utf-8"))
    assert record["status"] == "success"
    assert record["temperature"] == 0


def test_output_written_on_success(tmp_path):
    """RT4-004: ai/outputs/<id>.json exists with ai_advisory=True."""
    result, _, _ = _run_fixture_query(tmp_path, "memory_query")
    output_id = result["output"]["output_id"]
    out_path = tmp_path / "ai" / "outputs" / f"{output_id}.json"
    assert out_path.is_file()
    on_disk = json.loads(out_path.read_text(encoding="utf-8"))
    assert on_disk["ai_advisory"] is True
    assert on_disk["requires_human_review"] is True


def test_high_confidence_no_citations_blocked(tmp_path):
    """RT3-001: high-confidence answer with no citations is a hallucination signal."""
    setup_phase_h_repo(tmp_path)
    PromptRegistry().load(str(tmp_path))
    seed_promoted_artifacts(tmp_path)
    response = json.dumps(
        {"answer": "X", "citations": [], "confidence": "high", "gaps": ""}
    )
    api = CountingAPICaller(response)
    adapter = AIAdapter(api_caller=api, data_lake_checker=FakeDataLakeChecker())
    result = adapter.query(
        task_type="memory_query",
        question="Q1?",
        repo_root=str(tmp_path),
    )
    assert result["status"] == "blocked"
    assert result["failure"]["failure_type"] == "no_citations_in_output"


def test_low_confidence_no_citations_warns_not_blocks(tmp_path):
    """RT3-002: low-confidence + no citations = success with grounded=False."""
    setup_phase_h_repo(tmp_path)
    PromptRegistry().load(str(tmp_path))
    seed_promoted_artifacts(tmp_path)
    response = json.dumps(
        {
            "answer": "Unknown",
            "citations": [],
            "confidence": "low",
            "gaps": "No relevant data found",
        }
    )
    api = CountingAPICaller(response)
    adapter = AIAdapter(api_caller=api, data_lake_checker=FakeDataLakeChecker())
    result = adapter.query(
        task_type="memory_query",
        question="Q1?",
        repo_root=str(tmp_path),
    )
    assert result["status"] == "success", result.get("reason")
    assert result["output"]["grounded"] is False
    assert result["output"]["confidence"] == "low"


def test_replay_determinism(tmp_path):
    """RT4-001: identical inputs produce identical output bodies."""
    fix_name = "memory_query"
    adapter1, fixture, _, _ = _adapter_with_fixture(tmp_path, fix_name)
    r1 = adapter1.query(
        task_type=fixture["task_type"],
        question=fixture["mock_question"],
        repo_root=str(tmp_path),
    )

    # Second run in a fresh tmp.
    tmp2 = tmp_path / "second"
    tmp2.mkdir()
    adapter2, fixture2, _, _ = _adapter_with_fixture(tmp2, fix_name)
    r2 = adapter2.query(
        task_type=fixture2["task_type"],
        question=fixture2["mock_question"],
        repo_root=str(tmp2),
    )

    assert r1["status"] == r2["status"] == "success"
    o1 = r1["output"]
    o2 = r2["output"]
    # Stable parts: response payload, citations, advisory flags, confidence.
    assert o1["raw_response"] == o2["raw_response"]
    assert o1["citations"] == o2["citations"]
    assert o1["confidence"] == o2["confidence"]
    assert o1["ai_advisory"] is True and o2["ai_advisory"] is True
