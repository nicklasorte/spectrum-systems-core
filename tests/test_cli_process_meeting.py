"""CLI tests: `spectrum-core process-meeting` end-to-end."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from spectrum_systems_core.data_lake import (
    DEFAULT_WORKFLOWS,
    INDEX_FILENAME,
    artifact_markdown_filename,
    cli_main,
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


# --- core orchestration ----------------------------------------------------


def test_process_meeting_runs_all_default_workflows(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)

    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    assert [r.workflow_name for r in result.pipeline_results] == list(DEFAULT_WORKFLOWS)
    # The 'good' golden has DECISION/ACTION/QUESTION lines but no
    # CONTEXT/OPTION/RECOMMENDATION/RATIONALE lines, so decision_brief
    # is blocked by transcript_evidence. The other three promote.
    assert "meeting_minutes" in result.promoted_workflows
    assert "meeting_action_log" in result.promoted_workflows
    assert "agency_question_summary" in result.promoted_workflows


def test_process_meeting_writes_markdown_for_each_promoted_artifact(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)

    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    md_dir = markdown_dir(tmp_path, meeting_id)
    for artifact_type in result.promoted_workflows:
        path = md_dir / artifact_markdown_filename(artifact_type)
        assert path.is_file(), f"missing markdown for {artifact_type}"


def test_process_meeting_writes_index_markdown(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)

    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    assert result.index_path == markdown_dir(tmp_path, meeting_id) / INDEX_FILENAME
    assert result.index_path.is_file()


# --- frontmatter ----------------------------------------------------------


def _frontmatter_block(text: str) -> dict[str, str]:
    assert text.startswith("---\n"), "markdown must begin with YAML frontmatter"
    end = text.find("\n---\n", 4)
    assert end != -1, "markdown frontmatter terminator missing"
    block = text[4:end]
    out: dict[str, str] = {}
    for line in block.splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def test_artifact_markdown_has_required_frontmatter(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    md_path = (
        markdown_dir(tmp_path, meeting_id)
        / artifact_markdown_filename("meeting_minutes")
    )
    text = md_path.read_text(encoding="utf-8")
    fm = _frontmatter_block(text)

    for key in ("artifact_type", "meeting_id", "date", "title", "status", "trace_id"):
        assert key in fm, f"frontmatter missing {key}"
    assert fm["artifact_type"] == "meeting_minutes"
    assert fm["meeting_id"] == meeting_id
    assert fm["status"] == "promoted"
    assert fm["trace_id"].startswith("trace-")


def test_index_markdown_has_required_frontmatter(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    text = (markdown_dir(tmp_path, meeting_id) / INDEX_FILENAME).read_text(
        encoding="utf-8"
    )
    fm = _frontmatter_block(text)
    for key in ("artifact_type", "meeting_id", "date", "title", "status", "trace_id"):
        assert key in fm
    assert fm["artifact_type"] == "meeting_index"
    assert fm["meeting_id"] == meeting_id


# --- index linkage --------------------------------------------------------


def test_index_links_to_each_promoted_artifact_markdown(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)

    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    index_text = result.index_path.read_text(encoding="utf-8")

    for artifact_type in result.promoted_workflows:
        link = artifact_markdown_filename(artifact_type)
        assert f"[{artifact_type}]({link})" in index_text


def test_index_lists_blocked_workflows_with_reasons(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)

    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    index_text = result.index_path.read_text(encoding="utf-8")

    # decision_brief is blocked on this fixture (no CONTEXT/OPTION/...)
    assert "decision_brief" in result.blocked_workflows
    assert "decision_brief" in index_text


def test_index_explains_block_reasons_in_plain_english(tmp_path):
    """Fix M2: a non-engineer reader sees what the reason code means."""
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)

    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    index_text = result.index_path.read_text(encoding="utf-8")

    # decision_brief is blocked because the transcript has no
    # CONTEXT/OPTION/... lines for it. Index renders that in plain English.
    assert "no signal for this artifact type" in index_text


def test_index_trace_id_is_meaningful_meeting_token(tmp_path):
    """Fix S1: the index frontmatter trace_id is not empty and is meeting-keyed."""
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)

    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    text = (markdown_dir(tmp_path, meeting_id) / INDEX_FILENAME).read_text(
        encoding="utf-8"
    )
    fm = _frontmatter_block(text)
    assert fm["trace_id"] == f"meeting-{meeting_id}"
    assert fm["trace_id"] != ""


# --- JSON is the source of truth -----------------------------------------


def test_promoted_json_artifact_unchanged_by_markdown_step(tmp_path):
    """Markdown rendering must not modify the promoted JSON bytes."""
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)

    # First run: capture JSON bytes from the writer.
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    processed_dir = tmp_path / "processed" / "meetings" / meeting_id
    json_paths = sorted(
        p for p in processed_dir.glob("*.json")
        if not p.name.startswith(("manifest__", "debug__"))
    )
    assert json_paths, "expected at least one promoted JSON artifact"
    before = {p.name: p.read_bytes() for p in json_paths}

    # Wipe markdown but keep JSON; run again; JSON bytes must be byte-identical.
    md = markdown_dir(tmp_path, meeting_id)
    if md.exists():
        shutil.rmtree(md)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    after = {p.name: p.read_bytes() for p in json_paths}
    assert before == after, "promoted JSON bytes changed across runs"


def test_promoted_json_files_are_not_under_markdown_dir(tmp_path):
    """JSON artifacts live in the canonical processed dir, not under markdown/."""
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    md = markdown_dir(tmp_path, meeting_id)
    for child in md.iterdir():
        assert child.suffix == ".md", (
            f"unexpected non-markdown file in markdown dir: {child}"
        )


# --- CLI entry point ------------------------------------------------------


def test_cli_main_processes_meeting(tmp_path, capsys):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)

    rc = cli_main(
        [
            "process-meeting",
            "--lake",
            str(tmp_path),
            "--meeting-id",
            meeting_id,
        ]
    )
    assert rc == 0

    out = capsys.readouterr().out
    assert meeting_id in out
    assert "meeting_minutes" in out

    md = markdown_dir(tmp_path, meeting_id)
    assert (md / INDEX_FILENAME).is_file()
    assert (md / artifact_markdown_filename("meeting_minutes")).is_file()


def test_cli_main_supports_workflow_filter(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)

    rc = cli_main(
        [
            "process-meeting",
            "--lake",
            str(tmp_path),
            "--meeting-id",
            meeting_id,
            "--workflow",
            "meeting_minutes",
        ]
    )
    assert rc == 0

    md = markdown_dir(tmp_path, meeting_id)
    assert (md / artifact_markdown_filename("meeting_minutes")).is_file()
    # When restricted, the other workflows should not have produced markdown.
    assert not (md / artifact_markdown_filename("meeting_action_log")).exists()
    assert not (md / artifact_markdown_filename("decision_brief")).exists()


def test_cli_requires_lake_and_meeting_id(tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli_main(["process-meeting"])


# --- Determinism ---------------------------------------------------------


def test_markdown_is_deterministic_across_runs(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)

    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    md = markdown_dir(tmp_path, meeting_id)
    snapshot1 = {p.name: p.read_bytes() for p in sorted(md.iterdir())}

    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    snapshot2 = {p.name: p.read_bytes() for p in sorted(md.iterdir())}

    assert snapshot1 == snapshot2
