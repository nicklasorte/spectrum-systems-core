"""Phase Z — integration & contract tests.

Per CLAUDE.md: artifacts come from the fixture factories (which call
the REAL Z.1..Z.5 producers), are written to a real temp data-lake,
and each new artifact-touching script (run_dec18_loop.py,
loop_dashboard.py, check_f1_regression.py, ingest_corpus.py) is
exercised via subprocess against that directory. No mocked gates —
every rejection test feeds a REAL failing input through the REAL gate.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from spectrum_systems_core.data_lake.serialize import artifact_to_dict
from spectrum_systems_core.evals.extraction_comparison import (
    compare_extractions,
    contract_version,
)
from spectrum_systems_core.harness.improvement_cycle import (
    run_corpus_improvement_cycle,
)
from tests.integration.fixtures import (
    _ceiling_item,
    make_corpus_improvement_summary,
    make_corpus_ingest_summary,
    make_dec18_run_report,
    make_opus_ceiling_artifact,
    make_transcript_ingest_result,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
TID = "m-2025-12-18-7ghz-downlink-tig-kickoff"


def _env(**overrides: str) -> dict[str, str]:
    e = dict(os.environ)
    e.update(overrides)
    return e


def _run(script: str, *args: str, env: dict[str, str] | None = None):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )


def _validate(payload: dict[str, Any], expected_type: str) -> None:
    sys.path.insert(0, str(SCRIPTS))
    from _artifact_validator import validate_artifact  # noqa: WPS433

    validate_artifact(payload, expected_type)


# --------------------------------------------------------------------------
# Factory -> schema (catches writer/schema drift at the factory layer)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "factory,expected_type",
    [
        (make_dec18_run_report, "dec18_run_report"),
        (make_transcript_ingest_result, "transcript_ingest_result"),
        (make_corpus_ingest_summary, "corpus_ingest_summary"),
        (make_corpus_improvement_summary, "corpus_improvement_summary"),
    ],
)
def test_factory_artifacts_validate_against_schema(
    factory, expected_type
):
    art = factory()
    assert art.payload["artifact_type"] == expected_type
    _validate(art.payload, expected_type)


# --------------------------------------------------------------------------
# Z.1 — run_dec18_loop gates (REAL extract_ceiling / decide_control)
# --------------------------------------------------------------------------
def _seed_dec18_lake(tmp_path: Path) -> Path:
    dlp = tmp_path / "dl"
    tdir = dlp / "store" / "raw" / "meetings" / TID
    tdir.mkdir(parents=True)
    (tdir / "transcript.txt").write_text(
        "MEETING: 7 GHz Downlink TIG\nSPEAKER A: kickoff.\n",
        encoding="utf-8",
    )
    return dlp


def _ceiling_items_file(tmp_path: Path, n: int) -> Path:
    items = [
        {
            "item_id": f"c{i}",
            "schema_type": "decision",
            "source_turn_ids": [f"t{i}"],
            "source_text": f"approved item {i}",
            "payload": {},
        }
        for i in range(n)
    ]
    p = tmp_path / "ceiling_items.json"
    p.write_text(json.dumps({"items": items}), encoding="utf-8")
    return p


def test_z1_ceiling_floor_halt(tmp_path):
    dlp = _seed_dec18_lake(tmp_path)
    citems = _ceiling_items_file(tmp_path, 3)
    res = _run(
        "run_dec18_loop.py",
        env=_env(
            ANTHROPIC_API_KEY="test-key",
            DATA_LAKE_PATH=str(dlp),
            Z1_CEILING_ITEMS_JSON=str(citems),
        ),
    )
    assert res.returncode == 1, res.stdout + res.stderr
    report = json.loads(res.stdout)
    assert report["halt_reason"].startswith("ceiling_item_floor_not_met")
    assert "3 items" in report["halt_reason"]
    _validate(report, "dec18_run_report")


def test_z1_preflight_api_key_missing(tmp_path):
    dlp = _seed_dec18_lake(tmp_path)
    env = _env(DATA_LAKE_PATH=str(dlp))
    env.pop("ANTHROPIC_API_KEY", None)
    res = _run("run_dec18_loop.py", env=env)
    assert res.returncode == 1, res.stdout + res.stderr
    report = json.loads(res.stdout)
    assert report["halt_reason"] == (
        "environment_not_ready: ANTHROPIC_API_KEY missing"
    )
    _validate(report, "dec18_run_report")


def test_z1_no_haiku_artifact_halts(tmp_path):
    dlp = _seed_dec18_lake(tmp_path)
    citems = _ceiling_items_file(tmp_path, 60)
    res = _run(
        "run_dec18_loop.py",
        env=_env(
            ANTHROPIC_API_KEY="test-key",
            DATA_LAKE_PATH=str(dlp),
            Z1_CEILING_ITEMS_JSON=str(citems),
        ),
    )
    assert res.returncode == 1, res.stdout + res.stderr
    report = json.loads(res.stdout)
    assert report["halt_reason"] == "haiku_extraction_not_found"
    _validate(report, "dec18_run_report")


def test_z1_low_f1_blocks_and_is_schema_valid(tmp_path):
    """Spec Z.1 repro step 5: a low-F1 comparison -> control 'block',
    report schema-valid. Real ceiling (60) + real comparator; the
    Haiku artifact aligns only a handful so total_f1 << 0.70."""
    dlp = _seed_dec18_lake(tmp_path)
    citems = _ceiling_items_file(tmp_path, 60)
    haiku = make_opus_ceiling_artifact(
        items=[
            _ceiling_item("c0", "decision", ["t0"], "approved item 0"),
            _ceiling_item("c1", "decision", ["t1"], "approved item 1"),
        ]
    )
    hfile = tmp_path / "haiku.json"
    hfile.write_text(
        json.dumps(artifact_to_dict(haiku)), encoding="utf-8"
    )
    res = _run(
        "run_dec18_loop.py",
        env=_env(
            ANTHROPIC_API_KEY="test-key",
            DATA_LAKE_PATH=str(dlp),
            Z1_CEILING_ITEMS_JSON=str(citems),
            Z1_HAIKU_ARTIFACT_JSON=str(hfile),
        ),
    )
    report = json.loads(res.stdout)
    _validate(report, "dec18_run_report")
    assert report["control_decision"] == "block"
    assert report["total_f1"] is not None and report["total_f1"] < 0.70


def test_z1_prior_open_correction_pr_halts(tmp_path):
    dlp = _seed_dec18_lake(tmp_path)
    citems = _ceiling_items_file(tmp_path, 60)
    res = _run(
        "run_dec18_loop.py",
        env=_env(
            ANTHROPIC_API_KEY="test-key",
            DATA_LAKE_PATH=str(dlp),
            Z1_CEILING_ITEMS_JSON=str(citems),
            Z1_OPEN_PR_IDS="pr-correction-42",
        ),
    )
    assert res.returncode == 1
    report = json.loads(res.stdout)
    assert report["halt_reason"] == (
        "prior_open_correction_pr:pr-correction-42"
    )


# --------------------------------------------------------------------------
# Z.2 — loop_dashboard (read-only)
# --------------------------------------------------------------------------
def test_z2_no_artifacts_exits_0(tmp_path):
    store = tmp_path / "dl" / "store"
    store.mkdir(parents=True)
    res = _run(
        "loop_dashboard.py",
        "--transcript",
        TID,
        "--lake",
        str(tmp_path / "dl"),
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert f"no cycle has run yet for {TID}" in res.stdout


def test_z2_staleness_and_23_type_table(tmp_path):
    sys.path.insert(0, str(SCRIPTS))
    from _phase_z_lake import write_instrument  # noqa: WPS433

    store = tmp_path / "dl" / "store"
    store.mkdir(parents=True)

    # A real comparison artifact, written with created_at 36h in the
    # past so the dashboard flags it stale.
    from tests.integration.fixtures import (
        make_extraction_alignment_comparison_artifact,
    )

    cmp_art = make_extraction_alignment_comparison_artifact(
        transcript_id=TID
    )
    import datetime as _dt

    stale = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=36)
    ).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    object.__setattr__(cmp_art, "created_at", stale)
    write_instrument(store, TID, cmp_art)

    res = _run(
        "loop_dashboard.py", "--transcript", TID, "--lake",
        str(tmp_path / "dl"),
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "[STALE: 36h]" in res.stdout
    assert "(23 schema types enumerated)" in res.stdout


# --------------------------------------------------------------------------
# Z.3 — check_f1_regression (REAL comparator artifacts, REAL gate)
# --------------------------------------------------------------------------
def _cmp_artifact(per_type: dict[str, tuple[int, int, int]]):
    """Build a REAL extraction_alignment_comparison whose per-type
    (ceiling N, haiku M, aligned K) counts are exactly controlled:
    K matched pairs share turn-ids+text; the rest carry disjoint
    turn-ids and disjoint vocabulary so the real predicate cannot
    accidentally align them."""
    c_items: list[dict[str, Any]] = []
    h_items: list[dict[str, Any]] = []
    for st, (n, m, k) in per_type.items():
        for i in range(k):
            txt = f"{st} alpha{i} beta{i} gamma{i} matched span"
            c_items.append(
                _ceiling_item(f"{st}-c-m{i}", st, [f"{st}-shared{i}"], txt)
            )
            h_items.append(
                _ceiling_item(f"{st}-h-m{i}", st, [f"{st}-shared{i}"], txt)
            )
        for i in range(n - k):
            c_items.append(
                _ceiling_item(
                    f"{st}-c-u{i}", st, [f"{st}-cu{i}"],
                    f"{st} zeta{i} ceilingonly distinctword{i}",
                )
            )
        for i in range(m - k):
            h_items.append(
                _ceiling_item(
                    f"{st}-h-u{i}", st, [f"{st}-hu{i}"],
                    f"{st} omega{i} haikuonly otherword{i}",
                )
            )
    return compare_extractions(
        ceiling_artifact=make_opus_ceiling_artifact(items=c_items),
        haiku_artifact=make_opus_ceiling_artifact(items=h_items),
        alignment_contract_version=contract_version(),
    )


def _write_env(tmp_path: Path, name: str, art) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(artifact_to_dict(art)), encoding="utf-8")
    return p


def test_z3_total_regression_exit1(tmp_path):
    base = _cmp_artifact({"decision": (5, 5, 4)})       # f1 = 0.80
    new = _cmp_artifact({"decision": (13, 13, 10)})     # f1 ~ 0.769
    assert abs(base.payload["total_metrics"]["f1"] - 0.80) < 1e-9
    bp = _write_env(tmp_path, "base.json", base)
    np_ = _write_env(tmp_path, "new.json", new)
    res = _run(
        "check_f1_regression.py",
        env=_env(
            Z3_BASELINE_COMPARISON_JSON=str(bp),
            Z3_NEW_COMPARISON_JSON=str(np_),
        ),
    )
    assert res.returncode == 1, res.stdout + res.stderr
    assert "total_f1_regression: delta=-0.03" in res.stdout

    # Rollback switch -> exit 0, still logs the would-be verdict.
    res2 = _run(
        "check_f1_regression.py",
        env=_env(
            Z3_BASELINE_COMPARISON_JSON=str(bp),
            Z3_NEW_COMPARISON_JSON=str(np_),
            F1_REGRESSION_GATE_ENABLED="false",
        ),
    )
    assert res2.returncode == 0, res2.stdout + res2.stderr
    assert "gate disabled" in res2.stdout


def test_z3_per_type_regression_exit1(tmp_path):
    base = _cmp_artifact({"decision": (5, 5, 4), "claim": (5, 5, 4)})
    new = _cmp_artifact(
        {"decision": (50, 50, 37), "claim": (50, 50, 44)}
    )
    # total goes 0.80 -> 0.81 (no total regression) but decision
    # per-type f1 drops 0.80 -> 0.74 (delta -0.06).
    assert abs(base.payload["total_metrics"]["f1"] - 0.80) < 1e-9
    assert new.payload["total_metrics"]["f1"] > base.payload[
        "total_metrics"
    ]["f1"]
    bp = _write_env(tmp_path, "base.json", base)
    np_ = _write_env(tmp_path, "new.json", new)
    res = _run(
        "check_f1_regression.py",
        env=_env(
            Z3_BASELINE_COMPARISON_JSON=str(bp),
            Z3_NEW_COMPARISON_JSON=str(np_),
        ),
    )
    assert res.returncode == 1, res.stdout + res.stderr
    assert "per_type_regression: decision: delta=-0.06" in res.stdout
    assert "total_f1_regression" not in res.stdout


def test_z3_baseline_not_established_exits_0(tmp_path):
    store = tmp_path / "dl"
    (store / "store").mkdir(parents=True)
    res = _run(
        "check_f1_regression.py",
        env=_env(DATA_LAKE_PATH=str(store)),
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "baseline_not_yet_established" in res.stdout


def test_z3_comparator_idempotent():
    """Red-team Pass 1 #3: re-executing the comparator over the SAME
    frozen ceiling + Haiku artifacts is byte-identical (the gate's
    re-run must be replay-stable; this is exactly the Z.3 production
    path — one frozen ceiling re-scored, never rebuilt)."""
    from spectrum_systems_core.data_lake.serialize import canonical_json

    ceiling = make_opus_ceiling_artifact(
        items=[
            _ceiling_item("c0", "decision", ["t0"], "alpha beta gamma"),
            _ceiling_item("c1", "decision", ["t1"], "delta epsilon zeta"),
        ]
    )
    haiku = make_opus_ceiling_artifact(
        items=[
            _ceiling_item("h0", "decision", ["t0"], "alpha beta gamma"),
        ]
    )
    a = compare_extractions(
        ceiling_artifact=ceiling,
        haiku_artifact=haiku,
        alignment_contract_version=contract_version(),
    )
    b = compare_extractions(
        ceiling_artifact=ceiling,
        haiku_artifact=haiku,
        alignment_contract_version=contract_version(),
    )
    assert canonical_json(a.payload) == canonical_json(b.payload)


# --------------------------------------------------------------------------
# Z.4 — ingest_corpus
# --------------------------------------------------------------------------
def _corpus_lake(tmp_path: Path):
    dlp = tmp_path / "dl"
    raw = dlp / "store" / "raw" / "transcripts"
    raw.mkdir(parents=True)
    return dlp, raw


def _good_transcript(tag: str) -> str:
    turns = "\n".join(
        f"SPEAKER {chr(ord('A') + i % 5)}: substantive remark {i} for "
        f"{tag} about the 7 GHz downlink coexistence framework."
        for i in range(15)
    )
    return f"MEETING: {tag}\n{turns}\n"


def _few_turns_long_transcript() -> str:
    """>= 1000 chars + a MEETING header but only 5 speaker turns, so
    it fails SPECIFICALLY on the speaker-turn gate (Pass 2 #1 — the
    rejection input must reach the gate under test, not be short-
    circuited by the char-count gate)."""
    pad = "discussion of the aggregate interference methodology " * 8
    turns = "\n".join(
        f"SPEAKER {chr(ord('A') + i)}: {pad}" for i in range(5)
    )
    return f"MEETING: long-but-few-turns\n{turns}\n"


def test_z4_malformed_blocks_single_transcript(tmp_path):
    dlp, raw = _corpus_lake(tmp_path)
    (raw / "bad.txt").write_text(
        _few_turns_long_transcript(), encoding="utf-8"
    )
    manifest = tmp_path / "m.yaml"
    manifest.write_text(
        "transcripts:\n"
        "  - id: m-2026-09-09-bad\n"
        '    raw_path: "{data_lake}/raw/transcripts/bad.txt"\n',
        encoding="utf-8",
    )
    res = _run(
        "ingest_corpus.py", "--manifest", str(manifest),
        env=_env(DATA_LAKE_PATH=str(dlp)),
    )
    assert res.returncode == 0, res.stdout + res.stderr
    summary = json.loads(res.stdout)
    assert summary["blocked"] == 1 and summary["present"] == 0
    written = list(
        (dlp / "store" / "processed" / "meetings" / "m-2026-09-09-bad")
        .glob("transcript_ingest_result__*.json")
    )
    assert written, "no transcript_ingest_result written"
    rec = json.loads(written[0].read_text())["payload"]
    assert rec["status"] == "blocked"
    assert rec["reason"] == "ingest_format_error"
    # The SPECIFIC gate under test fired (not the char-count gate).
    assert rec["detail"].startswith("speaker_turn_count_below_min")
    assert rec["character_count"] >= 1000


def test_z4_wellformed_present_with_chunked_id(tmp_path):
    dlp, raw = _corpus_lake(tmp_path)
    (raw / "ok.txt").write_text(_good_transcript("ok"), encoding="utf-8")
    manifest = tmp_path / "m.yaml"
    manifest.write_text(
        "transcripts:\n"
        "  - id: m-2026-09-10-ok\n"
        '    raw_path: "{data_lake}/raw/transcripts/ok.txt"\n',
        encoding="utf-8",
    )
    res = _run(
        "ingest_corpus.py", "--manifest", str(manifest),
        env=_env(DATA_LAKE_PATH=str(dlp)),
    )
    summary = json.loads(res.stdout)
    assert summary["present"] == 1 and summary["blocked"] == 0
    mdir = dlp / "store" / "processed" / "meetings" / "m-2026-09-10-ok"
    rec = json.loads(
        next(mdir.glob("transcript_ingest_result__*.json")).read_text()
    )["payload"]
    assert rec["status"] == "present"
    assert rec["chunked_transcript_artifact_id"]
    assert list(mdir.glob("chunked_transcript__*.json"))


def test_z4_corpus_isolation_one_bad_two_good(tmp_path):
    """Red-team Pass 2 #3: transcripts 1 and 3 complete (have a
    chunked_transcript on disk) even though transcript 2 is
    malformed."""
    dlp, raw = _corpus_lake(tmp_path)
    (raw / "g1.txt").write_text(_good_transcript("g1"), encoding="utf-8")
    (raw / "b2.txt").write_text(
        "MEETING: x\nSPEAKER A: short.\n", encoding="utf-8"
    )
    (raw / "g3.txt").write_text(_good_transcript("g3"), encoding="utf-8")
    manifest = tmp_path / "m.yaml"
    manifest.write_text(
        "transcripts:\n"
        "  - id: m-2026-09-11-g1\n"
        '    raw_path: "{data_lake}/raw/transcripts/g1.txt"\n'
        "  - id: m-2026-09-12-b2\n"
        '    raw_path: "{data_lake}/raw/transcripts/b2.txt"\n'
        "  - id: m-2026-09-13-g3\n"
        '    raw_path: "{data_lake}/raw/transcripts/g3.txt"\n',
        encoding="utf-8",
    )
    res = _run(
        "ingest_corpus.py", "--manifest", str(manifest),
        env=_env(DATA_LAKE_PATH=str(dlp)),
    )
    summary = json.loads(res.stdout)
    assert summary["present"] == 2 and summary["blocked"] == 1
    assert summary["blocked_ids"] == ["m-2026-09-12-b2"]
    base = dlp / "store" / "processed" / "meetings"
    assert list((base / "m-2026-09-11-g1").glob("chunked_transcript__*.json"))
    assert list((base / "m-2026-09-13-g3").glob("chunked_transcript__*.json"))
    assert not list(
        (base / "m-2026-09-12-b2").glob("chunked_transcript__*.json")
    )


# --------------------------------------------------------------------------
# Z.5 — run_corpus_improvement_cycle pre-flight (REAL gate, real inputs)
# --------------------------------------------------------------------------
def _runner_promoted(_tid: str) -> dict[str, Any]:
    return {
        "overall_status": "promoted",
        "total_f1": 0.80,
        "false_negative_count": 1,
        "correction_candidates_produced": 0,
        "blocking_phase": None,
        "error_or_none": None,
    }


def _ingest_ok(n: int) -> dict[str, Any]:
    return {
        "artifact_type": "corpus_ingest_summary",
        "schema_version": "1.0.0",
        "produced_at": "1970-01-01T00:00:00+00:00",
        "total_transcripts": n,
        "present": n,
        "blocked": 0,
        "blocked_ids": [],
    }


def test_z5_preflight_no_summary_halts():
    art = run_corpus_improvement_cycle(
        transcript_ids=["a", "b"],
        corpus_ingest_summary_loader=lambda: None,
        per_transcript_runner=_runner_promoted,
    )
    p = art.payload
    assert p["preflight_halt_reason"] == "corpus_not_ingested"
    assert p["per_transcript"] == []  # Pass 2 #4 — NO phase ran
    assert p["present"] == 0 and p["corpus_f1"] is None


def test_z5_preflight_blocked_transcripts_halts():
    summary = _ingest_ok(2)
    summary["blocked"] = 1
    summary["blocked_ids"] = ["b"]
    art = run_corpus_improvement_cycle(
        transcript_ids=["a", "b"],
        corpus_ingest_summary_loader=lambda: summary,
        per_transcript_runner=_runner_promoted,
    )
    p = art.payload
    assert p["preflight_halt_reason"].startswith("corpus_partially_blocked")
    assert p["per_transcript"] == []


def test_z5_preflight_open_pr_halts():
    art = run_corpus_improvement_cycle(
        transcript_ids=["a", "b", "c"],
        corpus_ingest_summary_loader=lambda: _ingest_ok(3),
        per_transcript_runner=_runner_promoted,
        open_pr_lookup=lambda tid: ["pr-9"] if tid == "b" else [],
    )
    p = art.payload
    assert p["preflight_halt_reason"] == "prior_open_correction_pr:b"
    assert p["per_transcript"] == []


def test_z5_isolation_and_corpus_f1_mean():
    def runner(tid: str) -> dict[str, Any]:
        if tid == "c":
            return {
                "overall_status": "blocked",
                "total_f1": None,
                "false_negative_count": None,
                "correction_candidates_produced": None,
                "blocking_phase": "Y_3",
                "error_or_none": None,
            }
        return {
            "overall_status": "promoted",
            "total_f1": 0.80 if tid == "a" else 0.90,
            "false_negative_count": 2,
            "correction_candidates_produced": 1,
            "blocking_phase": None,
            "error_or_none": None,
        }

    art = run_corpus_improvement_cycle(
        transcript_ids=["a", "b", "c"],
        corpus_ingest_summary_loader=lambda: _ingest_ok(3),
        per_transcript_runner=runner,
    )
    p = art.payload
    assert p["present"] == 2 and p["blocked"] == 1
    assert abs(p["corpus_f1"] - 0.85) < 1e-9
    _validate(p, "corpus_improvement_summary")


def test_z5_corpus_f1_null_when_all_blocked():
    art = run_corpus_improvement_cycle(
        transcript_ids=["a", "b"],
        corpus_ingest_summary_loader=lambda: _ingest_ok(2),
        per_transcript_runner=lambda _t: {
            "overall_status": "blocked",
            "total_f1": None,
            "false_negative_count": None,
            "correction_candidates_produced": None,
            "blocking_phase": "Y_1",
            "error_or_none": None,
        },
    )
    p = art.payload
    assert p["corpus_f1"] is None
    assert p["corpus_f1_null_reason"] == (
        "no_present_transcript_with_total_f1"
    )


def test_z5_per_transcript_error_isolated():
    def runner(tid: str) -> dict[str, Any]:
        if tid == "b":
            raise RuntimeError("transcript b exploded")
        return _runner_promoted(tid)

    art = run_corpus_improvement_cycle(
        transcript_ids=["a", "b", "c"],
        corpus_ingest_summary_loader=lambda: _ingest_ok(3),
        per_transcript_runner=runner,
    )
    p = art.payload
    statuses = {r["transcript_id"]: r["overall_status"]
                for r in p["per_transcript"]}
    assert statuses == {"a": "promoted", "b": "error", "c": "promoted"}
    assert p["present"] == 2
