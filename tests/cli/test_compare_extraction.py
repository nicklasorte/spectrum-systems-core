"""Phase AB.3 — compare-extraction CLI / runner tests.

No real API call: missing-key test gates before any client; stub-mode
and failure-injection tests use deterministic in-process extractors.
"""
from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

from spectrum_systems_core.data_lake import run_transcript_pipeline
from spectrum_systems_core.data_lake.cli import main as dl_main
from spectrum_systems_core.extraction.comparison_runner import (
    COMPARISON_TYPE,
    run_compare_extraction,
    slugify,
)

FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures" / "comparison_gold" / "meeting_real_001"
)
MEETING_ID = "meeting_real_001"


def _seed_and_chunk(lake_root: Path) -> None:
    """Seed raw inputs and run the real pipeline so a real
    ``source_record`` is on disk (the runner refuses to proceed
    without it)."""
    raw = lake_root / "raw" / "meetings" / MEETING_ID
    raw.mkdir(parents=True)
    shutil.copy(FIXTURE / "transcript.txt", raw / "transcript.txt")
    (raw / "metadata.json").write_text(
        json.dumps(
            {
                "meeting_id": MEETING_ID,
                "title": "Comparison gold meeting",
                "date": "2026-05-16",
                "source_type": "transcript",
            }
        ),
        encoding="utf-8",
    )
    result = run_transcript_pipeline(
        lake_root=lake_root,
        meeting_id=MEETING_ID,
        workflow_name="meeting_minutes",
    )
    assert Path(result.source_record_path).is_file()


def _comparison_files(lake_root: Path) -> list[Path]:
    meeting_dir = lake_root / "processed" / "meetings" / MEETING_ID
    if not meeting_dir.is_dir():
        return []
    return list(meeting_dir.glob(f"{COMPARISON_TYPE}__*.json"))


