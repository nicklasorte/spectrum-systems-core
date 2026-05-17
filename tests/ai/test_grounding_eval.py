"""Tests for spectrum_systems_core.ai.grounding_eval."""
from __future__ import annotations

import json
import uuid

from spectrum_systems_core.ai.grounding_eval import (
    MAX_QUERY_COST_USD,
    AIGroundingEval,
)

from ._fixtures import setup_phase_h_repo


def _make_output(**overrides):
    base = {
        "output_id": str(uuid.uuid4()),
        "query_id": str(uuid.uuid4()),
        "task_type": "memory_query",
        "raw_response": {"answer": "x"},
        "citations": [],
        "verified_citations": [],
        "unverified_citations": [],
        "grounded": False,
        "ai_advisory": True,
        "requires_human_review": True,
        "confidence": "low",
        "created_at": "2026-05-09T00:00:00+00:00",
        "provenance": {
            "produced_by": {"component": "ai_adapter", "version": "1.0.0"},
            "bundle_id": str(uuid.uuid4()),
            "bundle_hash": "sha256:" + ("0" * 64),
            "model": "claude-haiku-4-5-20251001",
            "temperature": 0,
        },
    }
    base.update(overrides)
    return base


def test_valid_output_allows(tmp_path):
    setup_phase_h_repo(tmp_path)
    cid = "f0c6a1e0-4a89-4ed1-8b5e-85c9a79d1f7f"
    out = _make_output(
        citations=[cid],
        verified_citations=[cid],
        unverified_citations=[],
        grounded=True,
        confidence="high",
    )
    result = AIGroundingEval().run(out, out["query_id"], str(tmp_path))
    assert result["decision"] == "allow"


def test_advisory_flag_missing_blocks(tmp_path):
    setup_phase_h_repo(tmp_path)
    out = _make_output(ai_advisory=False)
    result = AIGroundingEval().run(out, out["query_id"], str(tmp_path))
    assert result["decision"] == "block"


def test_unverified_citation_blocks(tmp_path):
    setup_phase_h_repo(tmp_path)
    cid = "f0c6a1e0-4a89-4ed1-8b5e-85c9a79d1f7f"
    out = _make_output(
        citations=[cid],
        verified_citations=[],
        unverified_citations=[cid],
        grounded=False,
        confidence="medium",
    )
    result = AIGroundingEval().run(out, out["query_id"], str(tmp_path))
    assert result["decision"] == "block"
    assert "fabricated_citation" in result["failure_types"]


def test_non_uuid_citation_blocks(tmp_path):
    setup_phase_h_repo(tmp_path)
    out = _make_output(
        citations=["/vault/notes/x.md"],
        verified_citations=[],
        unverified_citations=["/vault/notes/x.md"],
        grounded=False,
        confidence="medium",
    )
    result = AIGroundingEval().run(out, out["query_id"], str(tmp_path))
    assert result["decision"] == "block"
    assert "non_uuid_citation" in result["failure_types"]


def test_temperature_not_zero_blocks(tmp_path):
    setup_phase_h_repo(tmp_path)
    out = _make_output()
    out["provenance"]["temperature"] = 0.7
    result = AIGroundingEval().run(out, out["query_id"], str(tmp_path))
    assert result["decision"] == "block"


def test_cost_over_threshold_warns_not_blocks(tmp_path):
    setup_phase_h_repo(tmp_path)
    cid = "f0c6a1e0-4a89-4ed1-8b5e-85c9a79d1f7f"
    out = _make_output(
        citations=[cid],
        verified_citations=[cid],
        unverified_citations=[],
        grounded=True,
        confidence="medium",
    )
    cost_dir = tmp_path / "ai" / "costs"
    cost_dir.mkdir(parents=True, exist_ok=True)
    cost_path = cost_dir / f"{out['query_id']}.json"
    cost_path.write_text(
        json.dumps(
            {
                "cost_id": str(uuid.uuid4()),
                "query_id": out["query_id"],
                "task_type": "memory_query",
                "model": "claude-haiku-4-5-20251001",
                "input_tokens": 1000,
                "output_tokens": 1000,
                "estimated_cost_usd": MAX_QUERY_COST_USD + 0.5,
                "recorded_at": "2026-05-09T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    result = AIGroundingEval().run(out, out["query_id"], str(tmp_path))
    assert result["decision"] == "allow"
    assert any("cost_over_threshold" in code for code in result["warn_codes"])
