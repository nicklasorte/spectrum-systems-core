"""Deterministic transcript turn chunker.

`chunk_transcript` splits a raw transcript into speaker-turn chunks. The
output is a deterministic list of dicts; the same input always produces
the same output (same turn_ids, same order, same speaker assignments).

Boundary heuristic in priority order:

1. Lines matching ``^[A-Z][A-Z\\s\\-\\.]{1,40}:\\s`` (an ALL-CAPS speaker
   label followed by a colon and a space). Each match opens a new chunk.
2. Blank-line separation as fallback boundary when no speaker labels
   were detected.
3. If neither fires, the entire transcript is returned as one chunk.

Every chunk carries ``turn_id = f"t{index:04d}"`` where ``index`` is the
0-based position in the returned list. Chunk index IS turn index, always.

Speaker null rate (the fraction of chunks with ``speaker is None``) is
a health signal:

- ``> 0.5``  → ``no_speaker_detected`` (severity ``warn``)
- ``== 1.0`` → ``no_speaker_structure`` (severity ``block``)

This module is a pure-Python module with no I/O, no clocks, and no
randomness. The pipeline calls into it; tests assert determinism.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# A speaker label must look like ALL-CAPS letters with optional spaces,
# hyphens, or dots, ending with a colon and a space. The bound on the
# label length keeps lines like ``CONTEXT:`` (a prefix marker used by the
# decision_brief workflow) from being misclassified as speakers.
SPEAKER_LABEL_RE = re.compile(r"^([A-Z][A-Z\s\-\.]{1,40}):\s(.*)$")

NO_SPEAKER_DETECTED_FINDING = "no_speaker_detected"
NO_SPEAKER_STRUCTURE_FINDING = "no_speaker_structure"


@dataclass(frozen=True)
class ChunkerHealth:
    """Health signal computed from a chunked transcript.

    `speaker_null_rate` is ``0.0`` for an empty chunk list (no signal to
    measure); callers decide whether an empty chunk list itself is a
    block (the pipeline does — empty chunks → block before this struct
    is examined).
    """

    speaker_null_rate: float
    finding_code: str | None
    severity: str | None  # "warn" | "block" | None


def _make_chunk(
    *,
    index: int,
    speaker: str | None,
    text: str,
    line_start: int,
    line_end: int,
) -> dict:
    return {
        "turn_id": f"t{index:04d}",
        "speaker": speaker,
        "text": text,
        "line_start": line_start,
        "line_end": line_end,
    }


def _split_on_speaker_labels(
    lines: list[str],
) -> list[dict] | None:
    """Return chunks split on speaker labels, or None if no labels match.

    Each speaker-labelled line opens a new chunk. Lines before the first
    speaker label become a single leading chunk with ``speaker=None``.
    Subsequent lines without their own speaker label are appended to the
    most recent chunk's text.
    """
    matches: list[tuple[int, re.Match[str]]] = []
    for idx, raw in enumerate(lines):
        m = SPEAKER_LABEL_RE.match(raw)
        if m is not None:
            matches.append((idx, m))

    if not matches:
        return None

    chunks: list[dict] = []
    chunk_index = 0
    first_speaker_line = matches[0][0]

    # Leading chunk for any text BEFORE the first speaker label.
    if first_speaker_line > 0:
        leading_text = "\n".join(lines[0:first_speaker_line]).rstrip("\n")
        if leading_text.strip():
            chunks.append(
                _make_chunk(
                    index=chunk_index,
                    speaker=None,
                    text=leading_text,
                    line_start=1,
                    line_end=first_speaker_line,
                )
            )
            chunk_index += 1

    # Per speaker-label match, build a chunk that runs until the next
    # speaker-label line (or end-of-file). Trailing lines after the last
    # label are appended to the final chunk.
    for i, (line_idx, m) in enumerate(matches):
        speaker = m.group(1).strip()
        first_body = m.group(2).rstrip()
        end_idx = (
            matches[i + 1][0] - 1
            if i + 1 < len(matches)
            else len(lines) - 1
        )
        body_lines = [first_body] + [
            lines[j] for j in range(line_idx + 1, end_idx + 1)
        ]
        text = "\n".join(body_lines).rstrip("\n")
        chunks.append(
            _make_chunk(
                index=chunk_index,
                speaker=speaker,
                text=text,
                line_start=line_idx + 1,
                line_end=end_idx + 1,
            )
        )
        chunk_index += 1

    return chunks


def _split_on_blank_lines(lines: list[str]) -> list[dict] | None:
    """Return chunks split on blank lines, or None if no blank-line
    boundaries fired (single block of contiguous non-blank lines).

    A blank line is any line whose ``strip()`` is empty.
    """
    paragraphs: list[tuple[int, int, str]] = []
    start_line: int | None = None
    para_lines: list[str] = []
    for idx, raw in enumerate(lines):
        if raw.strip() == "":
            if start_line is not None:
                paragraphs.append(
                    (start_line, idx, "\n".join(para_lines))
                )
                start_line = None
                para_lines = []
            continue
        if start_line is None:
            start_line = idx + 1  # 1-based
        para_lines.append(raw)
    if start_line is not None:
        paragraphs.append(
            (start_line, len(lines), "\n".join(para_lines))
        )

    if len(paragraphs) <= 1:
        return None

    return [
        _make_chunk(
            index=i,
            speaker=None,
            text=text,
            line_start=line_start,
            line_end=line_end,
        )
        for i, (line_start, line_end, text) in enumerate(paragraphs)
    ]


def chunk_transcript(transcript_text: str) -> list[dict]:
    """Split a transcript into deterministic speaker-turn chunks.

    Same input → same output, always. Empty / whitespace-only inputs
    return an empty list; the pipeline treats that as a block condition.
    Each returned chunk has fields ``turn_id``, ``speaker``, ``text``,
    ``line_start``, ``line_end``. ``turn_id == f"t{index:04d}"`` is an
    invariant — index in the returned list IS the turn index.
    """
    if not transcript_text or not transcript_text.strip():
        return []

    lines = transcript_text.splitlines()

    by_speaker = _split_on_speaker_labels(lines)
    if by_speaker is not None:
        return by_speaker

    by_blank = _split_on_blank_lines(lines)
    if by_blank is not None:
        return by_blank

    # Fallback: whole transcript is one chunk.
    text = transcript_text.rstrip("\n")
    return [
        _make_chunk(
            index=0,
            speaker=None,
            text=text,
            line_start=1,
            line_end=max(1, len(lines)),
        )
    ]


def speaker_null_rate(chunks: list[dict]) -> float:
    """Fraction of chunks with ``speaker is None``. ``0.0`` for empty."""
    if not chunks:
        return 0.0
    null_count = sum(1 for c in chunks if c.get("speaker") is None)
    return null_count / len(chunks)


def chunker_health(chunks: list[dict]) -> ChunkerHealth:
    """Compute the structured health signal for a chunk list.

    Severity ladder:
    - 100% null speaker → block (``no_speaker_structure``). A structureless
      transcript cannot produce verifiable grounded claims.
    - >50% null speaker → warn (``no_speaker_detected``).
    - Otherwise → no finding.
    """
    if not chunks:
        return ChunkerHealth(
            speaker_null_rate=0.0, finding_code=None, severity=None
        )
    rate = speaker_null_rate(chunks)
    if rate >= 1.0:
        return ChunkerHealth(
            speaker_null_rate=rate,
            finding_code=NO_SPEAKER_STRUCTURE_FINDING,
            severity="block",
        )
    if rate > 0.5:
        return ChunkerHealth(
            speaker_null_rate=rate,
            finding_code=NO_SPEAKER_DETECTED_FINDING,
            severity="warn",
        )
    return ChunkerHealth(
        speaker_null_rate=rate, finding_code=None, severity=None
    )
