"""Phase AB.2 — pipeline invariant: Opus raw_output is never parsed.

The ONLY code permitted to read ``extraction_unconstrained.payload.
raw_output`` is the approximate parser in
``spectrum_systems_core.evals.extraction_gap``. Two independent
guards:

  A. STATIC — the literal token ``raw_output`` must appear in NO
     ``src/spectrum_systems_core/evals/*.py`` file except
     ``extraction_gap.py``, and in NO control / promotion / governed-
     loop module. This auto-covers evals that do not exist yet
     (red-team Pass 2: a future eval that reads raw_output is caught
     here without anyone editing this test).

  B. RUNTIME — every currently-registered ``run_*`` eval entrypoint is
     driven against an ``extraction_unconstrained`` artifact whose
     ``raw_output`` carries a unique sentinel that would surface as an
     extracted item if parsed. No eval_result may contain the
     sentinel. A frozen entrypoint set makes a NEW eval fail this
     test until its runtime coverage is added.
"""
from __future__ import annotations

import inspect
import json
import pathlib

import spectrum_systems_core.evals as evals_pkg
from spectrum_systems_core.artifacts import new_artifact
from spectrum_systems_core.extraction.comparison_runner import (
    UNCONSTRAINED_TYPE,
)

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "spectrum_systems_core"

SENTINEL = "SENTINEL_RAWOUTPUT_NEVER_PARSED_a9f3c1"

# raw_output is valid JSON whose parsed shape, if any eval parsed it,
# would inject a decision whose text is the sentinel.
_RAW_OUTPUT = json.dumps(
    {"decisions": [{"text": SENTINEL, "verb": "approved",
                    "source_turns": ["t0001"]}]}
)


def _evals_files() -> list[pathlib.Path]:
    return sorted((SRC / "evals").rglob("*.py"))


def test_static_raw_output_only_in_extraction_gap():
    offenders: list[str] = []
    for f in _evals_files():
        if f.name == "extraction_gap.py":
            continue
        text = f.read_text(encoding="utf-8")
        if "raw_output" in text:
            offenders.append(str(f.relative_to(SRC)))
    assert not offenders, (
        "raw_output referenced by a non-gap eval module — the Opus "
        f"never-parse invariant is broken: {offenders}"
    )


def test_static_raw_output_absent_from_loop_control_promotion():
    guarded = [
        SRC / "control" / "decision.py",
        SRC / "promotion" / "promoter.py",
        SRC / "workflows" / "_loop.py",
    ]
    offenders = [
        str(p.relative_to(SRC))
        for p in guarded
        if p.is_file() and "raw_output" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"raw_output referenced by a core-loop module: {offenders}"
    )


def test_extraction_gap_is_the_single_parser():
    """The approving direction of the static check: extraction_gap.py
    MUST reference raw_output (it is the one allowed parser). If this
    fails, the parser was moved/renamed and the invariant docs are
    stale."""
    gap = (SRC / "evals" / "extraction_gap.py").read_text(encoding="utf-8")
    assert "raw_output" in gap


def test_registered_eval_entrypoints_are_the_frozen_set():
    """Freeze the eval entrypoint surface. A NEW ``run_*`` eval must be
    added to this set AND given runtime coverage below — otherwise this
    test fails, forcing the author to prove the new eval ignores
    raw_output (red-team Pass 2 item 3)."""
    found = {
        n
        for n in dir(evals_pkg)
        if n.startswith("run_") and callable(getattr(evals_pkg, n))
    }
    expected = {
        "run_required_evals",
        "run_source_turn_validity_eval",
        "run_source_turn_validity_eval_from_chunks",
        "run_grounding_coverage_eval",
        "run_regulatory_verb_eval",
        "run_extraction_precision_eval",
        "run_llm_strict_schema_eval",
        "run_llm_nonempty_eval",
        "run_llm_within_source_eval",
        "run_llm_gt_coverage_eval",
        "run_tlc_routed_eval",
    }
    assert found == expected, (
        f"eval entrypoint surface changed: only-in-found="
        f"{sorted(found - expected)} only-in-expected="
        f"{sorted(expected - found)}. Add the new eval to this set and "
        f"to test_no_eval_surfaces_raw_output's call table."
    )


def _unconstrained_artifact():
    return new_artifact(
        artifact_type=UNCONSTRAINED_TYPE,
        payload={
            "meeting_id": "m-invariant-001",
            "raw_output": _RAW_OUTPUT,
            "model": "claude-opus-4-7",
            "prompt": "ignored",
            "cost_usd": 1.23,
            "latency_ms": 456,
        },
        trace_id="trace-invariant",
        status="draft",
    )


def _valid_source_record(tmp_path: pathlib.Path) -> pathlib.Path:
    """A minimal real source_record so source-record-reading evals run
    their FULL logic against the artifact instead of short-circuiting
    on a missing record."""
    sr = {
        "artifact_type": "source_record",
        "payload": {
            "meeting_id": "m-invariant-001",
            "transcript_hash": "deadbeef",
            "chunks": [
                {"turn_id": "t0001", "speaker": "CHAIR",
                 "text": "nothing relevant here", "line_start": 1,
                 "line_end": 1},
            ],
        },
    }
    p = tmp_path / "source_record__m-invariant-001.json"
    p.write_text(json.dumps(sr), encoding="utf-8")
    return p


def test_no_eval_surfaces_raw_output(tmp_path):
    art = _unconstrained_artifact()
    sr_path = _valid_source_record(tmp_path)

    # Every registered entrypoint, called with correct args. The
    # frozen-set test above guarantees this table stays exhaustive.
    results = []
    results += evals_pkg.run_required_evals(art)
    results.append(evals_pkg.run_source_turn_validity_eval(art, sr_path))
    results.append(
        evals_pkg.run_source_turn_validity_eval_from_chunks(
            art,
            [
                {"turn_id": "t0001", "speaker": "CHAIR",
                 "text": "nothing relevant here", "line_start": 1,
                 "line_end": 1},
            ],
        )
    )
    results.append(evals_pkg.run_grounding_coverage_eval(art))
    results.append(evals_pkg.run_regulatory_verb_eval(art))
    results.append(evals_pkg.run_extraction_precision_eval(art, sr_path))
    results.append(evals_pkg.run_llm_strict_schema_eval(art))
    results.append(evals_pkg.run_llm_nonempty_eval(art, "transcript text"))
    results.append(evals_pkg.run_llm_within_source_eval(art, "transcript text"))
    results.append(
        evals_pkg.run_llm_gt_coverage_eval(art, source_id=None, lake_root=None)
    )
    # P8-A routing eval: classifies content arrays and re-runs the
    # per-lane eval subset. It never reads raw_output (it iterates only
    # classified content-array keys), so the sentinel must not surface.
    results.append(
        evals_pkg.run_tlc_routed_eval(art, transcript_text="transcript text")
    )

    for r in results:
        assert r.artifact_type == "eval_result"
        blob = json.dumps(r.payload, default=str)
        assert SENTINEL not in blob, (
            f"eval '{r.payload.get('eval_type')}' surfaced Opus "
            f"raw_output content — never-parse invariant broken: "
            f"{r.payload}"
        )
