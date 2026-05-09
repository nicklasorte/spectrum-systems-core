"""Shared fixtures for Phase F synthesis tests."""
from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _execution_fingerprint(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def write_promoted_story(
    repo_root: Path,
    *,
    family: str = "books",
    source_id: str = "src-A",
    story_id: Optional[str] = None,
    summary: str = "A grounded story about adjacent channel interference.",
    theme: str = "adjacent channel interference modelling",
    tier_guess: str = "tier_1",
    status: str = "promoted",
    created_at: Optional[str] = None,
) -> str:
    sid = story_id or str(uuid.uuid4())
    target = repo_root / "processed" / family / source_id / "stories" / "promoted"
    target.mkdir(parents=True, exist_ok=True)
    story = {
        "story_id": sid,
        "source_id": source_id,
        "source_family": family,
        "chunk_id": str(uuid.uuid4()),
        "unit_ids": [str(uuid.uuid4())],
        "page_numbers": [1],
        "source_excerpt": "Excerpt verbatim of at least ten characters.",
        "story_summary": summary,
        "possible_theme": theme,
        "tier_guess": tier_guess,
        "why_it_might_work": "It illustrates a vivid moment.",
        "risk_flags": [],
        "storyworthy_score": {
            "five_second_moment": 3,
            "stakes": 3,
            "central_question": 3,
            "vulnerability": 2,
            "narrative_compression": 2,
            "total": 13,
        },
        "storyworthy_verdict": "admit",
        "extraction_model": "claude-haiku-4-5-20251001",
        "extraction_temperature": 0,
        "grounded": True,
        "grounded_unit_ids": [],
        "status": status,
        "superseded_by": None,
        "created_at": created_at or _now_iso(),
        "provenance": {
            "produced_by": {"component": "test_fixture", "version": "1.0.0"},
            "input_artifact_ids": [source_id],
            "execution_fingerprint_hash": _execution_fingerprint(sid),
        },
    }
    (target / f"{sid}.json").write_text(
        json.dumps(story, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return sid


def write_evidenced_claim(
    repo_root: Path,
    *,
    family: str = "working_papers",
    source_id: str = "paper-A",
    claim_id: Optional[str] = None,
    claim_text: str = "Adjacent channel interference impacts allocations.",
    materiality: str = "high",
    status: str = "evidenced",
) -> str:
    cid = claim_id or str(uuid.uuid4())
    target = repo_root / "processed" / family / source_id / "paper"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "claims.jsonl"
    claim = {
        "claim_id": cid,
        "source_id": source_id,
        "source_unit_id": str(uuid.uuid4()),
        "source_excerpt": "verbatim source excerpt at least ten chars.",
        "claim_text": claim_text,
        "claim_type": "factual",
        "materiality": materiality,
        "supported_by_evidence_ids": [],
        "contradicted_by_claim_ids": [],
        "extraction_model": "claude-haiku-4-5-20251001",
        "extraction_temperature": 0,
        "status": status,
        "created_at": _now_iso(),
        "provenance": {
            "produced_by": {"component": "test_fixture", "version": "1.0.0"},
            "input_artifact_ids": [source_id],
            "execution_fingerprint_hash": _execution_fingerprint(cid),
        },
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(claim, sort_keys=True) + "\n")
    return cid


def write_promoted_theme(
    repo_root: Path,
    *,
    family: str = "books",
    source_id: str = "src-A",
    theme_id: Optional[str] = None,
    theme_name: str = "adjacent channel interference",
    description: str = "Recurring theme across sources of adjacent channel.",
    source_story_ids: Optional[List[str]] = None,
) -> str:
    tid = theme_id or str(uuid.uuid4())
    target = repo_root / "processed" / family / source_id / "knowledge" / "promoted"
    target.mkdir(parents=True, exist_ok=True)
    record = {
        "theme_id": tid,
        "theme_name": theme_name,
        "description": description,
        "source_story_ids": source_story_ids or [str(uuid.uuid4())],
        "source_ids": [source_id],
        "supporting_excerpts": [
            {
                "unit_id": str(uuid.uuid4()),
                "excerpt": "Supporting excerpt text.",
                "source_id": source_id,
            }
        ],
        "status": "promoted",
        "created_at": _now_iso(),
    }
    (target / f"{tid}.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return tid


def make_bundle(
    *,
    run_id: Optional[str] = None,
    audience: str = "technical",
    purpose: str = "report",
    items: Optional[List[Dict[str, Any]]] = None,
    bundle_hash: Optional[str] = None,
    bundle_id: Optional[str] = None,
) -> Dict[str, Any]:
    rid = run_id or str(uuid.uuid4())
    bid = bundle_id or str(uuid.uuid4())
    items = items or []
    total = sum(int(it.get("token_estimate", 1)) for it in items)
    bh = bundle_hash or (
        "sha256:" + hashlib.sha256(("|".join(sorted(it["artifact_id"] for it in items))).encode()).hexdigest()
    )
    return {
        "bundle_id": bid,
        "run_id": rid,
        "recipe_id": "default_report_v1",
        "recipe_version": "1.0.0",
        "audience": audience,
        "purpose": purpose,
        "items": items,
        "total_token_estimate": total,
        "token_budget": 6000,
        "bundle_hash": bh,
        "assembled_at": _now_iso(),
        "provenance": {
            "produced_by": {"component": "test_fixture", "version": "1.0.0"},
            "input_artifact_ids": [it["artifact_id"] for it in items],
            "execution_fingerprint_hash": _execution_fingerprint(rid + bid),
        },
    }


def make_bundle_item(
    *,
    artifact_id: Optional[str] = None,
    artifact_type: str = "story_candidate",
    source_id: str = "src-A",
    excerpt: str = "Sample excerpt text long enough to satisfy minLength.",
    token_estimate: int = 50,
    promoted_status: str = "promoted",
) -> Dict[str, Any]:
    return {
        "item_id": str(uuid.uuid4()),
        "artifact_id": artifact_id or str(uuid.uuid4()),
        "artifact_type": artifact_type,
        "source_id": source_id,
        "content_excerpt": excerpt,
        "token_estimate": int(token_estimate),
        "promoted_status": promoted_status,
        "inclusion_reason": "test_fixture",
    }


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not path.is_file():
        return out
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out
