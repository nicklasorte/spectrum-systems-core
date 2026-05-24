"""Unit tests for the deterministic human-minutes parser.

The parser MUST be deterministic, MUST NOT call any LLM, and MUST
produce a schema-valid ``human_minutes`` artifact when wrapped via
``scripts/ingest_human_minutes.parsed_to_artifact``.

These tests exercise:
- The Dec 18 fixture (one discussion row, three action items, one next step).
- The Jan 22 fixture (variable column count: a "Slide Ref." column).
- A minutes file with no Discussion/Questions Log section.
- ``N/A`` follow-up handling.
- Determinism (re-parse yields identical output).
- Spacer-row skipping (rows whose cells are all the same).
- Schema validation of the parsed artifact.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import jsonschema
import pytest

from spectrum_systems_core.workflows.minutes_parser import (
    ActionItem,
    DiscussionItem,
    ParsedMinutes,
    parse_minutes_text,
    parse_minutes_txt,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "human_minutes"
DEC18 = FIXTURE_DIR / "dec18_minutes.txt"
JAN22 = FIXTURE_DIR / "jan22_minutes.txt"
NO_DISCUSSION = FIXTURE_DIR / "no_discussion_minutes.txt"

SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "src" / "spectrum_systems_core" / "schemas"
    / "human_minutes.schema.json"
)


def test_parse_dec18_minutes_discussion_items():
    parsed = parse_minutes_txt(DEC18)
    assert len(parsed.discussion_items) == 1
    item = parsed.discussion_items[0]
    assert item.category == "Scope / Geography"
    assert "US&P" in item.question_topic
    assert item.follow_up is None  # "N/A" -> None


def test_parse_dec18_minutes_action_items():
    parsed = parse_minutes_txt(DEC18)
    assert len(parsed.action_items) == 3
    first = parsed.action_items[0]
    assert first.text.startswith(
        "Review and provide comments on the Draft 7 GHz Study Plan"
    )
    assert first.responsible_party == "Agencies"
    assert first.due_date == "12/19/25"
    assert first.status == "Completed"


def test_parse_dec18_minutes_next_steps():
    parsed = parse_minutes_txt(DEC18)
    assert len(parsed.next_steps) == 1
    assert "cellular network characteristics" in parsed.next_steps[0]


def test_parse_handles_missing_sections():
    parsed = parse_minutes_txt(NO_DISCUSSION)
    assert parsed.discussion_items == ()
    assert len(parsed.action_items) == 1


def test_parse_handles_slide_ref_column():
    """Jan 22 minutes have a 7-cell Discussion/Questions Log."""
    parsed = parse_minutes_txt(JAN22)
    assert len(parsed.discussion_items) == 5
    # First item should be the Propagation question (Slide 4 column
    # was stripped, not folded into another field).
    first = parsed.discussion_items[0]
    assert first.category == "Propagation"
    assert "propagation model" in first.question_topic.lower()


def test_parse_skips_spacer_rows():
    """Spacer rows like 'AGENCY ACTION ITEMS | AGENCY ACTION ITEMS | ...' are skipped."""
    spacer_minutes = (
        "Spacer Test Minutes\n"
        "\n"
        "Meeting Name: | Spacer | Meeting Date: | 1/1/2026\n"
        "\n"
        "Action Items\n"
        "\n"
        "Item | Responsible Party | Due Date | Status\n"
        "\n"
        "AGENCY ACTION ITEMS | AGENCY ACTION ITEMS | AGENCY ACTION ITEMS | AGENCY ACTION ITEMS\n"
        "Submit the revised draft to the working group. | NTIA | 1/15/26 | Not started\n"
    )
    parsed = parse_minutes_text(spacer_minutes)
    # Spacer row is skipped, leaving exactly one action item.
    assert len(parsed.action_items) == 1
    assert parsed.action_items[0].text.startswith("Submit the revised draft")


def test_parse_na_followup_becomes_none():
    parsed = parse_minutes_txt(DEC18)
    assert parsed.discussion_items[0].follow_up is None


def test_parse_is_deterministic():
    """Two runs over the same input produce equal output."""
    p1 = parse_minutes_txt(DEC18)
    p2 = parse_minutes_txt(DEC18)
    assert p1 == p2
    # Stronger check: byte-identical JSON.
    j1 = json.dumps(_to_jsonable(p1), sort_keys=True)
    j2 = json.dumps(_to_jsonable(p2), sort_keys=True)
    assert j1 == j2


def test_parse_produces_valid_schema():
    """Parsed Dec 18 and Jan 22 minutes both validate against the schema."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    for fixture in (DEC18, JAN22):
        parsed = parse_minutes_txt(fixture)
        artifact = _parsed_to_artifact(parsed, source_id="test-source", path=fixture)
        jsonschema.validate(artifact, schema)


def test_parse_jan22_minutes_discussion_count():
    parsed = parse_minutes_txt(JAN22)
    assert len(parsed.discussion_items) == 5
    # All items should have a non-empty question_topic.
    assert all(item.question_topic for item in parsed.discussion_items)


def test_parser_has_no_llm_imports():
    """Static scan of the parser source for forbidden imports."""
    parser_src = (
        Path(__file__).resolve().parents[1]
        / "src" / "spectrum_systems_core" / "workflows"
        / "minutes_parser.py"
    ).read_text(encoding="utf-8")
    forbidden = ("anthropic", "claude", "completion", "openai")
    for token in forbidden:
        assert re.search(
            rf"\b{re.escape(token)}\b",
            parser_src,
            re.IGNORECASE,
        ) is None, (
            f"forbidden token {token!r} found in minutes_parser.py — the "
            "parser must be deterministic with zero LLM calls"
        )


def _to_jsonable(parsed: ParsedMinutes) -> dict:
    return {
        "meeting_name": parsed.meeting_name,
        "meeting_date": parsed.meeting_date,
        "prepared_by": parsed.prepared_by,
        "location": parsed.location,
        "overview": parsed.overview,
        "discussion_items": [
            {
                "item_number": d.item_number,
                "category": d.category,
                "question_topic": d.question_topic,
                "asked_by": d.asked_by,
                "response": d.response,
                "follow_up": d.follow_up,
            }
            for d in parsed.discussion_items
        ],
        "action_items": [
            {
                "text": a.text,
                "responsible_party": a.responsible_party,
                "due_date": a.due_date,
                "status": a.status,
            }
            for a in parsed.action_items
        ],
        "next_steps": list(parsed.next_steps),
    }


def _parsed_to_artifact(parsed: ParsedMinutes, *, source_id: str, path: Path) -> dict:
    raw = path.read_bytes()
    h = hashlib.sha256(raw).hexdigest()
    return {
        "artifact_type": "human_minutes",
        "schema_version": "1.0.0",
        "source_id": source_id,
        "meeting_name": parsed.meeting_name,
        "meeting_date": parsed.meeting_date,
        "prepared_by": parsed.prepared_by,
        "location": parsed.location,
        "overview": parsed.overview,
        "discussion_items": [
            {
                "item_number": d.item_number,
                "category": d.category,
                "question_topic": d.question_topic,
                "asked_by": d.asked_by,
                "response": d.response,
                "follow_up": d.follow_up,
            }
            for d in parsed.discussion_items
        ],
        "action_items": [
            {
                "text": a.text,
                "responsible_party": a.responsible_party,
                "due_date": a.due_date,
                "status": a.status,
            }
            for a in parsed.action_items
        ],
        "next_steps": list(parsed.next_steps),
        "produced_by": "minutes_parser",
        "raw_source_hash": f"sha256:{h}",
        "source_path": str(path),
    }
