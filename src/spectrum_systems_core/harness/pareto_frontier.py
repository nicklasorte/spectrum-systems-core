"""Phase AA.6 — Pareto frontier tracker.

File: ``processed/meetings/<meeting_id>/pareto_frontier.json`` (one per
transcript).

The frontier is **append-only and re-derivable**. Every
:func:`update_pareto_frontier` call rebuilds it FROM SCRATCH by reading
all ``score_summary__*.json`` files for the transcript — the JSON file
is a cache, never a source of truth. If it is missing or corrupted the
function re-derives and overwrites it; it NEVER halts (Red-Team: a
corrupt frontier must self-heal, not block the loop).

Dominance: candidate ``A`` dominates ``B`` iff ``A.total_f1 >=
B.total_f1`` AND ``A.context_tokens_used <= B.context_tokens_used``
(or ``B`` has null tokens), with at least one dimension strictly
better. Null-token handling (Red-Team Pass-1 #4): a null-token
candidate may be dominated by any candidate with >= f1, but a
null-token candidate never dominates a candidate that DOES report
tokens — unknown cost cannot win on cost.
"""
from __future__ import annotations

import datetime
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._io import read_json, write_json

PARETO_FRONTIER_FILENAME = "pareto_frontier.json"
_SCORE_SUMMARY_GLOB = "score_summary__*.json"

Clock = Callable[[], str]


def _now() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def pareto_frontier_path(processed_dir: Path | str) -> Path:
    return Path(processed_dir) / PARETO_FRONTIER_FILENAME


@dataclass(frozen=True)
class _Point:
    candidate_id: str
    candidate_type: str
    total_f1: float
    context_tokens_used: int | None
    trial_id: str
    produced_at: str

    def as_entry(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "candidate_type": self.candidate_type,
            "total_f1": self.total_f1,
            "context_tokens_used": self.context_tokens_used,
            "trial_id": self.trial_id,
            "produced_at": self.produced_at,
        }

    def sort_key(self) -> tuple:
        # Deterministic: best F1 first, then cheapest (null tokens last),
        # then stable on the candidate identity tuple.
        tok = self.context_tokens_used
        return (
            -self.total_f1,
            tok if tok is not None else float("inf"),
            self.trial_id,
            self.candidate_id,
        )


def _dominates(a: _Point, b: _Point) -> bool:
    """True iff ``a`` Pareto-dominates ``b`` (strictly better in at
    least one dimension, no worse in the other)."""
    if a is b:
        return False
    if a.total_f1 < b.total_f1:
        return False  # must be >= on F1

    a_tok = a.context_tokens_used
    b_tok = b.context_tokens_used
    if b_tok is None:
        token_ok = True  # "or B has null tokens"
    elif a_tok is None:
        # Unknown cost never wins on the cost axis against a known cost.
        return False
    else:
        token_ok = a_tok <= b_tok
    if not token_ok:
        return False

    strictly_better = a.total_f1 > b.total_f1
    if not strictly_better:
        if b_tok is None and a_tok is not None:
            strictly_better = True
        elif (
            a_tok is not None
            and b_tok is not None
            and a_tok < b_tok
        ):
            strictly_better = True
    return strictly_better


def _read_points(processed_dir: Path) -> list[_Point]:
    points: list[_Point] = []
    for path in sorted(processed_dir.glob(_SCORE_SUMMARY_GLOB)):
        summary = read_json(path)
        if not isinstance(summary, dict):
            continue
        f1 = summary.get("total_f1")
        if not isinstance(f1, (int, float)):
            # A trial with no scored F1 cannot sit on an F1 frontier.
            continue
        tok = summary.get("context_tokens_used")
        tok_val = int(tok) if isinstance(tok, (int, float)) else None
        trial_id = str(summary.get("trial_id") or path.stem)
        points.append(
            _Point(
                candidate_id=str(
                    summary.get("candidate_id") or trial_id
                ),
                candidate_type=str(
                    summary.get("candidate_type") or "prompt"
                ),
                total_f1=float(f1),
                context_tokens_used=tok_val,
                trial_id=trial_id,
                produced_at=str(summary.get("produced_at") or ""),
            )
        )
    return points


def _derive_frontier(points: list[_Point]) -> list[_Point]:
    """Non-dominated points only. Exact (f1, tokens, candidate_id)
    duplicates are de-duplicated deterministically so re-derivation is
    idempotent."""
    seen: set[tuple] = set()
    unique: list[_Point] = []
    for p in sorted(points, key=lambda x: x.sort_key()):
        key = (p.total_f1, p.context_tokens_used, p.candidate_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)

    frontier = [
        p
        for p in unique
        if not any(_dominates(o, p) for o in unique if o is not p)
    ]
    frontier.sort(key=lambda x: x.sort_key())
    return frontier


def update_pareto_frontier(
    *,
    processed_dir: Path | str,
    transcript_id: str,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Re-derive the frontier from every score summary on disk and
    overwrite ``pareto_frontier.json``. Never halts."""
    processed_dir = Path(processed_dir)
    processed_dir.mkdir(parents=True, exist_ok=True)
    points = _read_points(processed_dir)
    frontier = _derive_frontier(points)
    doc = {
        "transcript_id": transcript_id,
        "updated_at": (clock or _now)(),
        "frontier": [p.as_entry() for p in frontier],
    }
    write_json(pareto_frontier_path(processed_dir), doc)
    return doc


def load_pareto_frontier(
    processed_dir: Path | str,
) -> list[dict[str, Any]]:
    """Best-effort read of the cached frontier list. Returns ``[]`` on a
    missing or corrupt file — the caller should re-derive."""
    doc = read_json(pareto_frontier_path(processed_dir))
    if not isinstance(doc, dict):
        return []
    frontier = doc.get("frontier")
    return frontier if isinstance(frontier, list) else []


__all__ = [
    "PARETO_FRONTIER_FILENAME",
    "pareto_frontier_path",
    "update_pareto_frontier",
    "load_pareto_frontier",
]
