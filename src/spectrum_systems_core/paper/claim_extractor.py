"""ClaimExtractor: text_units.jsonl -> claims.jsonl via the Anthropic API.

Model: claude-haiku-4-5-20251001 at temperature=0 (FINDING-D-007).
Each text unit produces zero-or-more technical_claim artifacts. Failures
(API exceptions, JSON parse errors, schema violations) are logged and the
unit is skipped so a single bad unit does not halt the pipeline (matches
StoryExtractor pattern from Phase C).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import jsonschema

from ..extraction._paths import find_processed_dir
from ._paths import paper_schema_path

_COMPONENT_NAME = "claim_extractor"
_COMPONENT_VERSION = "1.0.0"
EXTRACTION_MODEL = "claude-haiku-4-5-20251001"
EXTRACTION_TEMPERATURE = 0
MAX_TOKENS = 2000
MIN_UNIT_CHARS = 50

CLAIM_EXTRACTION_PROMPT = """You are a technical claim extractor for working papers.
Extract ALL technical claims from the following text unit.
A technical claim is a specific, verifiable assertion about facts, methods, predictions, or norms.

Source ID: {source_id}
Unit ID: {unit_id}
Unit type: {unit_type}

Text:
{text}

Return ONLY valid JSON. No preamble. No markdown.

{{
  "claims": [
    {{
      "claim_text": "exact claim as stated or closely paraphrased",
      "claim_type": "factual|methodological|predictive|normative",
      "materiality": "high|medium|low",
      "source_excerpt": "verbatim supporting text from above, min 10 chars"
    }}
  ]
}}

Rules:
- source_excerpt must be VERBATIM from the text above.
- Return empty claims array if no technical claims found.
- materiality high = load-bearing for paper conclusions.
- Do not invent claims not present in the text.
"""


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _execution_fingerprint(unit_id: str, source_excerpt: str) -> str:
    seed = f"{unit_id}|{source_excerpt}|{_COMPONENT_NAME}:{_COMPONENT_VERSION}"
    return "sha256:" + _sha256_hex(seed.encode("utf-8"))


def _failure(reason: str) -> Dict[str, Any]:
    return {"status": "failure", "claims": [], "reason": reason}


def _read_text_units(path: Path) -> List[Dict[str, Any]]:
    units: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                units.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return units


class ClaimExtractor:
    """Extract technical_claim artifacts from text units via the Anthropic API."""

    def __init__(self, api_caller: Optional[Callable[[str], str]] = None):
        self._api_caller = api_caller

    def extract_from_source(
        self, source_id: str, repo_root: str
    ) -> Dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        processed_dir, _ = find_processed_dir(repo_root_path, source_id)
        if processed_dir is None:
            return _failure("text_units_not_found")
        text_units_path = processed_dir / "text_units.jsonl"
        if not text_units_path.is_file():
            return _failure("text_units_not_found")

        try:
            text_units = _read_text_units(text_units_path)
        except OSError as exc:
            return _failure(f"text_units_unreadable: {exc}")

        if self._api_caller is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                return _failure("api_key_missing")
            try:
                self._api_caller = self._build_default_api_caller()
            except ImportError as exc:
                return _failure(f"anthropic_sdk_missing: {exc}")

        try:
            schema = json.loads(
                paper_schema_path("technical_claim").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError) as exc:
            return _failure(f"schema_unreadable: {exc}")
        validator = jsonschema.Draft202012Validator(schema)

        all_claims: List[Dict[str, Any]] = []

        for unit in text_units:
            text = unit.get("text", "") or ""
            if len(text) < MIN_UNIT_CHARS:
                continue
            unit_id = unit.get("unit_id")
            if not isinstance(unit_id, str):
                continue

            prompt = CLAIM_EXTRACTION_PROMPT.format(
                source_id=source_id,
                unit_id=unit_id,
                unit_type=unit.get("unit_type", ""),
                text=text,
            )
            try:
                response_text = self._api_caller(prompt)
            except Exception:
                # API error: skip this unit, do not crash pipeline.
                continue

            try:
                parsed = json.loads(response_text)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(parsed, dict):
                continue

            raw_claims = parsed.get("claims") or []
            if not isinstance(raw_claims, list):
                continue

            for raw in raw_claims:
                if not isinstance(raw, dict):
                    continue
                claim = self._assemble_claim(raw, source_id=source_id, unit_id=unit_id)
                try:
                    validator.validate(claim)
                except jsonschema.ValidationError:
                    continue
                all_claims.append(claim)

        paper_dir = processed_dir / "paper"
        paper_dir.mkdir(parents=True, exist_ok=True)
        out_path = paper_dir / "claims.jsonl"
        try:
            with out_path.open("w", encoding="utf-8") as fh:
                for claim in all_claims:
                    fh.write(
                        json.dumps(claim, sort_keys=True, separators=(",", ":"))
                        + "\n"
                    )
        except OSError as exc:
            return _failure(f"write_error: {exc}")

        # Write the view-only projection.
        from ..ingestion.obsidian_projection import ObsidianProjection
        ObsidianProjection().write_paper_claims_projection(
            source_id, all_claims, str(repo_root_path)
        )

        return {"status": "success", "claims": all_claims, "reason": ""}

    def _assemble_claim(
        self,
        raw: Dict[str, Any],
        *,
        source_id: str,
        unit_id: str,
    ) -> Dict[str, Any]:
        source_excerpt = str(raw.get("source_excerpt") or "")
        return {
            "claim_id": str(uuid.uuid4()),
            "source_id": source_id,
            "source_unit_id": unit_id,
            "source_excerpt": source_excerpt,
            "claim_text": str(raw.get("claim_text") or ""),
            "claim_type": str(raw.get("claim_type") or "factual"),
            "materiality": str(raw.get("materiality") or "low"),
            "supported_by_evidence_ids": [],
            "contradicted_by_claim_ids": [],
            "extraction_model": EXTRACTION_MODEL,
            "extraction_temperature": EXTRACTION_TEMPERATURE,
            "status": "candidate",
            "created_at": _now_iso(),
            "provenance": {
                "produced_by": {
                    "component": _COMPONENT_NAME,
                    "version": _COMPONENT_VERSION,
                },
                "input_artifact_ids": [unit_id],
                "execution_fingerprint_hash": _execution_fingerprint(
                    unit_id, source_excerpt
                ),
            },
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
            parts: List[str] = []
            for block in message.content:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    parts.append(text)
            return "\n".join(parts)

        return _call
