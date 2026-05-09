"""CLI tests: `spectrum-core process-meeting` end-to-end."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from spectrum_systems_core.data_lake import (
    AGENCIES_SUBDIR,
    ARTIFACTS_SUBDIR,
    DEFAULT_WORKFLOWS,
    INDEX_FILENAME,
    RUNS_SUBDIR,
    TOPICS_SUBDIR,
    artifact_markdown_filename,
    artifact_markdown_path,
    artifacts_markdown_dir,
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
    # is blocked. It also has no AGENCY: line, so after SSC-023's
    # non-empty required-field hardening, agency_question_summary is also
    # blocked (empty agency, empty question). meeting_minutes and
    # meeting_action_log promote.
    assert "meeting_minutes" in result.promoted_workflows
    assert "meeting_action_log" in result.promoted_workflows
    assert "agency_question_summary" in result.blocked_workflows
    assert "decision_brief" in result.blocked_workflows


def test_process_meeting_writes_markdown_for_each_promoted_artifact(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)

    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    for artifact_type in result.promoted_workflows:
        path = artifact_markdown_path(tmp_path, meeting_id, artifact_type)
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

    md_path = artifact_markdown_path(tmp_path, meeting_id, "meeting_minutes")
    text = md_path.read_text(encoding="utf-8")
    fm = _frontmatter_block(text)

    for key in (
        "artifact_type",
        "artifact_id",
        "meeting_id",
        "date",
        "title",
        "status",
        "trace_id",
        "content_hash",
        "canonical_json_path",
    ):
        assert key in fm, f"frontmatter missing {key}"
    assert fm["artifact_type"] == "meeting_minutes"
    assert fm["meeting_id"] == meeting_id
    assert fm["status"] == "promoted"
    assert fm["trace_id"].startswith("trace-")
    # canonical_json_path is relative; the JSON file it names must exist.
    rel_json = fm["canonical_json_path"]
    if rel_json.startswith('"') and rel_json.endswith('"'):
        rel_json = rel_json[1:-1]
    json_path = (md_path.parent / rel_json).resolve()
    assert json_path.is_file(), (
        f"canonical_json_path {rel_json!r} from {md_path} does not exist"
    )


def test_index_markdown_has_required_frontmatter(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    text = (markdown_dir(tmp_path, meeting_id) / INDEX_FILENAME).read_text(
        encoding="utf-8"
    )
    fm = _frontmatter_block(text)
    for key in (
        "artifact_type",
        "meeting_id",
        "date",
        "title",
        "status",
        "trace_id",
        "canonical",
    ):
        assert key in fm
    assert fm["artifact_type"] == "meeting_index"
    assert fm["meeting_id"] == meeting_id
    # The index is a view, not canonical.
    assert fm["status"] == "view"
    assert fm["canonical"] == "false"


# --- index linkage --------------------------------------------------------


def test_index_links_to_each_promoted_artifact_markdown(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)

    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    index_text = result.index_path.read_text(encoding="utf-8")

    for artifact_type in result.promoted_workflows:
        link = f"{ARTIFACTS_SUBDIR}/{artifact_markdown_filename(artifact_type)}"
        assert f"({link})" in index_text


def test_index_lists_blocked_workflows_with_reasons(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)

    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    index_text = result.index_path.read_text(encoding="utf-8")

    # decision_brief is blocked on this fixture (no CONTEXT/OPTION/...)
    assert "decision_brief" in result.blocked_workflows
    assert "decision_brief" in index_text


def test_index_explains_block_reasons_in_plain_english(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)

    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    index_text = result.index_path.read_text(encoding="utf-8")

    # decision_brief is blocked because the transcript has no
    # CONTEXT/OPTION/... lines for it. Index renders that in plain English.
    assert "no signal for this artifact type" in index_text


def test_index_trace_id_is_meaningful_meeting_token(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)

    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    text = (markdown_dir(tmp_path, meeting_id) / INDEX_FILENAME).read_text(
        encoding="utf-8"
    )
    fm = _frontmatter_block(text)
    assert fm["trace_id"] == f"meeting-{meeting_id}"
    assert fm["trace_id"] != ""


def test_index_links_to_canonical_json_for_promoted(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    index_text = result.index_path.read_text(encoding="utf-8")
    # The canonical JSON for meeting_minutes lives next to processed dir.
    processed = tmp_path / "processed" / "meetings" / meeting_id
    json_files = sorted(processed.glob("meeting_minutes__*.json"))
    assert json_files, "expected a promoted meeting_minutes JSON"
    assert json_files[0].name in index_text


def test_index_lists_source_transcript_path(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    index_text = result.index_path.read_text(encoding="utf-8")
    transcript_path = tmp_path / "raw" / "meetings" / meeting_id / "transcript.txt"
    assert str(transcript_path) in index_text


def test_index_lists_run_records_with_manifest_and_debug(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    index_text = result.index_path.read_text(encoding="utf-8")

    # Each workflow produces a manifest and a debug report.
    processed = tmp_path / "processed" / "meetings" / meeting_id
    manifest_files = list(processed.glob("manifest__*.json"))
    debug_files = list(processed.glob("debug__*.json"))
    assert manifest_files and debug_files
    for f in manifest_files + debug_files:
        assert f.name in index_text


# --- backlinks (SSC-027) ---------------------------------------------------


def test_artifact_markdown_links_back_to_index(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    md = artifact_markdown_path(tmp_path, meeting_id, "meeting_minutes")
    text = md.read_text(encoding="utf-8")
    assert "(../index.md)" in text


def test_artifact_markdown_has_meeting_wikilink(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    md = artifact_markdown_path(tmp_path, meeting_id, "meeting_minutes")
    text = md.read_text(encoding="utf-8")
    assert f"[[Meeting/{meeting_id}]]" in text


def test_artifact_markdown_links_to_agency_when_metadata_has_agency(tmp_path):
    # m-golden-inquiry has agency: FCC in metadata.
    meeting_id = "m-golden-inquiry"
    _seed(tmp_path, meeting_id)
    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    # agency_question_summary should promote on this fixture.
    assert "agency_question_summary" in result.promoted_workflows
    md = artifact_markdown_path(tmp_path, meeting_id, "agency_question_summary")
    text = md.read_text(encoding="utf-8")
    assert f"../{AGENCIES_SUBDIR}/fcc.md" in text
    assert "[[Agency/FCC]]" in text


def test_index_links_agency_when_metadata_has_agency(tmp_path):
    meeting_id = "m-golden-inquiry"
    _seed(tmp_path, meeting_id)
    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    text = result.index_path.read_text(encoding="utf-8")
    assert f"{AGENCIES_SUBDIR}/fcc.md" in text


def test_no_broken_relative_links_in_artifact_markdown(tmp_path):
    """Every relative link in artifact md must resolve to an existing file."""
    import re
    meeting_id = "m-golden-inquiry"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    md = artifact_markdown_path(tmp_path, meeting_id, "agency_question_summary")
    text = md.read_text(encoding="utf-8")
    pattern = re.compile(r"\]\((?!https?://)([^)]+)\)")
    for rel in pattern.findall(text):
        target = (md.parent / rel).resolve()
        assert target.exists(), (
            f"broken relative link {rel!r} from {md} -> {target}"
        )


# --- agency / topic notes (SSC-027 + SSC-025) ------------------------------


def test_agency_note_written_when_metadata_has_agency(tmp_path):
    meeting_id = "m-golden-inquiry"
    _seed(tmp_path, meeting_id)
    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    agency_md = (
        markdown_dir(tmp_path, meeting_id) / AGENCIES_SUBDIR / "fcc.md"
    )
    assert agency_md.is_file()
    text = agency_md.read_text(encoding="utf-8")
    fm = _frontmatter_block(text)
    assert fm["artifact_type"] == "agency_note"
    assert fm["canonical"] == "false"
    assert "[[Meeting/m-golden-inquiry]]" in text
    assert agency_md in result.agency_paths


def test_topic_note_written_when_metadata_has_topic(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    topic_dir = markdown_dir(tmp_path, meeting_id) / TOPICS_SUBDIR
    assert topic_dir.is_dir()
    files = list(topic_dir.glob("*.md"))
    assert files, "expected at least one topic note"
    text = files[0].read_text(encoding="utf-8")
    fm = _frontmatter_block(text)
    assert fm["artifact_type"] == "topic_note"
    assert fm["canonical"] == "false"


# --- JSON is the source of truth -----------------------------------------


def test_promoted_json_artifact_unchanged_by_markdown_step(tmp_path):
    """Markdown rendering must not modify the promoted JSON bytes."""
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)

    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    processed_dir = tmp_path / "processed" / "meetings" / meeting_id
    json_paths = sorted(
        p for p in processed_dir.glob("*.json")
        if not p.name.startswith(("manifest__", "debug__"))
    )
    assert json_paths, "expected at least one promoted JSON artifact"
    before = {p.name: p.read_bytes() for p in json_paths}

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
    for path in md.rglob("*"):
        if path.is_file():
            assert path.suffix == ".md", (
                f"unexpected non-markdown file under markdown dir: {path}"
            )


def test_artifact_index_does_not_include_markdown_files(tmp_path):
    """Markdown views must never appear as product artifacts in the index."""
    from spectrum_systems_core.data_lake import (
        collect_index_records,
        write_artifact_index,
    )

    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    index_path = write_artifact_index(tmp_path)

    text = index_path.read_text(encoding="utf-8")
    assert ".md" not in text, "no markdown files should appear in index"
    records = collect_index_records(tmp_path)
    for r in records:
        assert not r.path.endswith(".md")


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
    assert artifact_markdown_path(tmp_path, meeting_id, "meeting_minutes").is_file()


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

    assert artifact_markdown_path(tmp_path, meeting_id, "meeting_minutes").is_file()
    # When restricted, the other workflows should not have produced markdown.
    assert not artifact_markdown_path(
        tmp_path, meeting_id, "meeting_action_log"
    ).exists()
    assert not artifact_markdown_path(
        tmp_path, meeting_id, "decision_brief"
    ).exists()


def test_cli_requires_lake_and_meeting_id(tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli_main(["process-meeting"])


# --- SSC-023 (preserved): empty agency on agency_question_summary blocks --


def test_agency_question_summary_blocks_when_agency_missing(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)

    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    assert "agency_question_summary" in result.blocked_workflows
    aqs_result = next(
        r for r in result.pipeline_results
        if r.workflow_name == "agency_question_summary"
    )
    assert aqs_result.promoted is False
    assert aqs_result.target.status == "rejected"


def test_index_explains_empty_agency_in_plain_english(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)

    result = process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    index_text = result.index_path.read_text(encoding="utf-8")

    assert "agency_question_summary" in index_text
    assert "empty_required_field:agency" in index_text
    assert "required field 'agency' was empty" in index_text


def test_no_promoted_agency_question_summary_markdown_when_blocked(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    md_path = artifact_markdown_path(
        tmp_path, meeting_id, "agency_question_summary"
    )
    assert not md_path.exists()


def test_no_promoted_agency_question_summary_json_when_blocked(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)

    processed_dir = tmp_path / "processed" / "meetings" / meeting_id
    product_jsons = list(processed_dir.glob("agency_question_summary__*.json"))
    assert product_jsons == []


def test_markdown_is_deterministic_across_runs(tmp_path):
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)

    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    md = markdown_dir(tmp_path, meeting_id)
    snapshot1 = {
        str(p.relative_to(md)): p.read_bytes()
        for p in sorted(md.rglob("*"))
        if p.is_file()
    }

    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    snapshot2 = {
        str(p.relative_to(md)): p.read_bytes()
        for p in sorted(md.rglob("*"))
        if p.is_file()
    }

    assert snapshot1 == snapshot2
