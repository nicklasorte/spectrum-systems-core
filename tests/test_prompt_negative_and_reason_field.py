"""Phase 3.A — G-PROMPT-NEGATIVE + G-REASON-FIELD contract tests.

Phase 2.C cascade F1 came in at 33.0%, below the 39.5% baseline gate.
The cascade approach failed because Haiku over-extracted at the source
(2.85x ratio vs Opus) and post-hoc filtering dropped signal with
noise. Phase 3.A pivots to precision-at-extraction-time by combining
two prompt techniques:

1. G-PROMPT-NEGATIVE — an explicit "DO NOT EXTRACT" section enumerates
   non-extractable categories (brainstorming, prior-decision recaps,
   agenda restatements, conditional/speculative statements,
   meta-procedural talk, third-party quotes, repeated mentions, bare
   numeric mentions). Placed BEFORE per-type guidance so the model
   reads negatives before deciding what to emit.
2. G-REASON-FIELD — every item in the 14 claim-shaped extraction
   types must include a `reason` field (5-500 chars) explaining WHY
   the item qualifies. The forcing-function sentence is "If you
   cannot articulate a reason in one sentence, DO NOT extract this
   item."

This test file pins three contracts so the Haiku and Opus prompts
cannot drift out of lockstep and the schema additions stay in sync
with the prompt's requirements.

Contracts:
- The DO NOT EXTRACT section is byte-identical in both prompts.
- The forcing-function sentence is in proximity to each of the 14
  reason-bearing type names.
- The reason field is in the schema's properties block for each of
  the 14 types and is absent from the descriptive types.
- The canonical schema_version constant in promotion.gate is the
  single source of truth for the active version (additive optional
  fields do not require bumping it).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spectrum_systems_core.promotion.gate import (
    GROUNDING_BINDING_SCHEMA_VERSION,
)


_PROMPTS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "spectrum_systems_core"
    / "workflows"
    / "prompts"
)
_HAIKU_PROMPT_PATH = _PROMPTS_DIR / "meeting_minutes_llm.md"
_OPUS_PROMPT_PATH = _PROMPTS_DIR / "meeting_minutes_opus.md"

_SCHEMAS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "spectrum_systems_core"
    / "schemas"
)
_MEETING_MINUTES_SCHEMA = _SCHEMAS_DIR / "meeting_minutes.schema.json"


REASON_BEARING_TYPES: tuple[str, ...] = (
    "decisions",
    "action_items",
    "open_questions",
    "commitments",
    "claims",
    "risks",
    "cross_references",
    "regulatory_references",
    "issue_registry_entry",
    "position_statement",
    "dissent_or_objection",
    "precedent_reference",
    "external_stakeholder_input",
    "procedural_ruling",
)
assert len(REASON_BEARING_TYPES) == 14

DESCRIPTIVE_TYPES: tuple[str, ...] = (
    "attendees",
    "topics",
    "technical_parameters",
    "named_artifacts",
    "scheduled_events",
    "sentiment_indicators",
    "meeting_phases",
    "agenda_item",
    "glossary_definition",
)


def _strip_trailing_whitespace(text: str) -> str:
    """Drop trailing whitespace per-line so the byte-identity check
    tolerates a stray space introduced by an editor without losing the
    structural lockstep guarantee."""
    return "\n".join(line.rstrip() for line in text.splitlines())


def _extract_section(text: str, start_header: str, end_header: str) -> str:
    """Return the chunk of ``text`` starting at ``start_header`` and
    ending just before ``end_header`` (exclusive). Both headers must
    appear or pytest fails the test immediately so a missing section
    surfaces as a clear failure rather than an empty match."""
    start = text.find(start_header)
    if start == -1:
        pytest.fail(f"section header {start_header!r} not found")
    end = text.find(end_header, start + len(start_header))
    if end == -1:
        pytest.fail(
            f"section end header {end_header!r} not found after "
            f"{start_header!r}"
        )
    return text[start:end]


# ---------------------------------------------------------------------------
# Test 1: negative-categories section present + byte-identical
# ---------------------------------------------------------------------------


def test_negative_categories_present_in_prompt() -> None:
    """The DO NOT EXTRACT section must exist in BOTH prompts and be
    byte-identical (modulo trailing whitespace).

    Lockstep is a hard requirement: if only Haiku carries the negative
    categories, the Opus reference baseline's item count drifts and
    the comparison engine's F1 number is no longer interpretable.
    """
    haiku = _HAIKU_PROMPT_PATH.read_text(encoding="utf-8")
    opus = _OPUS_PROMPT_PATH.read_text(encoding="utf-8")

    for label, text in (("haiku", haiku), ("opus", opus)):
        assert "## DO NOT EXTRACT" in text, (
            f"{label} prompt missing the `## DO NOT EXTRACT` section"
        )
        for category in (
            "### Brainstorming and hypothetical reasoning",
            "### Recapping prior decisions",
            "### Restating the agenda",
            "### Conditional or speculative statements",
            "### Meta-procedural talk about the meeting itself",
            "### Quotes attributed to absent third parties",
            "### Repeated mentions of the same item",
            "### Banding / numeric mentions without context",
        ):
            assert category in text, (
                f"{label} prompt missing the {category!r} subsection"
            )

    haiku_section = _extract_section(
        haiku, "## DO NOT EXTRACT", "## Reason field"
    )
    opus_section = _extract_section(
        opus, "## DO NOT EXTRACT", "## Reason field"
    )

    assert _strip_trailing_whitespace(haiku_section) == _strip_trailing_whitespace(
        opus_section
    ), (
        "DO NOT EXTRACT section drifted between meeting_minutes_llm.md "
        "and meeting_minutes_opus.md — they must stay byte-identical "
        "modulo trailing whitespace so the Haiku/Opus F1 comparison "
        "stays interpretable."
    )


# ---------------------------------------------------------------------------
# Test 2: forcing-function sentence in proximity to each type
# ---------------------------------------------------------------------------


_FORCING_FUNCTION_SENTENCE = (
    "If you cannot articulate a reason in one sentence, DO NOT extract "
    "this item."
)


@pytest.mark.parametrize(
    "prompt_path",
    [_HAIKU_PROMPT_PATH, _OPUS_PROMPT_PATH],
    ids=["haiku", "opus"],
)
def test_reason_field_required_in_prompt_for_each_type(prompt_path: Path) -> None:
    """The forcing-function sentence must appear in the prompt AND
    every one of the 14 reason-bearing type names must appear in the
    same section.

    The unified `Reason field` section names each type, so the
    forcing-function sentence is in proximity to every type at the
    cost of one section, not 14 per-type duplications.
    """
    text = prompt_path.read_text(encoding="utf-8")

    # The forcing-function sentence may wrap across a newline in the
    # rendered markdown; normalize whitespace before searching.
    normalized = " ".join(text.split())
    assert _FORCING_FUNCTION_SENTENCE in normalized, (
        f"{prompt_path.name} missing the forcing-function sentence: "
        f"{_FORCING_FUNCTION_SENTENCE!r}"
    )

    # The Reason field section must list every reason-bearing type
    # name. We extract from `## Reason field` to the next `##` header
    # and assert every type name is present.
    section_start = text.find("## Reason field (REQUIRED on 14 claim-shaped types)")
    assert section_start != -1, (
        f"{prompt_path.name} missing the Phase 3.A "
        "'## Reason field (REQUIRED on 14 claim-shaped types)' section"
    )
    section_end = text.find("\n## ", section_start + 1)
    if section_end == -1:
        section_end = len(text)
    section = text[section_start:section_end]

    for type_name in REASON_BEARING_TYPES:
        assert f"`{type_name}`" in section, (
            f"{prompt_path.name} Reason field section missing the "
            f"`{type_name}` type — the unified section is the proximity "
            "anchor for the forcing function"
        )


# ---------------------------------------------------------------------------
# Test 3: reason field present in schema for each reason-bearing type
# ---------------------------------------------------------------------------


def _items_properties(schema_array_node: dict) -> dict:
    """Resolve the properties dict for an array sub-schema.

    Some types declare ``items`` as a single object schema; the
    ``decisions``/``action_items``/``open_questions`` arrays declare
    ``items.oneOf`` because they accept BOTH a plain string and a
    structured object. Return the structured-branch ``properties`` so
    the reason-field assertion can target the actual field location.
    """
    items = schema_array_node["items"]
    if "oneOf" in items:
        for branch in items["oneOf"]:
            if branch.get("type") == "object" and "properties" in branch:
                return branch["properties"]
        pytest.fail(
            f"oneOf items branch with object properties not found in: "
            f"{schema_array_node!r}"
        )
    if items.get("type") == "object" and "properties" in items:
        return items["properties"]
    pytest.fail(f"could not resolve item properties for: {schema_array_node!r}")
    raise AssertionError  # unreachable, satisfies the type checker


@pytest.fixture(scope="module")
def meeting_minutes_schema() -> dict:
    return json.loads(_MEETING_MINUTES_SCHEMA.read_text(encoding="utf-8"))


@pytest.mark.parametrize("type_name", REASON_BEARING_TYPES)
def test_reason_field_in_schema_for_each_type(
    meeting_minutes_schema: dict, type_name: str
) -> None:
    """Every reason-bearing type's items schema must declare a
    ``reason`` property with the canonical Phase 3.A constraints:
    string, minLength=5, maxLength=500.

    The field is additive optional in the JSON Schema (legacy
    artifacts that omit it still validate). The prompt is what
    requires it; the schema records the shape so downstream readers
    can rely on a stable type and length range when the field is
    present.
    """
    node = meeting_minutes_schema["properties"][type_name]
    properties = _items_properties(node)

    assert "reason" in properties, (
        f"type {type_name!r} missing the `reason` field in its items "
        "schema — Phase 3.A requires it on every claim-shaped type"
    )

    reason_field = properties["reason"]
    assert reason_field["type"] == "string", (
        f"{type_name}.items.properties.reason.type must be `string`, "
        f"got {reason_field.get('type')!r}"
    )
    assert reason_field.get("minLength") == 5, (
        f"{type_name}.items.properties.reason.minLength must be 5, "
        f"got {reason_field.get('minLength')!r} — short stubs are not "
        "informative enough to justify the over-extraction filter"
    )
    assert reason_field.get("maxLength") == 500, (
        f"{type_name}.items.properties.reason.maxLength must be 500, "
        f"got {reason_field.get('maxLength')!r} — a paragraph-length "
        "reason indicates the model is justifying, not articulating"
    )


# ---------------------------------------------------------------------------
# Test 4: reason field absent from descriptive types
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("type_name", DESCRIPTIVE_TYPES)
def test_reason_field_NOT_in_schema_for_descriptive_types(
    meeting_minutes_schema: dict, type_name: str
) -> None:
    """Descriptive types (attendees, agenda_item, meeting_phases,
    topics, scheduled_events, technical_parameters, named_artifacts,
    sentiment_indicators, glossary_definition) are structural, not
    claim-shaped. Adding a ``reason`` to them would invite the model
    to invent justifications for purely descriptive items.

    `additionalProperties: false` on each items schema already rejects
    a stray ``reason`` at runtime; this test pins the absence at the
    declared-schema layer so a future edit cannot quietly admit it.
    """
    node = meeting_minutes_schema["properties"][type_name]
    properties = _items_properties(node)
    assert "reason" not in properties, (
        f"descriptive type {type_name!r} unexpectedly has a `reason` "
        "field in its items schema. Descriptive types must NOT carry "
        "reason; only the 14 claim-shaped types do."
    )


# ---------------------------------------------------------------------------
# Test 5: schema_version unchanged (additive optional fields) or
# canonically bumped via the gate constant.
# ---------------------------------------------------------------------------


def test_schema_version_unchanged_or_canonically_bumped(
    meeting_minutes_schema: dict,
) -> None:
    """Phase 3.A additions are optional fields and do NOT require a
    schema_version bump (the schema's ``schema_version`` enum policy
    is documented at the field declaration). If a future change does
    bump the binding version, ``promotion.gate.GROUNDING_BINDING_SCHEMA_VERSION``
    is the SINGLE canonical source — every downstream producer reads
    from that constant rather than hardcoding a string literal.

    This test asserts:

    1. The constant exists and is the documented current version (1.4.0).
    2. The schema's enum still admits that version.
    """
    assert GROUNDING_BINDING_SCHEMA_VERSION == "1.4.0", (
        "Phase 3.A is purely additive optional fields and must NOT bump "
        "the canonical schema version. If you intentionally bumped it, "
        "make sure every place that hardcoded the old value now reads "
        "from `promotion.gate.GROUNDING_BINDING_SCHEMA_VERSION`."
    )
    versions = meeting_minutes_schema["properties"]["schema_version"]["enum"]
    assert GROUNDING_BINDING_SCHEMA_VERSION in versions, (
        f"the canonical schema_version "
        f"{GROUNDING_BINDING_SCHEMA_VERSION!r} is not in the schema's "
        f"enum {versions!r} — these two must stay in lockstep"
    )
