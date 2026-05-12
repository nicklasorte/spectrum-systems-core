"""Pre-commit safety: no NEW schema may use the deprecated ``artifact_kind`` key.

If any schema in ``contracts/schemas/`` reintroduces ``"artifact_kind"``
as a JSON Schema property, this test fails. This catches accidental
reversion during the Pre-N migration. The artifact-as-evidence: this
file IS the regression test (no prose-only claim).

A small allow-list captures schemas that legitimately still carry
``artifact_kind`` alongside the new ``artifact_type`` while the
migration completes. Phase V must not extend this list.
"""
from __future__ import annotations

import pathlib


SCHEMAS_DIR = (
    pathlib.Path(__file__).resolve().parent.parent / "contracts" / "schemas"
)

# Schemas that still carry artifact_kind as part of the in-flight
# Pre-N migration. Adding new entries is a regression; removing entries
# is progress. Phase V adds zero entries -- new schemas use artifact_type
# exclusively.
_LEGACY_DUAL_KEY_SCHEMAS = frozenset({
    "obsidian_input_artifact.schema.json",
    "review_artifact.schema.json",
    "source_record.schema.json",
})


def test_no_new_schema_uses_artifact_kind() -> None:
    violations = []
    for schema_path in SCHEMAS_DIR.rglob("*.schema.json"):
        if schema_path.name in _LEGACY_DUAL_KEY_SCHEMAS:
            continue
        content = schema_path.read_text(encoding="utf-8")
        if '"artifact_kind"' in content:
            violations.append(str(schema_path.relative_to(SCHEMAS_DIR.parent.parent)))
    assert not violations, (
        "Schemas reintroduced artifact_kind (migration reverted): "
        + ", ".join(violations)
    )


def test_legacy_dual_key_schemas_still_carry_artifact_type() -> None:
    """The grandfathered set must each retain artifact_type alongside
    artifact_kind. If artifact_type goes missing on one of them, that
    schema is a regression even though artifact_kind remains.
    """
    for name in _LEGACY_DUAL_KEY_SCHEMAS:
        path = SCHEMAS_DIR / name
        content = path.read_text(encoding="utf-8")
        assert '"artifact_type"' in content, (
            f"{name} lost artifact_type while still carrying artifact_kind"
        )


def test_every_phase_v_schema_declares_artifact_type() -> None:
    """The Phase V schemas in particular must use the new naming."""
    targets = [
        SCHEMAS_DIR / "verification" / "source_verification_result.schema.json",
        SCHEMAS_DIR / "extraction" / "meeting_extraction.v2.schema.json",
    ]
    for path in targets:
        content = path.read_text(encoding="utf-8")
        assert '"artifact_type"' in content, f"{path.name} missing artifact_type"
        assert '"artifact_kind"' not in content, (
            f"{path.name} reintroduced artifact_kind"
        )
