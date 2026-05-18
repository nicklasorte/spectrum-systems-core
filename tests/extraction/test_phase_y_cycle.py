"""Phase Y.8 — improvement-cycle harness driver."""
from __future__ import annotations

from spectrum_systems_core.harness.improvement_cycle import (
    PHASES,
    run_improvement_cycle,
)

TRANSCRIPT = "m-2025-12-18-7ghz-downlink-tig-kickoff"


def test_repro_all_preconditions_met_every_phase_present():
    funcs = {p: (lambda p=p: f"art-{p}") for p in PHASES}
    art = run_improvement_cycle(
        transcript_id=TRANSCRIPT,
        phase_funcs=funcs,
        open_pr_lookup=lambda _t: [],
    )
    ps = art.payload["phase_status"]
    assert all(ps[p]["status"] == "present" for p in PHASES)
    assert art.payload["overall_status"] == "promoted"
    assert art.payload["blocking_phase"] is None
    assert art.payload["prior_open_pr_check"] == {
        "checked": True,
        "found_open_pr_id": None,
    }


def test_repro_y5_raises_blocks_with_blocking_phase():
    funcs = {p: (lambda p=p: f"art-{p}") for p in PHASES}
    funcs["Y_5"] = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    art = run_improvement_cycle(
        transcript_id=TRANSCRIPT,
        phase_funcs=funcs,
        open_pr_lookup=lambda _t: [],
    )
    ps = art.payload["phase_status"]
    assert ps["Y_5"]["status"] == "missing"
    assert "boom" in ps["Y_5"]["error_or_none"]
    assert art.payload["overall_status"] == "blocked"
    assert art.payload["blocking_phase"] == "Y_5"
    # Phases after the failure never ran.
    assert ps["Y_6"]["status"] == "unknown"
    assert ps["Y_7"]["status"] == "unknown"
    # Phases before it still ran.
    assert ps["Y_4"]["status"] == "present"


def test_repro_preflight_open_pr_blocks_no_phases_run():
    ran: list[str] = []

    def _tracked(p):
        def _f():
            ran.append(p)
            return f"art-{p}"

        return _f

    funcs = {p: _tracked(p) for p in PHASES}
    art = run_improvement_cycle(
        transcript_id=TRANSCRIPT,
        phase_funcs=funcs,
        open_pr_lookup=lambda _t: ["correction/cand-9 (PR #42)"],
    )
    assert ran == []  # NO phase ran
    assert art.payload["overall_status"] == "blocked"
    assert art.payload["blocking_phase"] == "preflight"
    assert art.payload["prior_open_pr_check"]["found_open_pr_id"] == (
        "correction/cand-9 (PR #42)"
    )
    assert all(
        art.payload["phase_status"][p]["status"] == "unknown" for p in PHASES
    )
