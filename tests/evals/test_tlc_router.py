"""P8-A TLC routing unit tests.

Proves the routing contract end-to-end at the eval layer:

* HIGH_STAKES item routes to the FULL eval set (regulatory_verb +
  within_source + strict_schema + nonempty).
* STANDARD-only payload SKIPS regulatory_verb + nonempty (within_source
  + strict_schema only).
* HIGH_STAKES bad verb → combined block (regulatory_verb fired).
* STANDARD bad enum → combined block (strict_schema still enforced for
  the STANDARD lane).
* STANDARD item with no verb → combined allow (regulatory_verb not
  applicable / skipped).
* Any item not in source → combined PASS with the miss demoted to a
  logged warn (within_source is a measurement instrument, not a trust
  gate — it never blocks, for any type).
* An unknown content array → combined block (never silently routed).
* The lane sets are disjoint AND exactly cover every content array the
  meeting_minutes_llm workflow can emit (no-drift gate).
* The live-LLM workflow stamps provenance.model_id from the registry
  entry, and the routing eval is appended additively (no existing eval
  removed).
"""
from __future__ import annotations

import json
import pathlib

from spectrum_systems_core.artifacts import new_artifact
from spectrum_systems_core.evals import (
    HIGH_STAKES_TYPES,
    STANDARD_TYPES,
    TLC_PAYLOAD_NOT_OBJECT,
    TLC_ROUTED_EVAL_TYPE,
    TLC_SUBEVAL_FAIL_PREFIX,
    TLC_UNKNOWN_TYPE_PREFIX,
    run_tlc_routed_eval,
)
from spectrum_systems_core.evals.tlc_router import _ALL_CONTENT_ARRAYS

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _mk(payload: dict):
    return new_artifact(
        artifact_type="meeting_minutes",
        payload=payload,
        trace_id="trace-test",
        status="draft",
    )


def _base(**overrides) -> dict:
    """A schema-valid 1.0.0 meeting_minutes payload with empty arrays;
    overrides set the array(s) under test."""
    payload = {
        "title": "T",
        "summary": "S",
        "decisions": [],
        "action_items": [],
        "open_questions": [],
        "schema_version": "1.0.0",
        "provenance": {"produced_by": "meeting_minutes_llm"},
    }
    payload.update(overrides)
    return payload


def _run(payload: dict, transcript: str = "short"):
    return run_tlc_routed_eval(_mk(payload), transcript_text=transcript)


# ----------------------------------------------------------------------
# Routing-set contract (no-drift gate referenced by the module docstring)
# ----------------------------------------------------------------------


def test_lane_sets_match_mission_spec():
    # ``risks`` was demoted from HIGH_STAKES to STANDARD: a risk is an
    # analytical observation, not a binding commitment, so a
    # within_source miss there is a logged WARN, not a hard block.
    # ``decisions`` stays HIGH_STAKES with its verbatim hard block.
    assert HIGH_STAKES_TYPES == frozenset(
        {
            "decisions",
            "regulatory_references",
            "technical_parameters",
            "issue_registry_entry",
            "position_statement",
            "dissent_or_objection",
            "precedent_reference",
            "external_stakeholder_input",
        }
    )
    assert "risks" not in HIGH_STAKES_TYPES
    assert STANDARD_TYPES == frozenset(
        {
            "action_items",
            "attendees",
            "scheduled_events",
            "named_artifacts",
            "topics",
            "cross_references",
            "claims",
            "open_questions",
            "commitments",
            "glossary_definition",
            "procedural_ruling",
            "agenda_item",
            "meeting_phases",
            "sentiment_indicators",
            "risks",
        }
    )


