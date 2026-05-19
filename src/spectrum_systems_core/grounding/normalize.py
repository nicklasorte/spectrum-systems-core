"""Deterministic transcript normalization for grounding gate.

The gate compares each artifact item's ``source_quote`` against the
normalized transcript byte-for-byte. Both the transcript and the quote
are normalized with the SAME function so the comparison is symmetric.

Normalization rules:

- Lowercase ASCII.
- Collapse any run of whitespace (spaces, tabs, newlines, CR, FF) to
  a single space character.
- Strip the ASCII punctuation set ``.,;:!?'"()`` — preserve hyphens
  ``-`` and ampersands ``&`` (they appear inside domain terms like
  ``5G-Advanced`` and ``DoD & NTIA``).

The normalizer also returns a ``position_map`` that gives, for every
byte index ``i`` in the normalized output, the byte index of the
corresponding character in the original transcript. The map exists
so the gate can compute ``quote_offset_original`` from the
normalized offset that was used for the byte match — the original
offset is what a human reviewer needs to see in the rejection
report.

Round-trip property: for any normalized substring ``norm[a:b]`` that
matches a known span, ``position_map[a]`` is the original offset of
the first character of that span. The mapping is exact for every
character that survives normalization; whitespace and punctuation
that are dropped or collapsed produce a map entry pointing at the
original character that triggered the collapse.

This module is pure: no I/O, no model calls, no time-dependent
state. Two calls on the same input produce identical output.
"""
from __future__ import annotations

from dataclasses import dataclass


_PUNCT_TO_STRIP: frozenset[str] = frozenset(".,;:!?'\"()")
# Whitespace characters that collapse to a single space.
_WHITESPACE: frozenset[str] = frozenset(" \t\n\r\f\v")


@dataclass(frozen=True)
class NormalizedTranscript:
    """Output of :func:`normalize_transcript`.

    Attributes:
        text: the normalized transcript string.
        position_map: ``position_map[i]`` is the original-transcript
            byte offset of the character at normalized byte offset ``i``.
            The list has the same length as ``text``.
    """

    text: str
    position_map: tuple[int, ...]


def normalize_transcript(original: str) -> NormalizedTranscript:
    """Return the normalized transcript and an original-offset map.

    Args:
        original: the raw transcript text. May be empty.

    Returns:
        A :class:`NormalizedTranscript` whose ``text`` is the normalized
        form and whose ``position_map`` has the same length as ``text``.

    The normalizer never raises on legal input. An empty input returns
    an empty normalized text and an empty position map.
    """
    out_chars: list[str] = []
    out_map: list[int] = []
    n = len(original)
    i = 0
    # ``prev_was_space`` tracks whether the LAST emitted normalized
    # character was a single space, so a run of whitespace + stripped
    # punctuation collapses to one space without emitting a leading
    # space.
    prev_was_space = True  # treat the start of the string as a boundary
    while i < n:
        ch = original[i]
        if ch in _WHITESPACE:
            if not prev_was_space:
                out_chars.append(" ")
                out_map.append(i)
                prev_was_space = True
            i += 1
            continue
        if ch in _PUNCT_TO_STRIP:
            # Punctuation is dropped entirely. Do NOT toggle
            # prev_was_space: stripping ``"`` should not merge two
            # adjacent words into one — but ``" hello "`` should still
            # collapse to ``"hello "``. We do nothing here, which means
            # surrounding whitespace handles spacing.
            i += 1
            continue
        out_chars.append(ch.lower())
        out_map.append(i)
        prev_was_space = False
        i += 1
    # Strip a single trailing space to keep the normalized form clean.
    if out_chars and out_chars[-1] == " ":
        out_chars.pop()
        out_map.pop()
    return NormalizedTranscript(
        text="".join(out_chars),
        position_map=tuple(out_map),
    )


def normalize_quote(quote: str) -> str:
    """Return the normalized form of a quote, without a position map.

    The gate compares ``normalize_quote(item.source_quote)`` against
    the normalized transcript. This is the same algorithm as
    :func:`normalize_transcript` but discards the position map (the
    quote's own offsets are not needed; only its bytes are).
    """
    return normalize_transcript(quote).text
