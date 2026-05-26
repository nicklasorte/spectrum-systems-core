"""Corpus-level manifest mapping ``source_id`` → ``(date, meeting_type)``.

The minutes-pairing planner (``scripts/_match_minutes_transcripts.py``) keys
matches on a single 8-digit date token. That works for most meetings but
collides when two distinct meetings share a date — for 2026-01-21 the
data lake carries BOTH a 7 GHz Uplink TIG transcript AND a 7 GHz
Adjudication WG transcript. Date alone cannot distinguish them, so the
planner emits ``ambiguous`` and skips both pairings.

This module narrows the key from ``date`` to ``(date, meeting_type)``.
The minutes filename carries enough information to classify which
meeting it covers; a parallel manifest gives the corresponding mapping
for each known transcript ``source_id``. With both classifiers in
agreement, the Jan 21 pair resolves cleanly.

This is data, not policy: control and promotion are unaffected. The
manifest exists to break the pairing tie deterministically; no LLM
calls, no guesses.
"""
from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "CORPUS_MANIFEST",
    "classify_minutes_filename",
    "find_minutes_for_source",
]


# Single source of truth for ``source_id`` → ``(YYYYMMDD, meeting_type)``.
# The 13 entries are the transcripts currently ingested into the data lake;
# adding a new transcript means adding an entry here. Keep the keys in
# chronological order so the file reads like a timeline.
CORPUS_MANIFEST: dict[str, tuple[str, str]] = {
    "20251216---p2p-tig-meeting-16dec2025---transcript":
        ("20251216", "p2p_tig_kickoff"),
    "7-ghz-ul-kickoff-transcript-20251217":
        ("20251217", "uplink_tig_kickoff"),
    "7-ghz-downlink-tig-meeting-kickoff---transcript-20251218":
        ("20251218", "downlink_tig_kickoff"),
    "7-ghz-study-working-group-meeting---transcript-20260115":
        ("20260115", "working_group"),
    "7-ghz-fixed_transportable-point-to-point--p2p--tig-meeting-transcript-20260120":
        ("20260120", "p2p_tig"),
    "7-ghz-study-plan-comment-adjudication-meeting-with-working-group---transcript-20260121":
        ("20260121", "adjudication_wg"),
    "7-ghz-uplink-tig-meeting-transcript-21jan26":
        ("20260121", "uplink_tig"),
    "7-ghz-downlink-tig-meeting-transcript---22jan2026":
        ("20260122", "downlink_tig"),
    "7-ghz-study-working-group-meeting-5feb2026---transcript":
        ("20260205", "working_group"),
    "7-ghz-p2p-tig---transcript-2-17-26":
        ("20260217", "p2p_tig"),
    "7-ghz-uplink-tig---transcript-2-18-26":
        ("20260218", "uplink_tig"),
    "7-ghz-downlink-tig-meeting---transcript-2-19-26":
        ("20260219", "downlink_tig"),
    "7-ghz-study-working-group-meeting---5mar2026---transcript":
        ("20260305", "working_group"),
}


# ``Adjudication`` is checked BEFORE ``Working Group`` because the
# adjudication filename embeds the phrase "Adjudication Meeting with
# Working Group"; a naive WG check would mis-classify it. Order matters.
def classify_minutes_filename(filename: str) -> tuple[str, str]:
    """Return ``(YYYYMMDD, meeting_type)`` for an NTIA minutes filename.

    The meeting_type tokens match the ``CORPUS_MANIFEST`` vocabulary so
    a transcript and its minutes resolve to the same pair. Returns
    ``("unknown", "unknown")`` rather than raising — callers handle a
    missing date / unrecognized type by reporting it as unmatched.
    """
    name = filename.lower()

    date_match = re.search(r"(\d{8})", filename)
    date = date_match.group(1) if date_match else "unknown"

    if "adjudication" in name or "comment" in name:
        meeting_type = "adjudication_wg"
    elif "p2p" in name or "point-to-point" in name or "point to point" in name:
        meeting_type = "p2p_tig_kickoff" if "kickoff" in name else "p2p_tig"
    elif "uplink" in name or " ul " in name or name.startswith("ul "):
        meeting_type = "uplink_tig_kickoff" if "kickoff" in name else "uplink_tig"
    elif "downlink" in name or " dl " in name or name.startswith("dl "):
        meeting_type = "downlink_tig_kickoff" if "kickoff" in name else "downlink_tig"
    elif "working group" in name or " wg " in name or "wg meeting" in name:
        meeting_type = "working_group"
    else:
        meeting_type = "unknown"

    return date, meeting_type


def find_minutes_for_source(
    source_id: str, minutes_dir: Path
) -> Path | None:
    """Locate the minutes ``.txt`` that pairs with ``source_id``.

    Matching keys on ``(date, meeting_type)``, not date alone, so two
    meetings on the same calendar day resolve to distinct files.
    Returns ``None`` when the source is unknown, the directory is
    absent, or no minutes filename classifies to the same pair.
    """
    pair = CORPUS_MANIFEST.get(source_id)
    if pair is None:
        return None
    date, meeting_type = pair

    if not minutes_dir.is_dir():
        return None

    for candidate in sorted(minutes_dir.glob("*.txt")):
        cdate, ctype = classify_minutes_filename(candidate.name)
        if cdate == date and ctype == meeting_type:
            return candidate
    return None
