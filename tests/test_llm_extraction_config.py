"""Step 1 gate: feature flag default + fail-closed pre-run config check.

Gate (from the mission): flag=True, API key absent -> config_error
pre-run. flag=False -> no check, no error. These tests are the
artifact-evidence for that gate.
"""
from __future__ import annotations

import json

import pytest

from spectrum_systems_core.config import (
    CONFIG_ERROR_REASON_CODE,
    LLMConfigError,
    llm_extraction_enabled,
    preflight_llm_config,
)


def test_flag_default_is_false():
    # No override, no data lake -> the rollback-safe default.
    assert llm_extraction_enabled() is False


def test_flag_override_true_wins():
    assert llm_extraction_enabled(override=True) is True
    assert llm_extraction_enabled(override=False) is False


def test_flag_from_data_lake_is_fail_closed(tmp_path):
    # Absent flag file -> False (fail closed), not an error.
    assert llm_extraction_enabled(data_lake_path=tmp_path) is False

    cfg = tmp_path / "store" / "artifacts" / "config"
    cfg.mkdir(parents=True)
    (cfg / "llm_extraction_enabled.json").write_text(
        json.dumps({"enabled": True}), encoding="utf-8"
    )
    assert llm_extraction_enabled(data_lake_path=tmp_path) is True


def test_preflight_flag_true_missing_key_raises_config_error():
    with pytest.raises(LLMConfigError) as excinfo:
        preflight_llm_config(enabled=True, env={})
    # The reason code is a FIELD on the exception, not parsed prose.
    assert excinfo.value.reason_code == CONFIG_ERROR_REASON_CODE
    assert excinfo.value.reason_code == "config_error"


def test_preflight_flag_true_blank_key_raises_config_error():
    with pytest.raises(LLMConfigError) as excinfo:
        preflight_llm_config(enabled=True, env={"ANTHROPIC_API_KEY": "   "})
    assert excinfo.value.reason_code == "config_error"


def test_preflight_flag_false_no_check_no_error():
    # Flag off: returns cleanly even with an empty environment. This is
    # the proof that the feature is a no-op for non-opted-in consumers.
    assert preflight_llm_config(enabled=False, env={}) is None


def test_preflight_flag_true_with_key_passes():
    assert (
        preflight_llm_config(
            enabled=True, env={"ANTHROPIC_API_KEY": "sk-test-key"}
        )
        is None
    )
