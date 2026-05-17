"""MitigationSuggester: one suggestion per candidate prediction (Haiku, T=0).

FINDING-E-006: add_evidence mitigations require non-empty evidence_search_terms.
Empty search terms cause the suggestion to be skipped (not written to disk),
with reason "add_evidence_requires_search_terms".
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

EXTRACTION_MODEL = "claude-haiku-4-5-20251001"
EXTRACTION_TEMPERATURE = 0
MAX_TOKENS = 400
_COMPONENT_NAME = "mitigation_suggester"
_COMPONENT_VERSION = "1.0.0"


_VALID_MITIGATION_TYPES = {
    "add_evidence",
    "revise_claim",
    "add_caveat",
    "restructure_argument",
    "add_sensitivity_analysis",
    "cite_precedent",
}


MITIGATION_PROMPT = """You are suggesting how to address a predicted agency objection.

Predicted objection: {objection_text}
Objection type: {objection_type}
Agency: {agency_slug}

Available mitigation types:
- add_evidence: add supporting evidence to the paper
- revise_claim: reword a specific claim
- add_caveat: add appropriate qualifications
- restructure_argument: reorganize the paper section
- add_sensitivity_analysis: add analysis of alternative scenarios
- cite_precedent: reference prior accepted precedents

Suggest ONE specific mitigation. Return ONLY valid JSON. No preamble.

