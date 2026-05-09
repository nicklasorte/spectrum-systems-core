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
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import jsonschema

from ..extraction._paths import find_processed_dir
from ._paths import paper_schema_path

_COMPONENT_NAME = "assumption_extractor"
_COMPONENT_VERSION = "1.0.0"
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
      "source_excerpt": "verbatim text showing the assumption, min 10 chars or null if implied"
    }}
  ]
}}

Rules:
- explicit: true only if the assumption is directly stated in the text.
- explicit: false if you are inferring an implied assumption.
- source_excerpt: null is allowed ONLY for implicit assumptions.
- Do not invent assumptions with no textual basis.
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


def _failure(reason: str) -> Dict[str, Any]:
    return {"status": "failure", "assumptions": [], "reason": reason}


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


class AssumptionExtractor:
    """Extract assumption_record artifacts from text units via the API."""

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
                paper_schema_path("assumption_record").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError) as exc:
            return _failure(f"schema_unreadable: {exc}")
        validator = jsonschema.Draft202012Validator(schema)

        all_assumptions: List[Dict[str, Any]] = []

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
                record = self._assemble_assumption(
                    raw, source_id=source_id, unit_id=unit_id
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
        raw: Dict[str, Any],
        *,
        source_id: str,
        unit_id: str,
    ) -> Dict[str, Any]:
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
            "assumption_id": str(uuid.uuid4()),
            "source_id": source_id,
            "source_unit_id": unit_id,
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
            parts: List[str] = []
            for block in message.content:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    parts.append(text)
            return "\n".join(parts)

        return _call
