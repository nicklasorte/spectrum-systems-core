"""JSON Schema registry for Phase X write-time validation.

The directory is the source of truth for artifact shape during the
extraction loop. Each ``.schema.json`` file is loaded at runtime by
``spectrum_systems_core.validation.validate_artifact`` and meta-validated
by ``tests/ci/test_phase_x_schemas.py`` so a malformed schema cannot ship.

Schemas are intentionally narrower than the legacy contracts/schemas
tree: write-time validation fails closed on any unknown key
(``additionalProperties: false``) and requires both ``artifact_type``
and ``schema_version`` on every artifact so we can route a future
artifact to the correct validator without sniffing payload fields.
"""
from __future__ import annotations

from pathlib import Path

SCHEMAS_DIR: Path = Path(__file__).resolve().parent


def schema_path(artifact_type: str) -> Path:
    """Return the on-disk path for ``<artifact_type>.schema.json``."""
    return SCHEMAS_DIR / f"{artifact_type}.schema.json"
