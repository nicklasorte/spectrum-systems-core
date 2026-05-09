"""Phase G — Harness memory and learning.

Per SSC-VISION-001: NO autonomous mutation of evals, policies, or workflows.
All learning produces candidate artifacts. Humans promote them via CLI.
Harness failures NEVER block the synthesis pipeline (fail-closed warnings only).
"""
from __future__ import annotations

RUN_HISTORY_RETENTION_DAYS = 90
MAX_ACTIVE_RUN_HISTORY = 1000
EVAL_CASE_CANDIDATE_REQUIRES_HUMAN = True  # immutable — never change this
OVERRIDE_DEFAULT_EXPIRY_DAYS = 365
OVERRIDE_EXPIRY_WARNING_DAYS = 30
PATTERN_JACCARD_THRESHOLD = 0.7
MIN_CLUSTER_SIZE = 2

from .run_history import RunHistoryStore
from .eval_history import EvalScoreHistory
from .failure_patterns import FailurePatternIndex
from .outcome_memory import OutcomeMemoryStore
from .workflow_comparator import WorkflowComparator
from .override_store import OverrideStore
from .entropy_auditor import EntropyAuditor

__all__ = [
    "RUN_HISTORY_RETENTION_DAYS",
    "MAX_ACTIVE_RUN_HISTORY",
    "EVAL_CASE_CANDIDATE_REQUIRES_HUMAN",
    "OVERRIDE_DEFAULT_EXPIRY_DAYS",
    "OVERRIDE_EXPIRY_WARNING_DAYS",
    "PATTERN_JACCARD_THRESHOLD",
    "MIN_CLUSTER_SIZE",
    "RunHistoryStore",
    "EvalScoreHistory",
    "FailurePatternIndex",
    "OutcomeMemoryStore",
    "WorkflowComparator",
    "OverrideStore",
    "EntropyAuditor",
]
