"""Phase X2.1 — heuristic agenda-item boundary detector.

Pure-regex deterministic detector for agenda-section boundaries in
.docx-derived transcript text. Distinct from
``spectrum_systems_core.agenda.agenda_detector`` (LLM-based; gated by
the legacy Phase W feature flag): this module makes ZERO model calls
and never raises.

Detection rules, evaluated against the raw transcript text line by
line (in priority order; the first matching rule wins for a line):

1. Lines matching ``AGENDA_HEADER_PATTERN`` (case-insensitive prefixes
   like ``agenda item``, ``item 1``, ``discussion:``, ``topic:``).
2. Numbered formats: ``1.``, ``2)``, ``Item 3``.
3. All-caps "header" lines: trimmed line is entirely upper-case
   letters / digits / whitespace / punctuation, has at least one
   letter, is <= ``MAX_HEADER_CHARS``, and the NEXT non-blank line
   looks like a speaker turn (matches ``_SPEAKER_TURN_HINT``). The
   speaker-turn lookahead suppresses chapter-style headings inside
   long quoted blocks.

When zero headers detected the caller assigns ``agenda_item_id =
"unclassified"`` to every chunk. "unclassified" is a valid slice value
(``meeting_type:unclassified``) — NOT an error — many transcripts have
no explicit agenda headers.

Per CLAUDE.md Phase X2 amendment: ``agenda_item_id`` is a STRING that
is either the assigned ``AI-NNN`` id or the literal string
``"unclassified"``. Never ``None`` after this detector runs.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "AgendaItem",
    "AGENDA_DETECTION_ENABLED_ENV",
    "MAX_HEADER_CHARS",
    "UNCLASSIFIED_AGENDA_ID",
    "assign_agenda_item_ids",
    "detect_agenda_items",
]


# Public constants -----------------------------------------------------

UNCLASSIFIED_AGENDA_ID: str = "unclassified"
MAX_HEADER_CHARS: int = 60
AGENDA_DETECTION_ENABLED_ENV: str = "AGENDA_DETECTION_ENABLED"
_DISABLED_VALUES: frozenset = frozenset({"0", "false", "no", "off"})

# Regex anchors ---------------------------------------------------------

# Priority 1: explicit prefix markers. Case-insensitive; the line may
# carry trailing colons / dashes / numbering. The capture group is the
# trimmed label text used as the section title.
AGENDA_HEADER_PATTERN: re.Pattern = re.compile(
    r"""
    ^\s*
    (?:
        (?:agenda\s+item\s*[#:.-]?\s*\d*[.):-]?\s*(?P<label1>.*))
      | (?:item\s+\d+[.):-]?\s*(?P<label2>.*))
      | (?:discussion[:\-]\s*(?P<label3>.*))
      | (?:topic[:\-]\s*(?P<label4>.*))
    )
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Priority 2: numbered list-style headers ``1. Title``, ``2) Title``.
NUMBERED_HEADER_PATTERN: re.Pattern = re.compile(
    r"""
    ^\s*
    (?P<num>\d{1,2})
    [.)]\s+
    (?P<label>[A-Z][^\n]{2,})
    \s*$
    """,
    re.VERBOSE,
)

# Priority 3: all-caps line. Allow letters/digits/whitespace/punctuation
# but require at least one letter, at least 4 chars total, and that the
# original (case-sensitive) text has no lowercase letters.
_ALLCAPS_PATTERN: re.Pattern = re.compile(r"^[A-Z0-9\s\W]+$")
_HAS_LETTER: re.Pattern = re.compile(r"[A-Z]")
_HAS_LOWER: re.Pattern = re.compile(r"[a-z]")

# Speaker-turn lookahead pattern. Mirrors the chunker's speaker-label
# heuristic (see ``extraction/chunker.py::_SPEAKER_LABEL_RE``) but is
# intentionally looser: we just need to recognise "Name HH:MM" or
# "Name:" style turn openings so we know an all-caps line is plausibly
# a section header preceding a turn.
_SPEAKER_TURN_HINT: re.Pattern = re.compile(
    r"""
    ^\s*
    (?:[A-Za-z+][^\t\n:]{0,80})
    (?:
        (?:[ \t]{1,}\d{1,2}:\d{2})   # trailing timestamp
      | (?::\s*\S)                    # "Name: utterance"
    )
    """,
    re.VERBOSE,
)


# Data ----------------------------------------------------------------


@dataclass
class AgendaItem:
    """One agenda section detected in the transcript.

    ``start_turn_index`` / ``end_turn_index`` index INTO THE TURN LIST
    passed to ``assign_agenda_item_ids``. They are NOT character
    offsets and they are NOT chunk indices -- the chunker assigns
    turn ordinals when it splits text_units into chunks; we use those
    here so the boundaries survive the merge / split passes.
    """

    agenda_item_id: str
    title: str
    start_turn_index: int
    end_turn_index: int  # inclusive


