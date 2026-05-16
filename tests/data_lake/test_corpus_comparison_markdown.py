"""Phase AC.3 — corpus comparison Markdown view tests."""
from __future__ import annotations

import copy

from spectrum_systems_core.data_lake.markdown_views import (
    CORPUS_COMPARISON_MD_FILENAME,
    render_corpus_comparison_markdown,
    write_corpus_comparison_markdown,
)

_PARTIAL_ITEM = {
    "extracted_text": "approved the broad framework thing",
    "best_gold_text": "approved the 3.45 GHz framework",
    "lcs": 0.55,
}


def _payload(status: str = "degraded") -> dict:
    return {
        "schema_version": "1.0.0",
        "corpus_id": "corpus-abc1234567890def",
        "transcripts_dir": "/lake/raw/transcripts",
        "meeting_ids": ["m_alpha", "m_beta"],
        "discovery_findings": ["skipped_non_txt:minutes.docx"],
        "per_meeting": {
            "m_alpha": {
                "transcript_file": "/lake/raw/transcripts/m_alpha.txt",
                "comparison_artifact_id": "deadbeef",
                "extractor_status": {"haiku": "ok", "opus": "ok"},
                "gold_present": True,
                "per_entity_f1": {
                    "decisions": {"haiku": 0.8, "opus": 0.9},
                    "actions": {"haiku": 0.6, "opus": 0.7},
                    "questions": {"haiku": 0.5, "opus": 0.55},
                },
                "per_entity_metrics": {
                    "haiku": {
                        "decisions": {"partial_items": [_PARTIAL_ITEM]},
                        "actions": {"partial_items": []},
                        "questions": {"partial_items": []},
                    },
                    "opus": {
                        "decisions": {"partial_items": []},
                        "actions": {"partial_items": []},
                        "questions": {"partial_items": []},
                    },
                },
                "findings": [],
            },
            "m_beta": {
                "transcript_file": "/lake/raw/transcripts/m_beta.txt",
                "comparison_artifact_id": None,
                "extractor_status": {
                    "haiku": "failed:haiku_boom|retry: spectrum-core "
                    "compare-extraction --lake /lake --transcript-file "
                    "/lake/raw/transcripts/m_beta.txt",
                    "opus": "ok",
                },
                "gold_present": False,
                "per_entity_f1": None,
                "per_entity_metrics": None,
                "findings": ["no_gold_set"],
            },
        },
        "aggregate": {
            "per_entity_f1": {
                "decisions": {"haiku": 0.8, "opus": 0.9},
                "actions": {"haiku": 0.6, "opus": 0.7},
                "questions": {"haiku": 0.5, "opus": 0.55},
            },
            "per_entity_f1_n_averaged": {
                "decisions": {"haiku": 1, "opus": 1},
                "actions": {"haiku": 1, "opus": 1},
                "questions": {"haiku": 1, "opus": 1},
            },
            "total_cost_usd": {"haiku": 0.0031, "opus": 0.241},
            "total_latency_ms": {"haiku": 8120, "opus": 40960},
            "meetings_processed": 1,
            "meetings_failed": 1,
        },
        "corpus_status": status,
    }


def test_markdown_has_all_three_sections():
    md = render_corpus_comparison_markdown(corpus_payload=_payload())
    assert "## Aggregate per-entity F1" in md
    assert "## Cost & latency" in md
    assert "## Per-meeting breakdown" in md
    assert md.startswith("# Corpus Comparison — corpus-abc1234567890def")


def test_n_averaged_surfaced_in_every_f1_cell():
    md = render_corpus_comparison_markdown(corpus_payload=_payload())
    # Every aggregate F1 cell shows how many meetings fed the mean so a
    # reader cannot mistake a 1-meeting mean for a corpus average.
    assert "0.8000 (n=1)" in md
    assert "0.9000 (n=1)" in md
    # Real telemetry surfaced (latency converted to seconds).
    assert "$0.241000" in md
    assert "40.96" in md


def test_zero_n_renders_na_not_zero():
    p = _payload("rejected")
    for cat in ("decisions", "actions", "questions"):
        p["aggregate"]["per_entity_f1_n_averaged"][cat] = {
            "haiku": 0,
            "opus": 0,
        }
    md = render_corpus_comparison_markdown(corpus_payload=p)
    # n=0 means "no gold-backed successful meeting", NOT "0.0 bad".
    assert "n/a (n=0)" in md
    assert "(n=0)" in md


def test_degraded_status_has_explicit_warning_banner():
    md = render_corpus_comparison_markdown(corpus_payload=_payload("degraded"))
    assert "**DEGRADED**" in md
    # Warning is at the TOP (before the first data section).
    assert md.index("**DEGRADED**") < md.index("## Aggregate per-entity F1")


def test_rejected_status_has_explicit_warning_banner():
    md = render_corpus_comparison_markdown(corpus_payload=_payload("rejected"))
    assert "**REJECTED**" in md
    assert md.index("**REJECTED**") < md.index("## Aggregate per-entity F1")
    assert "NOT" in md  # "NOT trustworthy"


def test_complete_status_has_no_warning_banner():
    md = render_corpus_comparison_markdown(corpus_payload=_payload("complete"))
    assert "**DEGRADED**" not in md
    assert "**REJECTED**" not in md


def test_partial_items_rendered_as_separate_section():
    md = render_corpus_comparison_markdown(corpus_payload=_payload())
    assert "## Partial matches (diagnostic)" in md
    # The partial item itself (not just a count) reaches the reader.
    assert "approved the broad framework thing" in md
    assert "approved the 3.45 GHz framework" in md
    assert "0.55" in md
    # Explicit note that partials are excluded from F1.
    assert "EXCLUDED from F1" in md


def test_no_partial_section_when_no_partials():
    p = _payload("complete")
    p["per_meeting"]["m_alpha"]["per_entity_metrics"]["haiku"][
        "decisions"
    ]["partial_items"] = []
    md = render_corpus_comparison_markdown(corpus_payload=p)
    assert "## Partial matches (diagnostic)" not in md


def test_skipped_inputs_surfaced():
    md = render_corpus_comparison_markdown(corpus_payload=_payload())
    assert "## Skipped inputs" in md
    assert "skipped_non_txt:minutes.docx" in md


def test_render_is_deterministic_byte_identical():
    p = _payload()
    a = render_corpus_comparison_markdown(corpus_payload=p)
    b = render_corpus_comparison_markdown(corpus_payload=p)
    assert a == b
    assert a.endswith("\n")


def test_render_does_not_mutate_payload():
    p = _payload()
    snapshot = copy.deepcopy(p)
    render_corpus_comparison_markdown(corpus_payload=p)
    assert p == snapshot  # the view never edits the canonical artifact


def test_delete_then_rerender_is_byte_identical(tmp_path):
    p = _payload()
    p1 = write_corpus_comparison_markdown(
        tmp_path, corpus_id=p["corpus_id"], corpus_payload=p
    )
    assert p1.name == CORPUS_COMPARISON_MD_FILENAME
    first = p1.read_bytes()

    # Deleting the view loses nothing — the JSON artifact is canonical.
    p1.unlink()
    assert not p1.exists()

    p2 = write_corpus_comparison_markdown(
        tmp_path, corpus_id=p["corpus_id"], corpus_payload=p
    )
    assert p2.read_bytes() == first
