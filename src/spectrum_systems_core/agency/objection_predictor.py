"""ObjectionPredictor: predict likely agency objections against a paper.

Uses Haiku at temperature=0. FINDING-E-001: every prediction carries
evidence_basis (list of position_ids); empty evidence_basis forces
confidence=low and sets no_evidence_basis_flag. FINDING-E-007: only
positions with valid_until=null OR valid_until within last
POSITION_RECENCY_YEARS years are used.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jsonschema

from ..extraction._paths import find_processed_dir
from ._paths import agency_schema_path
from .profile_store import AgencyProfileStore

POSITION_RECENCY_YEARS = 3
EXTRACTION_MODEL = "claude-haiku-4-5-20251001"
EXTRACTION_TEMPERATURE = 0
MAX_TOKENS = 500
_COMPONENT_NAME = "objection_predictor"
_COMPONENT_VERSION = "1.0.0"


OBJECTION_PREDICTION_PROMPT = """You are predicting likely objections from
an agency based on their historical positions.

Agency: {agency_name}
Paper being reviewed: {paper_title}
Paper claims (top 5 by materiality):
{claims_summary}

Agency's active positions (most recent first):
{positions_summary}

Agency's past objection types:
{objection_history_summary}

Predict the most likely objection this agency would raise. Be specific.
Base your prediction ONLY on the positions and history provided above.

Return ONLY valid JSON. No preamble.

{{
  "predicted_objection_text": "specific predicted objection (min 20 chars)",
  "objection_type": "technical_dispute|scope_objection|methodology_concern|evidence_gap|policy_conflict|procedural",
  "confidence": "high|medium|low",
  "rationale": "why you predict this based on the positions above (min 10 chars)",
  "positions_referenced": ["list of topic strings from positions used"]
}}

