"""Source-span grounding for transcript-derived artifacts.

Grounding records the line(s) of the transcript that produced each item
in a payload. The fields live on a `grounding` list on the artifact
payload so a reviewer can verify any extracted claim against the source.

Field shape:
    {
        "kind": "decision" | "action_item" | "open_question" | ...,
        "text": str,                # the extracted text
        "source_excerpt": str,      # exact substring of the transcript
        "start_line": int,          # 1-based, inclusive
        "end_line": int,            # 1-based, inclusive
        "speaker": str | None,      # optional
    }

A grounding eval verifies every excerpt is a verbatim substring of the
source transcript.
"""
from __future__ import annotations

from collections.abc import Iterable

from .loader import TranscriptInput

GROUNDING_KEY = "grounding"
MEETING_ID_KEY = "meeting_id"


def find_lines_with_prefix(
    transcript_input: TranscriptInput, prefix: str
) -> list[tuple[int, str]]:
    """Return (1-based line number, full line) pairs whose line starts with prefix.

    Matching is case-insensitive on the prefix to mirror the existing
    deterministic extractors. Whitespace at the start of the line is ignored.
    """
    out: list[tuple[int, str]] = []
    p = prefix.upper()
    for idx, raw in enumerate(transcript_input.transcript_lines, start=1):
        stripped = raw.lstrip()
        if stripped.upper().startswith(p):
            out.append((idx, raw))
    return out


def grounding_span(
    transcript_input: TranscriptInput,
    *,
    kind: str,
    text: str,
    line_number: int,
    speaker: str | None = None,
) -> dict:
    """Build a single grounding record. Verifies bounds at construction time."""
    if line_number < 1 or line_number > len(transcript_input.transcript_lines):
        raise ValueError(
            f"grounding line {line_number} out of range "
            f"1..{len(transcript_input.transcript_lines)}"
        )
    excerpt = transcript_input.transcript_lines[line_number - 1]
    record = {
        "kind": kind,
        "text": text,
        "source_excerpt": excerpt,
        "start_line": line_number,
        "end_line": line_number,
    }
    if speaker:
        record["speaker"] = speaker
    return record


def excerpt_is_in_transcript(
    transcript_input: TranscriptInput, excerpt: str
) -> bool:
    return bool(excerpt) and excerpt in transcript_input.transcript_text


def evaluate_grounding(
    transcript_input: TranscriptInput, payload: dict
) -> tuple[bool, list[str]]:
    """Returns (passed, reason_codes).

    Pass condition: every grounding entry's source_excerpt is a verbatim
    substring of the transcript text and its line numbers are valid.
    Empty grounding lists pass trivially; absence of any grounding entries
    is treated as 'no claims to ground' rather than a failure, because some
    artifact_types may legitimately have no extractable spans.
    """
    grounding: Iterable[dict] = payload.get(GROUNDING_KEY) or []
    reasons: list[str] = []
    n_lines = len(transcript_input.transcript_lines)
    for i, entry in enumerate(grounding):
        excerpt = entry.get("source_excerpt", "")
        if not excerpt_is_in_transcript(transcript_input, excerpt):
            reasons.append(f"grounding[{i}]:excerpt_not_in_transcript")
        start = entry.get("start_line")
        end = entry.get("end_line")
        if isinstance(start, int) and (start < 1 or start > n_lines):
            reasons.append(f"grounding[{i}]:start_line_out_of_range")
        if isinstance(end, int) and (end < 1 or end > n_lines):
            reasons.append(f"grounding[{i}]:end_line_out_of_range")
        if isinstance(start, int) and isinstance(end, int) and end < start:
            reasons.append(f"grounding[{i}]:end_before_start")
    return (not reasons, reasons)
