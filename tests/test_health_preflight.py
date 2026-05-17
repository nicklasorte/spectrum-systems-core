"""Class 3: feature flag presence preflight."""
from __future__ import annotations

import io
import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from spectrum_systems_core.health.preflight import (
    PREFLIGHT_ENABLED_ENV_VAR,
    REQUIRED_FEATURE_FLAGS,
    check_feature_flags,
    run_preflight,
)


def _config_dir(lake: Path) -> Path:
    return lake / "store" / "artifacts" / "config"


def _seed(
    lake: Path, name: str, *, enabled: bool, malformed: bool = False
) -> Path:
    d = _config_dir(lake)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.json"
    if malformed:
        path.write_text("{not json", encoding="utf-8")
    else:
        path.write_text(
            json.dumps(
                {
                    "artifact_type": "feature_flag",
                    "schema_version": "1.0.0",
                    "flag_name": name,
                    "enabled": enabled,
                }
            ),
            encoding="utf-8",
        )
    return path


@pytest.fixture(autouse=True)
def _clear_env() -> Iterator[None]:
    """Ensure the rollback env var is unset for each test."""
    saved = os.environ.pop(PREFLIGHT_ENABLED_ENV_VAR, None)
    yield
    if saved is not None:
        os.environ[PREFLIGHT_ENABLED_ENV_VAR] = saved
    else:
        os.environ.pop(PREFLIGHT_ENABLED_ENV_VAR, None)


def test_missing_flag_emits_halt(tmp_path: Path) -> None:
    findings = check_feature_flags(tmp_path)
    halts = [f for f in findings if f.severity == "halt"]
    codes = {f.finding_code for f in halts}
    assert "feature_flag_missing" in codes
    # Every required flag with no sentinel-suppression rule must halt.
    expected = set(REQUIRED_FEATURE_FLAGS) - {"phase_w_agenda_detection_enabled"}
    found_flags = {f.context["flag_name"] for f in halts}
    assert expected <= found_flags
    # phase_w is also missing here (no file) -> also halt.
    assert "phase_w_agenda_detection_enabled" in found_flags


def test_disabled_flag_emits_warn(tmp_path: Path) -> None:
    _seed(tmp_path, "phase_v_post_hoc_verification_enabled", enabled=False)
    _seed(tmp_path, "phase_w_agenda_detection_enabled", enabled=False)
    # Sentinel exists -> phase_w disabled warn fires.
    _seed(tmp_path, "phase_w_merged", enabled=True)

    findings = check_feature_flags(tmp_path)
    warns = [f for f in findings if f.finding_code == "feature_flag_disabled"]
    names = {f.context["flag_name"] for f in warns}
    assert "phase_v_post_hoc_verification_enabled" in names
    assert "phase_w_agenda_detection_enabled" in names


def test_phase_w_warn_suppressed_without_sentinel(tmp_path: Path) -> None:
    _seed(tmp_path, "phase_v_post_hoc_verification_enabled", enabled=True)
    _seed(tmp_path, "phase_w_agenda_detection_enabled", enabled=False)
    # No phase_w_merged sentinel -> phase_w disabled is expected.

    findings = check_feature_flags(tmp_path)
    codes = {(f.finding_code, f.context.get("flag_name")) for f in findings}
    assert (
        "feature_flag_disabled",
        "phase_w_agenda_detection_enabled",
    ) not in codes


def test_all_enabled_no_findings(tmp_path: Path) -> None:
    _seed(tmp_path, "phase_v_post_hoc_verification_enabled", enabled=True)
    _seed(tmp_path, "phase_w_agenda_detection_enabled", enabled=True)
    findings = check_feature_flags(tmp_path)
    assert findings == []


def test_malformed_flag_treated_as_missing(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        "phase_v_post_hoc_verification_enabled",
        enabled=True,
        malformed=True,
    )
    findings = check_feature_flags(tmp_path)
    codes = {f.finding_code for f in findings}
    # Malformed JSON is treated as missing flag, not a crash.
    assert "feature_flag_missing" in codes


def test_pipeline_halts_on_missing_flag(tmp_path: Path) -> None:
    out = io.StringIO()
    rc = run_preflight(tmp_path, pipeline_run_id="run-1", out_stream=out)
    assert rc == 1, "missing required flag must cause non-zero exit"
    # Halt findings must be persisted.
    written = list((tmp_path / "store" / "artifacts" / "health").glob("*.json"))
    assert written, "halt finding must be written before exit"


def test_preflight_clean_returns_zero(tmp_path: Path) -> None:
    _seed(tmp_path, "phase_v_post_hoc_verification_enabled", enabled=True)
    _seed(tmp_path, "phase_w_agenda_detection_enabled", enabled=True)
    out = io.StringIO()
    rc = run_preflight(tmp_path, pipeline_run_id="run-1", out_stream=out)
    assert rc == 0


def test_preflight_bypass_via_env_var(tmp_path: Path) -> None:
    os.environ[PREFLIGHT_ENABLED_ENV_VAR] = "false"
    out = io.StringIO()
    rc = run_preflight(tmp_path, pipeline_run_id="run-1", out_stream=out)
    assert rc == 0
    assert "bypassed" in out.getvalue()
