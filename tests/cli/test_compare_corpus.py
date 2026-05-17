"""Phase AC.2 — compare-corpus CLI / runner tests.

No real API call: the missing-key test gates before any client; every
other test runs in stub mode (``COMPARE_EXTRACTION_STUB=1``) with
deterministic in-process extractors. Each corpus-status test asserts
BOTH the status string AND the count of per_meeting entries with a
failed extractor (status alone is not fail-closed — red-team Pass 2
item 2).
"""
from __future__ import annotations

import glob
import io
import json
from pathlib import Path

from spectrum_systems_core.data_lake.cli import main as dl_main
from spectrum_systems_core.extraction import llm_haiku
from spectrum_systems_core.extraction.comparison_runner import (
    _stub_opus_extract,
)
from spectrum_systems_core.extraction.corpus_runner import run_compare_corpus

FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures" / "comparison_gold" / "meeting_real_001"
)
_GOLD = json.loads(
    (FIXTURE / "independent_gold.json").read_text(encoding="utf-8")
)


def _corpus_files(lake: Path) -> list[str]:
    return glob.glob(
        str(lake / "processed" / "corpus" / "*" / "corpus_comparison__*.json")
    )


def _load_corpus(lake: Path) -> dict:
    files = _corpus_files(lake)
    assert len(files) == 1, files
    return json.loads(Path(files[0]).read_text(encoding="utf-8"))


def _write_transcript(d: Path, name: str, body: str, gold: bool) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.txt").write_text(body, encoding="utf-8")
    if gold:
        (d / "independent_gold.json").write_text(
            json.dumps(_GOLD), encoding="utf-8"
        )


def _failed_count(corpus: dict) -> int:
    pm = corpus["payload"]["per_meeting"]
    return sum(
        1
        for m in pm.values()
        if m["extractor_status"].get("haiku") != "ok"
        or m["extractor_status"].get("opus") != "ok"
    )


# ------------------------------------------------------------- gates

def test_empty_transcripts_dir_exit_1_no_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPARE_EXTRACTION_STUB", "1")
    tdir = tmp_path / "empty"
    tdir.mkdir()
    lake = tmp_path / "lake"
    out = io.StringIO()

    rc = run_compare_corpus(
        lake_root=lake,
        transcripts_dir=tdir,
        env={"COMPARE_EXTRACTION_STUB": "1"},
        stream=out,
    )

    assert rc == 1
    assert "empty_transcripts_dir" in out.getvalue()
    assert _corpus_files(lake) == []
    assert not lake.exists()


