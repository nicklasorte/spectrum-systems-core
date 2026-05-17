"""Regression tests for must_fix and should_fix items in
docs/reviews/ssc_next_memory_redteam_3.md."""
from __future__ import annotations

import shutil
from pathlib import Path

from spectrum_systems_core.data_lake import (
    INDEX_FILENAME,
    RUNS_SUBDIR,
    markdown_dir,
    process_meeting,
)

FIXTURES = Path(__file__).parent / "fixtures" / "golden_meetings"


def _seed(lake_root: Path, meeting_id: str) -> None:
    src = FIXTURES / meeting_id
    dst = lake_root / "raw" / "meetings" / meeting_id
    dst.mkdir(parents=True)
    shutil.copy(src / "transcript.txt", dst / "transcript.txt")
    shutil.copy(src / "metadata.json", dst / "metadata.json")


def _frontmatter_artifact_type(text: str) -> str:
    assert text.startswith("---\n")
    end = text.find("\n---\n", 4)
    block = text[4:end]
    for line in block.splitlines():
        if line.startswith("artifact_type:"):
            return line.partition(":")[2].strip()
    raise AssertionError("frontmatter has no artifact_type")


def test_M7_contract_pinned_artifact_type_tokens_are_emitted(tmp_path):
    """Every Markdown view kind named in the contract §6.3 table must
    actually appear in the generated layout for a meeting that has
    every relevant input."""
    meeting_id = "m-golden-inquiry"  # has agency: FCC, topic: 3.5 GHz
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    md = markdown_dir(tmp_path, meeting_id)
    found: set[str] = set()
    for p in sorted(md.rglob("*.md")):
        found.add(_frontmatter_artifact_type(p.read_text(encoding="utf-8")))

    expected_view_tokens = {
        "meeting_index",
        "agency_note",
        "topic_note",
        "run_note",
    }
    expected_artifact_tokens_at_least_one = {
        "meeting_minutes",
        "agency_question_summary",
        "meeting_action_log",
    }
    missing_view = expected_view_tokens - found
    assert not missing_view, f"missing view tokens: {missing_view}"
    assert expected_artifact_tokens_at_least_one & found, (
        f"expected at least one promoted artifact token, found {found}"
    )


def test_M7_index_status_is_view_not_promoted(tmp_path):
    """The index is a view, not a product. Frontmatter must say so."""
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    text = (markdown_dir(tmp_path, meeting_id) / INDEX_FILENAME).read_text(
        encoding="utf-8"
    )
    block = text.split("\n---\n", 2)[0]
    assert "status: view" in block
    assert "canonical: false" in block


def test_M7_runs_md_carries_run_note_and_decision_field(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    runs_dir = markdown_dir(tmp_path, meeting_id) / RUNS_SUBDIR
    for r in result.pipeline_results:
        text = (runs_dir / f"{r.run_id}.md").read_text(encoding="utf-8")
        block = text.split("\n---\n", 2)[0]
        assert "artifact_type: run_note" in block
        assert "decision:" in block
        assert "workflow_name:" in block
