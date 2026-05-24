"""Deterministic parser for NTIA-authored meeting minutes (.txt).

Reads a plain-text NTIA meeting minutes file and returns a structured
``ParsedMinutes`` value object. ZERO LLM calls — the parser is pure
text processing, so a given input always yields the same output. This
is the human-authored gold standard for F1 measurement against
extraction artifacts.

NTIA minutes follow a consistent skeleton:

  Meeting Overview         - prose summary
  Discussion/Questions Log - pipe-delimited table
  Next Steps               - prose list
  Action Items             - pipe-delimited table

Tables use the ``|`` character as a cell separator. Section headers are
their own lines. Multi-line cells (where a row's content wraps across
several lines) are joined on a single space.

The parser tolerates:
- Variable column counts (e.g. an extra "Slide Ref." column in some
  meetings).
- Missing sections (a meeting without a Discussion/Questions Log
  produces an empty ``discussion_items`` list).
- Spacer rows whose cells are all the same value.
- ``N/A`` follow-up cells (mapped to ``None``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_SECTION_HEADERS: tuple[str, ...] = (
    "Meeting Overview",
    "Discussion/Questions Log",
    "Discussion / Questions Log",
    "Next Steps",
    "Action Items",
    "Next Meeting",
)

# Column tokens that identify the header row of a table. We match
# case-insensitively against the joined-cells string of the row.
_DISCUSSION_HEADER_TOKENS: tuple[str, ...] = (
    "category",
    "question/topic",
    "question / topic",
    "asked by",
)
_ACTION_HEADER_TOKENS: tuple[str, ...] = (
    "responsible party",
    "due date",
)

_METADATA_KEYS: tuple[str, ...] = (
    "meeting name",
    "meeting date",
    "prepared by",
    "location",
)


@dataclass(frozen=True)
class DiscussionItem:
    item_number: int
    category: str
    question_topic: str
    asked_by: str
    response: str
    follow_up: str | None


@dataclass(frozen=True)
class ActionItem:
    text: str
    responsible_party: str
    due_date: str | None
    status: str | None


@dataclass(frozen=True)
class ParsedMinutes:
    meeting_name: str
    meeting_date: str
    prepared_by: str
    location: str
    overview: str
    discussion_items: tuple[DiscussionItem, ...]
    action_items: tuple[ActionItem, ...]
    next_steps: tuple[str, ...]
    raw_text: str
    source_path: str


def parse_minutes_txt(path: Path) -> ParsedMinutes:
    """Parse an NTIA meeting minutes .txt file deterministically."""
    raw = Path(path).read_text(encoding="utf-8")
    return _parse_text(raw, source_path=str(path))


def parse_minutes_text(text: str, source_path: str = "<inline>") -> ParsedMinutes:
    """Parse minutes content from an in-memory string (used by tests)."""
    return _parse_text(text, source_path=source_path)


# ---------------------------------------------------------------------------
# internal


def _parse_text(raw: str, *, source_path: str) -> ParsedMinutes:
    lines = raw.splitlines()
    sections = _split_sections(lines)

    metadata = _extract_metadata(lines)

    overview = _join_prose(sections.get("Meeting Overview", []))

    discussion_lines = (
        sections.get("Discussion/Questions Log")
        or sections.get("Discussion / Questions Log")
        or []
    )
    discussion_items = _parse_discussion_items(discussion_lines)

    action_lines = sections.get("Action Items", [])
    action_items = _parse_action_items(action_lines)

    next_steps_lines = sections.get("Next Steps", [])
    next_steps = _parse_next_steps(next_steps_lines)

    return ParsedMinutes(
        meeting_name=metadata.get("meeting name", ""),
        meeting_date=metadata.get("meeting date", ""),
        prepared_by=metadata.get("prepared by", ""),
        location=metadata.get("location", ""),
        overview=overview,
        discussion_items=tuple(discussion_items),
        action_items=tuple(action_items),
        next_steps=tuple(next_steps),
        raw_text=raw,
        source_path=source_path,
    )


def _split_sections(lines: list[str]) -> dict[str, list[str]]:
    """Split the file into a section_name -> [body lines] mapping."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    current_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        header = _match_section_header(stripped)
        if header is not None:
            if current is not None:
                sections[current] = current_lines
            current = header
            current_lines = []
            continue
        if current is None:
            continue
        current_lines.append(line)
    if current is not None:
        sections[current] = current_lines
    return sections


def _match_section_header(stripped: str) -> str | None:
    if not stripped:
        return None
    for header in _SECTION_HEADERS:
        if stripped.lower() == header.lower():
            return header
    return None


def _extract_metadata(lines: list[str]) -> dict[str, str]:
    """Pull ``Meeting Name: X | Meeting Date: Y`` style metadata.

    Metadata appears at the top of an NTIA minutes file. The format
    varies — sometimes one key per line, sometimes pipe-delimited pairs
    on a single line. We extract whichever keys we can find without
    being strict about layout.
    """
    found: dict[str, str] = {}
    for line in lines:
        if not line.strip() or _match_section_header(line.strip()):
            continue
        cells = _row_cells(line)
        i = 0
        while i < len(cells) - 1:
            key = cells[i].rstrip(":").strip().lower()
            if key in _METADATA_KEYS and key not in found:
                found[key] = cells[i + 1].strip()
                i += 2
                continue
            i += 1
    return found


