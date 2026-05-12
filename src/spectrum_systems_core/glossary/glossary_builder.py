"""Phase V.1: build + load the versioned spectrum glossary artifact.

Single source of truth for prompt-injection terminology:
``store/artifacts/glossary/spectrum_glossary_v1.json``.

The artifact differs from the per-term ``glossary_term`` files used by
the legacy ``extraction.glossary_manager.GlossaryManager``: it is a
single aggregate, schema-validated, content-hashed, version-bumped
document with explicit ``short_definition`` (<= 200 chars) so prompt
injection cannot blow the context budget. The legacy per-term files
are still produced by ``scripts/seed_glossary.py``; the versioned
artifact is derived from the same source list.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional


GLOSSARY_SCHEMA_VERSION: str = "1.0.0"
GLOSSARY_ARTIFACT_TYPE: str = "spectrum_glossary"
GLOSSARY_FILENAME: str = "spectrum_glossary_v1.json"
RETIREMENT_FILENAME: str = "working_paper.retired.json"

# Required on every term entry. ``abbreviation`` is permitted to be
# None but must be present.
REQUIRED_TERM_FIELDS: tuple[str, ...] = (
    "term_id",
    "term",
    "abbreviation",
    "definition",
    "short_definition",
    "authoritative_source",
    "domain_scope",
    "related_term_ids",
)

_LOG = logging.getLogger(__name__)


def compute_glossary_content_hash(
    glossary_version: str, terms: List[Dict[str, Any]]
) -> str:
    """Compute the deterministic SHA-256 content hash for the glossary.

    ``sort_keys=True`` and the compact separators ensure the hash is
    insensitive to whitespace and key order. Term order is preserved
    deliberately -- ``terms`` is a list, so reordering is itself a
    content change.
    """
    payload = {"glossary_version": glossary_version, "terms": terms}
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def load_versioned_glossary(
    glossary_root: Path | str,
) -> Optional[Dict[str, Any]]:
    """Load ``spectrum_glossary_v1.json`` from ``glossary_root``.

    Returns the parsed artifact dict on success.
    Returns None if the file does not exist or fails to parse -- this
    is intentional: per-chunk terminology injection is a quality
    enhancement, not a correctness gate. The caller is expected to
    record an info finding when the result is None.
    """
    root = Path(glossary_root)
    path = root / GLOSSARY_FILENAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _LOG.warning("versioned_glossary_load_failed: %s", exc)
        return None


def validate_term(term: Dict[str, Any]) -> List[str]:
    """Return a list of validation error strings for ``term``.

    Empty list means the term is well-formed. The caller decides what
    to do with non-empty results (raise, log, emit finding).
    """
    errors: List[str] = []
    for field in REQUIRED_TERM_FIELDS:
        if field not in term:
            errors.append(f"missing_field:{field}")
    if "term" in term and not isinstance(term["term"], str):
        errors.append("term_not_string")
    if "term" in term and isinstance(term["term"], str) and not term["term"].strip():
        errors.append("term_empty")
    if "short_definition" in term:
        sd = term["short_definition"]
        if not isinstance(sd, str):
            errors.append("short_definition_not_string")
        elif len(sd) > 200:
            errors.append("short_definition_too_long")
    if "abbreviation" in term:
        abbrev = term["abbreviation"]
        if abbrev is not None and not isinstance(abbrev, str):
            errors.append("abbreviation_not_string_or_null")
    if "related_term_ids" in term:
        if not isinstance(term["related_term_ids"], list):
            errors.append("related_term_ids_not_list")
    return errors


__all__ = [
    "GLOSSARY_ARTIFACT_TYPE",
    "GLOSSARY_FILENAME",
    "GLOSSARY_SCHEMA_VERSION",
    "REQUIRED_TERM_FIELDS",
    "RETIREMENT_FILENAME",
    "compute_glossary_content_hash",
    "load_versioned_glossary",
    "validate_term",
]
