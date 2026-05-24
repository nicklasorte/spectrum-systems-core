"""Phase 4.C — cascade prompt + per-type guidance contract tests.

The cascade prompt template is the single source of truth for what
Sonnet sees on every batch. These tests pin its shape:

* the three required placeholders are present;
* a response-format section is present;
* type definitions come from `meeting_minutes.schema.json` (and stay
  in sync if the schema descriptions change);
* per-type disqualifiers come from the extraction prompt's DO NOT
  EXTRACT section (or the future 4.B PER_TYPE_PRECISION_GUARD block).
"""
from __future__ import annotations

import json
import pathlib

from spectrum_systems_core.promotion.cascade_filter import (
    CASCADE_PROMPT_PATH,
    load_cascade_prompt_template,
    load_meeting_minutes_prompt,
    load_type_definitions,
    parse_type_disqualifiers,
)
from spectrum_systems_core.promotion.grounding_gate import CLAIM_SHAPED_TYPES


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMAS_DIR = REPO_ROOT / "src" / "spectrum_systems_core" / "schemas"


def test_cascade_prompt_template_has_required_placeholders():
    template = load_cascade_prompt_template()
    for placeholder in ("{type_definitions}", "{type_disqualifiers}", "{items_json}"):
        assert placeholder in template, f"missing {placeholder!r}"


def test_cascade_prompt_response_format_section_present():
    template = load_cascade_prompt_template()
    assert "## Response format" in template
    # Three decision tokens must be named explicitly in the response
    # format so a future template edit cannot silently drop one.
    for token in ("keep", "drop", "modify"):
        assert token in template, f"response format lost {token!r}"


def test_cascade_prompt_modify_grounding_constraint_present():
    """The prompt MUST tell Sonnet that modify must be a substring."""
    template = load_cascade_prompt_template()
    # The substring constraint is the core trust property of modify.
    assert "substring" in template
    # And the consequence: a modify that breaks grounding is dropped.
    assert "modify_broke_grounding" in template


def test_cascade_prompt_path_is_under_workflows_prompts():
    """The on-disk location must stay consistent across PRs."""
    assert CASCADE_PROMPT_PATH.exists()
    parts = CASCADE_PROMPT_PATH.parts
    assert "workflows" in parts
    assert "prompts" in parts
    assert CASCADE_PROMPT_PATH.name == "cascade_filter.md"


def test_type_definitions_come_from_schema_descriptions():
    """Every claim-shaped type gets a non-empty definition string from the
    meeting_minutes schema's property descriptions (or the fallback)."""
    defs = load_type_definitions()
    for typ in CLAIM_SHAPED_TYPES:
        assert typ in defs, f"definition missing for {typ}"
        assert defs[typ].strip(), f"empty definition for {typ}"

    # Spot-check: the `decisions` definition should mention "decision"
    # somewhere (either the schema docstring or the fallback says it).
    assert "decision" in defs["decisions"].lower()


def test_type_definitions_loaded_directly_from_schema_file():
    """The definitions are read from the on-disk schema file at runtime."""
    schema = json.loads(
        (SCHEMAS_DIR / "meeting_minutes.schema.json").read_text()
    )
    schema_descriptions = {
        typ: schema.get("properties", {}).get(typ, {}).get("description")
        for typ in CLAIM_SHAPED_TYPES
    }
    defs = load_type_definitions()
    for typ in CLAIM_SHAPED_TYPES:
        if schema_descriptions.get(typ):
            # The cleaned definition is a truncated, whitespace-collapsed
            # view of the schema description. The first 50 chars should
            # appear (after whitespace collapse) in BOTH so we know we
            # actually pulled from the schema and didn't fall back.
            sd = " ".join(str(schema_descriptions[typ]).split())[:50]
            assert sd in defs[typ], (
                f"definition for {typ} did not come from the schema "
                f"description: schema starts {sd!r}, def is {defs[typ]!r}"
            )


def test_type_disqualifiers_come_from_4b_precision_guard_or_donotextract():
    """Every claim-shaped type gets a non-empty disqualifier string.

    The parser prefers the Phase 4.B PER_TYPE_PRECISION_GUARD block
    when present; until 4.B lands the fallback maps DO NOT EXTRACT
    subsections to the claim-shaped types they reference.
    """
    disq = parse_type_disqualifiers(load_meeting_minutes_prompt())
    for typ in CLAIM_SHAPED_TYPES:
        assert typ in disq, f"disqualifier missing for {typ}"
        assert disq[typ].strip(), f"empty disqualifier for {typ}"


def test_disqualifier_parser_reads_phase_4b_bold_block():
    """The cascade reads the Phase 4.B (PR #247) PRECISION_GUARD_4B
    marker block with the bold-headed `**type** —` layout."""
    fake_prompt = """
## DO NOT EXTRACT

### Brainstorming
generic generic generic.

<!-- PRECISION_GUARD_4B_BEGIN -->
### Per-type precision guard (from extraction analysis)

**decisions** — Do NOT extract as a decision:
- Tentative statements using 'probably' or 'maybe'
- Recapped prior decisions; those belong in precedent_reference

**action_items** — Do NOT extract:
- Agenda restatement patterns
- Meta-procedural calls
<!-- PRECISION_GUARD_4B_END -->
"""
    disq = parse_type_disqualifiers(fake_prompt)
    assert "Tentative statements" in disq["decisions"]
    assert "Recapped prior decisions" in disq["decisions"]
    assert "Agenda restatement" in disq["action_items"]


def test_disqualifier_parser_accepts_legacy_per_type_marker_name():
    """For forward compatibility the parser also accepts the legacy
    PER_TYPE_PRECISION_GUARD_* marker name with the ### heading layout."""
    fake_prompt = """
<!-- PER_TYPE_PRECISION_GUARD_BEGIN -->

### decisions
Drop tentative statements using 'probably' or 'maybe' — those are not
decisions.

### action_items
Drop the agenda restatement pattern.

<!-- PER_TYPE_PRECISION_GUARD_END -->
"""
    disq = parse_type_disqualifiers(fake_prompt)
    assert "tentative statements using 'probably'" in disq["decisions"]
    assert "agenda restatement" in disq["action_items"]


def test_disqualifier_parser_falls_back_when_no_4b_block():
    """Without the 4.B block the parser still produces text per type."""
    fake_prompt = """
## DO NOT EXTRACT

### Brainstorming
Drop hedging language items.

### Recapping prior decisions
Drop items that re-state prior decisions.
"""
    disq = parse_type_disqualifiers(fake_prompt)
    # The fallback mapping puts 'Brainstorming' on decisions/action_items/etc.
    assert "Drop hedging language" in disq["decisions"]
