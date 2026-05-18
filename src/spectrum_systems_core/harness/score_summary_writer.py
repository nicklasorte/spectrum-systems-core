"""Phase AA.2 — score summary artifact.

Writes ``score_summary__<trial_id>.json`` next to the trial's AA.1
harness snapshot. This is a lightweight, proposer-readable score file —
NOT a governed envelope (no ``artifact_type``, no promotion gate, no
data-lake index entry), exactly like ``debug__<run_id>.json``.

The single non-negotiable invariant: the ``harness_snapshot_commit_sha``
written here MUST equal the sha recorded in the AA.1
``harness_snapshot__<trial_id>/commit_sha.txt``. If the score-summary
caller thinks it is at a different commit than the snapshot was taken
at, the trial's F1 cannot be attributed to a known code state, so the
writer halts with ``commit_sha_mismatch`` and writes nothing. A missing
or unreadable snapshot sha is the same fail-closed halt
(``commit_sha_unavailable``) — never write a summary whose provenance
cannot be proven.
"""
from __future__ import annotations

import datetime
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ._io import write_json
from .trace_capture import harness_snapshot_dirname

SCORE_SUMMARY_PREFIX = "score_summary__"

REQUIRED_FIELDS: tuple[str, ...] = (
    "trial_id",
    "transcript_id",
    "produced_at",
    "total_f1",
    "per_type_f1",
    "false_negative_count",
    "false_positive_count",
    "harness_snapshot_commit_sha",
    "extraction_alignment_comparison_artifact_id",
    "prompt_addition_ids_active",
    "note",
)

Clock = Callable[[], str]


class ScoreSummaryError(RuntimeError):
    def __init__(self, message: str, *, reason_code: str):
        super().__init__(message)
        self.reason_code = reason_code


def _now() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def score_summary_filename(trial_id: str) -> str:
    return f"{SCORE_SUMMARY_PREFIX}{trial_id}.json"


def _snapshot_commit_sha(processed_dir: Path, trial_id: str) -> str:
    """Read the AA.1 snapshot's recorded sha. Fail-closed: a missing or
    empty file is ``commit_sha_unavailable`` (never silently treated as
    a match)."""
    sha_path = (
        processed_dir
        / harness_snapshot_dirname(trial_id)
        / "commit_sha.txt"
    )
    try:
        recorded = sha_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ScoreSummaryError(
            f"harness snapshot commit_sha.txt unreadable: {exc}",
            reason_code="commit_sha_unavailable",
        ) from exc
    if not recorded:
        raise ScoreSummaryError(
            "harness snapshot commit_sha.txt is empty",
            reason_code="commit_sha_unavailable",
        )
    return recorded


def write_score_summary(
    *,
    processed_dir: Path | str,
    trial_id: str,
    transcript_id: str,
    expected_commit_sha: str,
    total_f1: float | None,
    per_type_f1: dict[str, float] | None = None,
    false_negative_count: int = 0,
    false_positive_count: int = 0,
    extraction_alignment_comparison_artifact_id: str | None = None,
    prompt_addition_ids_active: list[str] | None = None,
    note: str | None = None,
    context_tokens_used: int | None = None,
    candidate_id: str | None = None,
    candidate_type: str | None = None,
    clock: Clock | None = None,
) -> Path:
    """Write ``score_summary__<trial_id>.json``. Halt on a sha mismatch.

    ``expected_commit_sha`` is the commit the scoring caller believes it
    ran the trial at. It is checked against the AA.1 snapshot's recorded
    sha BEFORE any file is written, so a divergence can never produce a
    summary attributing F1 to the wrong code state.
    """
    processed_dir = Path(processed_dir)
    recorded = _snapshot_commit_sha(processed_dir, trial_id)
    if recorded != expected_commit_sha:
        raise ScoreSummaryError(
            f"snapshot commit sha {recorded!r} != expected "
            f"{expected_commit_sha!r}",
            reason_code="commit_sha_mismatch",
        )

    summary: dict[str, Any] = {
        "trial_id": trial_id,
        "transcript_id": transcript_id,
        "produced_at": (clock or _now)(),
        "total_f1": total_f1,
        "per_type_f1": dict(per_type_f1 or {}),
        "false_negative_count": int(false_negative_count),
        "false_positive_count": int(false_positive_count),
        "harness_snapshot_commit_sha": recorded,
        "extraction_alignment_comparison_artifact_id": (
            extraction_alignment_comparison_artifact_id
        ),
        "prompt_addition_ids_active": list(prompt_addition_ids_active or []),
        "note": note,
        # AA.6 inputs: the Pareto tracker re-derives the frontier from
        # these. Always present (nullable) so the frontier builder never
        # branches on a missing key. Not in REQUIRED_FIELDS — the AA.2
        # contract's required set is exactly the brief's 11 fields.
        "context_tokens_used": (
            int(context_tokens_used)
            if context_tokens_used is not None
            else None
        ),
        "candidate_id": candidate_id if candidate_id else trial_id,
        "candidate_type": candidate_type or "prompt",
    }
    # Defensive: prove every required field is present before write so a
    # future edit that drops a key fails here, not in the proposer.
    missing = [f for f in REQUIRED_FIELDS if f not in summary]
    if missing:
        raise ScoreSummaryError(
            f"score_summary missing required fields: {missing}",
            reason_code="malformed_score_summary",
        )

    out = processed_dir / score_summary_filename(trial_id)
    write_json(out, summary)
    return out


__all__ = [
    "SCORE_SUMMARY_PREFIX",
    "REQUIRED_FIELDS",
    "ScoreSummaryError",
    "score_summary_filename",
    "write_score_summary",
]
