"""EvalMetrics: derive coverage / precision / items_requiring_review.

Phase M.4. Pure function over a single alignment_result -- no I/O, no
LLM, never raises. The three numbers it emits are deliberately separate
trust signals:

* coverage   -- fraction of minutes items that the pipeline produced
                a match for. Drives regression detection on "did we
                stop catching real items?".
* precision  -- fraction of extracted items that line up with a real
                minutes item. Drives regression detection on "did we
                start producing items the minutes does not back?".
* items_requiring_review -- the count of extracted items with
                alignment_status == "requires_review". This is a HITL
                queue, not a hallucination verdict. The model may be
                right and the minutes incomplete; only a human can say.

The terminology is deliberate: the M.4 spec rejects "spurious_add" /
"hallucination" for this signal because it conflates a queue rate with
a verdict.
"""
from __future__ import annotations

import datetime
import uuid
from typing import Any, Dict


SCHEMA_VERSION = "1.0.0"
PRODUCED_BY = "EvalMetrics"


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


class EvalMetrics:
    """Compute coverage / precision / items_requiring_review."""

    SCHEMA_VERSION = SCHEMA_VERSION
    PRODUCED_BY = PRODUCED_BY

    def compute(
        self,
        alignment_result: Dict[str, Any],
        *,
        pipeline_run_id: str = "",
        prompt_version: str = "",
    ) -> Dict[str, Any]:
        """Return an eval_result artifact dict.

        Never raises. Missing alignment_result keys default to safe
        zero-state (coverage=0.0, precision=0.0, review=0).
        """
        coverage_aligns = alignment_result.get("coverage_alignments") or []
        review_aligns = alignment_result.get("review_alignments") or []

        total_minutes = len(coverage_aligns)
        matched_minutes = sum(
            1 for c in coverage_aligns if c.get("alignment_status") == "matched"
        )
        total_extracted = len(review_aligns)
        matched_extracted = sum(
            1 for r in review_aligns if r.get("alignment_status") == "matched"
        )
        items_requiring_review = total_extracted - matched_extracted

        coverage = (
            matched_minutes / total_minutes if total_minutes > 0 else 0.0
        )
        precision = (
            matched_extracted / total_extracted if total_extracted > 0 else 0.0
        )
        review_rate = (
            items_requiring_review / total_extracted
            if total_extracted > 0
            else 0.0
        )

        return {
            "eval_result_id": str(uuid.uuid4()),
            "alignment_result_id": alignment_result.get(
                "alignment_result_id", ""
            ),
            "source_artifact_id": alignment_result.get(
                "source_artifact_id", ""
            ),
            "minutes_artifact_id": alignment_result.get(
                "minutes_artifact_id", ""
            ),
            "pair_id": alignment_result.get("pair_id", ""),
            "pipeline_run_id": pipeline_run_id or "",
            "prompt_version": prompt_version or "",
            "artifact_type": "eval_result",
            "schema_version": self.SCHEMA_VERSION,
            "created_at": _now_iso(),
            "chunking_strategy": alignment_result.get(
                "chunking_strategy", "unknown"
            ),
            "coverage": float(coverage),
            "precision": float(precision),
            "items_requiring_review": int(items_requiring_review),
            "items_requiring_review_rate": float(review_rate),
            "total_extracted_items": int(total_extracted),
            "total_minutes_items": int(total_minutes),
            "provenance": {"produced_by": self.PRODUCED_BY},
        }
