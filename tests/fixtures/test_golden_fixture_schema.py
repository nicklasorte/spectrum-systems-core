"""Schema validation for Phase Z golden fixtures.

The Phase Z fixtures live under ``tests/fixtures/golden_meetings/`` with
directory names that start with ``gm-``. The legacy ``m-golden-*``
fixtures use a different ``expected.json`` schema and are validated by
``tests/test_golden_transcripts.py`` — this test deliberately ignores
them so the two conventions can co-exist while the codebase migrates.
"""
from __future__ import annotations

import json
import pathlib

import pytest

FIXTURES_DIR = pathlib.Path(__file__).parent / "golden_meetings"
REQUIRED_KEYS = {"schema_version", "fixture_id", "decisions",
                 "actions", "questions"}
DECISION_KEYS = {"text", "verb", "source_turns"}
ACTION_KEYS = {"text", "owner", "source_turns"}
QUESTION_KEYS = {"text", "source_turns"}
VALID_VERBS = {"approved", "rejected", "deferred", "noted",
               "directed", "considered"}


def _phase_z_fixtures() -> list[pathlib.Path]:
    if not FIXTURES_DIR.is_dir():
        return []
    return sorted(
        p for p in FIXTURES_DIR.iterdir()
        if p.is_dir() and p.name.startswith("gm-")
    )


@pytest.mark.parametrize("fixture_dir", _phase_z_fixtures(),
                         ids=lambda p: p.name)
def test_expected_json_schema(fixture_dir):
    expected = json.loads(
        (fixture_dir / "expected.json").read_text(encoding="utf-8")
    )
    assert REQUIRED_KEYS <= set(expected.keys()), (
        f"{fixture_dir.name}/expected.json missing required keys: "
        f"{REQUIRED_KEYS - set(expected.keys())}"
    )
    for d in expected["decisions"]:
        assert DECISION_KEYS <= set(d.keys())
        assert d["verb"] in VALID_VERBS | {"ambiguous"}
        assert isinstance(d["source_turns"], list)
        assert len(d["source_turns"]) >= 1
    for a in expected["actions"]:
        assert ACTION_KEYS <= set(a.keys())
        assert isinstance(a["source_turns"], list)
        assert len(a["source_turns"]) >= 1
    for q in expected["questions"]:
        assert QUESTION_KEYS <= set(q.keys())
        assert isinstance(q["source_turns"], list)
        assert len(q["source_turns"]) >= 1


@pytest.mark.parametrize("fixture_dir", _phase_z_fixtures(),
                         ids=lambda p: p.name)
def test_transcript_exists(fixture_dir):
    transcript = fixture_dir / "transcript.txt"
    assert transcript.exists(), f"missing transcript at {transcript}"
    content = transcript.read_text(encoding="utf-8")
    assert len(content) > 100, (
        f"{fixture_dir.name}/transcript.txt is too short to be non-trivial"
    )
    assert ":" in content, (
        f"{fixture_dir.name}/transcript.txt has no speaker labels"
    )


@pytest.mark.parametrize("fixture_dir", _phase_z_fixtures(),
                         ids=lambda p: p.name)
def test_source_turns_resolve_against_chunker(fixture_dir):
    """Every source_turn referenced in expected.json must resolve to a
    real turn_id the chunker emits for transcript.txt.

    This protects the fixture against drift: if someone edits the
    transcript without updating expected.json (or vice versa), the
    cross-reference breaks loudly here, before any eval relies on it.
    """
    from spectrum_systems_core.data_lake.chunker import chunk_transcript

    expected = json.loads(
        (fixture_dir / "expected.json").read_text(encoding="utf-8")
    )
    transcript = (fixture_dir / "transcript.txt").read_text(
        encoding="utf-8"
    )
    valid_turn_ids = {c["turn_id"] for c in chunk_transcript(transcript)}
    for collection_name in ("decisions", "actions", "questions"):
        for item in expected[collection_name]:
            for turn_id in item["source_turns"]:
                assert turn_id in valid_turn_ids, (
                    f"{fixture_dir.name}/expected.json "
                    f"{collection_name} item references unknown "
                    f"turn_id {turn_id!r}; valid ids: "
                    f"{sorted(valid_turn_ids)}"
                )
