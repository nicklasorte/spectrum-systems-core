"""Phase 1 — normalization round-trip tests.

The position map produced by :func:`normalize_transcript` must let a
caller recover the ORIGINAL-transcript offset of any normalized
substring it later matches. If the mapping is off even by one
character, the gate's ``quote_offset_original`` field is unreliable
and a downstream reviewer cannot find the cited span in the original
text — so the normalization is verified end-to-end here.

The tests are pure (no transcript I/O, no model calls) and run in
milliseconds.
"""
from __future__ import annotations

import string

import pytest

from spectrum_systems_core.grounding.normalize import (
    normalize_quote,
    normalize_transcript,
)


def test_normalize_lowercases_and_strips_punctuation():
    nt = normalize_transcript("CHAIR: Thanks for joining.")
    assert nt.text == "chair thanks for joining"


def test_normalize_collapses_whitespace_runs():
    nt = normalize_transcript("a   b\t\tc\n\nd")
    assert nt.text == "a b c d"


def test_normalize_preserves_hyphen_and_ampersand():
    nt = normalize_transcript("5G-Advanced & spectrum.")
    assert nt.text == "5g-advanced & spectrum"


def test_normalize_strips_full_punctuation_set():
    for ch in ".,;:!?'\"()":
        nt = normalize_transcript(f"hello{ch}world")
        # punctuation drop must not merge or break words inappropriately
        assert nt.text == "helloworld", (
            f"normalization of {ch!r} produced {nt.text!r}; "
            "expected 'helloworld'"
        )


def test_position_map_length_matches_text_length():
    raw = "CHAIR: Thanks for joining."
    nt = normalize_transcript(raw)
    assert len(nt.position_map) == len(nt.text)


def test_position_map_recovers_original_offset_for_known_span():
    """The canonical round-trip property the gate depends on."""
    raw = (
        "CHAIR: Thanks for joining. We will be posting Nick's paper "
        "for review next week.\nNICK: Sounds good."
    )
    nt = normalize_transcript(raw)
    needle = "we will be posting nicks paper"
    start = nt.text.find(needle)
    assert start >= 0
    end = start + len(needle)
    original_start = nt.position_map[start]
    original_end_inclusive = nt.position_map[end - 1]
    original_span = raw[original_start : original_end_inclusive + 1]
    # The recovered original span must start with "We will be posting"
    # — case preserved, punctuation present.
    assert original_span.lower().startswith("we will be posting")
    assert "Nick" in original_span


def test_position_map_round_trip_for_every_normalized_character():
    """Each normalized-byte's map entry must point at a character in the
    original whose normalization is the SAME character. This is the
    strict "off by one" check the red-team checklist calls for."""
    raw = (
        "CHAIR: Thanks!! For \"joining\", we'll be posting Nick's paper. "
        "5G-Advanced & spectrum policy.\nNICK: Sounds good. "
    )
    nt = normalize_transcript(raw)
    for i, ch in enumerate(nt.text):
        orig_idx = nt.position_map[i]
        assert 0 <= orig_idx < len(raw)
        if ch == " ":
            # Whitespace in normalized text must point at a whitespace
            # character in the original (the one that triggered the
            # collapse).
            assert raw[orig_idx] in " \t\n\r\f\v"
        else:
            assert raw[orig_idx].lower() == ch, (
                f"position_map[{i}]={orig_idx}: "
                f"raw[{orig_idx}]={raw[orig_idx]!r} did not normalize "
                f"to text[{i}]={ch!r}"
            )


def test_normalize_quote_is_idempotent_with_normalize_transcript():
    raw = "We will be posting Nick's paper for review next week."
    nt = normalize_transcript(raw)
    qn = normalize_quote(raw)
    assert qn == nt.text


def test_normalize_handles_empty_input():
    nt = normalize_transcript("")
    assert nt.text == ""
    assert nt.position_map == ()


def test_normalize_strips_trailing_whitespace():
    nt = normalize_transcript("hello world   ")
    assert nt.text == "hello world"


def test_normalize_collapses_leading_whitespace():
    nt = normalize_transcript("   hello")
    assert nt.text == "hello"


def test_long_transcript_round_trip():
    """Synthetic stress test: build a long transcript and confirm the
    position map recovers any random span to byte-exact original
    bounds."""
    parts = [
        "CHAIR: Welcome.",
        "NICK: Hi.",
        "CHAIR: Today we discuss 5G-Advanced & spectrum.",
        "JANE: I propose a band plan from 3550 to 3700 MHz.",
        "NICK: That's a lot of bandwidth.",
        "CHAIR: Decisions: approve the plan, post to NTIA.",
    ]
    raw = "\n".join(parts) + "\n"
    nt = normalize_transcript(raw)
    # Sample 5 spans and confirm each recovers.
    spans = [
        "welcome",
        "3550 to 3700 mhz",
        "5g-advanced & spectrum",
        "post to ntia",
        "decisions",
    ]
    for span in spans:
        idx = nt.text.find(span)
        assert idx >= 0, f"{span!r} not in normalized text"
        end = idx + len(span)
        orig_start = nt.position_map[idx]
        orig_end_inclusive = nt.position_map[end - 1]
        recovered = raw[orig_start : orig_end_inclusive + 1]
        # The recovered original span — when re-normalized — must
        # equal the span exactly.
        re_norm = normalize_quote(recovered)
        assert re_norm == span, (
            f"round-trip mismatch for {span!r}: recovered "
            f"{recovered!r} -> {re_norm!r}"
        )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Hello world", "hello world"),
        ("Hello   world", "hello world"),
        ("Hello\nworld", "hello world"),
        ("Hello, world!", "hello world"),
        ("Don't stop", "dont stop"),  # apostrophe stripped
        ("(parens)", "parens"),
        ("foo & bar - baz", "foo & bar - baz"),
    ],
)
def test_canonical_examples(raw, expected):
    assert normalize_transcript(raw).text == expected


def test_position_map_never_skips_a_kept_character():
    """The map is monotonically non-decreasing on every kept character.
    A jump backwards would imply the map points at the wrong original
    character and the gate's recovered original offset would lie."""
    raw = "abc, def. ghi!"
    nt = normalize_transcript(raw)
    last = -1
    for pos in nt.position_map:
        assert pos >= last, (
            f"position_map went backwards: {pos} after {last}"
        )
        last = pos


def test_position_map_does_not_blow_up_on_all_punctuation():
    nt = normalize_transcript(string.punctuation)
    # Many of these chars survive (hyphen, ampersand, etc.); the rest
    # are stripped. What we care about is that the function returns
    # cleanly and the map length matches text length.
    assert len(nt.position_map) == len(nt.text)
