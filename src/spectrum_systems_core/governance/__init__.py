"""Phase I — System-wide governance audit.

Per SSC-VISION-001: visibility, not action. Audits surface complexity for
human review. NO autonomous mutation outside governance/. apply-compression
is the ONLY path that may rename or warn (FINDING-I-006).

Audit failures NEVER block the synthesis pipeline (same rule as Phase G
harness). Determinism: zero LLM calls in any scanner.
"""
from __future__ import annotations

COST_TREND_WINDOW_DAYS = 30
COMPRESSION_INACTIVITY_DAYS = 60
DASHBOARD_SUMMARY_MAX_LINES = 30
DIVERGENCE_KEY_FIELDS = ("task_type", "recipe_id", "audience")
EXCEPTION_ACCUMULATION_THRESHOLD = 5

from .schema_drift_scanner import SchemaDriftScanner
from .eval_coverage_scanner import EvalCoverageScanner
from .decision_divergence_detector import DecisionDivergenceDetector
from .exception_accumulation_tracker import ExceptionAccumulationTracker
from .hidden_logic_scanner import HiddenLogicScanner
from .markdown_authority_scanner import MarkdownAuthorityScanner
from .cost_trend_reporter import CostTrendReporter
from .compression_scanner import CompressionScanner
from .dashboard import GovernanceDashboard
from .gov10_certification import (
    CERTIFICATION_COST_CEILING_USD,
    GOV10CertificationStep,
)

__all__ = [
    "COST_TREND_WINDOW_DAYS",
    "COMPRESSION_INACTIVITY_DAYS",
    "DASHBOARD_SUMMARY_MAX_LINES",
    "DIVERGENCE_KEY_FIELDS",
    "EXCEPTION_ACCUMULATION_THRESHOLD",
    "SchemaDriftScanner",
    "EvalCoverageScanner",
    "DecisionDivergenceDetector",
    "ExceptionAccumulationTracker",
    "HiddenLogicScanner",
    "MarkdownAuthorityScanner",
    "CostTrendReporter",
    "CompressionScanner",
    "GovernanceDashboard",
    "CERTIFICATION_COST_CEILING_USD",
    "GOV10CertificationStep",
]