def test_only_non_txt_files_is_empty_dir_with_skip_finding(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("COMPARE_EXTRACTION_STUB", "1")
    tdir = tmp_path / "docx_only"
    tdir.mkdir()
    (tdir / "minutes.docx").write_text("not a transcript", encoding="utf-8")
    lake = tmp_path / "lake"
    out = io.StringIO()

    rc = run_compare_corpus(
        lake_root=lake,
        transcripts_dir=tdir,
        env={"COMPARE_EXTRACTION_STUB": "1"},
        stream=out,
    )

    assert rc == 1
    body = out.getvalue()
    assert "empty_transcripts_dir" in body
    assert "skipped_non_txt:minutes.docx" in body
    assert _corpus_files(lake) == []


def test_missing_api_key_exit_1_no_artifact(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("COMPARE_EXTRACTION_STUB", raising=False)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "alpha", "DECISION: approved x\n", gold=False)
    lake = tmp_path / "lake"

    rc = dl_main(
        ["compare-corpus", "--lake", str(lake), "--transcripts", str(tdir)]
    )

    assert rc == 1
    assert _corpus_files(lake) == []
    assert not lake.exists()


def test_transcripts_dir_not_found_exit_1(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPARE_EXTRACTION_STUB", "1")
    lake = tmp_path / "lake"
    out = io.StringIO()
    rc = run_compare_corpus(
        lake_root=lake,
        transcripts_dir=tmp_path / "nope",
        env={"COMPARE_EXTRACTION_STUB": "1"},
        stream=out,
    )
    assert rc == 1
    assert "transcripts_dir_not_found" in out.getvalue()
    assert _corpus_files(lake) == []


# ------------------------------------------------------ status: complete

def test_stub_three_transcripts_status_complete(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPARE_EXTRACTION_STUB", "1")
    tdir = tmp_path / "tx"
    for n in ("alpha_meeting", "beta_meeting", "gamma_meeting"):
        _write_transcript(
            tdir, n, f"DECISION: approved {n}\nACTION: do {n}\n", gold=False
        )
    lake = tmp_path / "lake"

    rc = dl_main(
        ["compare-corpus", "--lake", str(lake), "--transcripts", str(tdir)]
    )

    assert rc == 0
    corpus = _load_corpus(lake)
    assert corpus["payload"]["corpus_status"] == "complete"
    assert len(corpus["payload"]["per_meeting"]) == 3
    assert _failed_count(corpus) == 0
    assert corpus["payload"]["aggregate"]["meetings_processed"] == 3
    assert corpus["payload"]["aggregate"]["meetings_failed"] == 0
    assert corpus["status"] == "promoted"
    # Markdown projection written alongside the JSON.
    md = glob.glob(
        str(lake / "processed" / "corpus" / "*" / "markdown"
            / "corpus_comparison.md")
    )
    assert len(md) == 1


# ------------------------------------------------------ status: degraded

def test_stub_one_haiku_failure_status_degraded_excludes_failed_from_mean(
    tmp_path,
):
    """3 gold-backed meetings; Haiku fails for exactly one. Corpus is
    DEGRADED, the failed meeting is excluded from the Haiku F1 mean
    (n_averaged haiku == 2, opus == 3), and the failed-extractor count
    is exactly 1 (status string alone is not fail-closed)."""
    tdir = tmp_path / "tx"
    for n in ("aaa_ok", "bbb_ok", "ccc_FAILME"):
        # Unique file stem per meeting (the dir name is decorative; the
        # meeting_id is the slugified FILE stem) so no slug collision.
        _write_transcript(
            tdir / n, n,
            f"DECISION: approved {n}\nmarker {n}\n", gold=True,
        )
    lake = tmp_path / "lake"

    def _haiku(transcript: str):
        if "FAILME" in transcript:
            raise RuntimeError("haiku_boom:injected")
        return llm_haiku.stub_extract(transcript)

    out = io.StringIO()
    rc = run_compare_corpus(
        lake_root=lake,
        transcripts_dir=tdir,
        env={"COMPARE_EXTRACTION_STUB": "1"},
        haiku_extract=_haiku,
        opus_extract=_stub_opus_extract,
        stream=out,
    )

    assert rc == 0  # degraded still produces a usable instrument
    corpus = _load_corpus(lake)
    payload = corpus["payload"]
    assert payload["corpus_status"] == "degraded"
    # Status AND the failed-entry count (fail-closed assertion).
    assert _failed_count(corpus) == 1
    failed = [
        mid
        for mid, m in payload["per_meeting"].items()
        if m["extractor_status"]["haiku"] != "ok"
    ]
    assert len(failed) == 1
    # The failed status carries a copy-paste retry command.
    fstatus = payload["per_meeting"][failed[0]]["extractor_status"]["haiku"]
    assert fstatus.startswith("failed:")
    assert "|retry: spectrum-core compare-extraction" in fstatus

    # Aggregate F1 is the mean of SUCCESSFUL meetings only.
    n_avg = payload["aggregate"]["per_entity_f1_n_averaged"]
    for cat in ("decisions", "actions", "questions"):
        assert n_avg[cat]["haiku"] == 2  # the FAILME meeting excluded
        assert n_avg[cat]["opus"] == 3   # Opus succeeded everywhere
    assert payload["aggregate"]["meetings_failed"] == 1
    assert payload["aggregate"]["meetings_processed"] == 2

    # Value check: the aggregate Haiku F1 equals the mean of ONLY the
    # two successful meetings' Haiku F1 — the failed meeting's value is
    # NOT folded in. (Stub Haiku output is constant, so each successful
    # meeting's F1 is identical and the mean equals that value; the
    # n=2 above already proves the failed one was structurally
    # excluded.) Computed independently from per_meeting, not trusted
    # from the aggregate block.
    ok_mids = [
        mid
        for mid, m in payload["per_meeting"].items()
        if m["extractor_status"]["haiku"] == "ok"
    ]
    assert len(ok_mids) == 2
    for cat in ("decisions", "actions", "questions"):
        ok_f1s = [
            payload["per_meeting"][mid]["per_entity_f1"][cat]["haiku"]
            for mid in ok_mids
        ]
        expected = round(sum(ok_f1s) / len(ok_f1s), 4)
        assert payload["aggregate"]["per_entity_f1"][cat]["haiku"] == expected


def test_empty_transcript_marks_meeting_failed_and_degrades(tmp_path):
    tdir = tmp_path / "tx"
    _write_transcript(tdir / "good", "transcript", "DECISION: ok\n", True)
    (tdir / "blank").mkdir(parents=True)
    (tdir / "blank" / "transcript.txt").write_text("   \n\t\n", "utf-8")
    # Distinct stems so the two transcript.txt files do not slug-collide.
    (tdir / "good" / "transcript.txt").rename(
        tdir / "good" / "good_meeting.txt"
    )
    (tdir / "blank" / "transcript.txt").rename(
        tdir / "blank" / "blank_meeting.txt"
    )
    lake = tmp_path / "lake"
    out = io.StringIO()

    rc = run_compare_corpus(
        lake_root=lake,
        transcripts_dir=tdir,
        env={"COMPARE_EXTRACTION_STUB": "1"},
        stream=out,
    )

    assert rc == 0
    payload = _load_corpus(lake)["payload"]
    assert payload["corpus_status"] == "degraded"
    blank = payload["per_meeting"]["blank-meeting"]
    assert blank["extractor_status"]["haiku"].startswith(
        "failed:empty_transcript"
    )
    assert "empty_transcript" in blank["findings"]
    assert _failed_count(_load_corpus(lake)) == 1


# ------------------------------------------------------ status: rejected

def test_stub_three_of_four_fail_status_rejected(tmp_path):
    tdir = tmp_path / "tx"
    names = ["m1_FAILME", "m2_FAILME", "m3_FAILME", "m4_ok"]
    for n in names:
        _write_transcript(
            tdir / n, n,
            f"DECISION: approved {n}\nmarker {n}\n", gold=True,
        )
    lake = tmp_path / "lake"

    def _haiku(transcript: str):
        if "FAILME" in transcript:
            raise RuntimeError("haiku_boom:injected")
        return llm_haiku.stub_extract(transcript)

    out = io.StringIO()
    rc = run_compare_corpus(
        lake_root=lake,
        transcripts_dir=tdir,
        env={"COMPARE_EXTRACTION_STUB": "1"},
        haiku_extract=_haiku,
        opus_extract=_stub_opus_extract,
        stream=out,
    )

    assert rc == 1  # rejected → exit 1
    corpus = _load_corpus(lake)  # artifact STILL written to explain run
    payload = corpus["payload"]
    assert payload["corpus_status"] == "rejected"
    # Haiku succeeded for only 1/4 < 0.5 → rejected. Failed-entry count
    # is 3 (status alone is not fail-closed — red-team Pass 2 item 2).
    assert _failed_count(corpus) == 3
    assert corpus["status"] == "rejected"


def test_partial_item_persisted_in_corpus_artifact_and_markdown(tmp_path):
    """A real partial (0.4 ≤ LCS < 0.7) produced by the classifier must
    land in the on-disk corpus artifact as a partial_items ENTRY (not
    just a count) and be rendered in the Markdown — red-team Pass 2
    item 5. Haiku output and gold are crafted so difflib ratio is
    EXACTLY 0.5 (a partial)."""
    a = "m" * 50 + "a" * 50  # vs gold below → difflib ratio 0.5
    b = "m" * 50 + "b" * 50
    tdir = tmp_path / "tx"
    tdir.mkdir(parents=True)
    (tdir / "partial_meeting.txt").write_text("DECISION: x\n", "utf-8")
    (tdir / "independent_gold.json").write_text(
        json.dumps({"decisions": [{"text": b}], "actions": [],
                    "questions": []}),
        encoding="utf-8",
    )
    lake = tmp_path / "lake"

    def _haiku(_transcript: str):
        return llm_haiku.HaikuExtractionResult(
            output={"decisions": [{"text": a}], "actions": [],
                    "questions": []},
            raw_response="{}",
            cost_usd=0.0,
            latency_ms=0,
            model="stub",
        )

    rc = run_compare_corpus(
        lake_root=lake,
        transcripts_dir=tdir,
        env={"COMPARE_EXTRACTION_STUB": "1"},
        haiku_extract=_haiku,
        opus_extract=_stub_opus_extract,
        stream=io.StringIO(),
    )
    assert rc == 0
    corpus = _load_corpus(lake)
    pe = corpus["payload"]["per_meeting"]["partial-meeting"][
        "per_entity_metrics"
    ]["haiku"]["decisions"]
    assert pe["partial_match_count"] == 1
    assert pe["tp"] == 0  # partial is NOT a true positive
    assert len(pe["partial_items"]) == 1  # the LIST, not just the count
    assert pe["partial_items"][0]["extracted_text"] == a

    md = glob.glob(
        str(lake / "processed" / "corpus" / "*" / "markdown"
            / "corpus_comparison.md")
    )[0]
    body = Path(md).read_text(encoding="utf-8")
    assert "## Partial matches (diagnostic)" in body
    assert "EXCLUDED from F1" in body


def test_rerun_same_corpus_is_idempotent_single_artifact(tmp_path):
    tdir = tmp_path / "tx"
    for n in ("alpha_meeting", "beta_meeting"):
        _write_transcript(tdir, n, f"DECISION: approved {n}\n", gold=False)
    lake = tmp_path / "lake"
    common = dict(
        transcripts_dir=tdir,
        env={"COMPARE_EXTRACTION_STUB": "1"},
        stream=io.StringIO(),
    )
    assert run_compare_corpus(lake_root=lake, **common) == 0
    first = _corpus_files(lake)
    assert run_compare_corpus(lake_root=lake, **common) == 0
    # Deterministic corpus_id → same path → overwrite, never accumulate.
    assert _corpus_files(lake) == first
