"""Integration contract test for ``scripts/compare_opus_haiku.py``.

CLAUDE.md non-negotiable: a script that reads a pipeline artifact
(the promoted ``meeting_minutes`` envelope) and calls
``validate_artifact`` must have an integration test that

  1. Uses ``tests/integration/fixtures.py`` factories — never a
     hand-rolled dict — to produce the artifact via the real writer
     (here: the real LLM governed loop + ``write_promoted_artifact``).
  2. Writes artifacts to a real temp directory (not mocked).
  3. Calls the script via ``subprocess.run`` against the temp dir.
  4. Asserts the correct output on disk (not just the return code).

This catches writer/reader field drift at the fixture-factory level
before the script logic runs — the exact bug class CLAUDE.md cites.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from tests.integration.fixtures import (
    make_human_minutes_gt_pair,
    make_opus_reference_baseline,
    make_promoted_meeting_minutes_artifact,
    make_source_record,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "compare_opus_haiku.py"
SOURCE_ID = "7-ghz-downlink-tig-meeting-kickoff---transcript-20251218"
OPUS_MODEL = "claude-opus-4-6"

DECISIONS = ["The group approved the 7 GHz downlink threshold."]
ACTION_ITEMS = ["DoD will submit revised ERP values before the next session."]
OPEN_QUESTIONS = [
    "What is the coordination distance for federal incumbents?"
]


def _seed(tmp_path: Path, *, gt: bool = True, llm: bool = True) -> Path:
    dl = tmp_path / "data-lake"
    store = dl / "store"
    sid_dir = store / "processed" / "meetings" / SOURCE_ID
    sid_dir.mkdir(parents=True)
    source_artifact_id = str(uuid.uuid4())
    (sid_dir / "source_record.json").write_text(
        json.dumps(make_source_record(SOURCE_ID, source_artifact_id)),
        encoding="utf-8",
    )

    make_opus_reference_baseline(
        data_lake_root=dl,
        source_id=SOURCE_ID,
        source_artifact_id=source_artifact_id,
        model=OPUS_MODEL,
        items_by_type={
            "decisions": DECISIONS
            + ["The group deferred the aggregate interference methodology."],
            "action_items": ACTION_ITEMS,
            "open_questions": OPEN_QUESTIONS,
        },
    )

    if llm:
        path = make_promoted_meeting_minutes_artifact(
            lake_root=store,
            source_id=SOURCE_ID,
            decisions=DECISIONS,
            action_items=ACTION_ITEMS,
            open_questions=OPEN_QUESTIONS,
        )
        assert path.name.startswith("meeting_minutes__")
        assert path.is_file()

    if gt:
        gt_path = (
            sid_dir / "ground_truth" / "human_minutes_gt_pairs.jsonl"
        )
        gt_path.parent.mkdir(parents=True, exist_ok=True)
        pair = make_human_minutes_gt_pair(
            source_id=SOURCE_ID,
            source_artifact_id=source_artifact_id,
            ground_truth_text=DECISIONS[0],
            extraction_type="decision",
        )
        gt_path.write_text(json.dumps(pair) + "\n", encoding="utf-8")

    return dl


def _run(args: list[str]):
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # prove no live model path
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )


def _comparison_artifact(dl: Path) -> Path:
    comp_dir = (
        dl / "store" / "processed" / "meetings" / SOURCE_ID
        / "comparisons"
    )
    files = sorted(comp_dir.glob("haiku_vs_opus_*.json"))
    assert files, f"no comparison artifact under {comp_dir}"
    return files[-1]


def test_subprocess_writes_comparison_and_eval_history(
    tmp_path: Path,
) -> None:
    dl = _seed(tmp_path)
    result = _run(["--data-lake", str(dl), "--source-id", SOURCE_ID])
    assert result.returncode == 0, (
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )

    art = json.loads(
        _comparison_artifact(dl).read_text(encoding="utf-8")
    )
    assert art["artifact_type"] == "comparison_result"
    assert "artifact_kind" not in json.dumps(art)
    assert art["schema_version"] == "1.0.0"
    assert art["opus_model_id"] == OPUS_MODEL
    s = art["summary"]
    # Opus = 2 decisions + 1 action + 1 question = 4. Haiku reproduced
    # 1 decision + 1 action + 1 question = 3 verbatim. The deferred
    # decision is the one false negative.
    assert s["total_opus_items"] == 4
    assert s["total_haiku_items"] == 3
    assert s["true_positives"] == 3
    assert s["false_negatives"] == 1
    assert s["haiku_recall_vs_opus"] == 0.75
    assert s["haiku_precision_vs_opus"] == 1.0
    assert s["gt_recall_haiku"] == 1.0

    eh = (
        dl / "store" / "processed" / "meetings" / SOURCE_ID
        / "eval_history.jsonl"
    )
    rows = [
        json.loads(ln)
        for ln in eh.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert any(
        r.get("eval_type") == "haiku_vs_opus_comparison" for r in rows
    )


def test_subprocess_dry_run_writes_nothing(tmp_path: Path) -> None:
    dl = _seed(tmp_path)
    result = _run(
        ["--data-lake", str(dl), "--source-id", SOURCE_ID, "--dry-run"]
    )
    assert result.returncode == 0, result.stderr
    comp_dir = (
        dl / "store" / "processed" / "meetings" / SOURCE_ID
        / "comparisons"
    )
    assert not comp_dir.exists() or not list(comp_dir.glob("*.json"))
    # The end-of-run dry-run marker goes to STDERR, never STDOUT.
    assert "DRY RUN — artifact not written" in result.stderr
    assert "DRY RUN" not in result.stdout


def _assert_stdout_pure_json(result) -> dict:
    """STDOUT must parse as a single JSON object even with debug flags.

    The workflow tees STDOUT to a file the summary/threshold steps
    json.loads — any debug print leaking onto STDOUT silently breaks
    the F1 < 0.70 correction-miner gate. This is the property the
    print flags must never violate.
    """
    assert result.returncode == 0, (
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    data = json.loads(result.stdout)
    assert data["status"] == "success"
    return data


def test_subprocess_print_inputs_to_stderr_stdout_stays_json(
    tmp_path: Path,
) -> None:
    dl = _seed(tmp_path)
    result = _run(
        [
            "--data-lake", str(dl), "--source-id", SOURCE_ID,
            "--print-inputs",
        ]
    )
    _assert_stdout_pure_json(result)
    # Paths + counts land on STDERR, confirming both artifacts read.
    assert "=== print_inputs ===" in result.stderr
    assert "opus artifact path:" in result.stderr
    assert "haiku artifact path:" in result.stderr
    # Opus = 2 decisions + 1 action + 1 question = 4 baseline rows.
    assert "opus item count:     4" in result.stderr
    # Haiku reproduced 1 decision + 1 action + 1 question = 3 items.
    assert "haiku item count:    3" in result.stderr
    # The debug readout never leaks onto STDOUT (the json.loads in
    # _assert_stdout_pure_json already proves STDOUT is one clean
    # object; this pins the specific markers out of it too).
    assert "=== print_inputs ===" not in result.stdout
    assert "opus item count:" not in result.stdout
    # Observe-only: the artifact is still written normally.
    assert json.loads(
        _comparison_artifact(dl).read_text(encoding="utf-8")
    )["summary"]["total_opus_items"] == 4


def test_subprocess_print_scores_to_stderr_stdout_stays_json(
    tmp_path: Path,
) -> None:
    dl = _seed(tmp_path)
    result = _run(
        [
            "--data-lake", str(dl), "--source-id", SOURCE_ID,
            "--print-scores",
        ]
    )
    data = _assert_stdout_pure_json(result)
    assert "=== print_scores ===" in result.stderr
    # The full comparison_result payload (summary + by_type) on STDERR.
    assert '"haiku_f1_vs_opus"' in result.stderr
    assert '"haiku_precision_vs_opus"' in result.stderr
    assert '"haiku_recall_vs_opus"' in result.stderr
    assert '"by_type"' in result.stderr
    # STDOUT is still exactly the run-summary JSON, nothing more.
    assert data["summary"]["true_positives"] == 3


def test_subprocess_all_debug_flags_stdout_stays_json(
    tmp_path: Path,
) -> None:
    """All three flags together: STDOUT must still be one JSON object
    and a dry run must still write nothing."""
    dl = _seed(tmp_path)
    result = _run(
        [
            "--data-lake", str(dl), "--source-id", SOURCE_ID,
            "--dry-run", "--print-inputs", "--print-scores",
        ]
    )
    _assert_stdout_pure_json(result)
    assert "=== print_inputs ===" in result.stderr
    assert "=== print_scores ===" in result.stderr
    assert "DRY RUN — artifact not written" in result.stderr
    comp_dir = (
        dl / "store" / "processed" / "meetings" / SOURCE_ID
        / "comparisons"
    )
    assert not comp_dir.exists() or not list(comp_dir.glob("*.json"))
    assert not (
        dl / "store" / "processed" / "meetings" / SOURCE_ID
        / "eval_history.jsonl"
    ).exists()


def test_subprocess_missing_opus_baseline_halts(tmp_path: Path) -> None:
    dl = tmp_path / "data-lake"
    (dl / "store" / "processed" / "meetings" / SOURCE_ID).mkdir(
        parents=True
    )
    result = _run(["--data-lake", str(dl), "--source-id", SOURCE_ID])
    assert result.returncode == 1
    assert "missing_opus_baseline" in result.stdout


def test_subprocess_no_llm_artifact_halts(tmp_path: Path) -> None:
    dl = _seed(tmp_path, llm=False)
    result = _run(["--data-lake", str(dl), "--source-id", SOURCE_ID])
    assert result.returncode == 1
    assert "missing_haiku_llm_output" in result.stdout


# Object-form decision text (each value is in OBJ_TRANSCRIPT verbatim so
# the within-source gate passes and the artifact actually promotes).
OBJ_DECISIONS = [
    {
        "text": "The group approved the 7 GHz downlink threshold.",
        "verb": "approved",
        "stakeholders": ["DoD"],
        "confidence": 0.9,
    },
    {
        "text": "The group deferred the aggregate interference methodology.",
        "verb": "deferred",
    },
]
OBJ_TRANSCRIPT = (
    "7 GHz Downlink TIG kickoff\n"
    + "\n".join(
        [d["text"] for d in OBJ_DECISIONS] + ACTION_ITEMS + OPEN_QUESTIONS
    )
    + "\n"
)


def _seed_object_decisions(tmp_path: Path) -> Path:
    """Seed an Opus baseline + a promoted Haiku artifact whose
    ``decisions`` are OBJECT-form — the shape the real LLM workflow
    writes (the prompt encourages the object form and the workflow
    stamps a ``verb`` onto every object decision). Both sides are built
    from the SAME items via the real builders/writer."""
    dl = tmp_path / "data-lake"
    store = dl / "store"
    sid_dir = store / "processed" / "meetings" / SOURCE_ID
    sid_dir.mkdir(parents=True)
    source_artifact_id = str(uuid.uuid4())
    (sid_dir / "source_record.json").write_text(
        json.dumps(make_source_record(SOURCE_ID, source_artifact_id)),
        encoding="utf-8",
    )

    make_opus_reference_baseline(
        data_lake_root=dl,
        source_id=SOURCE_ID,
        source_artifact_id=source_artifact_id,
        model=OPUS_MODEL,
        items_by_type={
            "decisions": OBJ_DECISIONS,
            "action_items": ACTION_ITEMS,
            "open_questions": OPEN_QUESTIONS,
        },
    )

    path = make_promoted_meeting_minutes_artifact(
        lake_root=store,
        source_id=SOURCE_ID,
        decisions=OBJ_DECISIONS,
        action_items=ACTION_ITEMS,
        open_questions=OPEN_QUESTIONS,
        transcript_text=OBJ_TRANSCRIPT,
    )
    assert path.name.startswith("meeting_minutes__")
    art = json.loads(path.read_text(encoding="utf-8"))
    # Precondition: the artifact really is the real shape — object-form
    # decisions plus a populated grounding array (the operator-visible
    # signal that content WAS extracted, even when the comparison reads
    # 0). If this drifts the regression below is meaningless.
    assert isinstance(art["payload"]["decisions"][0], dict)
    assert len(art["payload"].get("grounding", [])) > 0
    return dl


def test_subprocess_object_form_decisions_are_compared(
    tmp_path: Path,
) -> None:
    """Regression: object-form ``decisions`` written by the real
    workflow must be READ by the comparison.

    Before the fix ``compare_opus_haiku._item_text`` resolved an
    object-form decision to ``''`` (it consulted a per-type map that no
    longer matched the Opus baseline producer, which had switched to a
    tolerant priority-field reader). The script then reported
    ``decisions.haiku_count == 0`` and 0.0 recall against an Opus
    baseline built from the SAME items — a lying diff on a real,
    promoted, fully-grounded artifact. This asserts the on-disk
    comparison artifact now reflects the decisions.
    """
    dl = _seed_object_decisions(tmp_path)
    result = _run(["--data-lake", str(dl), "--source-id", SOURCE_ID])
    assert result.returncode == 0, (
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    art = json.loads(
        _comparison_artifact(dl).read_text(encoding="utf-8")
    )
    s = art["summary"]
    by_type = art["by_type"]
    # The precise regression assertion: object-form decisions are read.
    # Pre-fix these were 0; post-fix they are 2.
    assert by_type["decisions"]["haiku_count"] == 2, by_type["decisions"]
    assert by_type["decisions"]["true_positives"] == 2, by_type[
        "decisions"
    ]
    # Opus = 2 decisions + 1 action + 1 question = 4; Haiku reproduced
    # all 4 verbatim. Pre-fix total_haiku_items was 2 (decisions
    # dropped) and recall 0.5 — both caught here too.
    assert s["total_opus_items"] == 4, s
    assert s["total_haiku_items"] == 4, s
    assert s["true_positives"] == 4, s
    assert s["haiku_recall_vs_opus"] == 1.0, s


STALE_DECISIONS = ["A stale decision from an earlier discarded run."]


def test_subprocess_selects_newest_when_two_artifacts_exist(
    tmp_path: Path,
) -> None:
    """Regression: two promoted LLM ``meeting_minutes`` artifacts for
    one source — the script must compare the NEWEST, not whichever
    sorts first by filename.

    Both artifacts are produced through the real workflow + writer
    (``make_promoted_meeting_minutes_artifact``); their filenames are
    ``meeting_minutes__<content-hash>.json``, so which one sorts first
    is content-dependent and unrelated to recency. The stale artifact
    (decisions NOT in the Opus baseline) is forced strictly OLDER on
    disk; the fresh artifact's decisions match the baseline. Pre-fix
    ``find_haiku_artifact`` returned the filename-first artifact and the
    comparison halted/lied at ``haiku_item_count == 0`` when that was
    the stale run. Post-fix it selects by recency, so the on-disk
    comparison must reflect the FRESH artifact.
    """
    dl = tmp_path / "data-lake"
    store = dl / "store"
    sid_dir = store / "processed" / "meetings" / SOURCE_ID
    sid_dir.mkdir(parents=True)
    source_artifact_id = str(uuid.uuid4())
    (sid_dir / "source_record.json").write_text(
        json.dumps(make_source_record(SOURCE_ID, source_artifact_id)),
        encoding="utf-8",
    )

    # Opus baseline = the FRESH items only.
    make_opus_reference_baseline(
        data_lake_root=dl,
        source_id=SOURCE_ID,
        source_artifact_id=source_artifact_id,
        model=OPUS_MODEL,
        items_by_type={
            "decisions": DECISIONS,
            "action_items": ACTION_ITEMS,
            "open_questions": OPEN_QUESTIONS,
        },
    )

    # Stale earlier run: real promoted artifact whose decision is NOT in
    # the baseline (so selecting it would tank recall and be detectable).
    stale_path = make_promoted_meeting_minutes_artifact(
        lake_root=store,
        source_id=SOURCE_ID,
        decisions=STALE_DECISIONS,
        action_items=ACTION_ITEMS,
        open_questions=OPEN_QUESTIONS,
    )
    # Fresh current run: real promoted artifact matching the baseline.
    fresh_path = make_promoted_meeting_minutes_artifact(
        lake_root=store,
        source_id=SOURCE_ID,
        decisions=DECISIONS,
        action_items=ACTION_ITEMS,
        open_questions=OPEN_QUESTIONS,
    )
    assert stale_path != fresh_path, (
        "factory must write distinct files for distinct content"
    )

    # Force the stale artifact strictly OLDER than the fresh one so the
    # recency ordering is unambiguous regardless of write timing /
    # filesystem mtime granularity.
    os.utime(stale_path, (1_000_000, 1_000_000))
    os.utime(fresh_path, (2_000_000, 2_000_000))

    result = _run(["--data-lake", str(dl), "--source-id", SOURCE_ID])
    assert result.returncode == 0, (
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    data = json.loads(result.stdout)
    # Direct assertion: the script selected the FRESH artifact.
    assert data["haiku_artifact_path"] == str(fresh_path), data

    art = json.loads(
        _comparison_artifact(dl).read_text(encoding="utf-8")
    )
    s = art["summary"]
    by_type = art["by_type"]
    # Fresh decision is in the baseline → it matches. The stale
    # decision string is not present anywhere, so had the stale
    # artifact been (wrongly) selected this would be 0.
    assert by_type["decisions"]["haiku_count"] == 1, by_type["decisions"]
    assert by_type["decisions"]["true_positives"] == 1, by_type[
        "decisions"
    ]
    assert s["total_opus_items"] == 3, s
    assert s["total_haiku_items"] == 3, s
    assert s["true_positives"] == 3, s
    assert s["haiku_recall_vs_opus"] == 1.0, s


def test_subprocess_empty_artifact_does_not_shadow_on_mtime_collision(
    tmp_path: Path,
) -> None:
    """End-to-end regression for the PR #183 follow-up bug.

    The runner reaches the selector only via ``clone-data-lake``
    (``git clone``), which stamps EVERY checked-out file's mtime with
    the single clone timestamp. With mtimes EQUAL the ``(st_mtime,
    filename)`` key ties and the pre-fix ``max()`` falls back to the
    content-blind filename — so a stale all-empty LLM run could
    deterministically shadow the real extraction (the data-lake's
    ``...67ccaa13dda9.json`` empty file sorted AFTER, hence over, the
    real ``...4138e10ad104.json``). The comparison then produced no
    real output and the push step was skipped.

    The compared/populated artifact is produced through the REAL
    factory + writer (CLAUDE.md integration rule). The stale shadow is
    the SAME factory envelope shape with its extraction arrays nulled —
    exactly the real ``67cc`` file (valid LLM envelope, zero content) —
    renamed so it sorts strictly AFTER the fresh one and given an
    IDENTICAL mtime. Pre-fix: subprocess selects the empty file
    (``total_haiku_items == 0``). Post-fix: the content check skips it
    and the on-disk comparison reflects the fresh artifact.
    """
    dl = tmp_path / "data-lake"
    store = dl / "store"
    sid_dir = store / "processed" / "meetings" / SOURCE_ID
    sid_dir.mkdir(parents=True)
    source_artifact_id = str(uuid.uuid4())
    (sid_dir / "source_record.json").write_text(
        json.dumps(make_source_record(SOURCE_ID, source_artifact_id)),
        encoding="utf-8",
    )

    make_opus_reference_baseline(
        data_lake_root=dl,
        source_id=SOURCE_ID,
        source_artifact_id=source_artifact_id,
        model=OPUS_MODEL,
        items_by_type={
            "decisions": DECISIONS,
            "action_items": ACTION_ITEMS,
            "open_questions": OPEN_QUESTIONS,
        },
    )

    # Fresh, real, promoted artifact — matches the Opus baseline.
    fresh_path = make_promoted_meeting_minutes_artifact(
        lake_root=store,
        source_id=SOURCE_ID,
        decisions=DECISIONS,
        action_items=ACTION_ITEMS,
        open_questions=OPEN_QUESTIONS,
    )

    # Stale shadow: build a real factory artifact, then null every
    # extraction array to reproduce the real ``67cc`` empty LLM file.
    # The factory shape is preserved; only the content is emptied.
    seed_stale = make_promoted_meeting_minutes_artifact(
        lake_root=store,
        source_id=SOURCE_ID,
        decisions=STALE_DECISIONS,
        action_items=ACTION_ITEMS,
        open_questions=OPEN_QUESTIONS,
    )
    stale_doc = json.loads(seed_stale.read_text(encoding="utf-8"))
    for key, val in list(stale_doc["payload"].items()):
        if isinstance(val, list):
            stale_doc["payload"][key] = []
    seed_stale.unlink()  # remove the populated seed; keep only 2 files
    # Name it so it sorts strictly AFTER the fresh file: the pre-fix
    # max((mtime, name)) then deterministically picks this empty file
    # once the mtimes tie.
    empty_path = sid_dir / "meeting_minutes__zzzzzzzzzzzzzzzz.json"
    empty_path.write_text(json.dumps(stale_doc), encoding="utf-8")
    assert empty_path.name > fresh_path.name, (
        empty_path.name,
        fresh_path.name,
    )

    # Simulate git clone: BOTH files get the IDENTICAL clone timestamp.
    clone_ts = 1_700_000_000
    os.utime(fresh_path, (clone_ts, clone_ts))
    os.utime(empty_path, (clone_ts, clone_ts))
    assert (
        fresh_path.stat().st_mtime == empty_path.stat().st_mtime
    )

    result = _run(["--data-lake", str(dl), "--source-id", SOURCE_ID])
    assert result.returncode == 0, (
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    data = json.loads(result.stdout)
    # The script must have selected the FRESH artifact, not the empty
    # shadow — this is the exact pre/post discriminator.
    assert data["haiku_artifact_path"] == str(fresh_path), data
    assert data["summary"]["total_haiku_items"] == 3, data["summary"]

    art = json.loads(
        _comparison_artifact(dl).read_text(encoding="utf-8")
    )
    s = art["summary"]
    assert s["total_opus_items"] == 3, s
    assert s["total_haiku_items"] == 3, s
    assert s["true_positives"] == 3, s
    assert s["haiku_recall_vs_opus"] == 1.0, s
