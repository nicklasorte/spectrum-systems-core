"""Tests for the Phase W.6 agenda_detection_rate regression gate.

Each test constructs a real ``AgendaDetectionMetrics`` and calls the
real ``RegressionGate.check_agenda_detection_regression``. Per RT2, the
tests must exercise the gate end-to-end -- not call helpers in
isolation.
"""
from __future__ import annotations

import pytest

from spectrum_systems_core.evals.m4.regression_gate import (
    AgendaDetectionMetrics,
    RegressionGate,
)


def _metrics(
    *,
    attempted: bool = True,
    attempted_count: int = 0,
    succeeded_count: int = 0,
    items_detected: int = 0,
) -> AgendaDetectionMetrics:
    return AgendaDetectionMetrics(
        agenda_detection_attempted=attempted,
        agenda_detection_succeeded=succeeded_count > 0,
        agenda_items_detected_count=items_detected,
        agenda_detection_attempted_count=attempted_count,
        agenda_detection_succeeded_count=succeeded_count,
    )


# ---------------------------------------------------------------------------
# Gate inactive when Phase W disabled
# ---------------------------------------------------------------------------


def test_gate_passes_when_phase_w_disabled():
    gate = RegressionGate()
    assert gate.check_agenda_detection_regression(
        _metrics(attempted=False), baseline_metrics=None,
    ) is True


def test_gate_passes_when_phase_w_disabled_even_with_baseline():
    """Even if a baseline existed, a disabled run must not be compared."""
    gate = RegressionGate()
    assert gate.check_agenda_detection_regression(
        _metrics(attempted=False),
        baseline_metrics={"agenda_detection_rate": 0.95},
    ) is True


# ---------------------------------------------------------------------------
# Absolute sanity floor (Attack 3)
# ---------------------------------------------------------------------------


def test_gate_blocks_below_absolute_sanity_floor():
    """5/10 = 0.5 < 0.60 -> BLOCK regardless of baseline."""
    gate = RegressionGate()
    cur = _metrics(attempted_count=10, succeeded_count=5)
    # Without baseline.
    assert gate.check_agenda_detection_regression(cur, None) is False
    # With a baseline that would otherwise pass (no regression vs baseline).
    assert gate.check_agenda_detection_regression(
        cur, {"agenda_detection_rate": 0.50},
    ) is False


def test_gate_passes_at_sanity_floor_when_no_baseline():
    """6/10 = 0.60 == floor -> PASS (not strictly less than)."""
    gate = RegressionGate()
    cur = _metrics(attempted_count=10, succeeded_count=6)
    assert gate.check_agenda_detection_regression(cur, None) is True


def test_gate_blocks_at_059_just_below_floor():
    """59/100 = 0.59 < 0.60 -> BLOCK."""
    gate = RegressionGate()
    cur = _metrics(attempted_count=100, succeeded_count=59)
    assert gate.check_agenda_detection_regression(cur, None) is False


# ---------------------------------------------------------------------------
# Baseline comparison
# ---------------------------------------------------------------------------


def test_gate_blocks_on_15_point_decline_vs_baseline():
    """Baseline 0.90, current 0.74 -> drop of 0.16 > 0.15 -> BLOCK."""
    gate = RegressionGate()
    cur = _metrics(attempted_count=100, succeeded_count=74)
    assert gate.check_agenda_detection_regression(
        cur, {"agenda_detection_rate": 0.90},
    ) is False


def test_gate_passes_when_within_tolerance():
    """Baseline 0.90, current 0.76 -> drop of 0.14 < 0.15 -> PASS."""
    gate = RegressionGate()
    cur = _metrics(attempted_count=100, succeeded_count=76)
    assert gate.check_agenda_detection_regression(
        cur, {"agenda_detection_rate": 0.90},
    ) is True


def test_gate_passes_when_above_baseline():
    gate = RegressionGate()
    cur = _metrics(attempted_count=100, succeeded_count=95)
    assert gate.check_agenda_detection_regression(
        cur, {"agenda_detection_rate": 0.80},
    ) is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_zero_attempts_does_not_block():
    """attempted=True but zero transcripts run -> PASS (degenerate)."""
    gate = RegressionGate()
    cur = _metrics(attempted_count=0, succeeded_count=0)
    assert gate.check_agenda_detection_regression(cur, None) is True


def test_three_states_correctly_tracked():
    """Attack 13: the dataclass must carry all three states distinctly,
    so a caller can serialise them onto eval_summary.
    """
    m = _metrics(attempted=True, attempted_count=10, succeeded_count=8,
                 items_detected=23)
    assert m.agenda_detection_attempted is True
    assert m.agenda_detection_succeeded is True
    assert m.agenda_items_detected_count == 23
    assert m.agenda_detection_attempted_count == 10
    assert m.agenda_detection_succeeded_count == 8


def test_succeeded_state_false_when_zero_successes():
    m = _metrics(attempted=True, attempted_count=10, succeeded_count=0)
    assert m.agenda_detection_succeeded is False
    assert m.agenda_detection_attempted is True
