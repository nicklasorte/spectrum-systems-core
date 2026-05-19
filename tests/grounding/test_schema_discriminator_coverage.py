"""Phase 1 — exhaustive schema discriminator coverage (Red Team Pass 1 #6).

Every item-type sub-schema in ``meeting_minutes.schema.json`` MUST
declare a ``grounding_mode`` discriminator. If a future addition
forgets to declare it, an LLM could emit items of that type without a
grounding mode and the gate would route them to neither bucket — a
silent-pass path. This test runs the schema through a structural
inspection and asserts the discriminator is present on every item
type, including both branches of any ``oneOf``.

The test also asserts that the in-code ``VERBATIM_TYPES`` and
``TURN_AGGREGATE_TYPES`` frozensets cover every item type the schema
exposes (so the gate routes by type cannot silently drop a new type
into the "neither" bucket).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from spectrum_systems_core.promotion.gate import (
    TURN_AGGREGATE_TYPES,
    VERBATIM_TYPES,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = (
    REPO_ROOT
    / "src"
    / "spectrum_systems_core"
    / "schemas"
    / "meeting_minutes.schema.json"
)


def _iter_item_object_schemas(schema: dict):
    """Yield (item_key, object_schema) for every array-of-objects item type."""
    for key, prop_schema in schema["properties"].items():
        if not isinstance(prop_schema, dict):
            continue
        if prop_schema.get("type") != "array":
            continue
        items = prop_schema.get("items")
        if not isinstance(items, dict):
            continue
        if "oneOf" in items:
            for branch in items["oneOf"]:
                if isinstance(branch, dict) and branch.get("type") == "object":
                    yield key, branch
        elif items.get("type") == "object":
            yield key, items


def test_every_item_type_object_schema_declares_grounding_mode():
    schema = json.loads(SCHEMA_PATH.read_text())
    missing: list[str] = []
    for key, obj_schema in _iter_item_object_schemas(schema):
        # ``grounding`` is the Phase Y per-item grounding registry — it
        # is a flat list of dicts, not an item-type carrying items
        # extracted from the transcript, so it's exempt.
        if key == "grounding":
            continue
        props = obj_schema.get("properties", {})
        if "grounding_mode" not in props:
            missing.append(key)
    assert not missing, (
        f"item-type schemas missing grounding_mode discriminator: {missing}"
    )


def test_grounding_mode_const_value_matches_in_code_tables():
    """The schema's ``grounding_mode.const`` value on each item type
    must match the in-code VERBATIM_TYPES / TURN_AGGREGATE_TYPES
    frozensets. Drift between the two would let the schema accept an
    item that the gate would not even route."""
    schema = json.loads(SCHEMA_PATH.read_text())
    drift: list[str] = []
    for key, obj_schema in _iter_item_object_schemas(schema):
        if key == "grounding":
            continue
        props = obj_schema.get("properties", {})
        gm = props.get("grounding_mode")
        if not isinstance(gm, dict):
            continue
        const = gm.get("const")
        if const == "verbatim":
            if key not in VERBATIM_TYPES:
                drift.append(
                    f"{key}: schema says verbatim, code does not"
                )
        elif const == "turn_aggregate":
            if key not in TURN_AGGREGATE_TYPES:
                drift.append(
                    f"{key}: schema says turn_aggregate, code does not"
                )
        else:
            drift.append(
                f"{key}: grounding_mode.const is unexpected {const!r}"
            )
    assert not drift, f"schema-vs-code drift: {drift}"


def test_verbatim_types_in_code_match_at_least_one_schema_item():
    """Every entry in VERBATIM_TYPES must correspond to either a real
    schema item-type OR a documented synonym. Catches typos in the
    in-code table."""
    schema = json.loads(SCHEMA_PATH.read_text())
    schema_verbatim: set[str] = set()
    for key, obj_schema in _iter_item_object_schemas(schema):
        gm = obj_schema.get("properties", {}).get("grounding_mode")
        if isinstance(gm, dict) and gm.get("const") == "verbatim":
            schema_verbatim.add(key)

    # In-code names must match the schema exactly — the schema is the
    # source of truth per the data lake contract. A drift here is the
    # canonical silent-pass path the red-team caught.
    for code_type in VERBATIM_TYPES:
        assert code_type in schema_verbatim, (
            f"VERBATIM_TYPES references {code_type!r} but the schema "
            f"does not have a verbatim item type by that name. "
            f"Schema verbatim types: {sorted(schema_verbatim)}"
        )


def test_turn_aggregate_types_in_code_match_schema_item():
    schema = json.loads(SCHEMA_PATH.read_text())
    schema_turn_agg: set[str] = set()
    for key, obj_schema in _iter_item_object_schemas(schema):
        gm = obj_schema.get("properties", {}).get("grounding_mode")
        if isinstance(gm, dict) and gm.get("const") == "turn_aggregate":
            schema_turn_agg.add(key)
    for code_type in TURN_AGGREGATE_TYPES:
        assert code_type in schema_turn_agg, (
            f"TURN_AGGREGATE_TYPES references {code_type!r} but the "
            f"schema does not have a turn_aggregate item type by that "
            f"name. Schema turn_aggregate types: {sorted(schema_turn_agg)}"
        )


@pytest.mark.parametrize(
    "schema_version",
    ["1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0"],
)
def test_every_legacy_schema_version_remains_in_enum(schema_version):
    schema = json.loads(SCHEMA_PATH.read_text())
    assert schema_version in schema["properties"]["schema_version"]["enum"]
