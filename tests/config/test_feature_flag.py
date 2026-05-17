"""Tests for ``config.feature_flag.FeatureFlag``.

Cover the fail-closed contract: every non-happy-path resolves to
``enabled=False`` and the call never raises.
"""
from __future__ import annotations

import json
import pathlib

from spectrum_systems_core.config.feature_flag import (
    PHASE_V_FLAG_NAME,
    FeatureFlag,
)


def _flag_dir(root: pathlib.Path) -> pathlib.Path:
    target = root / "store" / "artifacts" / "config"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _write_flag(root: pathlib.Path, name: str, payload) -> pathlib.Path:
    d = _flag_dir(root)
    p = d / f"{name}_enabled.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_flag_returns_true_when_enabled(tmp_path):
    _write_flag(tmp_path, PHASE_V_FLAG_NAME, {"enabled": True})
    flag = FeatureFlag(tmp_path)
    assert flag.is_enabled(PHASE_V_FLAG_NAME) is True


def test_flag_returns_false_when_disabled(tmp_path):
    _write_flag(tmp_path, PHASE_V_FLAG_NAME, {"enabled": False})
    flag = FeatureFlag(tmp_path)
    assert flag.is_enabled(PHASE_V_FLAG_NAME) is False


def test_flag_missing_file_returns_false(tmp_path):
    flag = FeatureFlag(tmp_path)
    assert flag.is_enabled(PHASE_V_FLAG_NAME) is False


def test_flag_malformed_json_returns_false(tmp_path):
    d = _flag_dir(tmp_path)
    (d / f"{PHASE_V_FLAG_NAME}_enabled.json").write_text("{", encoding="utf-8")
    flag = FeatureFlag(tmp_path)
    assert flag.is_enabled(PHASE_V_FLAG_NAME) is False


def test_flag_array_payload_returns_false(tmp_path):
    _write_flag(tmp_path, PHASE_V_FLAG_NAME, [True])
    flag = FeatureFlag(tmp_path)
    assert flag.is_enabled(PHASE_V_FLAG_NAME) is False


def test_flag_missing_enabled_key_defaults_false(tmp_path):
    _write_flag(tmp_path, PHASE_V_FLAG_NAME, {"flag_name": PHASE_V_FLAG_NAME})
    flag = FeatureFlag(tmp_path)
    assert flag.is_enabled(PHASE_V_FLAG_NAME) is False


def test_flag_data_lake_path_accepts_str(tmp_path):
    _write_flag(tmp_path, PHASE_V_FLAG_NAME, {"enabled": True})
    flag = FeatureFlag(str(tmp_path))
    assert flag.is_enabled(PHASE_V_FLAG_NAME) is True


def test_flag_never_raises_on_io_error(tmp_path, monkeypatch):
    _write_flag(tmp_path, PHASE_V_FLAG_NAME, {"enabled": True})
    flag = FeatureFlag(tmp_path)

    original_read_text = pathlib.Path.read_text

    def _boom(self, *a, **k):
        if self.name == f"{PHASE_V_FLAG_NAME}_enabled.json":
            raise OSError("disk on fire")
        return original_read_text(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "read_text", _boom)
    # Must not raise; must resolve fail-closed.
    assert flag.is_enabled(PHASE_V_FLAG_NAME) is False
