"""Shared meeting-date extraction for ingestion.

Extracts a meeting date from a filename or text string and normalises it
to ``YYYY-MM-DD``. Used by both ``MinutesProcessor`` (filenames + body
text of .docx files) and ``GroundTruthLinker`` (transcript source_record
titles / raw paths).

Recognised patterns, scanned in this priority order against the input:

  1. ``YYYYMMDD``                   — ``20251218`` -> ``2025-12-18``
  2. ``D[D]MonYYYY`` / ``D[D]MonYY`` — ``22Jan2026`` -> ``2026-01-22``,
                                       ``21Jan26``  -> ``2026-01-21``,
                                       ``5Feb2026`` -> ``2026-02-05``
  3. ``M-D-YY[YY]``                  — ``2-19-26``  -> ``2026-02-19``,
                                       ``2-19-2026`` -> ``2026-02-19``
  4. ``Month D[D], YYYY``            — ``January 22, 2026`` -> ``2026-01-22``

Two-digit years are pivoted ``00..79 -> 2000..2079`` and ``80..99 ->
1980..1999`` (working-paper history pre-2080 is the only realistic
context for this codebase).

Month-only-with-year (e.g. ``Jan2026``) is intentionally NOT matched —
fabricating a day-of-month would risk false matches downstream.

``extract_meeting_date`` never raises and returns ``None`` when no
pattern matches. Callers must treat ``None`` as "unmatched", never as
"matches another None".
"""
from __future__ import annotations

import datetime
import re
from typing import Optional

_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

# Compact YYYYMMDD (8 contiguous digits, not adjacent to other digits).
COMPACT_DATE_RE = re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)")
# Day + month-name + year (2 or 4 digit). The optional separators allow
# both ``22Jan2026`` and ``22-Jan-2026``; the lookahead ``(?![A-Za-z\d])``
# prevents partial-year matches such as ``5Feb20`` inside ``5Feb2026``.
DAY_MONTH_YEAR_RE = re.compile(
    r"(?<![A-Za-z\d])(\d{1,2})[-_.\s]?([A-Za-z]{3,9})[-_.\s]?(\d{4}|\d{2})(?![A-Za-z\d])"
)
# Numeric M-D-YY[YY] with separators.
NUMERIC_DATE_RE = re.compile(
    r"(?<!\d)(\d{1,2})[-_./](\d{1,2})[-_./](\d{4}|\d{2})(?!\d)"
)
# Month-name + day + year as written in body text: ``January 22, 2026``.
MONTH_DAY_YEAR_RE = re.compile(
    r"(?<![A-Za-z])([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})(?!\d)"
)


def _two_digit_to_full_year(y: int) -> int:
    if y < 100:
        return 2000 + y if y < 80 else 1900 + y
    return y


def _safe_iso_date(year: int, month: int, day: int) -> Optional[str]:
    try:
        return datetime.date(year, month, day).isoformat()
    except (ValueError, TypeError):
        return None


def extract_meeting_date(text: Optional[str]) -> Optional[str]:
    """Return the first recognised date in ``text`` as ``YYYY-MM-DD``, else ``None``.

    Tries all four regex families in order. Never raises. Empty /
    non-string input returns ``None``. Use ``extract_prose_date`` instead
    when scanning free-form body text where a numeric reference like
    ``section 1.2.26`` should NOT be misread as a date.
    """
    if not isinstance(text, str) or not text:
        return None

    try:
        m = COMPACT_DATE_RE.search(text)
        if m:
            d = _safe_iso_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if d is not None:
                return d

        m = DAY_MONTH_YEAR_RE.search(text)
        if m:
            month_name = m.group(2).lower()
            if month_name in _MONTHS:
                year = _two_digit_to_full_year(int(m.group(3)))
                d = _safe_iso_date(year, _MONTHS[month_name], int(m.group(1)))
                if d is not None:
                    return d

        m = NUMERIC_DATE_RE.search(text)
        if m:
            year = _two_digit_to_full_year(int(m.group(3)))
            d = _safe_iso_date(year, int(m.group(1)), int(m.group(2)))
            if d is not None:
                return d

        m = MONTH_DAY_YEAR_RE.search(text)
        if m:
            month_name = m.group(1).lower()
            if month_name in _MONTHS:
                d = _safe_iso_date(
                    int(m.group(3)), _MONTHS[month_name], int(m.group(2))
                )
                if d is not None:
                    return d
    except (ValueError, TypeError, IndexError):
        return None

    return None


def extract_prose_date(text: Optional[str]) -> Optional[str]:
    """Conservative date scan for free-form body text.

    Only ``Month D, YYYY`` and ``D Mon YYYY`` patterns are tried —
    purely-numeric and YYYYMMDD patterns are intentionally skipped to
    avoid mis-reading version strings (``v1.2.26``), section ids
    (``1.2.26``), or document numbers (``20251201``) as meeting dates.
    Never raises.
    """
    if not isinstance(text, str) or not text:
        return None
    try:
        m = MONTH_DAY_YEAR_RE.search(text)
        if m:
            month_name = m.group(1).lower()
            if month_name in _MONTHS:
                d = _safe_iso_date(
                    int(m.group(3)), _MONTHS[month_name], int(m.group(2))
                )
                if d is not None:
                    return d
        m = DAY_MONTH_YEAR_RE.search(text)
        if m:
            month_name = m.group(2).lower()
            if month_name in _MONTHS:
                year = _two_digit_to_full_year(int(m.group(3)))
                d = _safe_iso_date(year, _MONTHS[month_name], int(m.group(1)))
                if d is not None:
                    return d
    except (ValueError, TypeError, IndexError):
        return None
    return None
