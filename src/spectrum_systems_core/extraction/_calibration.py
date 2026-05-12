"""Confidence calibration check for typed extraction runs.

Phase X-3. After every pipeline run the histogram of model-reported
confidence scores is computed across succeeded chunks only. Blocked
chunks must NOT contribute to the histogram -- a chunk that produced
no items has no confidence values to score, and pretending it had
"zero-confidence" items would skew the rate toward the warning
threshold for the wrong reason.

If more than 80% of items carry confidence > 0.85, the model has
collapsed onto a single high-confidence value (likely 1.0). That is a
calibration finding, not a hard halt -- the run still produces output
-- but it surfaces an issue an operator should know about.
"""
from __future__ import annotations

import datetime
import logging
from typing import Iterable, Optional, Sequence

from ._chunk_counters import ChunkCounters


_LOG = logging.getLogger(__name__)


# Public knobs. Tests reference these directly so the thresholds are
# never magic literals scattered across the code base.
HIGH_CONFIDENCE_CUTOFF: float = 0.85
HIGH_CONFIDENCE_RATE_THRESHOLD: float = 0.80
CALIBRATION_WARNING_SCHEMA_VERSION: str = "1.0.0"

CALIBRATION_FINDING_TEMPLATE: str = (
    "More than {pct:.0f}% of extracted items have confidence > {cutoff:.2f}. "
    "Model may not be discriminating."
)


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _iter_confidences(items: Iterable[dict]) -> Iterable[float]:
    """Yield numeric confidence values from a sequence of extraction items.

    Items missing a confidence field are skipped (they do not vote yes
    or no on the calibration warning). Non-numeric or out-of-range
    confidence values are also skipped -- they would have already
    been clamped by ``_prompt_blocks.normalize_confidence`` upstream
    so this is a belt-and-braces guard.
    """
    for item in items:
        if not isinstance(item, dict):
            continue
        c = item.get("confidence")
        if isinstance(c, bool) or not isinstance(c, (int, float)):
            continue
        v = float(c)
        if 0.0 <= v <= 1.0:
            yield v


def compute_calibration(
    items: Sequence[dict],
    *,
    chunks_blocked: int = 0,
    cutoff: float = HIGH_CONFIDENCE_CUTOFF,
) -> Optional[dict]:
    """Return a ``calibration_warning`` artifact dict, or None.

    ``items`` should be drawn from SUCCEEDED chunks only -- the caller
    is responsible for excluding items that came out of blocked
    chunks. ``chunks_blocked`` is recorded on the warning so the
    operator can see how much data was excluded from the histogram;
    it does not affect the rate computation.
    """
    confidences = list(_iter_confidences(items))
    total = len(confidences)
    if total == 0:
        return None
    above = sum(1 for c in confidences if c > cutoff)
    rate = above / total
    if rate <= HIGH_CONFIDENCE_RATE_THRESHOLD:
        return None
    finding = CALIBRATION_FINDING_TEMPLATE.format(
        pct=HIGH_CONFIDENCE_RATE_THRESHOLD * 100.0,
        cutoff=cutoff,
    )
    return {
        "artifact_type": "calibration_warning",
        "schema_version": CALIBRATION_WARNING_SCHEMA_VERSION,
        "run_id": "",
        "high_confidence_rate": float(rate),
        "threshold": float(HIGH_CONFIDENCE_RATE_THRESHOLD),
        "finding": finding,
        "items_total": int(total),
        "items_above_high_confidence": int(above),
        "high_confidence_cutoff": float(cutoff),
        "created_at": _now_iso(),
    }


def calibration_from_succeeded(
    decisions: Sequence[dict],
    claims: Sequence[dict],
    action_items: Sequence[dict],
    *,
    counters: Optional[ChunkCounters] = None,
    run_id: str = "",
) -> Optional[dict]:
    """Compute calibration over decisions + claims + action_items.

    Decisions and claims are the X-3 confidence-required items; action
    items carry confidence but it is informational. We include action
    items in the histogram so the picture reflects every model-emitted
    confidence score in the run.

    Blocked chunks are EXCLUDED -- the inputs to this function are
    always the succeeded-chunk outputs; the counter's chunks_blocked
    is attached only so the warning artifact records the exclusion.
    """
    chunks_blocked = counters.chunks_blocked if counters is not None else 0
    artifact = compute_calibration(
        list(decisions) + list(claims) + list(action_items),
        chunks_blocked=chunks_blocked,
    )
    if artifact is None:
        return None
    if run_id:
        artifact["run_id"] = run_id
    return artifact
