"""Phase P — test the next-phase-handoff reminder printed after --set-baseline.

We invoke the real ``eval_ground_truth`` CLI function over fixture pairs
to verify that the reminder text appears on a successful baseline install.
We do NOT mock the runner — the reminder must be wired to actual
``summary.is_baseline == True``, not to the mere presence of the flag.
"""
from __future__ import annotations

import io
import shutil
from pathlib import Path

from spectrum_systems_core.cli import (
    _set_baseline_handoff_reminder,
    eval_ground_truth,
)

FIXTURE_DIR = (
    Path(__file__).resolve().parents[1] / "fixtures" / "eval" / "ground_truth"
)


def _stage_fixture_pairs(sdl_root: Path) -> None:
    target = sdl_root / "ground_truth"
    target.mkdir(parents=True, exist_ok=True)
    for path in FIXTURE_DIR.glob("*.json"):
        shutil.copy(path, target / path.name)


def test_prints_next_phase_handoff_reminder(
    tmp_path: Path, monkeypatch
) -> None:
    """Successful --set-baseline must print the next-phase-handoff reminder."""
    sdl_root = tmp_path / "sdl"
    _stage_fixture_pairs(sdl_root)
    monkeypatch.setenv("SDL_ROOT", str(sdl_root))

    buf = io.StringIO()
    rc = eval_ground_truth(
        data_lake=str(tmp_path),
        pipeline_run_id="run-phase-P-1",
        set_baseline=True,
        out_stream=buf,
    )
    text = buf.getvalue()
    assert rc == 0, text
    # The reminder must mention the next-phase-handoff command verbatim.
    assert "next-phase-handoff" in text
    # And it must be flagged as the BASELINE SET banner so the operator
    # can spot it amid the eval summary output.
    assert "BASELINE SET" in text


def test_no_reminder_when_set_baseline_not_requested(
    tmp_path: Path, monkeypatch
) -> None:
    """Happy-path: a regular eval run prints no reminder."""
    sdl_root = tmp_path / "sdl"
    _stage_fixture_pairs(sdl_root)
    monkeypatch.setenv("SDL_ROOT", str(sdl_root))

    buf = io.StringIO()
    rc = eval_ground_truth(
        data_lake=str(tmp_path),
        pipeline_run_id="run-phase-P-no-baseline",
        set_baseline=False,
        out_stream=buf,
    )
    text = buf.getvalue()
    assert rc == 0, text
    assert "BASELINE SET" not in text
    assert "next-phase-handoff" not in text


def test_reminder_template_contains_required_text() -> None:
    """The reminder template is the single source of truth — verify shape."""
    template = _set_baseline_handoff_reminder()
    assert "BASELINE SET" in template
    assert "next-phase-handoff" in template
    assert "python -m spectrum_systems_core.cli next-phase-handoff" in template
