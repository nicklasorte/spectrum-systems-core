"""Phase 2.B: tests for ``CHUNK_OVERLAP_TURNS`` on the LLM chunker.

The live-LLM speaker-turn chunker prepends prior turns' text onto each
chunk when ``CHUNK_OVERLAP_TURNS > 0``. Default-off
(``CHUNK_OVERLAP_TURNS=0`` or unset) preserves byte-identical
pre-Phase-2.B output — every existing chunker test must still pass.
"""
from __future__ import annotations

import os

import pytest

from spectrum_systems_core.data_lake.chunker import (
    MAX_LLM_CHUNK_CHARS,
    chunk_transcript,
    chunking_strategy_version,
)


@pytest.fixture(autouse=True)
def _clear_env():
    """Make every test start with CHUNK_OVERLAP_TURNS unset."""
    os.environ.pop("CHUNK_OVERLAP_TURNS", None)
    yield
    os.environ.pop("CHUNK_OVERLAP_TURNS", None)


# ---- Default-off byte-identicality -----------------------------------------


_DEFAULT_TRANSCRIPT = (
    "ALICE: hello world\n"
    "BOB: nice to meet you\n"
    "CAROL: today's agenda is full\n"
    "DAVE: let's get started\n"
)


def test_default_off_produces_no_overlap_metadata():
    """CHUNK_OVERLAP_TURNS unset → no overlap fields on chunks."""
    chunks = chunk_transcript(_DEFAULT_TRANSCRIPT)
    for c in chunks:
        assert "overlap_turns_prepended" not in c
        assert "overlap_clamped" not in c
        assert "prepended_overlap_turn_ids" not in c


def test_explicit_zero_is_byte_identical_to_unset():
    """Setting CHUNK_OVERLAP_TURNS=0 explicitly must match the unset case."""
    chunks_unset = chunk_transcript(_DEFAULT_TRANSCRIPT)
    os.environ["CHUNK_OVERLAP_TURNS"] = "0"
    chunks_zero = chunk_transcript(_DEFAULT_TRANSCRIPT)
    assert chunks_unset == chunks_zero


# ---- Overlap behaviour -----------------------------------------------------


def test_overlap_one_prepends_one_prior_turn():
    os.environ["CHUNK_OVERLAP_TURNS"] = "1"
    chunks = chunk_transcript(_DEFAULT_TRANSCRIPT)
    # First chunk has no predecessor: prepended count is 0.
    assert chunks[0]["overlap_turns_prepended"] == 0
    assert chunks[0]["prepended_overlap_turn_ids"] == []
    # Each subsequent chunk has 1 prepended turn.
    for i in range(1, len(chunks)):
        assert chunks[i]["overlap_turns_prepended"] == 1
        assert chunks[i]["prepended_overlap_turn_ids"] == [f"t{i-1:04d}"]
        # The chunk's text starts with the prior chunk's original text.
        # (We can't compare against chunks[i-1]["text"] because that
        # already has its own overlap applied; we compare against the
        # raw transcript content instead.)
        assert "nice to meet you" in chunks[1]["text"]


def test_overlap_two_prepends_two_prior_turns():
    os.environ["CHUNK_OVERLAP_TURNS"] = "2"
    chunks = chunk_transcript(_DEFAULT_TRANSCRIPT)
    assert chunks[0]["overlap_turns_prepended"] == 0
    assert chunks[1]["overlap_turns_prepended"] == 1  # only 1 available
    assert chunks[1]["prepended_overlap_turn_ids"] == ["t0000"]
    assert chunks[2]["overlap_turns_prepended"] == 2
    assert chunks[2]["prepended_overlap_turn_ids"] == ["t0000", "t0001"]
    assert chunks[3]["overlap_turns_prepended"] == 2
    assert chunks[3]["prepended_overlap_turn_ids"] == ["t0001", "t0002"]


def test_overlap_does_not_compound():
    """Each chunk's text uses the prior chunks' ORIGINAL text, not their
    overlap-augmented text. Without this, the prepended-text would grow
    quadratically.
    """
    os.environ["CHUNK_OVERLAP_TURNS"] = "1"
    chunks = chunk_transcript(_DEFAULT_TRANSCRIPT)
    # chunks[2] should have BOB's original text (not BOB's overlap-
    # augmented text, which would include ALICE's). The text should
    # contain "nice to meet you" exactly once.
    chunk2_text = chunks[2]["text"]
    assert chunk2_text.count("nice to meet you") == 1
    assert chunk2_text.count("hello world") == 0