If there is insufficient history to make a meaningful prediction, return:
{{"insufficient_history": true}}
"""


_VALID_OBJECTION_TYPES = {
    "technical_dispute",
    "scope_objection",
    "methodology_concern",
    "evidence_gap",
    "policy_conflict",
    "procedural",
}


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


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


def _execution_fingerprint(*parts: str) -> str:
    seed = "|".join(parts) + f"|{_COMPONENT_NAME}:{_COMPONENT_VERSION}"
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _truncate(text: str, limit: int) -> str:
    text = (text or "").replace("\n", " ").replace("\r", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


class ObjectionPredictor:
    """Generate objection_prediction artifacts using Haiku."""

    def __init__(self, api_caller: Callable[[str], str] | None = None):
        self._api_caller = api_caller

    def predict_for_paper(
        self,
        paper_source_id: str,
        agency_slug: str,
        repo_root: str,
    ) -> dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        processed_dir, _ = find_processed_dir(repo_root_path, paper_source_id)
        if processed_dir is None:
            return {
                "status": "failure",
                "predictions": [],
                "reason": "paper_source_not_found",
            }
        paper_dir = processed_dir / "paper"
        claims_path = paper_dir / "claims.jsonl"
        all_claims = _read_jsonl(claims_path)
        if not all_claims:
            return {
                "status": "failure",
                "predictions": [],
                "reason": "no_claims",
            }

        store = AgencyProfileStore()
        try:
            profile = store.load(agency_slug, str(repo_root_path))
        except FileNotFoundError:
            return {
                "status": "failure",
                "predictions": [],
                "reason": "profile_not_found",
            }
        active_positions = store.get_active_positions(
            agency_slug,
            str(repo_root_path),
            recency_years=POSITION_RECENCY_YEARS,
        )
        if not active_positions:
            return {
                "status": "insufficient_history",
                "predictions": [],
                "reason": "no_active_positions",
            }
        history = store.get_objection_history(
            agency_slug, str(repo_root_path), limit=10
        )

        # Top 5 high-materiality claims first, then anything else if fewer.
        materiality_rank = {"high": 0, "medium": 1, "low": 2}

        def _rank(c: dict[str, Any]) -> int:
            return materiality_rank.get(str(c.get("materiality") or "low"), 99)

        sorted_claims = sorted(all_claims, key=_rank)[:5]

        claims_summary = "\n".join(
            f"- {c.get('claim_type', '?')}: {_truncate(c.get('claim_text', ''), 100)}"
            for c in sorted_claims
        ) or "(no claims summarized)"
        positions_summary = "\n".join(
            f"- {p.get('topic', '?')}: {p.get('position_type', '?')} — "
            f"{_truncate(p.get('position_statement', ''), 100)}"
            for p in active_positions
        ) or "(no active positions)"
        history_summary = "\n".join(
            f"- {h.get('objection_type', '?')}: "
            f"{_truncate(h.get('objection_text', ''), 80)}"
            for h in history
        ) or "(no past objections)"
        paper_title = profile.get("agency_name", "")  # placeholder if no title
        # Try the source_record for a real title.
        source_record_path = processed_dir / "source_record.json"
        if source_record_path.is_file():
            try:
                source_record = json.loads(
                    source_record_path.read_text(encoding="utf-8")
                )
                paper_title = (
                    source_record.get("payload", {}).get("title")
                    or paper_source_id
                )
            except (OSError, json.JSONDecodeError):
                paper_title = paper_source_id

        if self._api_caller is None:
            if os.environ.get("ANTHROPIC_API_KEY"):
                try:
                    self._api_caller = self._build_default_api_caller()
                except ImportError as exc:
                    return {
                        "status": "failure",
                        "predictions": [],
                        "reason": f"anthropic_sdk_missing: {exc}",
                    }
            else:
                return {
                    "status": "failure",
                    "predictions": [],
                    "reason": "api_key_missing",
                }

        prompt = OBJECTION_PREDICTION_PROMPT.format(
            agency_name=profile.get("agency_name", agency_slug),
            paper_title=paper_title,
            claims_summary=claims_summary,
            positions_summary=positions_summary,
            objection_history_summary=history_summary,
        )
        try:
            response = self._api_caller(prompt)
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "failure",
                "predictions": [],
                "reason": f"api_error: {type(exc).__name__}: {exc}",
            }

        try:
            parsed = json.loads(response)
        except (TypeError, json.JSONDecodeError) as exc:
            return {
                "status": "failure",
                "predictions": [],
                "reason": f"json_parse_error: {exc}",
            }
        if not isinstance(parsed, dict):
            return {
                "status": "failure",
                "predictions": [],
                "reason": "json_parse_error: not_object",
            }
        if parsed.get("insufficient_history") is True:
            return {
                "status": "insufficient_history",
                "predictions": [],
                "reason": "model_reported_insufficient_history",
            }

        # Build evidence_basis from referenced topics -> position_ids.
        referenced_topics_raw = parsed.get("positions_referenced") or []
        if not isinstance(referenced_topics_raw, list):
            referenced_topics_raw = []
        referenced_topics = {
            str(t).strip().lower()
            for t in referenced_topics_raw
            if isinstance(t, (str, int, float))
        }
        evidence_basis: list[str] = []
        for pos in active_positions:
            topic = str(pos.get("topic") or "").strip().lower()
            if topic and topic in referenced_topics:
                pid = pos.get("position_id")
                if isinstance(pid, str) and pid not in evidence_basis:
                    evidence_basis.append(pid)

        confidence = str(parsed.get("confidence") or "low").strip().lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"
        no_evidence_basis_flag = not evidence_basis
        if no_evidence_basis_flag:
            confidence = "low"  # FINDING-E-001

        objection_type = str(parsed.get("objection_type") or "").strip().lower()
        if objection_type not in _VALID_OBJECTION_TYPES:
            objection_type = "methodology_concern"

        predicted_text = str(parsed.get("predicted_objection_text") or "").strip()
        if len(predicted_text) < 20:
            return {
                "status": "failure",
                "predictions": [],
                "reason": "predicted_objection_text_too_short",
            }
        rationale = str(parsed.get("rationale") or "").strip()
        if len(rationale) < 10:
            return {
                "status": "failure",
                "predictions": [],
                "reason": "rationale_too_short",
            }

        prediction_id = str(uuid.uuid4())
        prediction = {
            "prediction_id": prediction_id,
            "agency_slug": agency_slug,
            "paper_source_id": paper_source_id,
            "predicted_objection_text": predicted_text,
            "objection_type": objection_type,
            "confidence": confidence,
            "evidence_basis": evidence_basis,
            "no_evidence_basis_flag": no_evidence_basis_flag,
            "rationale": rationale,
            "positions_referenced": [str(p.get("position_id") or "") for p in active_positions
                                     if str(p.get("topic") or "").strip().lower()
                                     in referenced_topics
                                     and isinstance(p.get("position_id"), str)],
            "recency_cutoff_applied": True,  # FINDING-E-007
            "extraction_model": EXTRACTION_MODEL,
            "extraction_temperature": EXTRACTION_TEMPERATURE,
            "status": "candidate",
            "created_at": _now_iso(),
            "provenance": {
                "produced_by": {
                    "component": _COMPONENT_NAME,
                    "version": _COMPONENT_VERSION,
                },
                "input_artifact_ids": [agency_slug, paper_source_id]
                + [p.get("position_id") for p in active_positions if isinstance(p.get("position_id"), str)],
                "execution_fingerprint_hash": _execution_fingerprint(
                    agency_slug, paper_source_id, predicted_text
                ),
            },
        }

        # Validate against schema.
        try:
            schema = json.loads(
                agency_schema_path("objection_prediction").read_text(encoding="utf-8")
            )
            jsonschema.Draft202012Validator(schema).validate(prediction)
        except (FileNotFoundError, OSError) as exc:
            return {
                "status": "failure",
                "predictions": [],
                "reason": f"schema_unreadable: {exc}",
            }
        except jsonschema.ValidationError as exc:
            return {
                "status": "failure",
                "predictions": [],
                "reason": f"schema_violation: {exc.message}",
            }

        # Write predictions.jsonl (overwrite, not append — CHECK-RT3-006).
        objections_dir = paper_dir / "objections"
        objections_dir.mkdir(parents=True, exist_ok=True)
        predictions_path = objections_dir / "predictions.jsonl"
        with predictions_path.open("w", encoding="utf-8") as fh:
            fh.write(
                json.dumps(prediction, sort_keys=True, separators=(",", ":")) + "\n"
            )

        # Projection.
        try:
            from ..ingestion.obsidian_projection import ObsidianProjection

            ObsidianProjection().write_objections_projection(
                paper_source_id, [prediction], str(repo_root_path)
            )
        except (FileNotFoundError, OSError):
            pass

        return {
            "status": "success",
            "predictions": [prediction],
            "reason": "",
        }

    def _build_default_api_caller(self) -> Callable[[str], str]:
        import anthropic

        client = anthropic.Anthropic()

        def _call(prompt: str) -> str:
            message = client.messages.create(
                model=EXTRACTION_MODEL,
                max_tokens=MAX_TOKENS,
                temperature=EXTRACTION_TEMPERATURE,
                messages=[{"role": "user", "content": prompt}],
            )
            parts: list[str] = []
            for block in message.content:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    parts.append(text)
            return "\n".join(parts)

        return _call
