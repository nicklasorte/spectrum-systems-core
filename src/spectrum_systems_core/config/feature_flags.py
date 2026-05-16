"""LLM-extraction feature flag + fail-closed pre-run config check.

This is the single rollback switch for the live-LLM meeting-minutes
extraction path. It is deliberately separate from the existing
``feature_flag.FeatureFlag`` data-lake reader: this module owns the
*code-level default* (``False``) and the *pre-run config gate* that
must halt BEFORE any artifact is produced when the operator opts in
but the runtime is not actually configured for a live model call.

Design rules honoured here:

- Default is ``False``. With the flag off, the live-LLM path is never
  taken and this whole module is a no-op for any consumer that does
  not explicitly opt in. Rollback is one config change.
- Fail-closed: if the flag is on but ``ANTHROPIC_API_KEY`` is absent,
  :func:`preflight_llm_config` raises :class:`LLMConfigError` carrying
  the machine-readable ``reason_code = "config_error"``. The caller
  must halt — it must not fall back to inference or to the regex
  extractor silently. The reason code is a field on the exception so
  a gate reads a value, never prose.
- The data-lake-backed :class:`~.feature_flag.FeatureFlag` reader is
  reused (not duplicated) when a ``data_lake_path`` is supplied, so the
  operator can flip the flag with the same JSON-artifact convention as
  the Phase V / Phase W flags.
"""
from __future__ import annotations

import os
from typing import Mapping, Optional, Union

import pathlib

from .feature_flag import FeatureFlag

# Flag name + on-disk artifact path segment, mirroring the Phase V / W
# convention: <data_lake>/store/artifacts/config/<name>_enabled.json
LLM_EXTRACTION_FLAG_NAME = "llm_extraction"

# Reason code emitted when the flag is on but the runtime cannot make a
# live model call. Read as a field off the raised exception — this is
# the artifact-evidence the Step 1 gate asserts on.
CONFIG_ERROR_REASON_CODE = "config_error"

# The single environment variable that must be present for a live call.
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"


class LLMConfigError(RuntimeError):
    """Raised pre-run when the LLM path is enabled but unconfigured.

    Carries ``reason_code`` so a control/gate path reads a value rather
    than parsing a message string. ``RuntimeError`` (not ``ValueError``)
    so it is not accidentally swallowed by the broad ``ValueError``
    handlers around schema validation.
    """

    def __init__(self, message: str, *, reason_code: str = CONFIG_ERROR_REASON_CODE):
        super().__init__(message)
        self.reason_code = reason_code


def llm_extraction_enabled(
    *,
    override: Optional[bool] = None,
    data_lake_path: Optional[Union[str, pathlib.Path]] = None,
) -> bool:
    """Resolve the ``llm_extraction`` flag. Default is ``False``.

    Resolution order (first decisive wins):

    1. ``override`` — an explicit boolean passed by a caller/test. This
       is the in-process switch the dispatcher uses. ``None`` means
       "not set, keep looking".
    2. ``data_lake_path`` — when supplied, the fail-closed
       :class:`FeatureFlag` reader is consulted at
       ``<data_lake>/store/artifacts/config/llm_extraction_enabled.json``.
    3. Otherwise ``False``.

    There is intentionally no environment-variable backdoor: the flag is
    either an explicit in-process decision or a governed data-lake
    artifact. A stray env var must not be able to turn live model calls
    on for an entire process.
    """
    if override is not None:
        return bool(override)
    if data_lake_path is not None:
        return FeatureFlag(data_lake_path).is_enabled(LLM_EXTRACTION_FLAG_NAME)
    return False


def preflight_llm_config(
    *,
    enabled: bool,
    env: Optional[Mapping[str, str]] = None,
) -> None:
    """Fail-closed pre-run config gate. Call BEFORE producing any artifact.

    Contract:

    - ``enabled is False`` → return immediately. No check, no error.
      The regex path is taken; this module imposes nothing.
    - ``enabled is True`` and ``ANTHROPIC_API_KEY`` present (non-empty
      after strip) → return. The live call may proceed.
    - ``enabled is True`` and the key is missing/blank → raise
      :class:`LLMConfigError` with ``reason_code == "config_error"``.
      The caller must halt; it must not produce an artifact or fall
      back to the regex extractor by accident.

    ``env`` defaults to ``os.environ``; tests pass an explicit mapping
    so the check is hermetic and does not depend on the runner's
    ambient environment.
    """
    if not enabled:
        return
    environ = env if env is not None else os.environ
    key = (environ.get(ANTHROPIC_API_KEY_ENV) or "").strip()
    if not key:
        raise LLMConfigError(
            f"{LLM_EXTRACTION_FLAG_NAME} flag is enabled but "
            f"{ANTHROPIC_API_KEY_ENV} is not set; halting before any "
            "artifact is produced (no silent fallback to the regex "
            "extractor or to inference).",
            reason_code=CONFIG_ERROR_REASON_CODE,
        )


__all__ = [
    "LLM_EXTRACTION_FLAG_NAME",
    "CONFIG_ERROR_REASON_CODE",
    "ANTHROPIC_API_KEY_ENV",
    "LLMConfigError",
    "llm_extraction_enabled",
    "preflight_llm_config",
]
