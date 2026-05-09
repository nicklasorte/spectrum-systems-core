"""RT4-002 / RT5-002: assert no free-form AI prompts live outside the registry.

Phase H requires that every AI prompt is constructed from
ai/registry/prompts.json via PromptRegistry. This test scans the
spectrum_systems_core.ai package for prompt-construction smells. Other
phases (paper, agency, synthesis) have their own internal prompts and
are explicitly out of scope here — they predate Phase H and use direct
API calls for non-memory tasks (see CLAUDE.md / report_generator).
"""
from __future__ import annotations

import re
from pathlib import Path


_AI_PACKAGE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "spectrum_systems_core"
    / "ai"
)

_PROMPT_SMELL = re.compile(
    r"\.format(_map)?\s*\(.*\{(question|context)\}", re.DOTALL
)


def test_no_freeform_prompts_in_ai_package():
    """Within the Phase H package, only prompt_registry.py may render prompts.

    RT5-002 + RT4-002 + CHECK-RT1-005: anything calling
    str.format()/format_map() with {question}/{context} placeholders
    outside prompt_registry.py is a free-form prompt and a violation.
    """
    flagged = []
    for path in _AI_PACKAGE.rglob("*.py"):
        if path.name in ("prompt_registry.py",):
            continue
        text = path.read_text(encoding="utf-8")
        if _PROMPT_SMELL.search(text):
            flagged.append(str(path))
    assert not flagged, (
        "Free-form prompt construction found outside prompt_registry.py: "
        + ", ".join(flagged)
    )


def test_only_registry_provides_prompt_templates():
    """The literal substring `{context}` must only appear in the AI package
    via the registry JSON or the registry loader/render path.
    """
    flagged = []
    for path in _AI_PACKAGE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "{context}" in text and path.name not in ("prompt_registry.py",):
            flagged.append(str(path))
    assert not flagged, (
        "Hard-coded {context} placeholder outside prompt_registry.py: "
        + ", ".join(flagged)
    )
