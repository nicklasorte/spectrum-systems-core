"""Phase 5 Variant C — meeting-type classifier + template loader.

Two responsibilities:

1. ``classify_meeting_type(source_id, title="")`` — pure function that
   maps a transcript ``source_id`` (e.g.
   ``"7-ghz-downlink-tig-meeting-kickoff---transcript-20251218"``) to
   one of the known meeting-type tokens. Returns ``"unknown"`` for
   ids that don't match any pattern.

2. ``load_template(meeting_type)`` — reads
   ``prompts/meeting_type_templates.md`` and returns the bounded
   section for the requested type. Returns ``None`` for ``"unknown"``
   (so the caller skips injection rather than emitting a useless
   "Unknown" preamble).

The classifier is intentionally narrow: pattern matching only on the
source_id string. It does not consult the data lake, does not read
metadata.json, and does not require the artifact pipeline to be
running. That keeps Variant C usable as a pure prompt-build step.

This module is NOT imported by the main extraction pipeline by default
— see the Phase 5 invariant "Variant C injects template at prompt
build time, not at schema level". The CLI wires this in only when
``--meeting-type-template`` is passed (or auto-detect is requested).
"""
from __future__ import annotations

from pathlib import Path
from typing import Final

MEETING_TYPE_DOWNLINK_TIG: Final[str] = "downlink_tig"
MEETING_TYPE_UPLINK_TIG: Final[str] = "uplink_tig"
MEETING_TYPE_P2P_TIG: Final[str] = "p2p_tig"
MEETING_TYPE_WORKING_GROUP: Final[str] = "working_group"
MEETING_TYPE_DOWNLINK_TIG_WORKING: Final[str] = "downlink_tig_working"
MEETING_TYPE_UPLINK_TIG_WORKING: Final[str] = "uplink_tig_working"
MEETING_TYPE_ADJUDICATION: Final[str] = "adjudication"
MEETING_TYPE_UNKNOWN: Final[str] = "unknown"


ALL_MEETING_TYPES: Final[frozenset[str]] = frozenset(
    {
        MEETING_TYPE_DOWNLINK_TIG,
        MEETING_TYPE_UPLINK_TIG,
        MEETING_TYPE_P2P_TIG,
        MEETING_TYPE_WORKING_GROUP,
        MEETING_TYPE_DOWNLINK_TIG_WORKING,
        MEETING_TYPE_UPLINK_TIG_WORKING,
        MEETING_TYPE_ADJUDICATION,
        MEETING_TYPE_UNKNOWN,
    }
)


TEMPLATES_PATH: Final[Path] = (
    Path(__file__).resolve().parent / "prompts" / "meeting_type_templates.md"
)


def classify_meeting_type(source_id: str, title: str = "") -> str:
    """Classify the meeting type from the source identifier and title.

    Pattern-matching only — no metadata.json read, no data-lake scan.
    The fall-back is :data:`MEETING_TYPE_UNKNOWN` so a caller can
    decide to skip injection entirely rather than receive a guess.

    Examples (the regression contract is exact-equality on these):

      >>> classify_meeting_type(
      ...     "7-ghz-downlink-tig-meeting-kickoff---transcript-20251218"
      ... )
      'downlink_tig'
      >>> classify_meeting_type("7-ghz-ul-kickoff-transcript-20251217")
      'uplink_tig'
      >>> classify_meeting_type("20251216---p2p-tig-meeting-16dec2025---transcript")
      'p2p_tig'
      >>> classify_meeting_type(
      ...     "7-ghz-study-working-group-meeting---transcript-20260115"
      ... )
      'working_group'
    """
    s = (source_id or "").lower()
    t = (title or "").lower()
    haystack = f"{s} {t}".strip()

    # Order matters: adjudication first (it can contain "tig" if a TIG
    # is being adjudicated), then p2p (most specific TIG flavour),
    # then kickoff variants of downlink/uplink, then working-session
    # variants, then working-group, then unknown.
    if "adjudication" in haystack or "comment-review" in haystack:
        return MEETING_TYPE_ADJUDICATION

    if "p2p" in haystack or "point-to-point" in haystack:
        return MEETING_TYPE_P2P_TIG

    # Working-group meetings are NOT TIG meetings; the "working-group"
    # token disambiguates them from "tig-working" sessions.
    if "working-group" in haystack or "wg-" in haystack or "study-working-group" in haystack:
        return MEETING_TYPE_WORKING_GROUP

    # TIG variants — distinguish kickoff from later working sessions.
    is_downlink = "downlink" in haystack or "dl-" in haystack or "-dl-" in haystack
    is_uplink = "uplink" in haystack or "ul-" in haystack or "-ul-" in haystack
    is_kickoff = "kickoff" in haystack
    is_working_session = (
        "working-session" in haystack
        or "working-meeting" in haystack
        or "tig-working" in haystack
    )

    if is_downlink:
        if is_kickoff:
            return MEETING_TYPE_DOWNLINK_TIG
        if is_working_session:
            return MEETING_TYPE_DOWNLINK_TIG_WORKING
        # A downlink-TIG token with no kickoff/working marker — default
        # to kickoff for the first observed meeting, working otherwise.
        # The classifier has no per-corpus memory; treat ambiguous as
        # kickoff (the conservative choice — kickoff expects fewer
        # items, so a working session classified as kickoff would
        # surface as over-extraction rather than silently passing).
        return MEETING_TYPE_DOWNLINK_TIG

    if is_uplink:
        if is_kickoff:
            return MEETING_TYPE_UPLINK_TIG
        if is_working_session:
            return MEETING_TYPE_UPLINK_TIG_WORKING
        return MEETING_TYPE_UPLINK_TIG

    return MEETING_TYPE_UNKNOWN


