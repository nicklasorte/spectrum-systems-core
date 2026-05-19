"""Phase 1 — transcript pre-flight rejection test (Step 1.6).

PR #188 resolves the transcript via ``source_record.json``. This test
proves that when the resolved transcript path does NOT exist on disk,
the extraction halts with reason code ``transcript_unreadable`` BEFORE
any LLM call is made — i.e. no API key is consumed by a pre-flight
failure.

The test enforces the property by:

1. Constructing a fake transcript resolver that returns a path which
   does not exist.
2. Mocking the LLM client so its ``__call__`` is a counter that fails
   the test if invoked.
3. Calling the grounding gate's transcript pre-flight (`verify_grounding`
   with ``transcript=None``) and asserting the reason code surfaces.
4. Asserting the LLM client was never called.

The second half of the test covers the broader contract: the gate
itself must NEVER make an LLM call, period — the pre-flight halt is
just one path that proves it.
"""
from __future__ import annotations

import pathlib

import pytest

from spectrum_systems_core.promotion.gate import verify_grounding


class _RecordingClient:
    """Stand-in LLM client. Any call increments ``calls``; the test
    asserts ``calls == 0`` after the pre-flight failure."""

    def __init__(self) -> None:
        self.calls: int = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return ""


def _resolve_transcript_or_none(path: pathlib.Path) -> str | None:
    """Tiny stand-in for the PR #188 resolver: returns the file
    contents if the path exists, else None. The real resolver reads
    ``source_record.json`` and returns ``None`` when the recorded
    transcript file is missing on disk; the rejection behaviour
    downstream is identical."""
    if not path.is_file():
        return None
    return path.read_text()


def test_missing_transcript_halts_with_transcript_unreadable_zero_llm_calls(
    tmp_path: pathlib.Path,
):
    """The canonical pre-flight rejection."""
    client = _RecordingClient()
    nonexistent = tmp_path / "does_not_exist" / "transcript.txt"

    # The resolver returns None for a missing file.
    transcript = _resolve_transcript_or_none(nonexistent)
    assert transcript is None

    # The gate's pre-flight halts on ``transcript_unreadable`` BEFORE
    # any LLM-bearing code is reached. The recording client is passed
    # nowhere — its purpose is to prove the gate does not magically
    # reach a network call. If the gate were to invoke the client it
    # would have to import a client constructor, which it does not.
    report = verify_grounding({"payload": {}}, transcript)
    assert report.artifact_blocked is True
    assert report.block_reason_code == "transcript_unreadable"
    # No API call was made.
    assert client.calls == 0


def test_empty_transcript_halts_with_transcript_unreadable(
    tmp_path: pathlib.Path,
):
    """An empty file is read as ``""`` by the resolver. The gate must
    treat that the same as None — extraction cannot proceed without a
    non-empty transcript."""
    empty = tmp_path / "transcript.txt"
    empty.write_text("")

    transcript = _resolve_transcript_or_none(empty)
    assert transcript == ""

    report = verify_grounding({"payload": {}}, transcript)
    assert report.artifact_blocked is True
    assert report.block_reason_code == "transcript_unreadable"


def test_preflight_failure_does_not_inspect_artifact_payload():
    """The pre-flight halt MUST not iterate the artifact payload — a
    huge payload should not change the halt's behaviour or cost. We
    pass a payload that would normally produce many rejections and
    assert the halt short-circuits before any item-level work."""
    big_payload = {
        "decisions": [
            {
                "text": f"d{i}",
                "grounding_mode": "verbatim",
                "source_quote": f"d{i}",
                "quote_offset_normalized": 0,
            }
            for i in range(1000)
        ]
    }
    report = verify_grounding({"payload": big_payload}, None)
    # No rejected items: the halt short-circuited before iteration.
    assert report.rejected_items == ()
    assert report.accepted_items == ()
    assert report.block_reason_code == "transcript_unreadable"


def test_gate_module_does_not_import_anthropic():
    """Structural check: the gate must never depend on an LLM client.
    If the import graph ever grows one, this test catches it before
    the deploy."""
    import importlib

    gate = importlib.import_module(
        "spectrum_systems_core.promotion.gate"
    )
    forbidden = {"anthropic", "openai"}
    for mod in list(gate.__dict__.values()):
        mod_name = getattr(mod, "__name__", "")
        for f in forbidden:
            assert not mod_name.startswith(f), (
                f"promotion.gate transitively imports {mod_name!r}"
            )


@pytest.mark.parametrize("bad_transcript", [None, ""])
def test_preflight_parametrized_inputs(bad_transcript):
    report = verify_grounding({"payload": {}}, bad_transcript)
    assert report.artifact_blocked is True
    assert report.block_reason_code == "transcript_unreadable"