def _row_cells(line: str) -> list[str]:
    """Split a row on ``|`` and trim each cell."""
    return [cell.strip() for cell in line.split("|")]


def _is_table_row(line: str) -> bool:
    """A line is a table row if it has at least three ``|`` separators."""
    return line.count("|") >= 3


def _is_header_row(cells: list[str], tokens: tuple[str, ...]) -> bool:
    joined = " ".join(c.lower() for c in cells)
    return any(tok in joined for tok in tokens)


def _is_spacer_row(cells: list[str]) -> bool:
    non_empty = [c for c in cells if c.strip()]
    if not non_empty:
        return True
    return len(set(non_empty)) == 1


def _normalize_followup(value: str) -> str | None:
    stripped = value.strip()
    if not stripped:
        return None
    if stripped.upper() in {"N/A", "NA", "NONE", "—", "-", "--"}:
        return None
    return stripped


def _parse_discussion_items(section_lines: list[str]) -> list[DiscussionItem]:
    rows = _collect_table_rows(section_lines, _DISCUSSION_HEADER_TOKENS)
    items: list[DiscussionItem] = []
    for cells in rows:
        if _is_spacer_row(cells):
            continue
        item = _build_discussion_item(cells, fallback_number=len(items) + 1)
        if item is None:
            continue
        items.append(item)
    return items


def _build_discussion_item(
    cells: list[str], *, fallback_number: int
) -> DiscussionItem | None:
    # Pad short rows so index access is safe; the table sometimes
    # omits trailing empty cells when a row ends in blank fields.
    if len(cells) < 5:
        return None
    item_number_raw = cells[0].strip()
    try:
        item_number = int(item_number_raw)
    except ValueError:
        # If the first cell is not numeric, the row may be a stray
        # continuation line; treat as not-an-item.
        return None
    # Some meetings insert a "Slide Ref." column between # and Category.
    # Detect by length: a 6-cell row is the canonical shape, 7+ means an
    # extra column was inserted and we keep the right-aligned trailing
    # five columns (category, question, asked_by, response, follow_up).
    if len(cells) >= 7:
        category = cells[2]
        question_topic = cells[3]
        asked_by = cells[4]
        response = cells[5]
        follow_up = cells[6] if len(cells) > 6 else ""
    else:
        category = cells[1]
        question_topic = cells[2]
        asked_by = cells[3]
        response = cells[4]
        follow_up = cells[5] if len(cells) > 5 else ""
    return DiscussionItem(
        item_number=item_number or fallback_number,
        category=category.strip(),
        question_topic=question_topic.strip(),
        asked_by=asked_by.strip(),
        response=response.strip(),
        follow_up=_normalize_followup(follow_up),
    )


def _parse_action_items(section_lines: list[str]) -> list[ActionItem]:
    rows = _collect_table_rows(section_lines, _ACTION_HEADER_TOKENS)
    items: list[ActionItem] = []
    for cells in rows:
        if _is_spacer_row(cells):
            continue
        if len(cells) < 2:
            continue
        text = cells[0].strip()
        if not text:
            continue
        responsible = cells[1].strip() if len(cells) > 1 else ""
        due_date = cells[2].strip() if len(cells) > 2 else ""
        status = cells[3].strip() if len(cells) > 3 else ""
        items.append(
            ActionItem(
                text=text,
                responsible_party=responsible,
                due_date=due_date or None,
                status=status or None,
            )
        )
    return items


def _collect_table_rows(
    section_lines: list[str], header_tokens: tuple[str, ...]
) -> list[list[str]]:
    """Return cell-split rows that follow the table header within a section.

    Multi-line cell content is folded — a non-pipe continuation line is
    appended to the most recent cell of the previous row using a single
    space separator.
    """
    rows: list[list[str]] = []
    in_table = False
    for line in section_lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _is_table_row(line):
            cells = _row_cells(line)
            if not in_table:
                if _is_header_row(cells, header_tokens):
                    in_table = True
                    continue
                # Some files omit the header row entirely; assume the
                # first table row IS data.
                in_table = True
            rows.append(cells)
        else:
            if in_table and rows:
                # Continuation of the previous row's final cell.
                last = rows[-1]
                last[-1] = (last[-1] + " " + stripped).strip()
    return rows


def _parse_next_steps(section_lines: list[str]) -> list[str]:
    steps: list[str] = []
    for line in section_lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Strip bullet markers.
        cleaned = re.sub(r"^[•\-\*]\s*", "", stripped)
        cleaned = re.sub(r"^\d+[.)]\s*", "", cleaned)
        cleaned = cleaned.strip()
        if not cleaned:
            continue
        steps.append(cleaned)
    return steps


def _join_prose(section_lines: list[str]) -> str:
    """Join prose section lines into a single paragraph string."""
    parts: list[str] = []
    for line in section_lines:
        stripped = line.strip()
        if stripped:
            parts.append(stripped)
    return " ".join(parts)