def test_missing_api_key_exits_1_and_writes_no_artifact(tmp_path, monkeypatch):
    _seed_and_chunk(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("COMPARE_EXTRACTION_STUB", raising=False)

    rc = dl_main(
        ["compare-extraction", "--lake", str(tmp_path),
         "--meeting-id", MEETING_ID]
    )

    assert rc == 1
    # Fail-closed is exit-code AND on-disk: no comparison artifact.
    assert _comparison_files(tmp_path) == []


def test_empty_api_key_also_fails_closed(tmp_path, monkeypatch):
    _seed_and_chunk(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    monkeypatch.delenv("COMPARE_EXTRACTION_STUB", raising=False)
    out = io.StringIO()

    rc = run_compare_extraction(
        lake_root=tmp_path, meeting_id=MEETING_ID,
        env={"ANTHROPIC_API_KEY": "   "}, stream=out,
    )

    assert rc == 1
    assert "missing_credentials:ANTHROPIC_API_KEY" in out.getvalue()
    assert _comparison_files(tmp_path) == []


def test_source_record_missing_fails_before_any_api_call(tmp_path):
    # Seed raw inputs only — do NOT run the pipeline, so no
    # source_record exists.
    raw = tmp_path / "raw" / "meetings" / MEETING_ID
    raw.mkdir(parents=True)
    shutil.copy(FIXTURE / "transcript.txt", raw / "transcript.txt")
    (raw / "metadata.json").write_text(
        json.dumps(
            {"meeting_id": MEETING_ID, "title": "t", "date": "2026-05-16",
             "source_type": "transcript"}
        ),
        encoding="utf-8",
    )
    out = io.StringIO()

    def _must_not_run(_):  # pragma: no cover - asserts it never runs
        raise AssertionError("extractor ran despite missing source_record")

    rc = run_compare_extraction(
        lake_root=tmp_path,
        meeting_id=MEETING_ID,
        env={"ANTHROPIC_API_KEY": "sk-ant-real"},
        haiku_extract=_must_not_run,
        opus_extract=_must_not_run,
        stream=out,
    )

    assert rc == 1
    assert "source_record_missing" in out.getvalue()
    assert _comparison_files(tmp_path) == []


def test_stub_mode_all_three_ok(tmp_path, monkeypatch):
    _seed_and_chunk(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("COMPARE_EXTRACTION_STUB", "1")

    rc = dl_main(
        ["compare-extraction", "--lake", str(tmp_path),
         "--meeting-id", MEETING_ID]
    )

    assert rc == 0
    files = _comparison_files(tmp_path)
    assert len(files) == 1
    art = json.loads(files[0].read_text(encoding="utf-8"))
    assert art["status"] == "promoted"
    assert art["payload"]["extractor_status"] == {
        "regex": "ok", "haiku": "ok", "opus": "ok",
    }
    # Sibling artifacts + markdown report all present.
    meeting_dir = tmp_path / "processed" / "meetings" / MEETING_ID
    assert list(meeting_dir.glob("extraction_telemetry__*.json"))
    assert list(meeting_dir.glob("extraction_unconstrained__*.json"))
    assert (meeting_dir / "markdown" / "extraction_comparison.md").is_file()


def test_slugify_exact_contract_example():
    # The exact mapping pinned by the task contract.
    assert slugify(
        "7 GHz Downlink TIG Meeting Kickoff - transcript 20251218"
    ) == "7-ghz-downlink-tig-meeting-kickoff-transcript-20251218"
    # Idempotent, and edges/runs collapse to a single hyphen.
    assert slugify("--A  B__C!!") == "a-b-c"
    assert slugify("already-slugged") == "already-slugged"


def _flat_transcript_file(tmp_path: Path) -> Path:
    """Write the gold transcript to a flat file whose stem has spaces
    and mixed case so slugify is exercised end-to-end."""
    p = tmp_path / "7 GHz Downlink TIG Meeting Kickoff - transcript 20251218.txt"
    p.write_text(
        (FIXTURE / "transcript.txt").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return p


def test_transcript_file_stub_mode_writes_under_derived_meeting_id(
    tmp_path, monkeypatch
):
    tf = _flat_transcript_file(tmp_path)
    lake = tmp_path / "lake"
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("COMPARE_EXTRACTION_STUB", "1")

    rc = dl_main(
        ["compare-extraction", "--lake", str(lake),
         "--transcript-file", str(tf)]
    )

    assert rc == 0
    derived = "7-ghz-downlink-tig-meeting-kickoff-transcript-20251218"
    meeting_dir = lake / "processed" / "meetings" / derived
    comp = list(meeting_dir.glob(f"{COMPARISON_TYPE}__*.json"))
    assert len(comp) == 1
    art = json.loads(comp[0].read_text(encoding="utf-8"))
    assert art["status"] == "promoted"
    assert art["payload"]["meeting_id"] == derived
    assert art["payload"]["extractor_status"] == {
        "regex": "ok", "haiku": "ok", "opus": "ok",
    }
    # No source_record was required: only the flat file existed.
    assert not (lake / "raw").exists()
    assert list(meeting_dir.glob("extraction_telemetry__*.json"))
    assert list(meeting_dir.glob("extraction_unconstrained__*.json"))
    assert (meeting_dir / "markdown" / "extraction_comparison.md").is_file()


def test_both_selectors_exit_1(tmp_path, monkeypatch):
    tf = _flat_transcript_file(tmp_path)
    monkeypatch.setenv("COMPARE_EXTRACTION_STUB", "1")
    out = io.StringIO()

    rc = run_compare_extraction(
        lake_root=tmp_path / "lake",
        meeting_id="some-id",
        transcript_file=tf,
        env={"COMPARE_EXTRACTION_STUB": "1"},
        stream=out,
    )

    assert rc == 1
    assert "source_selector_invalid" in out.getvalue()


def test_neither_selector_exits_1(tmp_path):
    out = io.StringIO()

    rc = run_compare_extraction(
        lake_root=tmp_path / "lake",
        env={"COMPARE_EXTRACTION_STUB": "1"},
        stream=out,
    )

    assert rc == 1
    assert "source_selector_invalid" in out.getvalue()


def test_transcript_file_not_found_exits_1(tmp_path):
    out = io.StringIO()

    rc = run_compare_extraction(
        lake_root=tmp_path / "lake",
        transcript_file=tmp_path / "does-not-exist.txt",
        env={"COMPARE_EXTRACTION_STUB": "1"},
        stream=out,
    )

    assert rc == 1
    assert "transcript_file_not_found" in out.getvalue()


def test_transcript_file_empty_exits_1(tmp_path):
    empty = tmp_path / "Empty Transcript.txt"
    empty.write_text("   \n\t\n", encoding="utf-8")
    out = io.StringIO()

    rc = run_compare_extraction(
        lake_root=tmp_path / "lake",
        transcript_file=empty,
        env={"COMPARE_EXTRACTION_STUB": "1"},
        stream=out,
    )

    assert rc == 1
    assert "transcript_file_empty" in out.getvalue()


def test_transcript_file_unslugifiable_stem_exits_1(tmp_path):
    # A stem with no [a-z0-9] slugifies to "" → invalid meeting_id.
    bad = tmp_path / "!!! @@@.txt"
    bad.write_text("Decisions:\n- something\n", encoding="utf-8")
    out = io.StringIO()

    rc = run_compare_extraction(
        lake_root=tmp_path / "lake",
        transcript_file=bad,
        env={"COMPARE_EXTRACTION_STUB": "1"},
        stream=out,
    )

    assert rc == 1
    assert "meeting_id_from_filename_invalid" in out.getvalue()


def test_transcript_file_missing_api_key_fails_closed(tmp_path, monkeypatch):
    tf = _flat_transcript_file(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("COMPARE_EXTRACTION_STUB", raising=False)
    out = io.StringIO()

    def _must_not_run(_):  # pragma: no cover - asserts it never runs
        raise AssertionError("extractor ran despite missing credentials")

    rc = run_compare_extraction(
        lake_root=tmp_path / "lake",
        transcript_file=tf,
        env={},
        haiku_extract=_must_not_run,
        opus_extract=_must_not_run,
        stream=out,
    )

    assert rc == 1
    assert "missing_credentials:ANTHROPIC_API_KEY" in out.getvalue()
    # Fail-closed on disk: nothing written for the flat-file path.
    assert not (tmp_path / "lake").exists()


def test_forced_extractor_failure_writes_rejected_and_exits_1(tmp_path):
    _seed_and_chunk(tmp_path)

    def _failing_haiku(_):
        raise RuntimeError("haiku_output_not_json:boom")

    from spectrum_systems_core.extraction.comparison_runner import (
        _stub_opus_extract,
    )

    out = io.StringIO()
    rc = run_compare_extraction(
        lake_root=tmp_path,
        meeting_id=MEETING_ID,
        env={"COMPARE_EXTRACTION_STUB": "1"},
        haiku_extract=_failing_haiku,
        opus_extract=_stub_opus_extract,
        stream=out,
    )

    assert rc == 1
    files = _comparison_files(tmp_path)
    assert len(files) == 1
    art = json.loads(files[0].read_text(encoding="utf-8"))
    assert art["status"] == "rejected"
    st = art["payload"]["extractor_status"]
    assert st["regex"] == "ok"
    assert st["haiku"].startswith("failed:haiku_output_not_json")
    assert st["opus"] == "ok"
