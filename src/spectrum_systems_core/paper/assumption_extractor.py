"""AssumptionExtractor: text_units.jsonl -> assumptions.jsonl via the API.

Same pattern as ClaimExtractor: Haiku at temperature=0, write valid records
to assumptions.jsonl, skip on API error / schema violation. Implicit
assumptions may have source_excerpt=null; explicit assumptions must have a
verbatim source_excerpt (FINDING-D-007 / EVAL-ASSUMP-002).
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

_COMPONENT_NAME = "assumption_extractor"
_COMPONENT_VERSION = "1.1.0"
_SCHEMA_VERSION = "1.1.0"
EXTRACTION_MODEL = "claude-haiku-4-5-20251001"
EXTRACTION_TEMPERATURE = 0
MAX_TOKENS = 2000
MIN_UNIT_CHARS = 50

ASSUMPTION_EXTRACTION_PROMPT = """You are an assumption extractor for working papers.
Extract assumptions — explicit or implied — from the following text.
An assumption is a premise that must be true for the claims to hold.

Source ID: {source_id}
Unit ID: {unit_id}

Text:
{text}

Return ONLY valid JSON. No preamble. No markdown.

{{
  "assumptions": [
    {{
      "assumption_text": "the assumption clearly stated",
      "assumption_type": "methodological|scope|data_quality|policy",
      "risk_if_wrong": "high|medium|low",
      "explicit": true or false,
      "source_excerpt": "verbatim text showing the assumption, min 10 chars or null if implied",
      "source_turn_ids": ["array containing the Unit ID above; required for every assumption"]
    }}
  ]
}}

Rules:
- explicit: true only if the assumption is directly stated in the text.
- explicit: false if you are inferring an implied assumption.
- source_excerpt: null is allowed ONLY for implicit assumptions.
- Do not invent assumptions with no textual basis.

SOURCE CITATION REQUIREMENT (mandatory):

For every item you extract, you MUST include the IDs of the specific
speaker-turn chunks (here, unit_ids) from which you extracted it.

The unit provided to you has a "Unit ID" field above. Use that exact
ID in the source_turn_ids array of every extracted assumption.

Rules:
- If you cannot identify which chunks support an item: DO NOT include
  that item. Omit it entirely from the assumptions array.
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


def _execution_fingerprint(unit_id: str, assumption_text: str) -> str:
    seed = (
        f"{unit_id}|{assumption_text}|{_COMPONENT_NAME}:{_COMPONENT_VERSION}"
    )
    return "sha256:" + _sha256_hex(seed.encode("utf-8"))


def _failure(reason: str) -> dict[str, Any]:
    return {"status": "failure", "assumptions": [], "reason": reason}


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


class AssumptionExtractor:
    """Extract assumption_record artifacts from text units via the API."""

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
                paper_schema_path("assumption_record").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError) as exc:
            return _failure(f"schema_unreadable: {exc}")
        validator = jsonschema.Draft202012Validator(schema)

        all_assumptions: list[dict[str, Any]] = []
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

            prompt = ASSUMPTION_EXTRACTION_PROMPT.format(
                source_id=source_id,
                unit_id=unit_id,
                text=text,
            )
            try:
                response_text = self._api_caller(prompt)
            except Exception:
                continue

            try:
                parsed = json.loads(response_text)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(parsed, dict):
                continue

            raw_items = parsed.get("assumptions") or []
            if not isinstance(raw_items, list):
                continue

            for raw in raw_items:
                if not isinstance(raw, dict):
                    continue

                raw_turn_ids = raw.get("source_turn_ids")
                if not isinstance(raw_turn_ids, list) or not raw_turn_ids:
                    _LOG.warning(
                        "extraction_missing_source_turns: assumption_record "
                        "omitted (unit_id=%s)",
                        unit_id,
                    )
                    continue
                turn_ids = [
                    str(t) for t in raw_turn_ids if isinstance(t, str)
                ]
                if not turn_ids:
                    _LOG.warning(
                        "extraction_missing_source_turns: assumption_record "
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

                record = self._assemble_assumption(
                    raw,
                    source_id=source_id,
                    unit_id=unit_id,
                    source_turn_ids=turn_ids,
                    source_turn_validation=source_turn_validation,
                )
                try:
                    validator.validate(record)
                except jsonschema.ValidationError:
                    continue
                all_assumptions.append(record)

        paper_dir = processed_dir / "paper"
        paper_dir.mkdir(parents=True, exist_ok=True)
        out_path = paper_dir / "assumptions.jsonl"
        try:
            with out_path.open("w", encoding="utf-8") as fh:
                for record in all_assumptions:
                    fh.write(
                        json.dumps(record, sort_keys=True, separators=(",", ":"))
                        + "\n"
                    )
        except OSError as exc:
            return _failure(f"write_error: {exc}")

        return {
            "status": "success",
            "assumptions": all_assumptions,
            "reason": "",
        }

    def _assemble_assumption(
        self,
        raw: dict[str, Any],
        *,
        source_id: str,
        unit_id: str,
        source_turn_ids: list[str],
        source_turn_validation: str,
    ) -> dict[str, Any]:
        explicit = bool(raw.get("explicit", False))
        excerpt_raw = raw.get("source_excerpt")
        # null is allowed only for implicit assumptions; the schema and the
        # consistency eval enforce this.
        if isinstance(excerpt_raw, str):
            source_excerpt: Any = excerpt_raw
        else:
            source_excerpt = None
        assumption_text = str(raw.get("assumption_text") or "")
        return {
            "schema_version": _SCHEMA_VERSION,
            "assumption_id": str(uuid.uuid4()),
            "source_id": source_id,
            "source_unit_id": unit_id,
            "source_turn_ids": list(source_turn_ids),
            "source_turn_validation": source_turn_validation,
            "source_excerpt": source_excerpt,
            "assumption_text": assumption_text,
            "assumption_type": str(raw.get("assumption_type") or "scope"),
            "risk_if_wrong": str(raw.get("risk_if_wrong") or "low"),
            "explicit": explicit,
            "status": "candidate",
            "created_at": _now_iso(),
            "provenance": {
                "produced_by": {
                    "component": _COMPONENT_NAME,
                    "version": _COMPONENT_VERSION,
                },
                "input_artifact_ids": [unit_id],
                "execution_fingerprint_hash": _execution_fingerprint(
                    unit_id, assumption_text
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
            parts: list[str] = []
            for block in message.content:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    parts.append(text)
            return "\n".join(parts)

        return _call
