"""Shared fixtures for Phase E agency tests."""
from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def write_paper_issue_records(
    repo_root: Path,
    *,
    family: str,
    paper_source_id: str,
    issues: List[Dict[str, Any]],
) -> Path:
    """Write processed/<family>/<paper_source_id>/paper/issues.jsonl."""
    target = repo_root / "processed" / family / paper_source_id / "paper"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "issues.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for issue in issues:
            fh.write(json.dumps(issue, sort_keys=True) + "\n")
    return path


def write_paper_claims(
    repo_root: Path,
    *,
    family: str,
    paper_source_id: str,
    claims: List[Dict[str, Any]],
) -> Path:
    target = repo_root / "processed" / family / paper_source_id / "paper"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "claims.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for c in claims:
            fh.write(json.dumps(c, sort_keys=True) + "\n")
    return path


def make_agency_comment_issue(
    *,
    paper_source_id: str,
    description: str,
    severity: str = "major",
    issue_id: str | None = None,
    comment_source_id: str = "comment-source-A",
) -> Dict[str, Any]:
    iid = issue_id or str(uuid.uuid4())
    fp = "sha256:" + hashlib.sha256((description + iid).encode()).hexdigest()
    return {
        "issue_id": iid,
        "issue_type": "agency_comment",
        "source_id": paper_source_id,
        "source_unit_id": None,
        "claim_id": None,
        "assumption_id": None,
        "description": description,
        "severity": severity,
        "similar_issue_ids": [],
        "status": "open",
        "created_at": _now_iso(),
        "provenance": {
            "produced_by": {
                "component": "comment_processor",
                "version": "1.0.0",
            },
            "input_artifact_ids": [comment_source_id],
            "execution_fingerprint_hash": fp,
        },
    }


def make_position_entry(
    *,
    agency_slug: str,
    topic: str,
    statement: str,
    position_type: str = "raises_concern",
    valid_from: str | None = None,
    valid_until: str | None = None,
    superseded_by: str | None = None,
    source_issue_id: str | None = None,
) -> Dict[str, Any]:
    return {
        "position_id": str(uuid.uuid4()),
        "agency_slug": agency_slug,
        "topic": topic,
        "position_statement": statement,
        "position_type": position_type,
        "source_issue_ids": [source_issue_id or str(uuid.uuid4())],
        "source_comment_ids": ["comment-source-A"],
        "valid_from": valid_from or _today(),
        "valid_until": valid_until,
        "superseded_by": superseded_by,
        "confidence_basis": "test fixture",
        "created_at": _now_iso(),
    }


def make_claim(*, claim_text: str, materiality: str = "high") -> Dict[str, Any]:
    cid = str(uuid.uuid4())
    fp = "sha256:" + hashlib.sha256(cid.encode()).hexdigest()
    return {
        "claim_id": cid,
        "source_id": "paper-A",
        "source_unit_id": str(uuid.uuid4()),
        "source_excerpt": claim_text[:80] or "excerpt",
        "claim_text": claim_text,
        "claim_type": "factual",
        "materiality": materiality,
        "supported_by_evidence_ids": [],
        "contradicted_by_claim_ids": [],
        "extraction_model": "claude-haiku-4-5-20251001",
        "extraction_temperature": 0,
        "status": "candidate",
        "created_at": "2026-05-09T00:00:00+00:00",
        "provenance": {
            "produced_by": {"component": "claim_extractor", "version": "1.0.0"},
            "input_artifact_ids": [cid],
            "execution_fingerprint_hash": fp,
        },
    }


def write_objection_history_entry(
    repo_root: Path,
    *,
    agency_slug: str,
    objection_text: str,
    objection_type: str = "methodology_concern",
    paper_source_id: str = "paper-A",
    raised_at: str | None = None,
    entry_id: str | None = None,
) -> Dict[str, Any]:
    target = repo_root / "agency" / agency_slug
    target.mkdir(parents=True, exist_ok=True)
    entry = {
        "entry_id": entry_id or str(uuid.uuid4()),
        "agency_slug": agency_slug,
        "objection_text": objection_text,
        "objection_type": objection_type,
        "source_issue_id": str(uuid.uuid4()),
        "paper_source_id": paper_source_id,
        "raised_at": raised_at or _today(),
        "resolved": False,
        "resolution_description": "",
        "created_at": _now_iso(),
    }
    path = target / "objection_history.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


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


def make_prediction(
    *,
    agency_slug: str,
    paper_source_id: str = "paper-A",
    confidence: str = "medium",
    evidence_basis: List[str] | None = None,
    objection_type: str = "methodology_concern",
    extraction_temperature: int = 0,
    recency_cutoff_applied: bool = True,
) -> Dict[str, Any]:
    pid = str(uuid.uuid4())
    fp = "sha256:" + hashlib.sha256(pid.encode()).hexdigest()
    if evidence_basis is None:
        evidence_basis = []
    return {
        "prediction_id": pid,
        "agency_slug": agency_slug,
        "paper_source_id": paper_source_id,
        "predicted_objection_text": (
            "The agency would likely object to the methodology used."
        ),
        "objection_type": objection_type,
        "confidence": confidence,
        "evidence_basis": evidence_basis,
        "no_evidence_basis_flag": not evidence_basis,
        "rationale": "Based on prior positions on similar topics.",
        "positions_referenced": list(evidence_basis),
        "recency_cutoff_applied": recency_cutoff_applied,
        "extraction_model": "claude-haiku-4-5-20251001",
        "extraction_temperature": extraction_temperature,
        "status": "candidate",
        "created_at": _now_iso(),
        "provenance": {
            "produced_by": {
                "component": "objection_predictor",
                "version": "1.0.0",
            },
            "input_artifact_ids": [agency_slug, paper_source_id],
            "execution_fingerprint_hash": fp,
        },
    }


def make_mitigation(
    *,
    prediction_id: str,
    agency_slug: str,
    mitigation_type: str = "revise_claim",
    evidence_search_terms: List[str] | None = None,
    extraction_temperature: int = 0,
) -> Dict[str, Any]:
    mid = str(uuid.uuid4())
    fp = "sha256:" + hashlib.sha256(mid.encode()).hexdigest()
    if evidence_search_terms is None:
        evidence_search_terms = (
            ["sensitivity", "robustness"]
            if mitigation_type == "add_evidence"
            else []
        )
    return {
        "mitigation_id": mid,
        "prediction_id": prediction_id,
        "agency_slug": agency_slug,
        "mitigation_text": (
            "Add a sentence acknowledging the alternative interpretation."
        ),
        "mitigation_type": mitigation_type,
        "evidence_search_terms": evidence_search_terms,
        "expected_effectiveness": "medium",
        "rationale": "Addresses the core objection point.",
        "extraction_model": "claude-haiku-4-5-20251001",
        "extraction_temperature": extraction_temperature,
        "status": "pending",
        "created_at": _now_iso(),
        "provenance": {
            "produced_by": {
                "component": "mitigation_suggester",
                "version": "1.0.0",
            },
            "input_artifact_ids": [prediction_id],
            "execution_fingerprint_hash": fp,
        },
    }
