"""Phase AB.5 — extraction comparison Markdown view tests."""
from __future__ import annotations

from spectrum_systems_core.data_lake.markdown_views import (
    EXTRACTION_COMPARISON_MD_FILENAME,
    render_extraction_comparison_markdown,
    write_extraction_comparison_markdown,
)

MEETING_ID = "meeting_real_001"

_COMPARISON = {
    "meeting_id": MEETING_ID,
    "transcript_artifact_id": "deadbeef",
    "extractor_status": {"regex": "ok", "haiku": "ok", "opus": "ok"},
    "regex_output": {
        "decisions": [{"text": "approved the framework"}],
        "actions": [],
        "questions": [],
    },
    "haiku_output": {
        "decisions": [
            {"text": "approved the framework", "verb": "approved"},
            {"text": "deferred the review"},
        ],
        "actions": [{"text": "carol drafts response", "owner": "Carol"}],
        "questions": [{"text": "do we need an eval?"}],
    },
    "opus_output_ref": "ext-abc123",
}
_TELEMETRY = {
    "meeting_id": MEETING_ID,
    "comparison_artifact_id": "cmp-abc",
    "regex": {"cost_usd": 0.0, "latency_ms": 0},
    "haiku": {"cost_usd": 0.00031, "latency_ms": 812, "model": "haiku"},
    "opus": {"cost_usd": 0.0241, "latency_ms": 4096, "model": "opus"},
}
_OPUS_RAW = "Decisions:\n- approved the framework\n- deferred the review\n"


def test_markdown_has_all_three_extractor_columns():
    md = render_extraction_comparison_markdown(
        comparison_payload=_COMPARISON,
        telemetry_payload=_TELEMETRY,
        opus_raw_text=_OPUS_RAW,
    )
    # Cost table carries all three rows.
    assert "| Regex" in md and "| Haiku" in md and "| Opus" in md
    # Each category renders a Regex / Haiku / Opus (raw) subsection.
    assert md.count("### Regex") == 3
    assert md.count("### Haiku") == 3
    assert md.count("### Opus (raw)") == 3
    # Real telemetry surfaced.
    assert "$0.0241" in md
    assert "4096" in md
    # Opus raw text is quoted verbatim in a fenced block.
    assert "```text" in md
    assert "approved the framework" in md
    # No gold set associated → explicit not-computed note (no NaN).
    assert "Not computed" in md


def test_render_is_deterministic_byte_identical():
    a = render_extraction_comparison_markdown(
        comparison_payload=_COMPARISON,
        telemetry_payload=_TELEMETRY,
        opus_raw_text=_OPUS_RAW,
    )
    b = render_extraction_comparison_markdown(
        comparison_payload=_COMPARISON,
        telemetry_payload=_TELEMETRY,
        opus_raw_text=_OPUS_RAW,
    )
    assert a == b
    assert a.endswith("\n")


def test_delete_then_rerender_is_byte_identical(tmp_path):
    p1 = write_extraction_comparison_markdown(
        tmp_path,
        meeting_id=MEETING_ID,
        comparison_payload=_COMPARISON,
        telemetry_payload=_TELEMETRY,
        opus_raw_text=_OPUS_RAW,
    )
    assert p1.name == EXTRACTION_COMPARISON_MD_FILENAME
    first = p1.read_bytes()

    # Deleting the view loses nothing — the artifact is canonical.
    p1.unlink()
    assert not p1.exists()

    p2 = write_extraction_comparison_markdown(
        tmp_path,
        meeting_id=MEETING_ID,
        comparison_payload=_COMPARISON,
        telemetry_payload=_TELEMETRY,
        opus_raw_text=_OPUS_RAW,
    )
    assert p2.read_bytes() == first


def test_gap_metrics_block_rendered_when_supplied():
    gap = {
        "regex": {"f1": 0.25},
        "haiku": {"f1": 0.8},
        "opus": {"f1": 0.9},
        "gap_1_to_2_f1": 0.55,
        "gap_2_to_3_f1": 0.1,
        "gold_item_count": 13,
        "opus_parser_warnings": ["opus_section_prose_fallback:actions"],
    }
    md = render_extraction_comparison_markdown(
        comparison_payload=_COMPARISON,
        telemetry_payload=_TELEMETRY,
        opus_raw_text=_OPUS_RAW,
        gap_metrics=gap,
    )
    assert "Gap 1→2 (regex → Haiku): 0.55" in md
    assert "Gap 2→3 (Haiku → Opus):  0.1" in md
    assert "Gold items: 13" in md
    assert "opus_section_prose_fallback:actions" in md
    assert "Not computed" not in md
