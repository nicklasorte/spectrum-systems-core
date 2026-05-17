"""FailurePatternIndex — cluster recurring failures, propose eval candidates.

FINDING-G-002: cluster by reason_code first, then by Jaccard similarity of
failure_detail (threshold 0.7, shared utility from utils.text_similarity).

FINDING-G-003: propose_eval_candidate writes ONLY to
harness/failures/eval_candidates.jsonl. It never writes to contracts/evals/.
Only the promote-eval-case CLI (run by a human) makes a candidate active.

Single-occurrence failures are buffered in pending_failures.jsonl and only
promoted to a pattern once a second similar failure arrives (CHECK-RT3-001).
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from ..utils.text_similarity import jaccard
from . import MIN_CLUSTER_SIZE, PATTERN_JACCARD_THRESHOLD
from ._io import append_jsonl, read_jsonl, utcnow_iso, write_jsonl
from ._paths import (
    eval_candidates_path,
    patterns_path,
    pending_failures_path,
)
from ._schema import validate_harness_artifact

_LOG = logging.getLogger(__name__)


class FailurePatternIndex:
    def ingest_failures(
        self,
        run_id: str,
        failures: list[dict[str, Any]],
        repo_root: str | Path,
    ) -> dict[str, Any]:
        """Cluster failures into patterns. Never raises."""
        try:
            patterns = read_jsonl(patterns_path(repo_root))
            pending = read_jsonl(pending_failures_path(repo_root))
            now = utcnow_iso()
            new_patterns = 0
            patterns_updated = 0

            for failure in failures or []:
                reason_code = str(failure.get("reason_code") or "").strip()
                detail = str(failure.get("failure_detail") or "").strip()
                if not reason_code:
                    continue

                # Step 1: try to merge with an existing pattern.
                merged = False
                for pattern in patterns:
                    if pattern.get("reason_code") != reason_code:
                        continue
                    members = pattern.get("member_failure_details", []) or []
                    if any(
                        jaccard(detail, m) >= PATTERN_JACCARD_THRESHOLD
                        for m in members
                    ):
                        pattern["member_run_ids"] = list(
                            pattern.get("member_run_ids", [])
                        )
                        pattern["member_run_ids"].append(run_id)
                        pattern["member_failure_details"] = members + [detail]
                        pattern["occurrence_count"] = (
                            int(pattern.get("occurrence_count", 1)) + 1
                        )
                        pattern["last_seen_at"] = now
                        patterns_updated += 1
                        merged = True
                        break
                if merged:
                    continue

                # Step 2: try to merge with a pending (single) buffered failure.
                pending_match_idx = None
                for idx, p_failure in enumerate(pending):
                    if p_failure.get("reason_code") != reason_code:
                        continue
                    if jaccard(detail, p_failure.get("failure_detail", "")) >= PATTERN_JACCARD_THRESHOLD:
                        pending_match_idx = idx
                        break
                if pending_match_idx is not None:
                    p_failure = pending[pending_match_idx]
                    new_pattern = {
                        "pattern_id": str(uuid.uuid4()),
                        "reason_code": reason_code,
                        "cluster_method": "reason_code_then_jaccard",
                        "jaccard_threshold": PATTERN_JACCARD_THRESHOLD,
                        "member_run_ids": [
                            str(p_failure.get("run_id", "")),
                            run_id,
                        ],
                        "member_failure_details": [
                            str(p_failure.get("failure_detail", "")),
                            detail,
                        ],
                        "first_seen_at": str(p_failure.get("seen_at") or now),
                        "last_seen_at": now,
                        "occurrence_count": MIN_CLUSTER_SIZE,
                        "eval_candidate_id": None,
                        "created_at": now,
                    }
                    ok, err = validate_harness_artifact(
                        new_pattern, "failure_pattern"
                    )
                    if not ok:
                        _LOG.warning("failure_pattern schema violation: %s", err)
                        continue
                    patterns.append(new_pattern)
                    pending.pop(pending_match_idx)
                    new_patterns += 1
                    continue

                # Step 3: buffer as pending (single occurrence).
                pending.append(
                    {
                        "run_id": run_id,
                        "reason_code": reason_code,
                        "failure_detail": detail,
                        "seen_at": now,
                    }
                )

            write_jsonl(patterns_path(repo_root), patterns)
            write_jsonl(pending_failures_path(repo_root), pending)
            return {
                "status": "success",
                "patterns_updated": patterns_updated,
                "new_patterns": new_patterns,
            }
        except OSError as exc:  # pragma: no cover
            _LOG.warning("FailurePatternIndex.ingest_failures failed: %s", exc)
            return {
                "status": "failure",
                "patterns_updated": 0,
                "new_patterns": 0,
                "reason": str(exc),
            }

    def propose_eval_candidate(
        self,
        pattern: dict[str, Any],
        repo_root: str | Path,
    ) -> dict[str, Any]:
        """Propose an eval_case_candidate. Never writes to contracts/evals/."""
        if pattern.get("eval_candidate_id"):
            return {"status": "skipped", "candidate_id": None}

        if int(pattern.get("occurrence_count", 0)) < 3:
            return {"status": "skipped", "candidate_id": None}

        reason_code = str(pattern.get("reason_code") or "")
        members = pattern.get("member_failure_details") or []
        triggering_detail = str(members[-1]) if members else ""

        target_artifact_type = "report_draft"
        if "claim" in reason_code:
            target_artifact_type = "technical_claim"
        elif "keynote" in reason_code:
            target_artifact_type = "keynote_scaffold"
        elif "evidence" in reason_code:
            target_artifact_type = "evidence_record"
        elif "issue" in reason_code:
            target_artifact_type = "issue_record"

        candidate = {
            "candidate_id": str(uuid.uuid4()),
            "proposed_eval_type": "policy_alignment",
            "proposed_metric_name": f"no_recurrence_of_{reason_code}",
            "proposed_target_artifact_type": target_artifact_type,
            "proposed_pass_condition": (
                f"reason_code '{reason_code}' must not appear in eval_results"
            ),
            "triggering_pattern_id": str(pattern.get("pattern_id") or ""),
            "triggering_failure_detail": triggering_detail,
            "proposed_by": "harness_memory",
            "requires_human_promotion": True,
            "status": "candidate",
            "promotion_note": "",
            "created_at": utcnow_iso(),
        }
        ok, err = validate_harness_artifact(candidate, "eval_case_candidate")
        if not ok:
            return {
                "status": "failure",
                "candidate_id": None,
                "reason": f"schema_violation: {err}",
            }
        try:
            append_jsonl(eval_candidates_path(repo_root), candidate)
            # Update pattern.eval_candidate_id in patterns.jsonl.
            patterns = read_jsonl(patterns_path(repo_root))
            for p in patterns:
                if p.get("pattern_id") == pattern.get("pattern_id"):
                    p["eval_candidate_id"] = candidate["candidate_id"]
                    pattern["eval_candidate_id"] = candidate["candidate_id"]
                    break
            write_jsonl(patterns_path(repo_root), patterns)
            return {
                "status": "success",
                "candidate_id": candidate["candidate_id"],
            }
        except OSError as exc:  # pragma: no cover
            return {
                "status": "failure",
                "candidate_id": None,
                "reason": str(exc),
            }

    def get_top_patterns(
        self, repo_root: str | Path, n: int = 10
    ) -> list[dict[str, Any]]:
        patterns = read_jsonl(patterns_path(repo_root))
        patterns.sort(
            key=lambda p: int(p.get("occurrence_count", 0)),
            reverse=True,
        )
        return patterns[: max(0, int(n))]

    def write_failure_projection(
        self,
        repo_root: str | Path,
        vault_root: str | Path | None = None,
    ) -> str:
        from ..ingestion.obsidian_projection import ObsidianProjection

        return ObsidianProjection().write_failure_patterns_projection(
            repo_root, vault_root
        )