def test_turn_ids_retained_verbatim():
    """Overlap turns retain their original turn_id (no new IDs minted)."""
    os.environ["CHUNK_OVERLAP_TURNS"] = "2"
    chunks = chunk_transcript(_DEFAULT_TRANSCRIPT)
    for i, c in enumerate(chunks):
        # Native turn_id format is unchanged.
        assert c["turn_id"] == f"t{i:04d}"
    # Prepended IDs are verbatim native IDs.
    assert chunks[2]["prepended_overlap_turn_ids"] == ["t0000", "t0001"]


# ---- Hard ceiling / clamp --------------------------------------------------


def test_overlap_clamped_when_exceeding_max_chars():
    """Clamping reduces overlap when prepend would exceed the ceiling."""
    # Construct two long turns whose combined size exceeds the ceiling.
    long_line_a = "a" * (MAX_LLM_CHUNK_CHARS - 50)
    long_line_b = "b" * 100
    transcript = f"ALICE: {long_line_a}\nBOB: {long_line_b}\n"
    os.environ["CHUNK_OVERLAP_TURNS"] = "1"
    chunks = chunk_transcript(transcript)
    # Without clamp, chunks[1] text would be ALICE's text + BOB's text,
    # exceeding MAX_LLM_CHUNK_CHARS. With clamp, overlap is reduced to 0.
    assert chunks[1]["overlap_clamped"] is True
    assert chunks[1]["overlap_turns_prepended"] == 0
    assert chunks[1]["prepended_overlap_turn_ids"] == []
    # The chunk's text falls back to its original (no overlap added).
    assert long_line_a not in chunks[1]["text"]


def test_clamp_never_raises_on_oversized_input():
    """The chunker must never raise even when overlap can't fit."""
    long_line = "x" * (MAX_LLM_CHUNK_CHARS * 2)
    transcript = f"ALICE: {long_line}\nBOB: short\n"
    os.environ["CHUNK_OVERLAP_TURNS"] = "1"
    # Must not raise.
    chunks = chunk_transcript(transcript)
    assert len(chunks) == 2


# ---- Invalid env-var values ------------------------------------------------


def test_invalid_env_var_falls_back_to_zero():
    """A non-integer env var falls back to 0 with a warning."""
    os.environ["CHUNK_OVERLAP_TURNS"] = "not-a-number"
    chunks = chunk_transcript(_DEFAULT_TRANSCRIPT)
    # Same behaviour as default-off — no overlap fields.
    for c in chunks:
        assert "overlap_turns_prepended" not in c


def test_negative_env_var_falls_back_to_zero():
    os.environ["CHUNK_OVERLAP_TURNS"] = "-3"
    chunks = chunk_transcript(_DEFAULT_TRANSCRIPT)
    for c in chunks:
        assert "overlap_turns_prepended" not in c


# ---- chunking_strategy_version helper --------------------------------------


def test_strategy_version_default_off():
    """Default → ``speaker_turn_v1``."""
    assert chunking_strategy_version() == "speaker_turn_v1"


def test_strategy_version_overlap_one():
    os.environ["CHUNK_OVERLAP_TURNS"] = "1"
    assert chunking_strategy_version() == "speaker_turn_v1_overlap1"


def test_strategy_version_overlap_two():
    os.environ["CHUNK_OVERLAP_TURNS"] = "2"
    assert chunking_strategy_version() == "speaker_turn_v1_overlap2"


def test_strategy_version_invalid_treated_as_default():
    os.environ["CHUNK_OVERLAP_TURNS"] = "bad"
    assert chunking_strategy_version() == "speaker_turn_v1"


# ---- Overlap applies only to speaker-turn path -----------------------------


def test_blank_line_fallback_no_overlap_applied():
    """Overlap is opt-in on speaker-turn; the blank-line fallback is
    untouched (positional boundaries have no "prior turn" semantics).
    """
    # Two non-speaker paragraphs separated by a blank line.
    transcript = "first paragraph here\n\nsecond paragraph here\n"
    os.environ["CHUNK_OVERLAP_TURNS"] = "2"
    chunks = chunk_transcript(transcript)
    # Blank-line path → chunker_version is blank_line_v1, no overlap meta.
    assert chunks[0]["chunker_version"] == "blank_line_v1"
    for c in chunks:
        assert "overlap_turns_prepended" not in c
