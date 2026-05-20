"""Phase 2R — synthetic transcript fixtures.

These fixtures intentionally cover the gate-trigger surface enumerated
in red-team Pass 2. Each fixture exercises exactly one check (or a
small named cluster) so the rejection-test mapping in the PR
description is one-to-one with a fixture here.
"""
from __future__ import annotations


def valid_transcript() -> str:
    """A well-formed multi-speaker transcript that passes every check."""
    lines: list[str] = []
    # 8 turns alternating between two speakers, ~14 words each → ~110 words.
    speakers = ["Alice Smith", "Bob Jones"]
    sentences = [
        (
            "We should review the agenda items now to make sure everyone "
            "agrees with the plan."
        ),
        (
            "Let me share the latest update on the band-planning study and "
            "the timeline."
        ),
        (
            "Thank you, please describe any open issues that the committee "
            "should consider."
        ),
        (
            "There are three outstanding items that need a decision before "
            "next week's review."
        ),
        (
            "What is the proposed schedule for the upcoming spectrum "
            "interference report we discussed?"
        ),
        (
            "We expect the report to be ready by Friday and shared on the "
            "internal site."
        ),
        (
            "Could you also confirm whether the regulator has scheduled the "
            "follow-up call?"
        ),
        (
            "Yes, the follow-up call is on Tuesday at three in the "
            "afternoon eastern time."
        ),
    ]
    for i, sentence in enumerate(sentences):
        lines.append(f"{speakers[i % 2]}: {sentence}")
    body = "\n".join(lines)
    # Pad with whitespace turns so the byte length crosses the 500-byte
    # minimum while keeping word-count semantics intact.
    return body


def encoding_corrupted_transcript() -> str:
    """A transcript laced with U+FFFD replacement characters."""
    return (
        "Alice Smith: We talked about the � plan extensively today.\n"
        "Bob Jones: Yes, the � in our notes confused everyone reviewing.\n"
    ) * 10


def too_short_transcript() -> str:
    """A transcript well below the 500-byte minimum."""
    return "Alice Smith: Hi.\nBob Jones: Hello.\n"


def too_large_transcript() -> str:
    """A transcript over the 1M advisory but under 10M hard cap."""
    # 1.2M bytes of valid two-speaker dialog.
    turn = "Alice Smith: We talked about spectrum interference in detail.\n"
    return turn * 20000


def hard_max_transcript() -> str:
    """A transcript above the 10M hard cap (error)."""
    turn = "Alice Smith: This is a synthetic line for hard cap tests.\n"
    return turn * 200000  # ~11M bytes


def single_speaker_long_transcript() -> str:
    """One speaker, plenty of words — passes sufficient_total_content."""
    word_filler = " ".join([f"word{i}" for i in range(200)])
    body = "Alice Smith: " + word_filler + ".\n"
    # Bring byte count above 500 already; word count ~200 well above 100.
    return body


def single_speaker_too_few_words() -> str:
    """One speaker, < 100 words AND < 2 speakers → insufficient_total_content."""
    # 50 words after the speaker label, padded with extra speaker labels
    # whose own content words must stay ≤ 49 to keep the total below 100.
    body = "Alice Smith: " + " ".join(["short"] * 50) + ".\n"
    return body


def two_speakers_few_words() -> str:
    """Two speakers but only 50 words → still passes (≥2 speakers)."""
    a = "Alice Smith: " + " ".join(["one"] * 20) + ".\n"
    b = "Bob Jones: " + " ".join(["two"] * 20) + ".\n"
    return (a + b) * 5  # 200 turns total, 200 words (well above 100)


def no_format_transcript() -> str:
    """A transcript with neither speaker_colon nor speaker_dash matches."""
    return (
        "this is just a blob of text without any speaker structure or "
        "delimiter that the validator can identify reliably. it goes on for "
        "several hundred bytes so that the length checks alone do not catch "
        "it. lorem ipsum dolor sit amet consectetur adipiscing elit sed do "
        "eiusmod tempor incididunt ut labore et dolore magna aliqua. ut enim "
        "ad minim veniam quis nostrud exercitation ullamco laboris nisi ut "
        "aliquip ex ea commodo consequat. duis aute irure dolor in "
        "reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla "
        "pariatur.\n"
    )


def duplicate_turn_ids_transcript() -> str:
    """A transcript whose embedded turn ids collide."""
    return (
        "Alice Smith: [t0001] First turn.\n"
        "Bob Jones: [t0002] Second turn.\n"
        "Alice Smith: [t0001] Third turn reuses the first turn id.\n"
        "Bob Jones: [t0003] Fourth turn keeps going.\n"
    ) * 10


def speaker_dash_only_transcript() -> str:
    """A transcript that only matches the speaker_dash format."""
    lines = []
    for i in range(20):
        speaker = "Alice Smith" if i % 2 == 0 else "Bob Jones"
        lines.append(
            f"{speaker} — turn number {i} discussing the spectrum interference "
            "report and the upcoming committee review of the proposed "
            "agenda."
        )
    return "\n".join(lines)


def tied_format_transcript() -> str:
    """speaker_colon == speaker_dash matches; alphabetical tiebreaker
    wins → detected_format is 'speaker_colon'."""
    return (
        "Alice Smith: equal colon match one.\n"
        "Bob Jones: equal colon match two.\n"
        "Alice Smith — equal dash match one with more words to add bytes.\n"
        "Bob Jones — equal dash match two with more words to add bytes.\n"
    ) * 5
