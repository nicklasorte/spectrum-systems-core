"""Phase 5 — model_selection resolver tests.

Covers the four `--model` tokens, the prompt-variant enum, the
Opus-prompt fail-closed branch, and the model-strings dict's
exhaustiveness against the schema.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from spectrum_systems_core.pipeline.governed_run import (
    ALL_PROMPT_VARIANTS,
    PROMPT_VARIANT_HAIKU_PROMPT_WITH_SONNET,
    PROMPT_VARIANT_OPUS_BASELINE,
    PROMPT_VARIANT_OPUS_PROMPT_WITH_SONNET,
    PROMPT_VARIANT_PRODUCTION_HAIKU,
)
from spectrum_systems_core.workflows.model_selection import (
    ALL_MODEL_TOKENS,
    HAIKU_PROMPT_PATH,
    MODEL_STRINGS,
    MODEL_TOKEN_HAIKU,
    MODEL_TOKEN_OPUS,
    MODEL_TOKEN_SONNET,
    MODEL_TOKEN_SONNET_UNCONSTRAINED,
    ModelSelectionError,
    OPUS_PROMPT_PATH,
    resolve_model_selection,
)


def test_haiku_resolves_to_production_haiku() -> None:
    sel = resolve_model_selection(MODEL_TOKEN_HAIKU)
    assert sel.model_id == "claude-haiku-4-7"
    assert sel.prompt_path == HAIKU_PROMPT_PATH
    assert sel.prompt_variant == PROMPT_VARIANT_PRODUCTION_HAIKU


def test_sonnet_uses_haiku_prompt() -> None:
    """`--model sonnet` is apples-to-apples: Haiku prompt + Sonnet model."""
    sel = resolve_model_selection(MODEL_TOKEN_SONNET)
    assert sel.model_id == "claude-sonnet-4-6"
    assert sel.prompt_path == HAIKU_PROMPT_PATH
    assert sel.prompt_variant == PROMPT_VARIANT_HAIKU_PROMPT_WITH_SONNET


def test_sonnet_unconstrained_requires_opus_prompt(tmp_path, monkeypatch) -> None:
    """`--model sonnet-unconstrained` fails fail-closed when Opus prompt is missing."""
    # Point the resolver at a non-existent prompt file by monkey-patching
    # the module-level constant.
    from spectrum_systems_core.workflows import model_selection as ms

    missing = tmp_path / "absent_meeting_minutes_opus.md"
    monkeypatch.setattr(ms, "OPUS_PROMPT_PATH", missing)
    with pytest.raises(ModelSelectionError) as exc_info:
        resolve_model_selection(MODEL_TOKEN_SONNET_UNCONSTRAINED)
    assert exc_info.value.reason_code == "opus_prompt_not_found_for_sonnet_unconstrained"


def test_sonnet_unconstrained_succeeds_when_opus_prompt_exists(
    tmp_path, monkeypatch
) -> None:
    from spectrum_systems_core.workflows import model_selection as ms

    stub_prompt = tmp_path / "meeting_minutes_opus.md"
    stub_prompt.write_text("# stub Opus prompt\n", encoding="utf-8")
    monkeypatch.setattr(ms, "OPUS_PROMPT_PATH", stub_prompt)
    sel = resolve_model_selection(MODEL_TOKEN_SONNET_UNCONSTRAINED)
    assert sel.model_id == "claude-sonnet-4-6"
    assert sel.prompt_path == stub_prompt
    assert sel.prompt_variant == PROMPT_VARIANT_OPUS_PROMPT_WITH_SONNET


def test_opus_requires_opus_prompt(tmp_path, monkeypatch) -> None:
    from spectrum_systems_core.workflows import model_selection as ms

    missing = tmp_path / "absent.md"
    monkeypatch.setattr(ms, "OPUS_PROMPT_PATH", missing)
    with pytest.raises(ModelSelectionError) as exc_info:
        resolve_model_selection(MODEL_TOKEN_OPUS)
    assert exc_info.value.reason_code == "opus_prompt_not_found"


def test_unknown_token_rejected() -> None:
    with pytest.raises(ModelSelectionError) as exc_info:
        resolve_model_selection("claude-3.5-sonnet")  # not in the enum
    assert exc_info.value.reason_code == "unknown_model_token"


def test_model_tokens_are_exhaustive() -> None:
    """Every CLI token must appear in MODEL_STRINGS."""
    assert set(MODEL_STRINGS) == set(ALL_MODEL_TOKENS)


def test_prompt_variants_match_schema() -> None:
    """ALL_PROMPT_VARIANTS must match the enum in meeting_minutes.schema.json."""
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "spectrum_systems_core"
        / "schemas"
        / "meeting_minutes.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    enum = (
        schema["properties"]["provenance"]["properties"]["extraction_config"]
        ["properties"]["prompt_variant"]["enum"]
    )
    assert set(enum) == ALL_PROMPT_VARIANTS, (
        f"prompt_variant enum drifted: schema={set(enum)} vs "
        f"code={ALL_PROMPT_VARIANTS}"
    )


def test_resolver_returns_one_of_four_prompt_variants(tmp_path, monkeypatch) -> None:
    """Every resolved model_token sets a prompt_variant from the enum."""
    from spectrum_systems_core.workflows import model_selection as ms

    stub = tmp_path / "stub_opus.md"
    stub.write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(ms, "OPUS_PROMPT_PATH", stub)

    seen = set()
    for tok in ALL_MODEL_TOKENS:
        sel = resolve_model_selection(tok)
        assert sel.prompt_variant in ALL_PROMPT_VARIANTS
        seen.add(sel.prompt_variant)
    # All four prompt variants are reachable from the four tokens
    # (sonnet-unconstrained ↔ opus_prompt_with_sonnet_model; opus ↔
    # opus_baseline; sonnet ↔ haiku_prompt_with_sonnet_model; haiku ↔
    # production_haiku).
    assert seen == ALL_PROMPT_VARIANTS
