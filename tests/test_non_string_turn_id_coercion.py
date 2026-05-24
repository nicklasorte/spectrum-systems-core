"""Regression: non_string_turn_id — integer turn IDs must be coerced to
strings before the strict-schema and source_turn_validity gates run.

The LLM occasionally emits bare integers for turn ID fields the schema
declares as ``string`` (or ``["string", "null"]``):

* ``meeting_phases[*].start_turn_id`` / ``end_turn_id``
* ``agenda_item[*].start_turn_id`` / ``end_turn_id``
* ``sentiment_indicators[*].turn_id``
* ``grounding[*].source_turns[*]`` (string per source_turn_validity)

The Run #4 failure on ``run-cascade-filter.yml`` blocked with three
parallel reason codes: ``failed:llm_extraction_strict_schema``,
``failed:tlc_routed_extraction``, ``failed:source_turn_validity``,
with ``schema_violation:... path=['meeting_phases', 0, 'start_turn_id']``
and ``source_turn_unresolved:grounding[N]:non_string_turn_id`` (repeated
across items). The defence-in-depth fix is a coercion guard in
``_parse_llm_payload``: integer leaves on these documented paths become
strings, with a stderr warning per coercion.

Trust properties defended here:

1. The parser's coercion converts ``"start_turn_id": 76`` -> ``"76"`` on
   meeting_phases, agenda_item, sentiment_indicators.
2. The coercion converts ``"source_turns": [76, 77]`` -> ``["76", "77"]``
   on grounding entries.
3. The coercion writes a ``WARN: coerced integer turn_id to string at
   path=[...]`` line to stderr — the drift remains auditable even on a
   promoting run.
4. The end-to-end workflow with a stubbed LLM emitting integer turn
   IDs no longer fails the strict-schema or source_turn_validity gates.
5. The coercion is narrow: it does NOT touch ``source_turn_ids``
   arrays (those are declared ``items: integer`` in
   meeting_minutes.schema.json) and does NOT touch unrelated integer
   fields.
"""
from __future__ import annotations

import json

from spectrum_systems_core.workflows.meeting_minutes_llm import (
    _coerce_int_turn_ids_to_string,
    _parse_llm_payload,
)


# ---- Unit: the coercion helper itself ----------------------------------


def test_non_string_turn_id_in_meeting_phases_is_coerced(capsys):
    payload = {
        "meeting_phases": [
            {
                "phase_id": "p1",
                "phase_name": "opening",
                "start_turn_id": 76,
                "end_turn_id": 84,
            }
        ]
    }
    _coerce_int_turn_ids_to_string(payload)
    assert payload["meeting_phases"][0]["start_turn_id"] == "76"
    assert payload["meeting_phases"][0]["end_turn_id"] == "84"
    err = capsys.readouterr().err
    assert "non_string_turn_id" not in err  # uses path= form, not reason code
    assert "path=['meeting_phases', 0, 'start_turn_id']" in err
    assert "path=['meeting_phases', 0, 'end_turn_id']" in err


def test_non_string_turn_id_in_agenda_item_is_coerced():
    payload = {
        "agenda_item": [
            {
                "item_id": "ag-1",
                "title": "Item",
                "start_turn_id": 12,
                "end_turn_id": None,  # null stays null
            }
        ]
    }
    _coerce_int_turn_ids_to_string(payload)
    assert payload["agenda_item"][0]["start_turn_id"] == "12"
    assert payload["agenda_item"][0]["end_turn_id"] is None


def test_non_string_turn_id_in_sentiment_indicators_is_coerced():
    payload = {
        "sentiment_indicators": [
            {
                "turn_id": 42,
                "speaker": "DoD Rep",
                "sentiment": "concern",
                "text_preview": "preview",
            }
        ]
    }
    _coerce_int_turn_ids_to_string(payload)
    assert payload["sentiment_indicators"][0]["turn_id"] == "42"


