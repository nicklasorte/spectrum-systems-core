"""Tests for spectrum_systems_core.ai.prompt_registry."""
from __future__ import annotations

import json

import pytest

from spectrum_systems_core.ai.prompt_registry import PromptRegistry

from ._fixtures import setup_phase_h_repo


def test_registry_loads_successfully(tmp_path):
    setup_phase_h_repo(tmp_path)
    PromptRegistry().load(str(tmp_path))
    types = PromptRegistry().list_task_types(str(tmp_path))
    assert {"memory_query", "claim_check", "objection_check", "story_fit"} <= set(types)


def test_unknown_task_type_raises(tmp_path):
    setup_phase_h_repo(tmp_path)
    PromptRegistry().load(str(tmp_path))
    with pytest.raises(ValueError) as exc:
        PromptRegistry().get("does_not_exist")
    assert "unregistered_task_type" in str(exc.value)


def test_all_templates_have_question_and_context_placeholders(tmp_path):
    """RT1-001: every template must have both placeholders."""
    setup_phase_h_repo(tmp_path)
    PromptRegistry().load(str(tmp_path))
    for name in PromptRegistry().list_task_types():
        task = PromptRegistry().get(name)
        assert "{question}" in task["prompt_template"]
        assert "{context}" in task["prompt_template"]


def test_temperature_zero_enforced_at_load(tmp_path):
    """Loading must fail if any task has temperature != 0."""
    setup_phase_h_repo(tmp_path)
    registry_path = tmp_path / "ai" / "registry" / "prompts.json"
    doc = json.loads(registry_path.read_text(encoding="utf-8"))
    doc["task_types"]["memory_query"]["temperature"] = 0.1
    registry_path.write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    PromptRegistry.reset_cache()
    with pytest.raises(ValueError) as exc:
        PromptRegistry().load(str(tmp_path))
    assert "temperature" in str(exc.value)


def test_render_prompt_substitutes_correctly(tmp_path):
    setup_phase_h_repo(tmp_path)
    PromptRegistry().load(str(tmp_path))
    prompt = PromptRegistry().render_prompt(
        "memory_query", "What is X?", "ctx-text-here"
    )
    assert "What is X?" in prompt
    assert "ctx-text-here" in prompt
    # The literal {{ ... }} from the JSON template should now render as { ... }.
    assert "{\"answer\":" in prompt


def test_registry_cached_after_first_load(tmp_path):
    setup_phase_h_repo(tmp_path)
    PromptRegistry().load(str(tmp_path))
    assert PromptRegistry._registry is not None
    # A second .load() with the same root is a no-op (cache hit).
    PromptRegistry().load(str(tmp_path))
    assert PromptRegistry._registry is not None


def test_template_missing_placeholder_blocks_load(tmp_path):
    """RT1-001: template missing {context} or {question} fails at load time."""
    setup_phase_h_repo(tmp_path)
    registry_path = tmp_path / "ai" / "registry" / "prompts.json"
    doc = json.loads(registry_path.read_text(encoding="utf-8"))
    doc["task_types"]["memory_query"]["prompt_template"] = "no placeholders here"
    registry_path.write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    PromptRegistry.reset_cache()
    with pytest.raises(ValueError) as exc:
        PromptRegistry().load(str(tmp_path))
    msg = str(exc.value)
    assert "{context}" in msg or "{question}" in msg
