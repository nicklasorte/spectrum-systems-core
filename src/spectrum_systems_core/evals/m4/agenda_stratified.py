"""Phase R.4: agenda-stratified eval metrics adapter.

The existing ``compute_per_agenda_item_metrics`` (Phase W) keys metrics
by ``agenda_item_id`` and excludes small agendas. R.4 reshapes that
output into a top-level ``per_agenda_section_metrics`` dict the
RegressionGate consumes, with two extra invariants:

1. **Unclassified fallback.** When AgendaDetector did not run (Phase W
   flag off or detection failed), the result must still contain a single
   ``"unclassified"`` section so the eval summary shape is stable.
2. **Safe baseline diff.** When comparing against a baseline, missing
   sections in baseline are *skipped* (not an error), and new sections
   in the current run are recorded as ``new_sections_discovered``.

The functions in this module are pure helpers. They never raise.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

UNCLASSIFIED_SECTION: str = "unclassified"


def _label_for_agenda(
    agenda_id: str, agenda_items: Sequence[Mapping[str, Any]],
) -> str:
    """Return a stable label for ``agenda_id``.

    Prefers an ``agenda_item_label`` field (R.4-aware AgendaDetector
    output), falls back to ``title`` or ``label``, falls back to the
    agenda_id itself.
    """
    for item in agenda_items or []:
        if not isinstance(item, dict):
            continue
        if item.get("agenda_item_id") != agenda_id:
            continue
        for key in ("agenda_item_label", "label", "title"):
            v = item.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return agenda_id
    return agenda_id


def build_per_agenda_section_metrics(
    per_agenda_item_metrics: Mapping[str, Any],
    agenda_items: Sequence[Mapping[str, Any]],
    *,
    chunks: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Reshape ``compute_per_agenda_item_metrics`` output to R.4 format.

    Each agenda_id becomes a section keyed by its label (or the id when
    no label is available). When no agenda metrics were produced (either
    Phase W disabled or detection failed) the result collapses to a
    single ``"unclassified"`` section so the schema-on-paper stays
    consistent across rollback states.

    Returns::

        {
          "agenda_item_<label>": {
            "coverage": float | "excluded_small",
            "precision": float | "excluded_small",
            "pairs": int,   # chunk count for the agenda
          },
          ...
        }
    """
    coverage = (per_agenda_item_metrics or {}).get("coverage_by_agenda_item") or {}
    precision = (per_agenda_item_metrics or {}).get("precision_by_agenda_item") or {}

    if not coverage and not precision:
        return {UNCLASSIFIED_SECTION: _unclassified_section(chunks)}

    # Count chunks per agenda for the ``pairs`` field. Chunks without an
    # agenda_item_id are pooled under ``unclassified``.
    chunk_counts: dict[str, int] = {}
    unclassified_chunks = 0
    for c in chunks or []:
        if not isinstance(c, dict):
            continue
        aid = c.get("agenda_item_id")
        if isinstance(aid, str) and aid:
            chunk_counts[aid] = chunk_counts.get(aid, 0) + 1
        else:
            unclassified_chunks += 1

    sections: dict[str, Any] = {}
    for aid, cov in coverage.items():
        label = _label_for_agenda(aid, agenda_items)
        section_key = f"agenda_item_{label}".strip()
        if not section_key:
            section_key = f"agenda_item_{aid}"
        sections[section_key] = {
            "coverage": cov,
            "precision": precision.get(aid, "excluded_small"),
            "pairs": int(chunk_counts.get(aid, 0)),
        }

    if unclassified_chunks > 0:
        sections[UNCLASSIFIED_SECTION] = {
            "coverage": 0.0,
            "precision": 0.0,
            "pairs": unclassified_chunks,
        }

    return sections


def _unclassified_section(chunks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "coverage": 0.0,
        "precision": 0.0,
        "pairs": int(len(list(chunks))) if chunks else 0,
    }


def diff_against_baseline(
    current: Mapping[str, Any], baseline: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare two ``per_agenda_section_metrics`` snapshots safely.

    Missing sections in baseline are skipped (not an error). New
    sections in current are recorded so the regression gate can choose
    to surface them.
    """
    base = baseline or {}
    cur = current or {}
    new_sections: list[str] = []
    missing_in_current: list[str] = []
    common_diffs: dict[str, dict[str, float]] = {}

    for section_key, cur_metrics in cur.items():
        if not isinstance(cur_metrics, dict):
            continue
        base_metrics = base.get(section_key)  # .get() per spec
        if base_metrics is None:
            new_sections.append(section_key)
            continue
        # Coverage / precision diff (None when baseline entry was
        # ``excluded_small`` rather than a number).
        diff: dict[str, float] = {}
        for metric in ("coverage", "precision"):
            cur_val = cur_metrics.get(metric)
            base_val = base_metrics.get(metric)
            if isinstance(cur_val, (int, float)) and isinstance(base_val, (int, float)):
                diff[f"{metric}_delta"] = float(cur_val) - float(base_val)
        if diff:
            common_diffs[section_key] = diff

    for section_key in base.keys():
        if section_key not in cur:
            missing_in_current.append(section_key)

    return {
        "new_sections_discovered": new_sections,
        "missing_sections_in_current": missing_in_current,
        "diffs": common_diffs,
    }


__all__ = [
    "UNCLASSIFIED_SECTION",
    "build_per_agenda_section_metrics",
    "diff_against_baseline",
]