@dataclass
class _AgendaCandidate:
    label: str
    line_index: int  # 0-based index into the source_text line list
    rule: str  # for debug logs / context fields
    char_offset: int = 0  # character offset of the line in source_text
    next_turn_index: Optional[int] = field(default=None)


# Public API -----------------------------------------------------------


def detection_enabled() -> bool:
    """Return False when the env var is set to a disabled value.

    Default is enabled. Rollback path: set
    ``AGENDA_DETECTION_ENABLED=false`` to restore pre-Phase-X2 behaviour
    (caller will see an empty agenda_items list and should fall back to
    ``UNCLASSIFIED_AGENDA_ID``).
    """
    raw = os.environ.get(AGENDA_DETECTION_ENABLED_ENV, "").strip().lower()
    if not raw:
        return True
    return raw not in _DISABLED_VALUES


def detect_agenda_items(source_text: str) -> List[AgendaItem]:
    """Parse ``source_text`` for agenda-item headers.

    Returns ``[]`` when zero headers are detected. The list is sorted
    by ``start_turn_index`` ascending. Every produced item has
    ``end_turn_index >= start_turn_index``.

    Note on turn indices: this function does NOT know about the
    chunker's turn list, so it returns 0-based indices computed from
    speaker-turn line lookahead in ``source_text``. The caller
    (``assign_agenda_item_ids``) bridges to actual chunks.
    """
    if not isinstance(source_text, str) or not source_text.strip():
        return []
    if not detection_enabled():
        return []

    lines = source_text.splitlines()

    # Pre-compute, for each line, the ordinal of the FIRST speaker turn
    # that begins at or after that line. We use this in two places:
    #   (1) to validate the all-caps lookahead (must have a following
    #       speaker turn).
    #   (2) to set ``start_turn_index`` on each agenda candidate.
    turn_line_indices: List[int] = []
    for idx, line in enumerate(lines):
        if _SPEAKER_TURN_HINT.match(line):
            turn_line_indices.append(idx)

    # Map every line back to "the turn ordinal that contains it".
    def _turn_at_or_after(line_idx: int) -> Optional[int]:
        for ord_i, turn_li in enumerate(turn_line_indices):
            if turn_li >= line_idx:
                return ord_i
        return None

    candidates: List[_AgendaCandidate] = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        # Priority 1.
        m1 = AGENDA_HEADER_PATTERN.match(stripped)
        if m1:
            label = (
                m1.group("label1")
                or m1.group("label2")
                or m1.group("label3")
                or m1.group("label4")
                or stripped
            ).strip()
            if not label:
                label = stripped
            candidates.append(
                _AgendaCandidate(
                    label=label,
                    line_index=idx,
                    rule="prefix_marker",
                    next_turn_index=_turn_at_or_after(idx + 1),
                )
            )
            continue

        # Priority 2.
        m2 = NUMBERED_HEADER_PATTERN.match(stripped)
        if m2:
            label = (m2.group("label") or "").strip()
            if label and _looks_like_section_label(label):
                candidates.append(
                    _AgendaCandidate(
                        label=label,
                        line_index=idx,
                        rule="numbered",
                        next_turn_index=_turn_at_or_after(idx + 1),
                    )
                )
            continue

        # Priority 3: all-caps lookahead.
        if _is_allcaps_header(stripped):
            next_turn = _turn_at_or_after(idx + 1)
            if next_turn is None:
                # No following speaker turn -- probably a footer or a
                # quoted block heading. Skip.
                continue
            candidates.append(
                _AgendaCandidate(
                    label=stripped,
                    line_index=idx,
                    rule="allcaps",
                    next_turn_index=next_turn,
                )
            )

    if not candidates:
        return []

    # De-duplicate identical (label, next_turn) pairs preserving order.
    seen: set = set()
    pruned: List[_AgendaCandidate] = []
    for c in candidates:
        key = (c.label.lower(), c.next_turn_index)
        if key in seen:
            continue
        seen.add(key)
        pruned.append(c)

    # Convert candidates to AgendaItem spans. The start_turn_index of
    # item N is its own ``next_turn_index`` (clamped to 0); the
    # end_turn_index is one less than item N+1's start, or the LAST
    # turn ordinal for the final item.
    total_turns = max(1, len(turn_line_indices))
    items: List[AgendaItem] = []
    for i, cand in enumerate(pruned):
        start = (
            cand.next_turn_index
            if isinstance(cand.next_turn_index, int)
            else 0
        )
        if start < 0:
            start = 0
        if start >= total_turns:
            start = total_turns - 1
        items.append(
            AgendaItem(
                agenda_item_id=f"AI-{i + 1:03d}",
                title=cand.label[:200],
                start_turn_index=start,
                end_turn_index=total_turns - 1,  # placeholder; fixed below
            )
        )

    # Fix end_turn_index: each item ends one before the next item's
    # start; the last item ends at the final turn.
    for i, item in enumerate(items):
        if i + 1 < len(items):
            next_start = items[i + 1].start_turn_index
            item.end_turn_index = max(item.start_turn_index, next_start - 1)
        else:
            item.end_turn_index = max(item.start_turn_index, total_turns - 1)

    # Sort defensively; collapse zero-width items (rare: two headers
    # adjacent with no speaker turn between them).
    items.sort(key=lambda it: it.start_turn_index)
    collapsed: List[AgendaItem] = []
    for item in items:
        if collapsed and item.start_turn_index <= collapsed[-1].start_turn_index:
            # Replace the previous item's title with the more specific
            # one if it is non-trivial; keep the earlier start.
            if len(item.title) > len(collapsed[-1].title):
                collapsed[-1].title = item.title
            continue
        collapsed.append(item)
    return collapsed