def _read_templates_source() -> str:
    if not TEMPLATES_PATH.is_file():
        raise FileNotFoundError(
            f"meeting_type_templates.md missing at {TEMPLATES_PATH}. "
            "Variant C requires this file; do not synthesize a default."
        )
    return TEMPLATES_PATH.read_text(encoding="utf-8")


def load_template(meeting_type: str) -> str | None:
    """Return the template body for ``meeting_type``, or ``None``.

    Returns ``None`` for :data:`MEETING_TYPE_UNKNOWN` so the caller can
    skip injection cleanly (the prompt is left at the baseline).
    For an unrecognised non-empty type, also returns ``None`` — the
    classifier is the gatekeeper for valid tokens; unknown values
    degrade to no-op rather than halting.
    """
    if meeting_type == MEETING_TYPE_UNKNOWN:
        return None
    if meeting_type not in ALL_MEETING_TYPES:
        return None

    source = _read_templates_source()
    begin_marker = f"<!-- TEMPLATE_BEGIN:{meeting_type} -->"
    end_marker = f"<!-- TEMPLATE_END:{meeting_type} -->"
    if begin_marker not in source or end_marker not in source:
        return None
    start = source.index(begin_marker) + len(begin_marker)
    end = source.index(end_marker)
    body = source[start:end].strip()
    return body


def build_meeting_context_preamble(meeting_type: str) -> str | None:
    """Return the full prompt preamble to inject, or ``None`` to skip.

    The returned block is a Markdown section titled ``## Meeting
    Context`` followed by the template body. The caller prepends this
    to the prompt before the ``## DO NOT EXTRACT`` section so the
    expected-counts ceiling primes every subsequent extraction rule.
    """
    template = load_template(meeting_type)
    if template is None:
        return None
    lines = [
        "## Meeting Context",
        "",
        f"Meeting type: {meeting_type}",
        "",
        template,
        "",
        (
            "Use this profile as a prior for extraction volume. If you "
            "find yourself extracting significantly more items than the "
            "expected range for any type, re-evaluate whether each item "
            "meets the type's strict definition."
        ),
    ]
    return "\n".join(lines)


__all__ = [
    "ALL_MEETING_TYPES",
    "MEETING_TYPE_ADJUDICATION",
    "MEETING_TYPE_DOWNLINK_TIG",
    "MEETING_TYPE_DOWNLINK_TIG_WORKING",
    "MEETING_TYPE_P2P_TIG",
    "MEETING_TYPE_UNKNOWN",
    "MEETING_TYPE_UPLINK_TIG",
    "MEETING_TYPE_UPLINK_TIG_WORKING",
    "MEETING_TYPE_WORKING_GROUP",
    "TEMPLATES_PATH",
    "build_meeting_context_preamble",
    "classify_meeting_type",
    "load_template",
]
