"""Locate and load Phase V schemas from contracts/schemas/verification/."""
from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, Optional

import jsonschema


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCHEMA_DIR = _REPO_ROOT / "contracts" / "schemas" / "verification"

_SCHEMA_CACHE: Dict[str, Dict[str, Any]] = {}


def load_schema(name: str) -> Dict[str, Any]:
    """Return the parsed JSON schema for ``name`` (e.g. ``source_verification_result``).

    Cached per process.
    """
    if name in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[name]
    path = _SCHEMA_DIR / f"{name}.schema.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    _SCHEMA_CACHE[name] = data
    return data


class SchemaValidationError(ValueError):
    """Raised when an artifact fails Phase V schema validation."""


def validate_source_verification_result(artifact: Dict[str, Any]) -> None:
    """Raise ``SchemaValidationError`` if ``artifact`` violates the schema.

    Uses the bundled JSON Schema. The conditional rule (verified ->
    supporting_text_excerpts.minItems == 1) is in the schema, but we also
    enforce it programmatically below as defense-in-depth so a partial
    JSON Schema implementation can't quietly drop the rule.
    """
    schema = load_schema("source_verification_result")
    try:
        # format_checker enforces format: uuid / date-time so a bad id
        # cannot silently sneak through (RT1 Sev-2 fix).
        jsonschema.validate(
            instance=artifact,
            schema=schema,
            format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
        )
    except jsonschema.ValidationError as exc:
        raise SchemaValidationError(
            f"source_verification_result schema violation: {exc.message}"
        ) from exc

    for v in artifact.get("item_verifications", []) or []:
        if v.get("verification_status") == "verified":
            if not v.get("supporting_text_excerpts"):
                raise SchemaValidationError(
                    "verified status requires at least one supporting_text_excerpt; "
                    f"item_id={v.get('item_id')}"
                )


def validate_meeting_extraction_v2(artifact: Dict[str, Any]) -> None:
    """Verify schema_version==2.0.0 meeting_extraction has verification_status
    populated on every decision/claim/action_item.

    v1.x artifacts are NOT validated here -- they remain valid under the
    existing meeting_extraction.schema.json.
    """
    version = artifact.get("schema_version")
    if version != "2.0.0":
        return
    allowed = {
        "verified", "unsupported", "contradicted",
        "insufficient_evidence", "verification_failed",
    }
    for key in ("decisions", "claims", "action_items"):
        for item in artifact.get(key, []) or []:
            status = item.get("verification_status")
            if status is None:
                raise SchemaValidationError(
                    f"meeting_extraction v2.0.0 requires verification_status "
                    f"on every {key[:-1]}; missing on item "
                    f"{item.get('id') or item.get('decision_text') or item.get('claim_text') or item.get('action')!r}"
                )
            if status not in allowed:
                raise SchemaValidationError(
                    f"meeting_extraction v2.0.0 invalid verification_status "
                    f"{status!r} on {key[:-1]} item"
                )