def test_lanes_disjoint_and_cover_every_emitted_array():
    # No HIGH_STAKES type can silently fall into STANDARD routing.
    assert not (HIGH_STAKES_TYPES & STANDARD_TYPES)
    # Every content array the workflow can emit is classified.
    from spectrum_systems_core.workflows.meeting_minutes_llm import (
        _LEGACY_ARRAYS,
        _STRUCTURED_ARRAYS,
    )

    emitted = set(_LEGACY_ARRAYS) | set(_STRUCTURED_ARRAYS)
    assert emitted == set(_ALL_CONTENT_ARRAYS)
    assert (HIGH_STAKES_TYPES | STANDARD_TYPES) == emitted


# ----------------------------------------------------------------------
# HIGH_STAKES → full eval set
# ----------------------------------------------------------------------


def test_high_stakes_decision_routes_full_set_and_allows():
    text = "The board adopted the interference threshold."
    art = _base(
        decisions=[{"text": text, "verb": "adopted"}],
    )
    res = _run(art, transcript=text).payload
    assert res["status"] == "pass"
    assert res["routed_high_stakes"] == ["decisions"]
    assert res["routed_standard"] == []
    # Full set ran for the HIGH_STAKES lane.
    for label in (
        "llm_extraction_strict_schema",
        "extraction_within_source_required",
        "regulatory_verb",
        "llm_extraction_nonempty_required",
    ):
        assert label in res["evals_run"], label
    assert res["evals_skipped_standard_only"] == []


def test_high_stakes_bad_verb_blocks():
    text = "The board frobnicated the interference threshold."
    art = _base(decisions=[{"text": text, "verb": "frobnicated"}])
    res = _run(art, transcript=text).payload
    assert res["status"] == "fail"
    assert f"{TLC_SUBEVAL_FAIL_PREFIX}regulatory_verb" in res["reason_codes"]
    assert "decisions" in res["routed_high_stakes"]
    # regulatory_verb DID run for the HIGH_STAKES lane.
    assert "regulatory_verb" in res["evals_run"]


def test_within_source_miss_warns_not_blocks_combined():
    # within_source is a measurement instrument, not a trust gate: a
    # decision whose text is absent from the transcript (good verb, so
    # regulatory_verb passes) is DEMOTED to a logged warn and the
    # combined result still PASSES — it never blocks the run, for any
    # type. The miss is still recorded for the correction miner.
    art = _base(
        decisions=[
            {"text": "A decision that never appears anywhere.", "verb": "adopted"}
        ]
    )
    res = _run(art, transcript="totally unrelated transcript body").payload
    assert res["status"] == "pass"
    assert (
        f"{TLC_SUBEVAL_FAIL_PREFIX}extraction_within_source_required"
        not in res["reason_codes"]
    )
    # The within_source eval still RAN and the miss was demoted, not
    # dropped: the combined result records it for the miner.
    assert "extraction_within_source_required" in res["evals_run"]
    assert res["within_source_demoted"] is True
    assert res["within_source_warn_codes"]


# ----------------------------------------------------------------------
# STANDARD lane → within_source + strict_schema only
# ----------------------------------------------------------------------


def test_standard_only_skips_regulatory_verb_and_nonempty():
    text = "Alice will circulate the revised ERP table before next session."
    art = _base(action_items=[text])
    res = _run(art, transcript=text).payload
    assert res["status"] == "pass"
    assert res["routed_standard"] == ["action_items"]
    assert res["routed_high_stakes"] == []
    assert "regulatory_verb" not in res["evals_run"]
    assert "llm_extraction_nonempty_required" not in res["evals_run"]
    assert res["evals_skipped_standard_only"] == [
        "regulatory_verb",
        "llm_extraction_nonempty_required",
    ]
    # The STANDARD subset DID run.
    assert "llm_extraction_strict_schema" in res["evals_run"]
    assert "extraction_within_source_required" in res["evals_run"]


