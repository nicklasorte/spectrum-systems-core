"""Phase T.3: deterministic speaker attribution for decisions and claims.

After the typed extractors have produced their items but before the
``meeting_extraction`` artifact is written, each item's ``source_turn_ids``
list is consulted to look up the originating chunk and attach a
``speaker`` and ``speaker_ambiguous`` field.

No model calls. The join is pure dict lookup so the speaker on a
decision is byte-stable given identical chunks. When a merged chunk
spans multiple speakers, the FIRST speaker wins (matching the chunk
merger's rule 5) and ``speaker_ambiguous: true`` is set so downstream
consumers can flag the item for review.

When a citation references a chunk that does not exist in the supplied
lookup table, the item gets ``speaker: null`` and a
``speaker_attribution_missing`` info finding is emitted. The decision
is NOT dropped -- citation drift is a forensic signal worth surfacing,
not a fatal error.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..health.finding import HealthFinding

_LOG = logging.getLogger(__name__)


def _chunk_speakers(chunk: Dict[str, Any]) -> List[str]:
    """Resolve all speaker labels associated with a chunk.

    A speaker-turn chunk has a single ``speaker`` field. A merged chunk
    (Phase R.0) carries the speaker of the FIRST component on
    ``speaker`` but may have unit_ids spanning multiple speakers. The
    chunker does not currently emit a per-unit speaker map, so the
    ambiguity check falls back to a ``speakers`` array if present, else
    a single-element list with the canonical ``speaker``. When neither
    is present, returns ``[]``.
    """
    speakers_field = chunk.get("speakers")
    if isinstance(speakers_field, list):
        out = [str(s).strip() for s in speakers_field if isinstance(s, str) and s.strip()]
        if out:
            return out
    sole = chunk.get("speaker")
    if isinstance(sole, str) and sole.strip():
        return [sole.strip()]
    return []


def resolve_speaker(
    item: Dict[str, Any],
    chunks_by_id: Dict[str, Dict[str, Any]],
) -> Tuple[Optional[str], bool, bool]:
    """Return ``(speaker_or_none, speaker_ambiguous, chunk_missing)``.

    Rules:
      1. Use the speaker of the FIRST chunk in ``source_turn_ids``.
      2. If that chunk has multiple speakers, set ``ambiguous=True``.
      3. If the chunk is not in the lookup table, set ``chunk_missing``;
         the caller emits ``speaker_attribution_missing``.
      4. If the item has no ``source_turn_ids``, ``chunk_missing=True``
         and ``speaker=None``.
    """
    turn_ids = item.get("source_turn_ids") or item.get("source_turns") or []
    if not isinstance(turn_ids, list) or not turn_ids:
        return None, False, True
    first_cid = turn_ids[0]
    if not isinstance(first_cid, str) or not first_cid:
        return None, False, True
    chunk = chunks_by_id.get(first_cid)
    if not isinstance(chunk, dict):
        return None, False, True
    speakers = _chunk_speakers(chunk)
    if not speakers:
        return None, False, False
    return speakers[0], (len(speakers) > 1), False


def attribute_speakers(
    items: Iterable[Dict[str, Any]],
    chunks_by_id: Dict[str, Dict[str, Any]],
    *,
    pipeline_run_id: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[HealthFinding]]:
    """Annotate every item with speaker / speaker_ambiguous in place.

    Returns ``(annotated_items, findings)``. Items are shallow-copied
    so the caller's input list is not mutated. Findings are constructed
    once per missing-chunk item so the run summary can report the
    aggregate rate.
    """
    annotated: List[Dict[str, Any]] = []
    findings: List[HealthFinding] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        speaker, ambiguous, chunk_missing = resolve_speaker(item, chunks_by_id)
        out = dict(item)
        out["speaker"] = speaker
        out["speaker_ambiguous"] = bool(ambiguous)
        annotated.append(out)
        if chunk_missing or speaker is None:
            findings.append(
                HealthFinding(
                    finding_code="speaker_attribution_missing",
                    severity="info",
                    pipeline_run_id=pipeline_run_id,
                    context={
                        "source_turn_ids": list(
                            item.get("source_turn_ids")
                            or item.get("source_turns")
                            or []
                        ),
                        "reason": (
                            "chunk_not_in_lookup"
                            if chunk_missing
                            else "chunk_has_no_speaker"
                        ),
                    },
                    remediation=(
                        "The cited chunk did not resolve to a speaker. "
                        "Either the citation drifted (chunk_id stale) or "
                        "the chunk lacked speaker metadata. The extraction "
                        "item is kept; the operator decides whether to "
                        "treat the missing speaker as a defect."
                    ),
                )
            )
    return annotated, findings


__all__ = [
    "attribute_speakers",
    "resolve_speaker",
]
