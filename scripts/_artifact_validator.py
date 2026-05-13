"""Validate pipeline artifacts against their JSON schemas before reading fields.

Usage in any script::

    from _artifact_validator import validate_artifact, ArtifactValidationError

    artifact = json.loads(path.read_text())
    try:
        validate_artifact(artifact, "meeting_extraction", str(path))
    except ArtifactValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

If validation fails, ``ArtifactValidationError`` carries the failing
field, the expected artifact_type, and the artifact path on disk so a
new engineer can read the error and immediately know what changed.

A missing schema file is treated as "no contract registered yet" --
the validator logs a warning and returns rather than blocking the
script. The package-level ``spectrum_systems_core.validation`` module
already enforces schema presence at write time; this script-side
helper exists to defend READ paths in CLI scripts that don't import
the full package.
"""
from __future__ import annotations

import json
import pathlib
import sys
from typing import Any, Dict, Optional

import jsonschema

# Resolve schema dirs relative to this file so the helper works
# whether scripts/ is added to sys.path or invoked as a module.
# The primary registry is the X-phase write-time schema dir; the
# contracts/ tree is searched as a fallback because ingestion-side
# artifacts (e.g. ``ground_truth_pair``) only live there.
_REPO_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parent.parent
SCHEMA_DIR: pathlib.Path = _REPO_ROOT / "src" / "spectrum_systems_core" / "schemas"
CONTRACTS_SCHEMA_DIR: pathlib.Path = _REPO_ROOT / "contracts" / "schemas"


class ArtifactValidationError(ValueError):
    """Raised when an artifact does not match its expected schema.

    Subclass of ``ValueError`` so callers that catch ``ValueError`` for
    other CLI input errors still see this. Messages include the
    expected artifact_type, the failing field path, and the artifact
    file path (when known).
    """


def _find_schema(expected_type: str) -> Optional[pathlib.Path]:
    """Search known schema locations for ``<expected_type>.schema.json``.

    Searches the X-phase write-time schema dir first, then walks the
    ``contracts/schemas/`` tree. Returns the first match or None.
    """
    primary = SCHEMA_DIR / f"{expected_type}.schema.json"
    if primary.exists():
        return primary
    if CONTRACTS_SCHEMA_DIR.is_dir():
        # contracts/schemas/ has subdirs per domain (ingestion/, eval/, ...)
        for match in CONTRACTS_SCHEMA_DIR.rglob(f"{expected_type}.schema.json"):
            return match
    return None


def validate_artifact(
    artifact: Dict[str, Any],
    expected_type: str,
    artifact_path: Optional[str] = None,
    *,
    require_artifact_type_field: bool = True,
) -> None:
    """Validate ``artifact`` against ``schemas/<expected_type>.schema.json``.

    The ``artifact_type`` field is checked FIRST, before the schema is
    loaded. A wrong-type artifact is a more useful error than a
    schema-validation tree, and the check works even when no schema
    file exists yet.

    Args:
      artifact: parsed JSON dict from disk.
      expected_type: the artifact_type the caller is reading. The
        script-side contract is "I am about to read fields specific to
        ``expected_type``, refuse if the dict doesn't claim to be that
        type".
      artifact_path: optional path string included in error messages
        so the operator can locate the bad file immediately.
      require_artifact_type_field: when True (default), the artifact
        MUST carry ``artifact_type == expected_type`` at the top level.
        Set to False for contracts/ artifacts that pre-date the
        artifact_type convention (``ground_truth_pair`` is the only
        live example today); the function then validates schema shape
        only.

    Raises:
      ArtifactValidationError: artifact_type mismatch or schema
        violation. Never silently continues with an invalid artifact.
    """
    if not isinstance(artifact, dict):
        suffix = f" in {artifact_path}" if artifact_path else ""
        raise ArtifactValidationError(
            f"expected a dict for {expected_type}, got "
            f"{type(artifact).__name__}{suffix}"
        )

    if require_artifact_type_field:
        actual_type = artifact.get("artifact_type")
        if actual_type != expected_type:
            suffix = f" in {artifact_path}" if artifact_path else ""
            raise ArtifactValidationError(
                f"expected artifact_type='{expected_type}' but got "
                f"{actual_type!r}{suffix}"
            )

    schema_path = _find_schema(expected_type)
    if schema_path is None:
        # No schema registered for this type -- warn but don't block.
        print(
            f"warning: no schema found for {expected_type} under "
            f"{SCHEMA_DIR} or {CONTRACTS_SCHEMA_DIR}",
            file=sys.stderr,
        )
        return

    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError(
            f"could not load schema for {expected_type} from {schema_path}: {exc}"
        ) from exc

    try:
        jsonschema.validate(artifact, schema)
    except jsonschema.ValidationError as exc:
        field_path = " -> ".join(str(p) for p in exc.absolute_path) or "<root>"
        suffix = f"\n  in {artifact_path}" if artifact_path else ""
        raise ArtifactValidationError(
            f"artifact validation failed for {expected_type}: "
            f"{exc.message}{suffix}\n  failed field: {field_path}"
        ) from exc