def test_standard_missing_verb_allows():
    # A scheduled_event (STANDARD) carries no governing verb; because
    # regulatory_verb is not applicable to the STANDARD lane it is
    # skipped and the routed result allows.
    art = _base(
        scheduled_events=[
            {
                "event_id": "ev-1",
                "title": "Next working session",
                "date": "2026-01-15",
            }
        ]
    )
    res = _run(art).payload
    assert res["status"] == "pass"
    assert res["routed_standard"] == ["scheduled_events"]
    assert "regulatory_verb" in res["evals_skipped_standard_only"]


def test_standard_bad_enum_blocks_via_schema():
    # meeting_phases.phase_name is an enum. A STANDARD-lane item with a
    # schema-invalid value still blocks — strict_schema is enforced for
    # the STANDARD lane, regulatory_verb is NOT run.
    art = _base(
        schema_version="1.2.0",
        meeting_phases=[{"phase_id": "p1", "phase_name": "lunch"}],
    )
    res = _run(art).payload
    assert res["status"] == "fail"
    assert (
        f"{TLC_SUBEVAL_FAIL_PREFIX}llm_extraction_strict_schema"
        in res["reason_codes"]
    )
    assert "meeting_phases" in res["routed_standard"]
    assert "regulatory_verb" not in res["evals_run"]


# ----------------------------------------------------------------------
# Fail-closed: unknown type / non-object payload
# ----------------------------------------------------------------------


def test_unknown_content_array_blocks():
    art = _base()
    art["totally_unknown_array"] = [{"x": 1}]
    res = _run(art).payload
    assert res["status"] == "fail"
    assert (
        f"{TLC_UNKNOWN_TYPE_PREFIX}totally_unknown_array"
        in res["reason_codes"]
    )
    assert "totally_unknown_array" in res["unknown_types"]


def test_non_object_payload_blocks():
    art = new_artifact(
        artifact_type="meeting_minutes",
        payload={},
        trace_id="trace-test",
        status="draft",
    )
    # Force a non-dict payload past the constructor.
    object.__setattr__(art, "payload", ["not", "a", "dict"])
    res = run_tlc_routed_eval(art, transcript_text="x").payload
    assert res["status"] == "fail"
    assert TLC_PAYLOAD_NOT_OBJECT in res["reason_codes"]


def test_combined_result_is_single_eval_result_decide_control_reads():
    # The combined artifact is ONE eval_result with the routing
    # eval_type so decide_control reads it unchanged.
    art = _base(decisions=[{"text": "x adopted y", "verb": "adopted"}])
    out = run_tlc_routed_eval(_mk(art), transcript_text="x adopted y")
    assert out.artifact_type == "eval_result"
    assert out.payload["eval_type"] == TLC_ROUTED_EVAL_TYPE
    assert out.payload["status"] in {"pass", "fail"}


# ----------------------------------------------------------------------
# Change 1 — model_id flows registry → workflow → artifact provenance
# ----------------------------------------------------------------------


def _registry_model_id() -> str:
    reg = json.loads(
        (_REPO_ROOT / "ai" / "registry" / "model_registry.json").read_text(
            encoding="utf-8"
        )
    )
    return reg["meeting_minutes_extraction"]["model_id"]


def test_registry_entry_pins_sonnet():
    assert _registry_model_id() == "claude-sonnet-4-6"


def test_workflow_stamps_registry_model_id_and_promotes():
    from spectrum_systems_core.workflows import (
        run_meeting_minutes_llm_workflow,
    )
    from tests.llm_stub import (
        DEC18_ACTION_ITEMS,
        DEC18_DECISIONS,
        DEC18_OPEN_QUESTIONS,
        DEC18_TECHNICAL_PARAMETERS,
        json_stub,
        load_fixture,
    )

    dec18 = load_fixture("dec18_transcript.txt")
    result = run_meeting_minutes_llm_workflow(
        dec18,
        client=json_stub(
            decisions=DEC18_DECISIONS,
            action_items=DEC18_ACTION_ITEMS,
            open_questions=DEC18_OPEN_QUESTIONS,
            technical_parameters=DEC18_TECHNICAL_PARAMETERS,
        ),
        meeting_id="m-7ghz-20251218",
    )
    prov = result.meeting_minutes.payload["provenance"]
    # Gate: model_id in the produced artifact == the registry entry.
    assert prov["model_id"] == _registry_model_id() == "claude-sonnet-4-6"
    assert prov["produced_by"] == "meeting_minutes_llm"
    # Happy path still promotes with the router added (additive).
    assert result.promoted is True
    assert result.control_decision.payload["decision"] == "allow"


