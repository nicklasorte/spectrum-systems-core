"""Write-time artifact schema validation gate.

Phase X-2. Replaces the previous ad-hoc per-module validation pattern
with a single ``validate_artifact(artifact, artifact_type)`` entry
point. Every artifact emitted from the X-0 / X-1 / X-2 / X-3 code
paths is validated before it can land on disk; a schema violation
raises ``ArtifactValidationError`` so a malformed artifact never
silently ships.

Schemas live in ``spectrum_systems_core/schemas/``. The directory is
the source of truth; CI meta-validates each file against the JSON
Schema 2020-12 vocabulary so a broken schema cannot be merged.

Bypass mechanism (X-2 rollback path): setting
``SCHEMA_VALIDATION_ENABLED=false`` skips validation but logs a
warning -- the operator wanted us to ship a malformed artifact, and
the log line proves they made that choice.
"""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import Any, Dict

import jsonschema

from .schemas import schema_path


_LOG = logging.getLogger(__name__)


SCHEMA_VALIDATION_ENV_VAR: str = "SCHEMA_VALIDATION_ENABLED"
_DISABLED_VALUES = {"false", "0", "no", "off"}


class ArtifactValidationError(ValueError):
    """Raised by ``validate_artifact`` on any schema violation.

    Subclass of ValueError so that callers that catch the broader type
    (e.g. legacy paths that wrapped jsonschema.ValidationError) still
    see the failure instead of swallowing it.
    """


class SchemaNotFoundError(LookupError):
    """Raised when no schema file exists for the requested artifact_type."""


@lru_cache(maxsize=128)
def _load_schema(artifact_type: str) -> Dict[str, Any]:
    """Load and cache the JSON Schema for ``artifact_type``.

    ``functools.lru_cache`` is fine here because the schema files are
    static once the process starts. Tests that need to swap schemas
    should call ``_load_schema.cache_clear()``.
    """
    path = schema_path(artifact_type)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SchemaNotFoundError(
            f"no schema for artifact_type={artifact_type!r} at {path}"
        ) from exc
    return json.loads(text)


def _validation_enabled() -> bool:
    raw = os.environ.get(SCHEMA_VALIDATION_ENV_VAR, "")
    if raw.strip().lower() in _DISABLED_VALUES:
        return False
    return True


def validate_artifact(artifact: Dict[str, Any], artifact_type: str) -> None:
    """Validate ``artifact`` against ``schemas/<artifact_type>.schema.json``.

    Raises:
      ArtifactValidationError: artifact does not match the schema.
      SchemaNotFoundError: no schema file for ``artifact_type``.

    When ``SCHEMA_VALIDATION_ENABLED=false`` is set in the environment,
    this function logs a warning (one per process, since lru_cache on
    the schema loader hides the per-call cost) and returns without
    validating. The warning is required so a deliberate bypass shows
    up in CI logs and operator dashboards.
    """
    if not _validation_enabled():
        _LOG.warning(
            "schema_validation_disabled: %s=false -- skipping artifact_type=%s "
            "validation. This is a deliberate bypass; restore the env var to "
            "re-enable the X-2 write-time gate.",
            SCHEMA_VALIDATION_ENV_VAR, artifact_type,
        )
        return

    if not isinstance(artifact, dict):
        raise ArtifactValidationError(
            f"artifact must be a dict, got {type(artifact).__name__}"
        )
    if not isinstance(artifact_type, str) or not artifact_type:
        raise ArtifactValidationError(
            f"artifact_type must be a non-empty string, got {artifact_type!r}"
        )

    schema = _load_schema(artifact_type)
    validator = jsonschema.Draft202012Validator(schema)
    try:
        validator.validate(artifact)
    except jsonschema.ValidationError as exc:
        # Surface the first failing path for the operator. The full
        # error tree is on the exception if we ever need it.
        raise ArtifactValidationError(
            f"artifact_type={artifact_type} failed schema: "
            f"{exc.message} at path={list(exc.absolute_path)}"
        ) from exc
