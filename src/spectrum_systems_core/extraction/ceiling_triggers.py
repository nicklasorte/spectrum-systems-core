"""Phase Y.1 — deterministic keyword table for the Opus ceiling gate.

The ``ceiling_minimum_counts`` eval needs a deterministic, model-free
answer to "does this transcript even contain a <schema_type>?" so it
can fail a ceiling that returned zero items for a type the transcript
visibly discusses. This module is that answer: a fixed keyword table
and a pure substring scan over the lowercased transcript.

No model call, no randomness, no I/O — the same transcript always
produces the same ``transcript_keyword_hits`` map, which is what makes
the Y.1 gate replay-stable.
"""
from __future__ import annotations

# The schema_type vocabulary the ceiling extractor and the alignment
# comparator share. Adding a type here means the gate will start
# expecting the ceiling to cover it whenever its keywords appear.
#
# "topics" (structural) was added after a 47-transcript calibration
# (2026-06-10, ~/trigger_calibration_findings.md): its keyword set hit
# 46/47 meetings that genuinely contain topics and, replayed against
# the git history of the truncated 7ghz-spd-sead-sync baselines, would
# have caught 2/2 real zero-topics truncations.
#
# "attendees" is DELIBERATELY ABSENT. The same calibration showed a
# keyword trigger for it is unreliable: 26/47 recall and 0/5 of the
# real attendees truncations caught, because in verbatim Teams
# transcripts attendance is encoded STRUCTURALLY (speaker-name +
# timestamp turn headers) with no lexical roll-call cue for a
# substring scan to find. Detecting attendees needs a future
# structural-signal gate (speaker-turn-header presence OR keyword),
# not an entry in this table — do not add it here.
#
# Rollback note: backing out the topics trigger means deleting
# "topics" from CEILING_SCHEMA_TYPES below and its entry in
# _KEYWORD_TABLE. The ceiling_minimum_counts gate (evals/runner.py)
# and the baseline-path gate (scripts/extract_opus_baseline.py) both
# iterate the keyword-hits map, so removing the entry stops both from
# expecting topics with no further edits. Already-written opus_ceiling
# artifacts keep their "topics" keys in per_type_counts /
# transcript_keyword_hits; the gate only reads types present in the
# hits map it is given, so replaying old artifacts stays valid.
CEILING_SCHEMA_TYPES: tuple[str, ...] = (
    "decision",
    "action_item",
    "open_question",
    "claim",
    "topics",
)

# schema_type -> keywords. Matched case-insensitively as plain
# substrings against the whole transcript. Kept deliberately
# conservative: a keyword should be specific enough that its presence
# is strong evidence the transcript genuinely contains that type, so a
# zero count for it is a real miss worth blocking on.
_KEYWORD_TABLE: dict[str, tuple[str, ...]] = {
    "decision": (
        "decision:",
        "we decided",
        "it was decided",
        "the group agreed",
        "we agreed",
        "approved",
        "rejected",
        "deferred",
        "resolved that",
    ),
    "action_item": (
        "action:",
        "action item",
        "will follow up",
        "to follow up",
        "assigned to",
        "owner:",
        "by next meeting",
        "take an action",
    ),
    "open_question": (
        "open question",
        "question:",
        "unresolved",
        "still unclear",
        "tbd",
        "to be determined",
        "we need to determine",
        "remains open",
    ),
    "claim": (
        "claim:",
        "asserts that",
        "states that",
        "according to",
        "the data shows",
        "evidence indicates",
    ),
    # Validated against all 47 corpus transcripts (calibration
    # 2026-06-10): fires on 46/47 meetings that genuinely contain
    # topics. The corpus has no topic-less meeting, so the stays-
    # silent-when-absent direction is covered by the synthetic negative
    # fixture in tests/extraction/test_topics_trigger.py instead. Do
    # NOT broaden beyond this validated tuple without re-running the
    # calibration.
    "topics": (
        "agenda",
        "agenda item",
        "next topic",
        "moving on to",
        "move on to",
        "first item",
        "next item",
        "let's move on",
        "next on the agenda",
        "first topic",
        "on today's agenda",
        "topics for",
    ),
}


def transcript_keyword_hits(transcript_text: str) -> dict[str, bool]:
    """Deterministic schema_type -> bool over ``CEILING_SCHEMA_TYPES``.

    True iff at least one keyword for that type appears (case-
    insensitive substring) in ``transcript_text``. Always returns an
    entry for every type in ``CEILING_SCHEMA_TYPES`` so the gate never
    has to guess at a missing key.
    """
    lowered = (transcript_text or "").lower()
    return {
        schema_type: any(kw in lowered for kw in _KEYWORD_TABLE[schema_type])
        for schema_type in CEILING_SCHEMA_TYPES
    }


__all__ = ["CEILING_SCHEMA_TYPES", "transcript_keyword_hits"]
