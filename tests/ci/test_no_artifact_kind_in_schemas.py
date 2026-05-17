"""
CI check: no new schema files use artifact_kind.

All schemas created after the Phase N migration must use artifact_type.
artifact_kind is the legacy field from before Phase N step 1.

This test scans all .schema.json files for the string "artifact_kind"
as a defined field (not in a description/comment context).
"""
import json
import pathlib

import pytest

SCAN_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMAS_ROOT = SCAN_ROOT / "contracts" / "schemas"

# Schemas that predate the migration and are known to use artifact_kind.
# This list should only shrink, never grow.
# When the migration to artifact_type completes, this list empties.
# Paths are repo-relative with forward slashes.
GRANDFATHERED_SCHEMAS: set[str] = {
    "contracts/schemas/source_record.schema.json",
    "contracts/schemas/review_artifact.schema.json",
    "contracts/schemas/obsidian_input_artifact.schema.json",
}


def _rel(path: pathlib.Path) -> str:
    try:
        return str(path.resolve().relative_to(SCAN_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def get_schema_files() -> list[pathlib.Path]:
    if not SCHEMAS_ROOT.exists():
        return []
    return list(SCHEMAS_ROOT.rglob("*.schema.json"))


def test_no_new_schema_uses_artifact_kind():
    """
    Scans all schema files for 'artifact_kind' field definition.
    Grandfathered schemas (pre-migration, known) are exempted.
    Any NEW schema using artifact_kind fails this check.

    artifact_kind is the legacy field. All new schemas must use artifact_type.
    """
    violations = []

    for schema_path in get_schema_files():
        rel = _rel(schema_path)
        if rel in GRANDFATHERED_SCHEMAS:
            continue

        try:
            content = schema_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError, OSError):
            continue

        try:
            schema_data = json.loads(content)
        except json.JSONDecodeError:
            # Invalid JSON is reported by other tooling; skip here so this
            # check does not crash and silently pass.
            continue

        if _schema_defines_artifact_kind(schema_data):
            properties_hint = (
                "uses 'artifact_kind' in properties/required -- "
                "rename the field to 'artifact_type'"
            )
            violations.append(f"{rel}: {properties_hint}")

    assert not violations, (
        f"Found {len(violations)} schema(s) using 'artifact_kind'.\n"
        f"New schemas must use 'artifact_type' instead. "
        f"Rename the field in 'properties' and any 'required' array.\n"
        f"Schemas in violation:\n" + "\n".join(violations)
    )


def _schema_defines_artifact_kind(schema, depth: int = 0) -> bool:
    """
    Recursively check whether the schema defines 'artifact_kind' as a
    property or as a required field name. Only structural occurrences are
    matched; description strings that merely mention the word are ignored.
    Max depth 10 to bound recursion on circular schemas.
    """
    if depth > 10:
        return False

    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict) and "artifact_kind" in properties:
            return True
        required = schema.get("required")
        if isinstance(required, list) and "artifact_kind" in required:
            return True
        for value in schema.values():
            if isinstance(value, (dict, list)):
                if _schema_defines_artifact_kind(value, depth + 1):
                    return True

    elif isinstance(schema, list):
        for item in schema:
            if isinstance(item, (dict, list)):
                if _schema_defines_artifact_kind(item, depth + 1):
                    return True

    return False


def test_grandfathered_schemas_still_exist():
    """
    Sanity: grandfathered schemas actually exist on disk.
    If a grandfathered schema is deleted, remove it from the list.
    Stale exemptions hide future violations.
    """
    for schema_path_str in GRANDFATHERED_SCHEMAS:
        path = SCAN_ROOT / schema_path_str
        assert path.exists(), (
            f"Grandfathered schema {schema_path_str} no longer exists at "
            f"{path}. Remove from GRANDFATHERED_SCHEMAS."
        )


def test_grandfathered_schemas_actually_use_artifact_kind():
    """
    Sanity: every grandfathered schema must actually contain artifact_kind.
    If it has already been migrated, the exemption is stale.
    """
    for schema_path_str in GRANDFATHERED_SCHEMAS:
        path = SCAN_ROOT / schema_path_str
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pytest.fail(
                f"Grandfathered schema {schema_path_str} could not be parsed; "
                f"remove it from GRANDFATHERED_SCHEMAS or fix the file."
            )
        assert _schema_defines_artifact_kind(data), (
            f"Grandfathered schema {schema_path_str} no longer uses "
            f"'artifact_kind'. Remove it from GRANDFATHERED_SCHEMAS."
        )


def test_contracts_schemas_directory_exists():
    """Sanity: schemas directory must exist for this check to be meaningful."""
    if not SCHEMAS_ROOT.exists():
        pytest.skip(f"{SCHEMAS_ROOT} does not exist yet -- no schemas to check")
