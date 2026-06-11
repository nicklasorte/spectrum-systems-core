"""Topics A-wide — the validated topics keyword trigger + shared gate.

Covers the four trust properties of the change:

1. Vocabulary: ``topics`` is in ``CEILING_SCHEMA_TYPES``; ``attendees``
   is deliberately NOT (calibration 2026-06-10 showed a keyword trigger
   for attendees catches 0/5 real truncations — structural, not
   lexical).
2. Negative fixture: a transcript containing NO topic/agenda keyword
   yields ``topics: False`` and the gate does not block on topics —
   the "stays silent when absent" direction the 47-transcript corpus
   could not prove (every corpus meeting has topics).
3. Positive fixture: a topics keyword hit with a zero topics count
   fails ``ceiling_minimum_counts`` (fail-closed) through the real
   ``run_required_evals`` + ``decide_control`` path.
4. Anti-drift: the baseline extraction script and the eval runner use
   the SAME comparison function object (id equality, mirroring the
   taxonomy tests), so the two gates cannot drift.
"""
from __future__ import annotations

import sys
from pathlib import Path

from spectrum_systems_core.artifacts import new_artifact
from spectrum_systems_core.control import decide_control
from spectrum_systems_core.evals.runner import (
    check_ceiling_minimum_counts,
    run_required_evals,
)
from spectrum_systems_core.extraction.ceiling_triggers import (
    CEILING_SCHEMA_TYPES,
    _KEYWORD_TABLE,
    transcript_keyword_hits,
)

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# No agenda/topic keyword, and none of the other types' keywords either
# (cross-checked against the whole _KEYWORD_TABLE in the test below).
_NO_KEYWORD_TRANSCRIPT = (
    "t0001 Chair: Good morning everyone.\n"
    "t0002 Staff: The weather is nice today.\n"
    "t0003 Chair: Thank you all for joining; see you next week.\n"
)

_AGENDA_TRANSCRIPT = (
    "t0001 Chair: Welcome. The agenda today has three parts.\n"
    "t0002 Chair: Moving on to the spectrum sharing study.\n"
    "t0003 Staff: Understood.\n"
)


def _ceiling(per_type_counts: dict, hits: dict):
    return new_artifact(
        artifact_type="opus_ceiling",
        payload={
            "artifact_type": "opus_ceiling",
            "schema_version": "1.0.0",
            "transcript_id": "m-topics-gate",
            "model_id": "claude-opus-4-7",
            "extracted_items": [
                {
                    "item_id": "x-1",
                    "schema_type": "action_item",
                    "source_turn_ids": ["t1"],
                    "source_text": "follow up",
                    "payload": {},
                }
            ],
            "per_type_counts": per_type_counts,
            "transcript_keyword_hits": hits,
        },
        trace_id="t-topics-gate",
        status="draft",
    )


def test_topics_in_vocabulary_attendees_deliberately_absent():
    assert "topics" in CEILING_SCHEMA_TYPES
    assert "topics" in _KEYWORD_TABLE
    # Calibration 2026-06-10: attendees keyword trigger caught 0/5 real
    # truncations — it must NOT be in the shared vocabulary.
    assert "attendees" not in CEILING_SCHEMA_TYPES
    assert "attendees" not in _KEYWORD_TABLE


def test_topics_keyword_tuple_is_exactly_the_validated_set():
    assert _KEYWORD_TABLE["topics"] == (
        "agenda",
        "agenda item",
        "next topic",
        "moving on to",
        "move on to",
        "first item",
        "next item",
        "let's move on",
        "next on the agenda",
        "first topic",
        "on today's agenda",
        "topics for",
    )


def test_negative_fixture_topics_stays_silent_and_gate_passes():
    """No agenda/topic keyword -> topics hit False -> a zero topics
    count does NOT block. The direction the 47-meeting corpus could
    not measure (it has no topic-less meeting)."""
    hits = transcript_keyword_hits(_NO_KEYWORD_TRANSCRIPT)
    assert hits["topics"] is False
    # The fixture must be keyword-free across the WHOLE table so this
    # test cannot silently rot if other tables gain keywords.
    lowered = _NO_KEYWORD_TRANSCRIPT.lower()
    for schema_type, keywords in _KEYWORD_TABLE.items():
        for kw in keywords:
            assert kw not in lowered, (
                f"negative fixture contains {kw!r} ({schema_type})"
            )
    counts = {t: 0 for t in CEILING_SCHEMA_TYPES}
    passed, reason_codes, failed_types = check_ceiling_minimum_counts(
        hits, counts
    )
    assert passed is True
    assert failed_types == []
    assert reason_codes == []


def test_positive_truncated_topics_fails_gate_end_to_end():
    """Topics keyword fires + zero topics items -> the gate MUST fail,
    name `topics` in failed_types, and block through decide_control."""
    hits = transcript_keyword_hits(_AGENDA_TRANSCRIPT)
    assert hits["topics"] is True
    counts = {t: 0 for t in CEILING_SCHEMA_TYPES}
    counts["action_item"] = 1
    ceiling = _ceiling(counts, hits)
    results = run_required_evals(ceiling)
    by_type = {r.payload["eval_type"]: r.payload for r in results}
    gate = by_type["ceiling_minimum_counts"]
    assert gate["status"] == "fail"
    assert "topics" in gate["failed_types"]
    assert "ceiling_zero_for_keyword_hit:topics" in gate["reason_codes"]
    decision = decide_control(ceiling, results)
    assert decision.payload["decision"] == "block"


def test_complete_topics_passes_gate():
    hits = transcript_keyword_hits(_AGENDA_TRANSCRIPT)
    counts = {t: 0 for t in CEILING_SCHEMA_TYPES}
    counts["topics"] = 2
    counts["action_item"] = 1
    # The agenda transcript fires only topics (verified here so the
    # pass is meaningful, not vacuous).
    assert [t for t, h in hits.items() if h] == ["topics"]
    passed, reason_codes, failed_types = check_ceiling_minimum_counts(
        hits, counts
    )
    assert passed is True
    assert failed_types == []


def test_shared_helper_fails_closed_on_missing_inputs():
    for hits, counts in ((None, {}), ({}, None), (None, None), ("x", {})):
        passed, reason_codes, failed_types = check_ceiling_minimum_counts(
            hits, counts
        )
        assert passed is False
        assert reason_codes == ["ceiling_missing_gate_inputs"]
        assert failed_types == []


def test_runner_and_baseline_script_share_one_comparison_function():
    """Anti-drift: both gates must be the SAME function object. A
    copy-paste reimplementation in either path fails this test."""
    import extract_opus_baseline as eob

    from spectrum_systems_core.evals import runner

    assert eob.check_ceiling_minimum_counts is runner.check_ceiling_minimum_counts


def test_baseline_array_mapping_covers_every_ceiling_type():
    """A CEILING_SCHEMA_TYPES entry without a baseline content-array
    mapping would make the baseline gate skip that type. The script
    halts on it at runtime; this test catches it at CI time."""
    import extract_opus_baseline as eob

    for schema_type in CEILING_SCHEMA_TYPES:
        assert schema_type in eob._BASELINE_ARRAY_BY_CEILING_TYPE, (
            f"no baseline content-array mapping for {schema_type!r}"
        )
