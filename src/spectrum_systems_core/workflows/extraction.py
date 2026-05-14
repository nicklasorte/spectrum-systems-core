"""Deterministic source-turn matcher for Phase Y.

Workflows call ``find_source_turns(item_text, chunks)`` to assign every
extracted item the turn_ids of the chunks it came from. The result is
deterministic given the same inputs — required by the constitution's
core trust property.

Algorithm (priority order, exactly):

1. Exact phrase match: if ``item_text`` is a substring of any chunk's
   ``text``, return ``[chunk["turn_id"]]`` for the LOWEST-INDEX matching
   chunk. Lowest index wins on ties.
2. LCS overlap: compute ``len(LCS(item_text, chunk["text"]))`` for every
   chunk. Return ``[chunk["turn_id"]]`` for the chunk with the highest
   score. Tiebreak: lowest chunk index.
3. Fallback: if no chunk has LCS > 0, return ``["t0000"]`` and the
   ``source_match_fallback`` finding code so the pipeline can record
   the degraded match.

This function NEVER returns an empty list. An empty list would silently
let a downstream eval pass — fail-loud instead. The fallback list still
points to a real chunk (the first one) so ``source_turn_validity`` does
not also fail; the ``source_match_fallback`` reason code is the signal
that something is wrong.

Callers that need to know whether the fallback fired (e.g. to emit a
``source_match_fallback`` finding) use :func:`match_source_turns`. The
two-tier API exists because the spec signature is ``-> list[str]``;
fallback detection has to live on a sibling function.
"""
from __future__ import annotations

from dataclasses import dataclass


FALLBACK_TURN_ID = "t0000"
SOURCE_MATCH_FALLBACK = "source_match_fallback"


@dataclass(frozen=True)
class MatchResult:
    """Result of one ``match_source_turns`` call.

    ``turn_ids`` is what callers should write into the artifact payload.
    ``was_fallback`` is True iff the match degraded to the fallback path
    (no exact-substring hit AND no chunk with LCS > 0). The pipeline
    converts ``was_fallback=True`` into a ``source_match_fallback``
    finding.
    """

    turn_ids: list[str]
    was_fallback: bool


def _lcs_length(a: str, b: str) -> int:
    """Length of the longest common subsequence of ``a`` and ``b``.

    Standard O(len(a) * len(b)) dynamic-programming implementation. The
    inputs are short (a transcript turn and an extracted item), so this
    is cheap; we do not bother with the Hunt-Szymanski refinement.
    """
    if not a or not b:
        return 0
    la, lb = len(a), len(b)
    prev = [0] * (lb + 1)
    curr = [0] * (lb + 1)
    for i in range(1, la + 1):
        ai = a[i - 1]
        for j in range(1, lb + 1):
            if ai == b[j - 1]:
                curr[j] = prev[j - 1] + 1
            else:
                curr[j] = max(prev[j], curr[j - 1])
        prev, curr = curr, [0] * (lb + 1)
    return prev[lb]


def match_source_turns(item_text: str, chunks: list[dict]) -> MatchResult:
    """Run the three-tier matcher; return turn_ids and the fallback flag.

    Raises ``ValueError`` on an empty ``chunks`` argument. Callers must
    guard before calling — a silent fallback on empty chunks would let
    fabricated extractions slip past the validity eval.
    """
    if not chunks:
        raise ValueError(
            "match_source_turns requires a non-empty chunks list; "
            "callers must guard against empty chunks before calling"
        )

    text = item_text or ""

    # 1. Exact substring match — lowest chunk index wins.
    if text:
        for chunk in chunks:
            if text in chunk.get("text", ""):
                return MatchResult(
                    turn_ids=[chunk["turn_id"]], was_fallback=False
                )

    # 2. LCS overlap — highest score wins, lowest chunk index tiebreak.
    if text:
        best_score = 0
        best_index: int | None = None
        for i, chunk in enumerate(chunks):
            score = _lcs_length(text, chunk.get("text", ""))
            if score > best_score:
                best_score = score
                best_index = i
        if best_index is not None and best_score > 0:
            return MatchResult(
                turn_ids=[chunks[best_index]["turn_id"]],
                was_fallback=False,
            )

    # 3. Fallback — first chunk. The caller is expected to emit the
    # ``source_match_fallback`` finding when ``was_fallback`` is True.
    return MatchResult(turn_ids=[FALLBACK_TURN_ID], was_fallback=True)


def find_source_turns(item_text: str, chunks: list[dict]) -> list[str]:
    """Spec signature: return just the turn_ids list.

    Identical behaviour to :func:`match_source_turns` but discards the
    fallback flag. Tests use this short form; the pipeline uses
    :func:`match_source_turns` so it can record fallbacks as findings.
    """
    return match_source_turns(item_text, chunks).turn_ids
