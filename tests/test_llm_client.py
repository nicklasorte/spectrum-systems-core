"""Shared AnthropicJSONClient transport contract.

Pins the meeting_minutes_llm production-block fix:

- The default output budget is sized for the full schema_version 1.3.0
  extraction (~24 structured arrays). The old 4000 default silently
  truncated a full-transcript response into invalid JSON, which
  _parse_llm_payload turned into None and the producer into a
  no-arrays base payload -> required_meeting_minutes_fields +
  regulatory_verb + llm_extraction_strict_schema all failed together.
- A response cut off at the token cap (stop_reason == "max_tokens")
  now fails LOUD with LLMClientError instead of silently. This does
  NOT change the promote/block outcome (a truncated response blocks
  either way); it makes the failure self-explaining on the debug
  artifact (CLAUDE.md auto-debug rule). Fail-closed is preserved.
- A normal completion is unchanged.
"""
from __future__ import annotations

import sys
import types

import pytest

from spectrum_systems_core.workflows import llm_client


def _fake_anthropic(stop_reason, text='{"decisions": []}'):
    captured: dict = {}

    class _Content:
        def __init__(self) -> None:
            self.text = text

    class _Message:
        content = [_Content()]

    _Message.stop_reason = stop_reason

    class _Messages:
        def create(self, **kw: object) -> "_Message":
            captured.update(kw)
            return _Message()

    class _Anthropic:
        messages = _Messages()

        def __init__(self) -> None:
            pass

    mod = types.ModuleType("anthropic")
    mod.Anthropic = _Anthropic  # type: ignore[attr-defined]
    return mod, captured


def test_default_max_tokens_sized_for_full_extraction():
    # 16384 mirrors the in-repo Opus precedent
    # (create_opus_reference_baselines._OPUS_MAX_TOKENS) for the SAME
    # full-extraction schema; 4000 truncated it into invalid JSON.
    assert llm_client._MAX_TOKENS == 16384
    assert llm_client.AnthropicJSONClient()._max_tokens == 16384


def test_truncated_response_fails_loud_not_silent(monkeypatch):
    mod, _ = _fake_anthropic("max_tokens")
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    with pytest.raises(llm_client.LLMClientError) as exc:
        llm_client.AnthropicJSONClient()(system="s", user="u")
    # machine-grepable, self-explaining cause on the debug artifact
    assert "llm_output_truncated:max_tokens" in str(exc.value)


def test_normal_completion_unchanged_and_budget_reaches_sdk(monkeypatch):
    mod, captured = _fake_anthropic("end_turn", text='{"decisions": []}')
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    out = llm_client.AnthropicJSONClient()(system="s", user="u")
    assert out == '{"decisions": []}'
    assert captured["max_tokens"] == 16384


def test_missing_stop_reason_does_not_raise(monkeypatch):
    # A transport whose message has no stop_reason attribute must behave
    # exactly as before (getattr default None -> no truncation raise) so
    # the change is additive for any non-conforming fake SDK.
    class _Content:
        text = '{"decisions": []}'

    class _Message:
        content = [_Content()]  # NOTE: no stop_reason attribute

    class _Messages:
        def create(self, **kw: object) -> "_Message":
            return _Message()

    class _Anthropic:
        messages = _Messages()

        def __init__(self) -> None:
            pass

    mod = types.ModuleType("anthropic")
    mod.Anthropic = _Anthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    assert llm_client.AnthropicJSONClient()(system="s", user="u") == (
        '{"decisions": []}'
    )