def test_registry_model_reaches_the_api_call(monkeypatch):
    """registry → workflow → API call: with no injected client the real
    AnthropicJSONClient is constructed from the registry, so the model
    string the SDK receives is exactly the registry entry (and the
    artifact's provenance.model_id matches it)."""
    import sys
    import types

    from spectrum_systems_core.workflows import (
        run_meeting_minutes_llm_workflow,
    )
    from tests.llm_stub import (
        DEC18_ACTION_ITEMS,
        DEC18_DECISIONS,
        DEC18_OPEN_QUESTIONS,
        DEC18_TECHNICAL_PARAMETERS,
        load_fixture,
    )

    captured: dict = {}
    payload = {
        "decisions": list(DEC18_DECISIONS),
        "action_items": list(DEC18_ACTION_ITEMS),
        "open_questions": list(DEC18_OPEN_QUESTIONS),
        "technical_parameters": list(DEC18_TECHNICAL_PARAMETERS),
    }

    class _Content:
        text = json.dumps(payload)

    class _Message:
        content = [_Content()]
        stop_reason = "end_turn"

    class _Messages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return _Message()

    class _Anthropic:
        messages = _Messages()

        def __init__(self):
            pass

    fake = types.ModuleType("anthropic")
    fake.Anthropic = _Anthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    dec18 = load_fixture("dec18_transcript.txt")
    result = run_meeting_minutes_llm_workflow(dec18, meeting_id="m-api")

    # registry → workflow → API call: the SDK received exactly the
    # registry model string …
    assert captured["model"] == _registry_model_id() == "claude-sonnet-4-6"
    # … and the same string was stamped into the artifact provenance
    # (promotion on the real-client path needs model-emitted grounding,
    # which this minimal fake SDK does not produce; the json_stub-based
    # happy-path promotion is covered by
    # test_workflow_stamps_registry_model_id_and_promotes).
    assert (
        result.meeting_minutes.payload["provenance"]["model_id"]
        == captured["model"]
    )


def test_routing_eval_appended_additively_no_existing_eval_removed():
    from spectrum_systems_core.workflows import (
        run_meeting_minutes_llm_workflow,
    )
    from tests.llm_stub import (
        DEC18_ACTION_ITEMS,
        DEC18_DECISIONS,
        DEC18_OPEN_QUESTIONS,
        DEC18_TECHNICAL_PARAMETERS,
        json_stub,
        load_fixture,
    )

    dec18 = load_fixture("dec18_transcript.txt")
    result = run_meeting_minutes_llm_workflow(
        dec18,
        client=json_stub(
            decisions=DEC18_DECISIONS,
            action_items=DEC18_ACTION_ITEMS,
            open_questions=DEC18_OPEN_QUESTIONS,
            technical_parameters=DEC18_TECHNICAL_PARAMETERS,
        ),
    )
    eval_types = {
        e.payload.get("eval_type") for e in result.eval_results
    }
    # Every pre-existing LLM eval still present …
    for required in (
        "llm_extraction_strict_schema",
        "llm_extraction_nonempty_required",
        "extraction_within_source_required",
        "extraction_vs_human_minutes_coverage",
        "regulatory_verb",
    ):
        assert required in eval_types, required
    # … AND the routing eval is added on top.
    assert TLC_ROUTED_EVAL_TYPE in eval_types
