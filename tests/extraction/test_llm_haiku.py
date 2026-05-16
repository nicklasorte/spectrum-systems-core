"""Phase AB.1 — Haiku adapter tests.

No real API call is ever made here: the stub path is pure, and the
real path is only exercised for its fail-closed credential gate with
a mocked ``anthropic`` module that raises if a client is constructed.
"""
from __future__ import annotations

import sys
import types

import pytest

from spectrum_systems_core.extraction import llm_haiku
from spectrum_systems_core.extraction.llm_haiku import (
    HAIKU_MODEL,
    REQUIRED_OUTPUT_KEYS,
    HaikuExtractionResult,
    real_extract,
    stub_extract,
)


def test_stub_returns_expected_structure():
    res = stub_extract("anything")
    assert isinstance(res, HaikuExtractionResult)
    assert set(res.output.keys()) == set(REQUIRED_OUTPUT_KEYS)
    assert res.output["decisions"][0]["verb"] == "approved"
    assert res.output["actions"][0]["owner"] == "stub-owner"
    assert res.output["questions"][0]["text"].endswith("?")
    assert res.cost_usd == 0.0
    assert res.latency_ms == 0
    assert res.model == "stub"


def test_stub_is_deterministic_across_calls():
    a = stub_extract("input one")
    b = stub_extract("different input")
    c = stub_extract("input one")
    assert a.output == b.output == c.output
    assert a.raw_response == b.raw_response == c.raw_response


def test_stub_output_matches_prompt_schema():
    """Every list carries the keys the system prompt's JSON schema
    declares (decisions: text/verb/source_turns, actions:
    text/owner/source_turns, questions: text/source_turns)."""
    out = stub_extract("x").output
    d = out["decisions"][0]
    assert {"text", "verb", "source_turns"} <= set(d)
    a = out["actions"][0]
    assert {"text", "owner", "source_turns"} <= set(a)
    q = out["questions"][0]
    assert {"text", "source_turns"} <= set(q)
    for coll in out.values():
        for item in coll:
            assert isinstance(item["source_turns"], list)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_real_extract_missing_or_empty_key_fails_closed(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_API_KEY", value)

    # Tripwire: importing/constructing the SDK in a unit test is a bug.
    tripwire = types.ModuleType("anthropic")

    def _boom(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("Anthropic client constructed in a unit test")

    tripwire.Anthropic = _boom
    monkeypatch.setitem(sys.modules, "anthropic", tripwire)

    with pytest.raises(RuntimeError, match="missing_credentials:ANTHROPIC_API_KEY"):
        real_extract("[t0001] CHAIR: hello")


def test_real_extract_does_not_touch_network_in_unit_tests(monkeypatch):
    """Even with a key set, the unit test proves no network call by
    making the mocked client raise — the test asserts the call path
    reaches the client and then stops (we never assert success)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    calls = {"constructed": 0}

    class _FakeClient:
        def __init__(self, *a, **k):
            calls["constructed"] += 1

        class messages:  # noqa: N801
            @staticmethod
            def create(*a, **k):
                raise RuntimeError("network blocked in unit test")

    fake = types.ModuleType("anthropic")
    fake.Anthropic = _FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    with pytest.raises(RuntimeError, match="network blocked"):
        real_extract("[t0001] CHAIR: hello")
    assert calls["constructed"] == 1


def test_haiku_model_is_not_deprecated():
    """The Haiku revision must not be a deprecated string and must
    track the live workflow's EXTRACTION_MODEL (single source)."""
    from spectrum_systems_core.workflows.llm_client import EXTRACTION_MODEL
    from tests.ci.test_no_deprecated_model_strings import (
        DEPRECATED_MODEL_STRINGS,
    )

    assert HAIKU_MODEL == EXTRACTION_MODEL
    assert HAIKU_MODEL not in DEPRECATED_MODEL_STRINGS


def test_real_extract_rejects_wrong_shape_json(monkeypatch):
    """Valid JSON but missing the required list keys must fail closed
    (HaikuOutputError) — never reported as a zero-item success."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")

    class _Block:
        text = '{"decisions": []}'  # missing actions + questions

    class _Usage:
        input_tokens = 10
        output_tokens = 5

    class _Resp:
        content = [_Block()]
        usage = _Usage()

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        class messages:  # noqa: N801
            @staticmethod
            def create(*a, **k):
                return _Resp()

    fake = types.ModuleType("anthropic")
    fake.Anthropic = _FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    with pytest.raises(llm_haiku.HaikuOutputError, match="missing_keys"):
        real_extract("[t0001] x")
