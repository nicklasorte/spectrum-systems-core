"""Pytest coverage for the PR smoke-test fixture.

Verifies the fixture file is present, has the expected shape, and that
the fixture smoke-test script's mock mode (no API calls) succeeds.
"""
import json
import pathlib
import sys

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "smoke_test_transcript.jsonl"


def _load_chunks():
    return [
        json.loads(line)
        for line in FIXTURE.read_text().splitlines()
        if line.strip()
    ]


def test_fixture_file_exists():
    assert FIXTURE.exists(), f"Fixture file missing at {FIXTURE}"


def test_fixture_has_10_chunks():
    assert len(_load_chunks()) == 10


def test_fixture_chunks_have_required_fields():
    for chunk in _load_chunks():
        assert "chunk_id" in chunk
        assert "text" in chunk
        assert "speaker" in chunk
        assert len(chunk["text"]) > 20


def test_fixture_smoke_test_mock_mode():
    """Fixture smoke test passes in mock mode (no API calls)."""
    scripts_dir = str(pathlib.Path(__file__).parent.parent / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from smoke_test_fixture import run_fixture_smoke_test

    assert run_fixture_smoke_test(mock=True) is True
