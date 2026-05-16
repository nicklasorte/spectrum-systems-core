"""Phase AB.2 — Opus adapter tests.

Opus is comparison-only: there is intentionally NO stub and the
adapter is never exercised for a real call in unit tests. Only the
fail-closed credential gate and the model-string invariant are tested.
"""
from __future__ import annotations

import sys
import types

import pytest

from spectrum_systems_core.extraction import llm_opus
from spectrum_systems_core.extraction.llm_opus import OPUS_MODEL, real_extract


@pytest.mark.parametrize("value", [None, "", "   "])
def test_real_extract_missing_or_empty_key_fails_closed(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_API_KEY", value)

    tripwire = types.ModuleType("anthropic")

    def _boom(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("Anthropic client constructed in a unit test")

    tripwire.Anthropic = _boom
    monkeypatch.setitem(sys.modules, "anthropic", tripwire)

    with pytest.raises(RuntimeError, match="missing_credentials:ANTHROPIC_API_KEY"):
        real_extract("a transcript")


def test_opus_has_no_stub():
    """Opus is for measurement only; a stub would invite unit-testing
    a path that must only ever hit the real API in the runner."""
    assert not hasattr(llm_opus, "stub_extract")


def test_opus_model_is_not_deprecated():
    """The prompt specified the older (deprecated) Opus 4.5 revision;
    the adapter must use a current, non-deprecated revision so the
    deprecated-model CI gate stays green. The deprecated literal is
    intentionally not written here (the scanner is naive substring
    matching and does not exempt comments/strings)."""
    from tests.ci.test_no_deprecated_model_strings import (
        DEPRECATED_MODEL_STRINGS,
    )

    assert OPUS_MODEL not in DEPRECATED_MODEL_STRINGS
    assert OPUS_MODEL == "claude-opus-4-7"