{{
  "mitigation_text": "specific action to take (min 20 chars)",
  "mitigation_type": "one of the types above",
  "evidence_search_terms": ["list of terms to search for supporting evidence",
                            "required non-empty if type is add_evidence"],
  "expected_effectiveness": "high|medium|low",
  "rationale": "why this mitigation addresses the objection (min 10 chars)"
}}
"""


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


class MitigationSuggester:
    """One suggestion per candidate prediction. Writes mitigations.jsonl."""

    def __init__(self, api_caller: Callable[[str], str] | None = None):
        self._api_caller = api_caller

    def suggest_for_predictions(
        self,
        paper_source_id: str,
        repo_root: str,
    ) -> dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        processed_dir, _ = find_processed_dir(repo_root_path, paper_source_id)
        if processed_dir is None:
            return {
                "status": "failure",
                "mitigations": 0,
                "blocked": 0,
                "reason": "paper_source_not_found",
                "blocked_reasons": [],
            }
        objections_dir = processed_dir / "paper" / "objections"
        predictions_path = objections_dir / "predictions.jsonl"
        predictions = _read_jsonl(predictions_path)
        candidates = [
            p for p in predictions if p.get("status") == "candidate"
        ]
        if not candidates:
            return {
                "status": "success",
                "mitigations": 0,
                "blocked": 0,
                "reason": "no_candidate_predictions",
                "blocked_reasons": [],
            }

        if self._api_caller is None:
            if os.environ.get("ANTHROPIC_API_KEY"):
                try:
                    self._api_caller = self._build_default_api_caller()
                except ImportError as exc:
                    return {
                        "status": "failure",
                        "mitigations": 0,
                        "blocked": 0,
                        "reason": f"anthropic_sdk_missing: {exc}",
                        "blocked_reasons": [],
                    }
            else:
                return {
                    "status": "failure",
                    "mitigations": 0,
                    "blocked": 0,
                    "reason": "api_key_missing",
                    "blocked_reasons": [],
                }

        # Skip predictions for which we already have a mitigation (CHECK-RT4-004).
        mitigations_path = objections_dir / "mitigations.jsonl"
        existing_mitigations = _read_jsonl(mitigations_path)
        already_addressed = {
            m.get("prediction_id") for m in existing_mitigations
        }

        try:
            schema = json.loads(
                agency_schema_path("mitigation_suggestion").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError) as exc:
            return {
                "status": "failure",
                "mitigations": 0,
                "blocked": 0,
                "reason": f"schema_unreadable: {exc}",
                "blocked_reasons": [],
            }
        validator = jsonschema.Draft202012Validator(schema)

        mitigations_written = 0
        blocked_count = 0
        warnings = 0
        new_mitigations: list[dict[str, Any]] = []
        blocked_reasons: list[str] = []

        for prediction in candidates:
            prediction_id = prediction.get("prediction_id")
            if not isinstance(prediction_id, str):
                continue
            if prediction_id in already_addressed:
                continue

            try:
                response = self._api_caller(
                    MITIGATION_PROMPT.format(
                        objection_text=prediction.get("predicted_objection_text", ""),
                        objection_type=prediction.get("objection_type", ""),
                        agency_slug=prediction.get("agency_slug", ""),
                    )
                )
            except Exception:  # noqa: BLE001
                warnings += 1
                continue
            if not response:
                warnings += 1
                continue
            try:
                parsed = json.loads(response)
            except (TypeError, json.JSONDecodeError):
                warnings += 1
                continue
            if not isinstance(parsed, dict):
                warnings += 1
                continue

            mitigation_type = str(parsed.get("mitigation_type") or "").strip().lower()
            if mitigation_type not in _VALID_MITIGATION_TYPES:
                warnings += 1
                continue

            search_terms = parsed.get("evidence_search_terms") or []
            if not isinstance(search_terms, list):
                search_terms = []
            search_terms = [
                str(t).strip() for t in search_terms if str(t).strip()
            ]
            if mitigation_type == "add_evidence" and not search_terms:
                blocked_count += 1
                blocked_reasons.append(
                    f"{prediction_id}: add_evidence_requires_search_terms"
                )
                continue

            mitigation_text = str(parsed.get("mitigation_text") or "").strip()
            if len(mitigation_text) < 20:
                warnings += 1
                continue
            rationale = str(parsed.get("rationale") or "").strip()
            if len(rationale) < 10:
                warnings += 1
                continue
            effectiveness = (
                str(parsed.get("expected_effectiveness") or "").strip().lower()
            )
            if effectiveness not in {"high", "medium", "low"}:
                effectiveness = "medium"

            mitigation_id = str(uuid.uuid4())
            mitigation = {
                "mitigation_id": mitigation_id,
                "prediction_id": prediction_id,
                "agency_slug": prediction.get("agency_slug", ""),
                "mitigation_text": mitigation_text,
                "mitigation_type": mitigation_type,
                "evidence_search_terms": search_terms,
                "expected_effectiveness": effectiveness,
                "rationale": rationale,
                "extraction_model": EXTRACTION_MODEL,
                "extraction_temperature": EXTRACTION_TEMPERATURE,
                "status": "pending",
                "created_at": _now_iso(),
                "provenance": {
                    "produced_by": {
                        "component": _COMPONENT_NAME,
                        "version": _COMPONENT_VERSION,
                    },
                    "input_artifact_ids": [prediction_id],
                    "execution_fingerprint_hash": _execution_fingerprint(
                        prediction_id, mitigation_text
                    ),
                },
            }

            try:
                validator.validate(mitigation)
            except jsonschema.ValidationError:
                warnings += 1
                continue

            new_mitigations.append(mitigation)
            mitigations_written += 1

        # Append new mitigations to mitigations.jsonl (idempotent: skipped
        # already-addressed prediction_ids above).
        if new_mitigations:
            objections_dir.mkdir(parents=True, exist_ok=True)
            with mitigations_path.open("a", encoding="utf-8") as fh:
                for mit in new_mitigations:
                    fh.write(
                        json.dumps(mit, sort_keys=True, separators=(",", ":")) + "\n"
                    )

        # Projection (re-render with full set, plus blocked summary).
        try:
            from ..ingestion.obsidian_projection import ObsidianProjection

            full_mitigations = _read_jsonl(mitigations_path)
            ObsidianProjection().write_mitigations_projection(
                paper_source_id,
                full_mitigations,
                blocked_reasons,
                str(repo_root_path),
            )
        except (FileNotFoundError, OSError):
            pass

        return {
            "status": "success",
            "mitigations": mitigations_written,
            "blocked": blocked_count,
            "warnings": warnings,
            "blocked_reasons": blocked_reasons,
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
