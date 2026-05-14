"""Phase P3-A T-1: post-extraction source-turn orphan and diversity gate.

Sister module to :mod:`source_turn_validity`. The validity eval (a
fail-closed gate against the source_record-on-disk) returns a
pass/fail eval_result. This module computes the *rate* metrics that
feed eval_summary so a regression in extraction grounding is visible
even when no item is fully orphaned.

Two metrics:

  - ``source_turn_orphan_rate`` — fraction of extracted items whose
    ``source_turn_ids`` reference at least one chunk id that does
    not appear in the live ``chunks.jsonl``. The "live" chunk id
    set is the set the runner already builds for
    ``available_turn_ids``; this module is intentionally additive
    to that set so the rate and the validity eval cannot drift.

  - ``source_turn_diversity_rate`` — number of distinct chunk ids
    cited across all extracted items, divided by the total number
    of valid chunk ids in the source. A low value means the model
    is over-citing a tiny cluster of chunks (the "the model just
    quotes turn 3 for everything" failure mode).

The module never raises and never reads from disk: it operates
purely on lists already in memory in the runner.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Sequence, Set


# How many orphaned item ids to surface in the report. Beyond this
# count the report degrades to a single ``... N more`` line so the
# log entry stays bounded on a fully-orphaned run.
_MAX_ORPHANED_IDS_REPORTED: int = 10


@dataclass
class SourceTurnReport:
    item_type: str
    total: int = 0
    orphan_count: int = 0
    orphan_rate: float = 0.0
    orphaned_item_ids: List[str] = field(default_factory=list)
    distinct_turns_cited: int = 0
    available_turn_count: int = 0
    diversity_rate: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "item_type": self.item_type,
            "total": self.total,
            "orphan_count": self.orphan_count,
            "orphan_rate": self.orphan_rate,
            "orphaned_item_ids": list(self.orphaned_item_ids),
            "distinct_turns_cited": self.distinct_turns_cited,
            "available_turn_count": self.available_turn_count,
            "diversity_rate": self.diversity_rate,
        }


def _item_turn_ids(item: Dict[str, Any]) -> List[str]:
    """Return the chunk-id strings cited by ``item``.

    Supports both ``source_turn_ids`` (the meeting_extraction schema
    field name) and ``source_turns`` (the alias the source_turn
    validity eval accepts). A non-list or empty value returns an
    empty list -- the caller decides whether that counts as an
    orphan or just "no provenance recorded".
    """
    raw = item.get("source_turn_ids")
    if raw is None:
        raw = item.get("source_turns")
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if isinstance(x, (str, int))]


def _item_identifier(item: Dict[str, Any], fallback: str) -> str:
    """Stable id for an item used in orphan reporting.

    Falls back to the primary text field for each extractor's items
    so an operator can recognise the orphaned record without an
    explicit ``id`` field.
    """
    for key in ("id", "decision_id", "claim_id", "action_id"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("decision_text", "claim_text", "action"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value[:80]
    return fallback


def compute_source_turn_report(
    extracted_items: Sequence[Dict[str, Any]],
    valid_turn_ids: Iterable[str],
    *,
    item_type: str,
) -> SourceTurnReport:
    """Compute the orphan and diversity metrics for ``extracted_items``.

    ``valid_turn_ids`` is the live set of chunk ids the runner
    already built. A case-insensitive comparison is used so a chunk
    id written with a different case in the extraction output
    still resolves; this mirrors the tolerance the task spec asks
    for in its source-turn-validity gate pseudocode.
    """
    valid_lower: Set[str] = {str(t).lower() for t in valid_turn_ids if t is not None}
    report = SourceTurnReport(item_type=item_type)
    report.available_turn_count = len(valid_lower)

    cited_turns: Set[str] = set()
    for index, item in enumerate(extracted_items or []):
        if not isinstance(item, dict):
            continue
        report.total += 1
        turn_ids = _item_turn_ids(item)
        if not turn_ids:
            # No provenance recorded -- not an orphan because there
            # is nothing to resolve. Surfaced upstream via the
            # source_turn_validation field on the item itself.
            continue
        item_orphaned = False
        for turn_id in turn_ids:
            cited_turns.add(turn_id.lower())
            if turn_id.lower() not in valid_lower:
                item_orphaned = True
        if item_orphaned:
            report.orphan_count += 1
            if len(report.orphaned_item_ids) < _MAX_ORPHANED_IDS_REPORTED:
                report.orphaned_item_ids.append(
                    _item_identifier(item, fallback=f"index_{index}")
                )

    report.orphan_rate = (
        report.orphan_count / report.total if report.total > 0 else 0.0
    )
    report.distinct_turns_cited = len(cited_turns)
    if valid_lower:
        # Diversity is over the LIVE turn population, not the
        # cited-turn population: "of N available turns, how many did
        # the model use?" Cap at 1.0 in case an item cites a turn id
        # that is not in the live set (it still bumps cited_turns).
        report.diversity_rate = min(1.0, report.distinct_turns_cited / len(valid_lower))
    else:
        report.diversity_rate = 0.0
    return report


def aggregate_source_turn_reports(
    reports: Sequence[SourceTurnReport],
) -> Dict[str, Any]:
    """Aggregate per-item-type reports into a single eval_summary block.

    Aggregate metrics:

      - ``total`` is the sum across types.
      - ``orphan_count`` is the sum.
      - ``orphan_rate`` is ``orphan_count / total`` across the
        aggregate (NOT an average of per-type rates).
      - ``distinct_turns_cited`` is the size of the union of cited
        turn ids across types (recomputed by the runner from raw
        items, not summed from per-type counts -- the runner builds
        a single :func:`compute_source_turn_report` call for the
        combined item list before passing the union here, OR a
        caller can keep the per-type reports and trust the
        aggregate's union via a separate call to
        :func:`compute_source_turn_report` with the combined list).
      - ``diversity_rate`` from the same union call.
    """
    total = sum(r.total for r in reports)
    orphan_count = sum(r.orphan_count for r in reports)
    return {
        "total_items": total,
        "orphan_count": orphan_count,
        "orphan_rate": orphan_count / total if total > 0 else 0.0,
        "by_type": {r.item_type: r.as_dict() for r in reports},
    }


__all__ = [
    "SourceTurnReport",
    "aggregate_source_turn_reports",
    "compute_source_turn_report",
]
