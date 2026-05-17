"""Phase V.5 tests: binding tuple extractor."""
from __future__ import annotations

import json
from typing import Any

import pytest

from spectrum_systems_core.extraction.binding_tuple_extractor import (
    BINDING_TUPLE_FIELDS,
    annotate_decisions,
    binding_tuple_enabled,
    parse_binding_tuple_response,
    render_binding_tuple_prompt,
)


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    monkeypatch.delenv("BINDING_TUPLE_ENABLED", raising=False)


def _approval_decision() -> dict[str, Any]:
    return {
        "decision_id": "d1",
        "decision_text": "NTIA approved the 7 GHz protection criterion.",
        "decision_outcome": "approval",
    }


def _deferral_decision() -> dict[str, Any]:
    return {
        "decision_id": "d2",
        "decision_text": "The methodology question was deferred.",
        "decision_outcome": "deferral",
    }


def test_disabled_by_default_zero_calls_null_tuple() -> None:
    calls: list[str] = []

    def fake(prompt: str) -> str:
        calls.append(prompt)
        return "{}"

    result = annotate_decisions(
        [_approval_decision(), _deferral_decision()], api_caller=fake
    )
    assert result.call_count == 0
    assert calls == []
    for dec in result.decisions:
        assert dec["binding_tuple"] is None
    assert result.findings == []


def test_enabled_with_complete_tuple(monkeypatch) -> None:
    monkeypatch.setenv("BINDING_TUPLE_ENABLED", "true")
    response = json.dumps({
        "actor": "NTIA",
        "action_verb": "approved",
        "object_description": "7 GHz protection criterion",
        "band_or_spectrum_ref": "7 GHz",
        "constraint_or_condition": None,
    })
    result = annotate_decisions(
        [_approval_decision()], api_caller=lambda p: response
    )
    assert result.call_count == 1
    tuple_obj = result.decisions[0]["binding_tuple"]
    assert tuple_obj["actor"] == "NTIA"
    assert tuple_obj["band_or_spectrum_ref"] == "7 GHz"
    # No finding because actor is populated on approval.
    assert result.findings == []


def test_enabled_null_actor_approval_emits_incomplete(monkeypatch) -> None:
    monkeypatch.setenv("BINDING_TUPLE_ENABLED", "true")
    response = json.dumps({
        "actor": None,
        "action_verb": "approved",
        "object_description": "7 GHz criterion",
        "band_or_spectrum_ref": "7 GHz",
        "constraint_or_condition": None,
    })
    result = annotate_decisions(
        [_approval_decision()], api_caller=lambda p: response
    )
    assert result.decisions[0]["binding_tuple"]["actor"] is None
    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.finding_code == "binding_tuple_incomplete"
    assert f.severity == "warn"
    assert f.context["missing_field"] == "actor"
    assert f.context["decision_outcome"] == "approval"


def test_enabled_null_actor_deferral_no_finding(monkeypatch) -> None:
    """Deferral / action_required / noted / question tolerate null actor."""
    monkeypatch.setenv("BINDING_TUPLE_ENABLED", "true")
    response = json.dumps({
        "actor": None,
        "action_verb": "deferred",
        "object_description": "methodology",
        "band_or_spectrum_ref": None,
        "constraint_or_condition": None,
    })
    result = annotate_decisions(
        [_deferral_decision()], api_caller=lambda p: response
    )
    assert result.findings == []


def test_enabled_parse_failure_marks_null_continues_others(monkeypatch) -> None:
    """A parse failure on one decision must not block the others."""
    monkeypatch.setenv("BINDING_TUPLE_ENABLED", "true")

    responses = iter([
        "not valid json",  # decision 1 fails
        json.dumps({
            "actor": "DoD",
            "action_verb": "required",
            "object_description": "submit ERP",
            "band_or_spectrum_ref": None,
            "constraint_or_condition": None,
        }),  # decision 2 succeeds
    ])

    result = annotate_decisions(
        [_approval_decision(), {
            "decision_id": "d3",
            "decision_text": "DoD required to submit ERP.",
            "decision_outcome": "action_required",
        }],
        api_caller=lambda p: next(responses),
    )
    assert result.call_count == 2
    assert result.decisions[0]["binding_tuple"] is None
    assert result.decisions[1]["binding_tuple"]["actor"] == "DoD"
    # Exactly one parse-failed finding.
    codes = [f.finding_code for f in result.findings]
    assert "binding_tuple_parse_failed" in codes
    parse_failed = [f for f in result.findings
                    if f.finding_code == "binding_tuple_parse_failed"]
    assert len(parse_failed) == 1
    # Count of non-null tuples in output.
    non_null = sum(
        1 for d in result.decisions if d["binding_tuple"] is not None
    )
    assert non_null == 1


def test_enabled_model_returns_all_nulls_no_crash(monkeypatch) -> None:
    monkeypatch.setenv("BINDING_TUPLE_ENABLED", "true")
    response = json.dumps({k: None for k in BINDING_TUPLE_FIELDS})
    # Use a deferral so no incomplete finding fires.
    result = annotate_decisions(
        [_deferral_decision()], api_caller=lambda p: response
    )
    tuple_obj = result.decisions[0]["binding_tuple"]
    assert isinstance(tuple_obj, dict)
    for field in BINDING_TUPLE_FIELDS:
        assert tuple_obj[field] is None


def test_enabled_call_count_recorded(monkeypatch) -> None:
    monkeypatch.setenv("BINDING_TUPLE_ENABLED", "true")
    response = json.dumps({k: None for k in BINDING_TUPLE_FIELDS})
    result = annotate_decisions(
        [_deferral_decision(), _deferral_decision()],
        api_caller=lambda p: response,
    )
    assert result.call_count == 2


def test_render_prompt_contains_decision_text() -> None:
    prompt = render_binding_tuple_prompt("foo bar baz")
    assert "foo bar baz" in prompt
    assert "binding tuple" in prompt
    assert "Do not infer" in prompt


def test_parse_strips_markdown_fence() -> None:
    payload = "```json\n" + json.dumps({"actor": "NTIA"}) + "\n```"
    parsed = parse_binding_tuple_response(payload)
    assert parsed is not None
    assert parsed["actor"] == "NTIA"


def test_parse_returns_none_on_garbage() -> None:
    assert parse_binding_tuple_response("not json at all") is None
    assert parse_binding_tuple_response("") is None
    assert parse_binding_tuple_response("[1, 2, 3]") is None  # not a dict


def test_enabled_with_no_api_caller_emits_findings(monkeypatch) -> None:
    """Misconfiguration must be loud, not silent."""
    monkeypatch.setenv("BINDING_TUPLE_ENABLED", "true")
    result = annotate_decisions([_approval_decision()], api_caller=None)
    assert result.call_count == 0
    assert result.decisions[0]["binding_tuple"] is None
    assert any(
        f.finding_code == "binding_tuple_parse_failed"
        for f in result.findings
    )


def test_binding_tuple_enabled_flag_reading(monkeypatch) -> None:
    monkeypatch.setenv("BINDING_TUPLE_ENABLED", "true")
    assert binding_tuple_enabled() is True
    monkeypatch.setenv("BINDING_TUPLE_ENABLED", "")
    assert binding_tuple_enabled() is False
    monkeypatch.setenv("BINDING_TUPLE_ENABLED", "false")
    assert binding_tuple_enabled() is False
