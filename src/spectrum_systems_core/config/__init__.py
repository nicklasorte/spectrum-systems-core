"""Phase V runtime configuration helpers.

Currently exposes only ``FeatureFlag`` -- a small, fail-closed reader for
JSON-backed feature flags stored under ``<data_lake>/store/artifacts/config/``.
"""
from .feature_flag import FeatureFlag, PHASE_V_FLAG_NAME, PHASE_W_FLAG_NAME

__all__ = ["FeatureFlag", "PHASE_V_FLAG_NAME", "PHASE_W_FLAG_NAME"]