def test_non_string_turn_id_in_grounding_source_turns_is_coerced(capsys):
    payload = {
        "grounding": [
            {"kind": "decision", "text": "x", "source_turns": [76, 77]},
            {"kind": "action_item", "text": "y", "source_turns": ["t0007"]},
        ]
    }
    _coerce_int_turn_ids_to_string(payload)
    assert payload["grounding"][0]["source_turns"] == ["76", "77"]
    # Already-string list is left alone.
    assert payload["grounding"][1]["source_turns"] == ["t0007"]
    err = capsys.readouterr().err
    assert "path=['grounding', 0, 'source_turns', 0]" in err
    assert "path=['grounding', 0, 'source_turns', 1]" in err


def test_coercion_skips_already_string_values(capsys):
    payload = {
        "meeting_phases": [
            {
                "phase_id": "p1",
                "phase_name": "opening",
                "start_turn_id": "76",
                "end_turn_id": None,
            }
        ],
        "grounding": [
            {"kind": "decision", "text": "x", "source_turns": ["t0007"]},
        ],
    }
    _coerce_int_turn_ids_to_string(payload)
    # No coercion, no warnings.
    assert capsys.readouterr().err == ""
    assert payload["meeting_phases"][0]["start_turn_id"] == "76"
    assert payload["meeting_phases"][0]["end_turn_id"] is None
    assert payload["grounding"][0]["source_turns"] == ["t0007"]


def test_coercion_does_not_touch_source_turn_ids_integer_array():
    """source_turn_ids on turn-aggregate items is schema-typed
    integer (meeting_minutes.schema.json). The coercion guard MUST
    NOT convert these to strings — that would corrupt the schema
    contract. Only the singular *.turn_id fields and grounding's
    source_turns are coerced."""
    payload = {
        "meeting_phases": [
            {
                "phase_id": "p1",
                "phase_name": "opening",
                "source_turn_ids": [7, 8, 9],
            }
        ]
    }
    _coerce_int_turn_ids_to_string(payload)
    assert payload["meeting_phases"][0]["source_turn_ids"] == [7, 8, 9]


def test_coercion_skips_bool_values():
    """``True`` / ``False`` are int subclasses in Python. They MUST
    NOT be coerced to ``"True"`` / ``"False"`` — a non-int, non-string
    value is left for the strict-schema eval to reject loudly."""
    payload = {
        "meeting_phases": [
            {
                "phase_id": "p1",
                "phase_name": "opening",
                "start_turn_id": True,
                "end_turn_id": False,
            }
        ]
    }
    _coerce_int_turn_ids_to_string(payload)
    assert payload["meeting_phases"][0]["start_turn_id"] is True
    assert payload["meeting_phases"][0]["end_turn_id"] is False


# ---- Integration: _parse_llm_payload applies the coercion --------------


def test_parse_llm_payload_coerces_integer_turn_ids_end_to_end(capsys):
    """Simulates the exact run #4 failure shape: meeting_phases with
    integer start_turn_id/end_turn_id and grounding with integer
    source_turns. After parse, every leaf is a string."""
    raw = json.dumps(
        {
            "decisions": ["a decision"],
            "action_items": [{"action": "an action"}],
            "open_questions": ["a question"],
            "meeting_phases": [
                {
                    "phase_id": "p1",
                    "phase_name": "opening",
                    "start_turn_id": 76,
                    "end_turn_id": 84,
                }
            ],
            "sentiment_indicators": [
                {
                    "turn_id": 12,
                    "speaker": "X",
                    "sentiment": "concern",
                    "text_preview": "preview",
                }
            ],
            "grounding": [
                {"kind": "decision", "text": "a decision", "source_turns": [76]},
            ],
        }
    )
    parsed = _parse_llm_payload(raw)
    assert parsed is not None
    assert parsed["meeting_phases"][0]["start_turn_id"] == "76"
    assert parsed["meeting_phases"][0]["end_turn_id"] == "84"
    assert parsed["sentiment_indicators"][0]["turn_id"] == "12"
    assert parsed["grounding"][0]["source_turns"] == ["76"]
    err = capsys.readouterr().err
    assert "WARN: coerced integer turn_id to string" in err