def assign_agenda_item_ids(
    chunks: Sequence[Dict[str, Any]],
    agenda_items: Sequence[AgendaItem],
) -> List[Dict[str, Any]]:
    """Annotate ``chunks`` with ``agenda_item_id`` and return the list.

    A new list of dicts is returned; the input chunks are not mutated.

    Each chunk is assigned to the agenda item whose
    ``[start_turn_index, end_turn_index]`` range contains the chunk's
    ``chunk_index``. Chunks outside every range fall through to
    ``UNCLASSIFIED_AGENDA_ID``.

    When ``agenda_items`` is empty, every chunk gets
    ``agenda_item_id = "unclassified"`` -- the field is ALWAYS a
    non-empty string after this function returns, per the Phase X2
    "string-not-null" amendment.
    """
    out: List[Dict[str, Any]] = []
    for chunk in chunks:
        new_chunk = dict(chunk) if isinstance(chunk, dict) else {}
        ci = new_chunk.get("chunk_index")
        if not isinstance(ci, int):
            try:
                ci = int(ci)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                ci = -1
        assigned: Optional[str] = None
        for item in agenda_items:
            if item.start_turn_index <= ci <= item.end_turn_index:
                assigned = item.agenda_item_id
                break
        if assigned is None:
            # Trailing-or-before chunks: attach to the nearest previous
            # agenda item if one exists; otherwise unclassified. We
            # search in reverse to find the latest item that starts at
            # or before ci.
            previous: Optional[AgendaItem] = None
            for item in agenda_items:
                if item.start_turn_index <= ci:
                    previous = item
            assigned = (
                previous.agenda_item_id if previous is not None
                else UNCLASSIFIED_AGENDA_ID
            )
        new_chunk["agenda_item_id"] = assigned
        out.append(new_chunk)
    return out


# Internal helpers -----------------------------------------------------


def _is_allcaps_header(line: str) -> bool:
    if not line:
        return False
    if len(line) > MAX_HEADER_CHARS:
        return False
    if _HAS_LOWER.search(line):
        return False
    if not _HAS_LETTER.search(line):
        return False
    if not _ALLCAPS_PATTERN.match(line):
        return False
    # Require at least 4 chars total (excluding punctuation/spaces) so
    # acronyms like "TWG" with no following label still count, but
    # "—" / "***" do not.
    letters = sum(1 for ch in line if ch.isalpha())
    return letters >= 3


def _looks_like_section_label(label: str) -> bool:
    """Numbered-header sanity check: ``1. Hi`` should not register,
    but ``1. Spectrum Sharing Analysis`` should.
    """
    if len(label) < 4:
        return False
    # Require at least two whitespace-separated tokens OR a single
    # token >= 6 chars (e.g. ``Introductions``).
    tokens = [t for t in label.split() if t]
    if len(tokens) >= 2:
        return True
    return bool(tokens) and len(tokens[0]) >= 6


def agenda_items_to_artifact_list(
    agenda_items: Sequence[AgendaItem],
) -> List[Dict[str, Any]]:
    """Serialise AgendaItem dataclasses for storage in source_record.

    The exact shape:

        {
          "agenda_item_id": "AI-001",
          "title": "Introductions and Logistics",
          "start_turn_index": 0,
          "end_turn_index": 4
        }
    """
    out: List[Dict[str, Any]] = []
    for item in agenda_items:
        out.append({
            "agenda_item_id": item.agenda_item_id,
            "title": item.title,
            "start_turn_index": item.start_turn_index,
            "end_turn_index": item.end_turn_index,
        })
    return out


def status_for_detection(
    agenda_items: Sequence[AgendaItem],
) -> str:
    """``"detected"`` when any item exists, else ``"unclassified"``."""
    return "detected" if agenda_items else "unclassified"
