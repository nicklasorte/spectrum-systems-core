"""Tests for the shared meeting-date extraction utility."""
from __future__ import annotations

from spectrum_systems_core.ingestion.date_utils import extract_meeting_date


def test_yyyymmdd_format() -> None:
    assert extract_meeting_date("20251218") == "2025-12-18"
    assert extract_meeting_date("Some Filename 20260115 - Transcript") == "2026-01-15"


def test_m_dd_yy_format() -> None:
    assert extract_meeting_date("2-19-26") == "2026-02-19"
    assert extract_meeting_date("Filename 2-17-26") == "2026-02-17"
    # 4-digit year still works.
    assert extract_meeting_date("Meeting 2-19-2026") == "2026-02-19"


def test_ddmonyyyy_format() -> None:
    assert extract_meeting_date("22Jan2026") == "2026-01-22"
    assert extract_meeting_date("Meeting Transcript - 22Jan2026") == "2026-01-22"
    # Hyphen-separated variant.
    assert extract_meeting_date("22-Jan-2026") == "2026-01-22"


def test_ddmonyy_format() -> None:
    assert extract_meeting_date("21Jan26") == "2026-01-21"
    assert extract_meeting_date("Uplink TIG Transcript 21Jan26") == "2026-01-21"


def test_ddmonyyyy_no_separator() -> None:
    """Single-digit day with no separator before the month name."""
    assert extract_meeting_date("5Feb2026") == "2026-02-05"
    assert extract_meeting_date("Working Group 5Mar2026 - Transcript") == "2026-03-05"


def test_month_dd_yyyy_format() -> None:
    assert extract_meeting_date("February 19, 2026") == "2026-02-19"
    assert extract_meeting_date("These minutes from January 22, 2026.") == "2026-01-22"
    # Without comma.
    assert extract_meeting_date("Meeting on December 18 2025") == "2025-12-18"


def test_no_date_returns_none() -> None:
    assert extract_meeting_date("no date here") is None
    assert extract_meeting_date("") is None
    # Month-only-with-year is intentionally NOT matched (no day-of-month).
    assert extract_meeting_date("Some Meeting Jan2026") is None


def test_never_raises() -> None:
    # Non-string and malformed inputs must not raise.
    assert extract_meeting_date(None) is None  # type: ignore[arg-type]
    assert extract_meeting_date(12345) is None  # type: ignore[arg-type]
    # Numbers that look like dates but represent invalid calendar values.
    assert extract_meeting_date("99999999") is None
    # 99999999 — year=9999, month=99, day=99 fails _safe_iso_date and
    # the function falls through other patterns to None.
    assert extract_meeting_date("13-32-26") is None  # month=13, day=32 invalid
    # A long mixed string with no extractable date must not raise.
    weird = "x" * 10000 + "\x00​" + "no date here"
    assert extract_meeting_date(weird) is None


def test_real_transcript_filenames_all_extract_correctly() -> None:
    """Smoke check across the 13 actual production filenames."""
    cases = {
        "20251216 - P2P TIG Meeting 16Dec2025 - Transcript": "2025-12-16",
        "7 GHz UL Kickoff transcript 20251217": "2025-12-17",
        "7 GHz Downlink TIG Meeting Kickoff - transcript 20251218": "2025-12-18",
        "7 GHz Study Working Group Meeting - transcript 20260115": "2026-01-15",
        "7 GHz Fixed_Transportable Point to Point (P2P) TIG Meeting Transcript 20260120": "2026-01-20",
        "7 GHz Study Plan Comment Adjudication Meeting with Working Group - Transcript 20260121": "2026-01-21",
        "7 GHz Uplink TIG Meeting Transcript 21Jan26": "2026-01-21",
        "7 GHz Downlink TIG Meeting Transcript - 22Jan2026": "2026-01-22",
        "7 GHz Study Working Group Meeting 5Feb2026 - Transcript": "2026-02-05",
        "7 GHz P2P TIG - Transcript 2-17-26": "2026-02-17",
        "7 GHz Uplink TIG - Transcript 2-18-26": "2026-02-18",
        "7 GHz Downlink TIG Meeting - transcript 2-19-26": "2026-02-19",
        "7 GHz Study Working Group Meeting - 5Mar2026 - Transcript": "2026-03-05",
    }
    for filename, expected in cases.items():
        assert extract_meeting_date(filename) == expected, filename
