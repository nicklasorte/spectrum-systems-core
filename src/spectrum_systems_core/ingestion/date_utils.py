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


# ---------------------------------------------------------------------------
# Family-token extraction for same-day collision disambiguation.
# ---------------------------------------------------------------------------

# Domain stopwords for 7 GHz TIG proceedings. Removed before computing
# pairwise overlap. Kept lower-case; comparison is case-insensitive.
_FAMILY_STOPWORDS: frozenset[str] = frozenset(
    {
        # English particles.
        "the", "a", "an", "of", "with", "and", "for", "in", "to", "at",
        "by", "on",
        # Document-type tokens (every transcript and every minutes carries
        # one of these; they cannot disambiguate two same-day records).
        "meeting", "transcript", "minutes", "final", "draft",
        # Domain-wide tokens that appear in EVERY 7 GHz TIG filename.
        "7", "ghz", "tig", "group",
    }
)

# Significant tokens are everything else that survives the stopword and
# date filters. The tokens explicitly called out in the design — kept as
# a comment for grep-ability and to anchor reviewer intent:
#   downlink, uplink, p2p, point, fixed, transportable, adjudication,
#   kickoff, working, study, plan, comment, wg, ul, dl, satellite,
#   fss, mss
# These are NOT a closed allow-list (a new significant token in a
# future filename should pass through automatically); they're documented
# so the stopword set never silently swallows one.

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_PURE_DIGITS_RE = re.compile(r"^\d+$")


def _is_date_token(token: str) -> bool:
    """True if ``token`` looks like a meeting date in any recognised form.

    Catches: pure-digit dates (``20251218``), day-month-year run-together
    forms (``21jan26``, ``5feb2026``, ``22jan2026``), and any other token
    that ``extract_meeting_date`` resolves. Pure-numeric tokens are also
    discarded — they are either dates (``20260121``), domain noise
    (``"7"`` from "7 GHz"), or single-digit days (``"5"`` from
    "5Feb2026") with no disambiguation value.
    """
    if _PURE_DIGITS_RE.match(token):
        return True
    if extract_meeting_date(token) is not None:
        return True
    return False


def family_tokens(text: Optional[str]) -> set[str]:
    """Extract significant lowercase tokens from a meeting filename / title.

    Used by GroundTruthLinker to disambiguate same-day collisions. The
    function:

    1. Lowercases ``text`` and splits on every non-alphanumeric character.
    2. Drops any token that looks like a date (pure digits or matching a
       date regex) — meeting dates are the COLLISION key, never the
       disambiguator.
    3. Drops domain stopwords (``meeting``, ``transcript``, ``minutes``,
       ``ghz``, ``tig``, ``group``, …). These appear in every record on
       both sides and contribute zero discriminating signal.
    4. Drops the literal token ``study`` when it is immediately followed
       by ``group`` (the phrase "Study Group" is generic; "Study Plan
       Comment" is meaningful and ``study`` survives).

    Returns an empty set for ``None``, empty string, or a title made
    entirely of stopwords / dates. Never raises.
    """
    if not isinstance(text, str) or not text:
        return set()
    raw_tokens = _TOKEN_RE.findall(text.lower())
    out: set[str] = set()
    for i, tok in enumerate(raw_tokens):
        if _is_date_token(tok):
            continue
        if tok in _FAMILY_STOPWORDS:
            continue
        # "Study Group" is generic boilerplate; drop "study" only when
        # immediately followed by "group". Standalone "study" (e.g.
        # "Study Plan Comment") survives.
        if tok == "study" and i + 1 < len(raw_tokens) and raw_tokens[i + 1] == "group":
            continue
        out.add(tok)
    return out


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
