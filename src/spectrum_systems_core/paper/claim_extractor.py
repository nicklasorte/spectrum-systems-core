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
import logging
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jsonschema

from ..extraction._paths import find_processed_dir
from ._paths import paper_schema_path

_LOG = logging.getLogger(__name__)

_COMPONENT_NAME = "claim_extractor"
_COMPONENT_VERSION = "1.1.0"
_SCHEMA_VERSION = "1.1.0"
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
      "source_excerpt": "verbatim supporting text from above, min 10 chars",
      "source_turn_ids": ["array containing the Unit ID above; required for every claim"]
    }}
  ]
}}

Rules:
- source_excerpt must be VERBATIM from the text above.
- Return empty claims array if no technical claims found.
- materiality high = load-bearing for paper conclusions.
- Do not invent claims not present in the text.

SOURCE CITATION REQUIREMENT (mandatory):

For every item you extract, you MUST include the IDs of the specific
speaker-turn chunks (here, unit_ids) from which you extracted it.

The unit provided to you has a "Unit ID" field above. Use that exact
ID in the source_turn_ids array of every extracted claim.

Rules:
- If you cannot identify which chunks support an item: DO NOT include
  that item. Omit it entirely from the claims array.
- Never invent or guess chunk IDs.
- A single item may cite multiple chunk_ids if it spans multiple turns.
- source_turn_ids must contain at least one valid chunk_id.
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


def _failure(reason: str) -> dict[str, Any]:
    return {"status": "failure", "claims": [], "reason": reason}


def _read_text_units(path: Path) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
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

    def __init__(self, api_caller: Callable[[str], str] | None = None):
        self._api_caller = api_caller

    def extract_from_source(
        self, source_id: str, repo_root: str
    ) -> dict[str, Any]:
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

        all_claims: list[dict[str, Any]] = []
        valid_unit_ids = {
            u["unit_id"] for u in text_units if isinstance(u.get("unit_id"), str)
        }

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

                raw_turn_ids = raw.get("source_turn_ids")
                if not isinstance(raw_turn_ids, list) or not raw_turn_ids:
                    _LOG.warning(
                        "extraction_missing_source_turns: technical_claim "
                        "omitted (unit_id=%s)",
                        unit_id,
                    )
                    continue
                turn_ids = [
                    str(t) for t in raw_turn_ids if isinstance(t, str)
                ]
                if not turn_ids:
                    _LOG.warning(
                        "extraction_missing_source_turns: technical_claim "
                        "omitted (unit_id=%s)",
                        unit_id,
                    )
                    continue
                invalid_turn_ids = [
                    t for t in turn_ids if t not in valid_unit_ids
                ]
                if invalid_turn_ids:
                    for bad in invalid_turn_ids:
                        _LOG.warning(
                            "extraction_invalid_source_turns: %s not in chunks",
                            bad,
                        )
                    source_turn_validation = "invalid"
                else:
                    source_turn_validation = "verified"

                claim = self._assemble_claim(
                    raw,
                    source_id=source_id,
                    unit_id=unit_id,
                    source_turn_ids=turn_ids,
                    source_turn_validation=source_turn_validation,
                )
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
        raw: dict[str, Any],
        *,
        source_id: str,
        unit_id: str,
        source_turn_ids: list[str],
        source_turn_validation: str,
    ) -> dict[str, Any]:
        source_excerpt = str(raw.get("source_excerpt") or "")
        return {
            "schema_version": _SCHEMA_VERSION,
            "claim_id": str(uuid.uuid4()),
            "source_id": source_id,
            "source_unit_id": unit_id,
            "source_turn_ids": list(source_turn_ids),
            "source_turn_validation": source_turn_validation,
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
            # Stream to stay under the SDK's 10-minute non-streaming cap.
            with client.messages.stream(
                model=EXTRACTION_MODEL,
                max_tokens=MAX_TOKENS,
                temperature=EXTRACTION_TEMPERATURE,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                message = stream.get_final_message()
            parts: list[str] = []
            for block in message.content:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    parts.append(text)
            return "\n".join(parts)

        return _call
