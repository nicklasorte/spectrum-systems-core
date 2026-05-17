"""EvidenceBuilder: deterministic claim → evidence linking.

No LLM. Searches text_units.jsonl for units that overlap with a claim's
keywords and emits evidence_record artifacts. source_record_hash is
captured at build time so EVAL-EVID-003 can warn on stale evidence
(FINDING-D-005).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import jsonschema

from ..extraction._paths import find_processed_dir
from ..ingestion.grounding import GroundingHelper
from ._paths import paper_schema_path

_COMPONENT_NAME = "evidence_builder"
_COMPONENT_VERSION = "1.0.0"

_STOPWORDS = {
    "the", "and", "for", "from", "with", "that", "this", "than", "when",
    "while", "after", "before", "about", "their", "there", "these", "those",
    "into", "over", "such", "have", "been", "will", "would", "could", "should",
    "where", "which", "while", "what", "whose", "they", "them", "were", "your",
    "more", "less", "much", "very", "some", "most", "also", "only", "just",
    "make", "made", "made", "into", "many", "must", "still", "even", "upon",
    "thus", "across", "among", "without", "within", "between",
}

MIN_OVERLAP_SCORE = 2
TOP_N = 3
EXCERPT_PREFIX_LEN = 200


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _execution_fingerprint(claim_id: str, unit_id: str) -> str:
    seed = f"{claim_id}|{unit_id}|{_COMPONENT_NAME}:{_COMPONENT_VERSION}"
    return "sha256:" + _sha256_hex(seed.encode("utf-8"))


def _significant_words(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.split():
        w = "".join(ch for ch in raw.lower() if ch.isalnum())
        if len(w) > 4 and w not in _STOPWORDS:
            out.append(w)
    return out


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.is_file():
        return out
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


class EvidenceBuilder:
    """Build evidence_record artifacts deterministically from text units."""

    def __init__(self, grounding: GroundingHelper | None = None) -> None:
        self._grounding = grounding or GroundingHelper()

    def build_for_claim(
        self,
        claim: dict[str, Any],
        source_id: str,
        repo_root: str,
    ) -> dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        processed_dir, _ = find_processed_dir(repo_root_path, source_id)
        if processed_dir is None:
            return {
                "status": "failure",
                "evidence_records": [],
                "reason": "source_not_found",
            }

        # Step 1: read source_record raw_hash to seal evidence freshness.
        source_record_path = processed_dir / "source_record.json"
        try:
            source_record = json.loads(
                source_record_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return {
                "status": "failure",
                "evidence_records": [],
                "reason": "source_record_unreadable",
            }
        raw_hash = source_record.get("payload", {}).get("raw_hash")
        if not isinstance(raw_hash, str) or not raw_hash.startswith("sha256:"):
            return {
                "status": "failure",
                "evidence_records": [],
                "reason": "source_record_hash_missing",
            }

        # Step 2: find candidate units.
        text_units = _read_jsonl(processed_dir / "text_units.jsonl")
        keywords = set(_significant_words(claim.get("claim_text", "")))
        if not keywords:
            return {
                "status": "success",
                "evidence_records": [],
                "reason": "",
            }

        scored: list[tuple] = []
        for unit in text_units:
            text = unit.get("text", "") or ""
            if not isinstance(text, str):
                continue
            unit_words = set(_significant_words(text))
            if not unit_words:
                continue
            score = len(keywords & unit_words)
            if score >= MIN_OVERLAP_SCORE:
                scored.append((score, unit))
        scored.sort(key=lambda t: t[0], reverse=True)
        top_units = [u for _, u in scored[:TOP_N]]

        # Step 3: verify each candidate excerpt.
        try:
            schema = json.loads(
                paper_schema_path("evidence_record").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError) as exc:
            return {
                "status": "failure",
                "evidence_records": [],
                "reason": f"schema_unreadable: {exc}",
            }
        validator = jsonschema.Draft202012Validator(schema)

        evidence_records: list[dict[str, Any]] = []
        for unit in top_units:
            excerpt = (unit.get("text") or "")[:EXCERPT_PREFIX_LEN]
            if len(excerpt) < 10:
                continue
            try:
                result = self._grounding.verify_excerpt(
                    excerpt, source_id, repo_root
                )
            except Exception:  # noqa: BLE001
                continue
            if not result.get("grounded"):
                continue
            unit_id = unit.get("unit_id")
            if not isinstance(unit_id, str):
                continue

            # FINDING-D evidence — claim cannot evidence itself.
            same_unit = unit_id == claim.get("source_unit_id")
            evidence_type = "direct_support" if not same_unit else "indirect_support"
            # We still emit if same_unit, but EVAL-EVID-004 will block it
            # downstream so the operator notices. Skip outright instead.
            if same_unit:
                continue

            record = {
                "evidence_id": str(uuid.uuid4()),
                "claim_id": claim["claim_id"],
                "source_id": source_id,
                "source_unit_id": unit_id,
                "source_excerpt": excerpt,
                "evidence_type": evidence_type,
                "source_record_hash": raw_hash,
                "grounded": True,
                "grounded_unit_ids": list(result.get("matching_unit_ids") or []),
                "created_at": _now_iso(),
                "provenance": {
                    "produced_by": {
                        "component": _COMPONENT_NAME,
                        "version": _COMPONENT_VERSION,
                    },
                    "input_artifact_ids": [claim["claim_id"], unit_id],
                    "execution_fingerprint_hash": _execution_fingerprint(
                        claim["claim_id"], unit_id
                    ),
                },
            }
            try:
                validator.validate(record)
            except jsonschema.ValidationError:
                continue
            evidence_records.append(record)

        return {
            "status": "success",
            "evidence_records": evidence_records,
            "reason": "",
        }

    def build_for_source(
        self, source_id: str, repo_root: str
    ) -> dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        processed_dir, _ = find_processed_dir(repo_root_path, source_id)
        if processed_dir is None:
            return {
                "status": "failure",
                "evidence_count": 0,
                "reason": "source_not_found",
            }
        paper_dir = processed_dir / "paper"
        paper_dir.mkdir(parents=True, exist_ok=True)
        claims_path = paper_dir / "claims.jsonl"
        if not claims_path.is_file():
            return {
                "status": "failure",
                "evidence_count": 0,
                "reason": "claims_jsonl_not_found",
            }

        claims = _read_jsonl(claims_path)
        all_evidence: list[dict[str, Any]] = []

        for claim in claims:
            if claim.get("status") != "candidate":
                continue
            res = self.build_for_claim(claim, source_id, repo_root)
            records = res.get("evidence_records", [])
            all_evidence.extend(records)
            claim["supported_by_evidence_ids"] = [
                r["evidence_id"] for r in records
            ]
            if records and claim.get("status") == "candidate":
                claim["status"] = "evidenced"

        evidence_path = paper_dir / "evidence.jsonl"
        try:
            with evidence_path.open("w", encoding="utf-8") as fh:
                for record in all_evidence:
                    fh.write(
                        json.dumps(record, sort_keys=True, separators=(",", ":"))
                        + "\n"
                    )
            with claims_path.open("w", encoding="utf-8") as fh:
                for claim in claims:
                    fh.write(
                        json.dumps(claim, sort_keys=True, separators=(",", ":"))
                        + "\n"
                    )
        except OSError as exc:
            return {
                "status": "failure",
                "evidence_count": 0,
                "reason": f"write_error: {exc}",
            }

        return {
            "status": "success",
            "evidence_count": len(all_evidence),
            "reason": "",
        }
