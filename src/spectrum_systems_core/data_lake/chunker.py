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
``turn_index`` is that same 0-based position carried as an explicit int
field; ``turn_id`` remains the single grounding key Phase Y's
``source_turn_validity`` eval validates against — this module is the one
turn-id authority and is deliberately NOT changed to a UUID scheme (a
second id space would orphan every already-grounded artifact).

Every chunk also carries ``chunker_version`` so a downstream consumer
can tell which boundary heuristic produced it:

- ``speaker_turn_v1``  — split on ALL-CAPS speaker labels.
- ``blank_line_v1``    — split on blank-line paragraph boundaries.
- ``recursive_512``    — terminal fallback: no speaker or paragraph
  structure, so the text is split into deterministic ~512-word windows.
  These boundaries are NOT speaker turns; ``speaker`` is ``None`` on
  every such chunk. ``turn_id`` is still emitted because the binding
  data-lake source_record contract (and ``source_turn_validity``)
  require every chunk to carry a string ``turn_id``; the
  ``chunker_version`` field is what tells a consumer the boundary is
  positional, not a real speaker turn.

``word_count`` is ``len(text.split())`` — a cheap deterministic size
signal used by the recursive fallback and available to consumers.

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

# chunker_version tags. Additive: legacy consumers ignore the field;
# Phase Y keys on turn_id regardless of which heuristic produced it.
CHUNKER_VERSION_SPEAKER_TURN = "speaker_turn_v1"
CHUNKER_VERSION_BLANK_LINE = "blank_line_v1"
CHUNKER_VERSION_RECURSIVE = "recursive_512"

# Word budget for one recursive-fallback window. 512 words is a
# deterministic, model-agnostic proxy for "about one context-friendly
# block" — no tokenizer dependency keeps the chunker pure.
RECURSIVE_WORD_BUDGET = 512

# Phase Z.4: deterministic agenda-item markers. The detector runs on a
# turn's text AFTER speaker-label stripping. Only the first pattern
# carries a capturing group; the other two intentionally do not, so
# the detector emits ``item-<digit>`` only when the marker is a
# numbered "Item N" form. Other agenda markers degrade to
# ``item-unknown`` rather than embedding free-form text into an id.
AGENDA_ITEM_NUMBER_RE = re.compile(
    r"^(?:agenda\s+)?item\s+(\d+)[:\.]", re.IGNORECASE
)
AGENDA_NUMBER_DOT_RE = re.compile(
    r"^\d+\.\s+(?!\d)"
)
AGENDA_TOPIC_RE = re.compile(
    r"^(?:topic|agenda)[:\s]+(?:.+)", re.IGNORECASE
)

AGENDA_PATTERNS = (
    AGENDA_ITEM_NUMBER_RE,
    AGENDA_NUMBER_DOT_RE,
    AGENDA_TOPIC_RE,
)


def _detect_agenda_item(turn_text: str) -> str | None:
    """Return ``item-<n>`` when the text begins with a numbered agenda
    marker, ``"item-unknown"`` when a non-numbered marker is present,
    or ``None`` when no marker fires. Never raises; never reads any
    state outside ``turn_text``."""
    if not isinstance(turn_text, str):
        return None
    text = turn_text.strip()
    if not text:
        return None
    m = AGENDA_ITEM_NUMBER_RE.match(text)
    if m is not None:
        return f"item-{m.group(1)}"
    for pattern in AGENDA_PATTERNS[1:]:
        if pattern.match(text):
            return "item-unknown"
    return None


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
    chunker_version: str,
    agenda_item_id: str | None = None,
) -> dict:
    return {
        "turn_id": f"t{index:04d}",
        "turn_index": index,
        "speaker": speaker,
        "text": text,
        "line_start": line_start,
        "line_end": line_end,
        "chunker_version": chunker_version,
        "word_count": len(text.split()),
        "agenda_item_id": agenda_item_id,
    }


def _propagate_agenda_ids(chunks: list[dict]) -> list[dict]:
    """Detect and propagate agenda_item_id across chunks.

    Rule: a chunk whose text starts with an agenda marker gets the
    detected id; every subsequent chunk inherits that id until a new
    marker is detected. Chunks that precede the first marker have
    ``agenda_item_id: None``.

    Returns a new list of chunk dicts; never mutates the input
    chunks in place beyond setting the agenda_item_id field.
    """
    current: str | None = None
    for chunk in chunks:
        detected = _detect_agenda_item(chunk.get("text", ""))
        if detected is not None:
            current = detected
        chunk["agenda_item_id"] = current
    return chunks


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
                    chunker_version=CHUNKER_VERSION_SPEAKER_TURN,
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
                chunker_version=CHUNKER_VERSION_SPEAKER_TURN,
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
            chunker_version=CHUNKER_VERSION_BLANK_LINE,
        )
        for i, (line_start, line_end, text) in enumerate(paragraphs)
    ]


def _split_recursive_512(
    lines: list[str],
) -> list[tuple[int, int, str]]:
    """Split lines into deterministic ~512-word windows.

    A window closes as soon as its cumulative word count reaches
    ``RECURSIVE_WORD_BUDGET``; a single over-budget line still forms its
    own window (lines are never split mid-line, so line ranges stay
    meaningful and the output stays a pure function of ``lines``).
    Returns ``(line_start, line_end, text)`` tuples, both bounds 1-based
    inclusive.
    """
    windows: list[tuple[int, int, str]] = []
    cur_lines: list[str] = []
    cur_start: int | None = None
    cur_words = 0
    for idx, raw in enumerate(lines):
        if cur_start is None:
            cur_start = idx + 1
        cur_lines.append(raw)
        cur_words += len(raw.split())
        if cur_words >= RECURSIVE_WORD_BUDGET:
            text = "\n".join(cur_lines).rstrip("\n")
            windows.append((cur_start, idx + 1, text))
            cur_lines = []
            cur_start = None
            cur_words = 0
    if cur_lines and cur_start is not None:
        text = "\n".join(cur_lines).rstrip("\n")
        windows.append((cur_start, len(lines), text))
    return windows


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
        return _propagate_agenda_ids(by_speaker)

    by_blank = _split_on_blank_lines(lines)
    if by_blank is not None:
        return _propagate_agenda_ids(by_blank)

    # Terminal fallback: no speaker labels, no blank-line paragraph
    # boundaries. The text has no turn structure, so we split it into
    # deterministic ~512-word windows. speaker is None on every window
    # and chunker_version marks the boundary as positional, not a real
    # speaker turn — turn_id is still emitted because the binding
    # source_record contract requires it.
    windows = _split_recursive_512(lines)
    if not windows:
        windows = [(1, max(1, len(lines)), transcript_text.rstrip("\n"))]
    return _propagate_agenda_ids([
        _make_chunk(
            index=i,
            speaker=None,
            text=text,
            line_start=line_start,
            line_end=line_end,
            chunker_version=CHUNKER_VERSION_RECURSIVE,
        )
        for i, (line_start, line_end, text) in enumerate(windows)
    ])


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
