"""Phase V runtime configuration helpers.

Currently exposes only ``FeatureFlag`` -- a small, fail-closed reader for
JSON-backed feature flags stored under ``<data_lake>/store/artifacts/config/``.
"""
from .feature_flag import PHASE_V_FLAG_NAME, PHASE_W_FLAG_NAME, FeatureFlag
from .feature_flags import (
    ANTHROPIC_API_KEY_ENV,
    CONFIG_ERROR_REASON_CODE,
    LLM_EXTRACTION_FLAG_NAME,
    LLMConfigError,
    llm_extraction_enabled,
    preflight_llm_config,
)

__all__ = [
    "FeatureFlag",
    "PHASE_V_FLAG_NAME",
    "PHASE_W_FLAG_NAME",
    "ANTHROPIC_API_KEY_ENV",
    "CONFIG_ERROR_REASON_CODE",
    "LLM_EXTRACTION_FLAG_NAME",
    "LLMConfigError",
    "llm_extraction_enabled",
    "preflight_llm_config",
]
