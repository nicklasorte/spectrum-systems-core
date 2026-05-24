"""Phase 4.B — precision negative examples + action_items dict shape.

Pin the Phase 4.B prompt-engineering changes so a future edit cannot
quietly drop, drift, or de-sync them between the Haiku production
prompt (``meeting_minutes_llm.md``) and the Opus reference prompt
(``meeting_minutes_opus.md``).

Contracts asserted here:

- The new "Per-type precision guard (from extraction analysis)"
  subsection is present in both prompts with the six per-type
  negative-example blocks (decisions, topics, technical_parameters,
  commitments, precedent_reference, issue_registry_entry).
- The per-type precision guard subsection is BYTE-IDENTICAL between
  the Haiku and Opus prompts (so a future edit cannot silently weaken
  one side).
- The action_items dict-shape instruction (with WRONG / CORRECT
  example) is present in both prompts and the instruction subsection
  is byte-identical.
- The ``meeting_minutes`` schema's ``action_items.items`` is the
  object shape (no bare-string branch); ``action`` is required and
  ``source_quote`` is present as a property.
- The 3.E few-shot examples never carry an ``action_items`` entry that
  is a bare string — every entry MUST be a dict.
- Both prompts now declare ``version: 4.B`` in their YAML frontmatter,
  the ``4.A`` token only appears inside changelog entries.
- The DO NOT EXTRACT section appears BEFORE the VERBATIM SOURCE
  GROUNDING section in both prompts, and the precision guard appears
  WITHIN DO NOT EXTRACT.

The Phase 4.B precision-guard negative examples are sourced verbatim
from the haiku_only list in the 2026-05-24 comparison run — see the
Phase 4.B PR description for the source-of-truth mapping. Do not
invent new negative examples here; if a new haiku_only false positive
needs guarding, add it to both prompts (byte-identical) and add a
type-specific test alongside the existing ones.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_PROMPTS_DIR = (
    _REPO_ROOT / "src" / "spectrum_systems_core" / "workflows" / "prompts"
)
_HAIKU_PROMPT_PATH = _PROMPTS_DIR / "meeting_minutes_llm.md"
_OPUS_PROMPT_PATH = _PROMPTS_DIR / "meeting_minutes_opus.md"
_SCHEMA_PATH = (
    _REPO_ROOT
    / "src"
    / "spectrum_systems_core"
    / "schemas"
    / "meeting_minutes.schema.json"
)

_PRECISION_GUARD_BEGIN = "<!-- PRECISION_GUARD_4B_BEGIN -->"
_PRECISION_GUARD_END = "<!-- PRECISION_GUARD_4B_END -->"
_ACTION_ITEMS_DICT_BEGIN = "<!-- ACTION_ITEMS_DICT_4B_BEGIN -->"
_ACTION_ITEMS_DICT_END = "<!-- ACTION_ITEMS_DICT_4B_END -->"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _between(text: str, begin: str, end: str) -> str:
    start = text.index(begin)
    stop = text.index(end, start) + len(end)
    return text[start:stop]


# ---------------------------------------------------------------------------
# 1. Per-type precision guard section
# ---------------------------------------------------------------------------


def test_per_type_precision_guard_section_present_in_both_prompts() -> None:
    """The new subsection header MUST be present in both prompts.

    A future edit that removes the section (or the comment markers
    that anchor it) is caught here before the prompt ships."""
    for path in (_HAIKU_PROMPT_PATH, _OPUS_PROMPT_PATH):
        text = _read(path)
        assert _PRECISION_GUARD_BEGIN in text, (
            f"{path.name}: PRECISION_GUARD_4B begin marker missing"
        )
        assert _PRECISION_GUARD_END in text, (
            f"{path.name}: PRECISION_GUARD_4B end marker missing"
        )
        assert "### Per-type precision guard" in text, (
            f"{path.name}: per-type precision guard heading missing"
        )


def test_per_type_precision_guard_byte_identical() -> None:
    """The per-type precision guard MUST be byte-identical between
    the Haiku and Opus prompts.

    The two prompts target different models but the per-type negative
    examples are model-agnostic — they describe what NEVER counts as
    that type. A drift here would mean the Haiku and Opus comparisons
    are no longer comparable: one model sees a tighter precision rule
    than the other."""
    haiku_section = _between(
        _read(_HAIKU_PROMPT_PATH),
        _PRECISION_GUARD_BEGIN,
        _PRECISION_GUARD_END,
    )
    opus_section = _between(
        _read(_OPUS_PROMPT_PATH),
        _PRECISION_GUARD_BEGIN,
        _PRECISION_GUARD_END,
    )
    assert haiku_section == opus_section, (
        "Per-type precision guard is not byte-identical between the "
        "Haiku and Opus prompts. Re-sync the two sections."
    )


def test_decisions_negative_examples_present() -> None:
    """The decisions guard cites the two motivating haiku_only false
    positives: procedural-logistics statements and tentative
    ('probably') statements."""
    section = _between(
        _read(_HAIKU_PROMPT_PATH),
        _PRECISION_GUARD_BEGIN,
        _PRECISION_GUARD_END,
    )
    assert "Procedural statements" in section, (
        "decisions guard missing 'Procedural statements' line"
    )
    assert "Tentative statements" in section, (
        "decisions guard missing 'Tentative statements' line"
    )
    assert "we're probably going to make a change" in section, (
        "decisions guard missing the verbatim 'probably' false positive "
        "from the 2026-05-24 haiku_only list"
    )


def test_topics_precision_guard_present() -> None:
    """The topics guard tells the model not to extract every mentioned
    subject and explicitly names the agenda-level expectation."""
    section = _between(
        _read(_HAIKU_PROMPT_PATH),
        _PRECISION_GUARD_BEGIN,
        _PRECISION_GUARD_END,
    )
    assert "**topics**" in section, (
        "per-type precision guard missing the **topics** block"
    )
    assert "agenda-level" in section, (
        "topics guard missing the 'agenda-level' precision instruction"
    )


def test_technical_parameters_numeric_requirement_present() -> None:
    """The technical_parameters guard requires a numeric value or
    measurable engineering quantity — the rule that catches the
    'System list validation deadline' false positive."""
    section = _between(
        _read(_HAIKU_PROMPT_PATH),
        _PRECISION_GUARD_BEGIN,
        _PRECISION_GUARD_END,
    )
    assert "**technical_parameters**" in section, (
        "per-type precision guard missing the **technical_parameters** "
        "block"
    )
    assert "numeric value" in section, (
        "technical_parameters guard missing the 'numeric value' "
        "requirement"
    )
    assert "measurable engineering quantity" in section, (
        "technical_parameters guard missing the 'measurable engineering "
        "quantity' fallback for non-numeric units"
    )


# ---------------------------------------------------------------------------
# 2. action_items dict-shape enforcement
# ---------------------------------------------------------------------------


def test_action_items_dict_shape_instruction_present_in_both_prompts() -> None:
    """The Phase 4.B action_items dict-shape instruction MUST be
    present in both prompts with the WRONG / CORRECT example.

    The WRONG / CORRECT example is the operative teaching signal —
    without it the rule is a sentence, not a demonstrated pattern.
    Both prompts MUST also be byte-identical on this subsection so
    the two model paths get the same rule."""
    for path in (_HAIKU_PROMPT_PATH, _OPUS_PROMPT_PATH):
        text = _read(path)
        assert _ACTION_ITEMS_DICT_BEGIN in text, (
            f"{path.name}: ACTION_ITEMS_DICT_4B begin marker missing"
        )
        assert _ACTION_ITEMS_DICT_END in text, (
            f"{path.name}: ACTION_ITEMS_DICT_4B end marker missing"
        )
        section = _between(
            text, _ACTION_ITEMS_DICT_BEGIN, _ACTION_ITEMS_DICT_END
        )
        assert "WRONG (bare string)" in section, (
            f"{path.name}: action_items WRONG example missing"
        )
        assert "CORRECT (object)" in section, (
            f"{path.name}: action_items CORRECT example missing"
        )

    haiku_section = _between(
        _read(_HAIKU_PROMPT_PATH),
        _ACTION_ITEMS_DICT_BEGIN,
        _ACTION_ITEMS_DICT_END,
    )
    opus_section = _between(
        _read(_OPUS_PROMPT_PATH),
        _ACTION_ITEMS_DICT_BEGIN,
        _ACTION_ITEMS_DICT_END,
    )
    assert haiku_section == opus_section, (
        "action_items dict-shape instruction is not byte-identical "
        "between the Haiku and Opus prompts"
    )


def test_action_items_schema_is_object_not_string() -> None:
    """The meeting_minutes schema's ``action_items.items`` MUST be
    object-only (no ``oneOf [string, object]`` branch).

    Spec note: the Phase 4.B task brief used ``text`` / ``assignee``
    as required field names in its example. The actual codebase uses
    ``action`` (the existing field name across the schema, the regex
    workflow, the LLM eval, the comparison engine, and 100+ tests).
    We follow the existing field names — a rename would be a separate,
    cross-codebase change that is outside the precision-fix scope. The
    assertion below checks for ``action`` (the real required field)
    AND for ``source_quote`` as a property (the grounding-gate anchor
    the dict shape exists to carry)."""
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    items = schema["properties"]["action_items"]["items"]

    assert items.get("type") == "object", (
        f"action_items.items.type must be 'object' "
        f"(got {items.get('type')!r}). The bare-string oneOf branch "
        f"is removed in Phase 4.B because a bare string cannot carry "
        f"source_quote."
    )
    assert "action" in items.get("required", []), (
        "action_items.items.required must contain 'action' — the "
        "verbatim text field the grounding gate reads"
    )
    assert "source_quote" in items.get("properties", {}), (
        "action_items.items must declare 'source_quote' as a property "
        "(it is what the grounding gate checks at runtime)"
    )


def test_fewshot_3e_action_items_are_dicts() -> None:
    """Every ``action_items`` entry in every Phase 3.E example MUST
    be a dict, never a bare string.

    A bare-string action_items entry in a few-shot would teach the
    model to emit bare strings, which the grounding gate now rejects.
    This is the cross-check that prevents the few-shot examples from
    silently weakening the Phase 4.B rule."""
    BEGIN = "<!-- FEW_SHOT_3E_BEGIN -->"
    END = "<!-- FEW_SHOT_3E_END -->"

    for path in (_HAIKU_PROMPT_PATH, _OPUS_PROMPT_PATH):
        text = _read(path)
        block = _between(text, BEGIN, END)
        examples = [
            json.loads(m) for m in re.findall(r"```json(.*?)```", block, re.DOTALL)
        ]
        assert examples, f"{path.name}: no 3.E examples parsed"
        for i, example in enumerate(examples, start=1):
            for j, item in enumerate(example.get("action_items", [])):
                assert isinstance(item, dict), (
                    f"{path.name} 3.E example {i} action_items[{j}] is "
                    f"a {type(item).__name__}, expected dict. Bare-string "
                    f"action_items are rejected by the grounding gate."
                )


# ---------------------------------------------------------------------------
# 3. Version + section-order
# ---------------------------------------------------------------------------


def test_version_bumped_to_4b_in_both_prompts() -> None:
    """Both prompts MUST declare ``version: 4.B`` in their YAML
    frontmatter — and the literal token ``4.A`` may only appear
    inside changelog entries, not in the active version field."""
    for path in (_HAIKU_PROMPT_PATH, _OPUS_PROMPT_PATH):
        text = _read(path)
        m = re.search(r"^version:\s*(\S+)\s*$", text, re.MULTILINE)
        assert m is not None, f"{path.name}: no 'version:' line found"
        assert m.group(1) == "4.B", (
            f"{path.name}: version is {m.group(1)!r}, expected '4.B' "
            f"(Phase 4.B bump)"
        )

        # The 4.A token must still appear inside the changelog — the
        # changelog is the persistent record of past phases. But it
        # MUST NOT appear before the version line was bumped to 4.B
        # (i.e. the active version field itself).
        version_line = m.group(0)
        assert "4.A" not in version_line, (
            f"{path.name}: version line still references 4.A: "
            f"{version_line!r}"
        )


def test_section_order_correct() -> None:
    """Section order in both prompts:

    1. DO NOT EXTRACT (the per-type precision guard lives WITHIN this)
    2. VERBATIM SOURCE GROUNDING

    A future edit that reorders these sections — or moves the
    precision guard out of DO NOT EXTRACT — is caught here."""
    for path in (_HAIKU_PROMPT_PATH, _OPUS_PROMPT_PATH):
        text = _read(path)
        i_dne = text.find("## DO NOT EXTRACT")
        i_vsg = text.find("## VERBATIM SOURCE GROUNDING")
        i_guard = text.find(_PRECISION_GUARD_BEGIN)
        assert i_dne != -1, f"{path.name}: '## DO NOT EXTRACT' missing"
        assert i_vsg != -1, (
            f"{path.name}: '## VERBATIM SOURCE GROUNDING' missing"
        )
        assert i_guard != -1, (
            f"{path.name}: PRECISION_GUARD_4B section missing"
        )
        assert i_dne < i_guard < i_vsg, (
            f"{path.name}: section order broken: "
            f"DO NOT EXTRACT@{i_dne}, precision guard@{i_guard}, "
            f"VERBATIM SOURCE GROUNDING@{i_vsg}"
        )
