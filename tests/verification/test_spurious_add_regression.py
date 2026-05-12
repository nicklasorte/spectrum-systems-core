"""Phase V — RegressionGate.check_spurious_add_regression tests."""
from __future__ import annotations

import pytest

from spectrum_systems_core.evals.m4.regression_gate import (
    SPURIOUS_ADD_ABSOLUTE_CEILING,
    SPURIOUS_ADD_TOLERANCE,
    RegressionGate,
)


def test_compute_spurious_add_rate_reads_summary():
    rg = RegressionGate()
    assert rg.compute_spurious_add_rate(
        {"summary": {"spurious_add_rate": 0.42}}
    ) == 0.42


def test_compute_spurious_add_rate_missing_summary_returns_zero():
    rg = RegressionGate()
    assert rg.compute_spurious_add_rate({}) == 0.0
    assert rg.compute_spurious_add_rate({"summary": {}}) == 0.0


def test_compute_spurious_add_rate_handles_invalid_value():
    rg = RegressionGate()
    assert rg.compute_spurious_add_rate(
        {"summary": {"spurious_add_rate": "nope"}}
    ) == 0.0


def test_blocks_when_no_baseline_and_absolute_threshold_exceeded():
    rg = RegressionGate()
    over = SPURIOUS_ADD_ABSOLUTE_CEILING + 0.05
    assert rg.check_spurious_add_regression(over, None) is False


def test_passes_when_no_baseline_and_below_absolute_threshold():
    rg = RegressionGate()
    under = SPURIOUS_ADD_ABSOLUTE_CEILING - 0.05
    assert rg.check_spurious_add_regression(under, None) is True


def test_passes_when_no_baseline_at_exact_threshold():
    rg = RegressionGate()
    assert rg.check_spurious_add_regression(SPURIOUS_ADD_ABSOLUTE_CEILING, None) is True


def test_blocks_when_baseline_set_and_15_percent_increase():
    rg = RegressionGate()
    baseline = 0.10
    # 15% rise means cur <= baseline * 1.15 still passes, so 0.116 blocks.
    current = baseline * (1.0 + SPURIOUS_ADD_TOLERANCE) + 0.001
    assert rg.check_spurious_add_regression(current, baseline) is False


def test_passes_when_baseline_set_and_within_tolerance():
    rg = RegressionGate()
    baseline = 0.10
    current = baseline * 1.10  # under tolerance
    assert rg.check_spurious_add_regression(current, baseline) is True


def test_passes_when_baseline_zero_and_current_zero():
    rg = RegressionGate()
    assert rg.check_spurious_add_regression(0.0, 0.0) is True


def test_blocks_when_baseline_zero_and_current_positive():
    rg = RegressionGate()
    # baseline 0 means any rise is "infinite %"; allowed only if cur is 0.
    assert rg.check_spurious_add_regression(0.01, 0.0) is False
