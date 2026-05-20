"""Phase 2R — CLI tests for ``spectrum-core check-transcript`` and the
``--enable-pre-flight-check`` flag on ``process-meeting``."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from spectrum_systems_core.data_lake.cli import main as cli_main
from spectrum_systems_core.transcript_quality.cli_integration import (
    run_check_transcript_cli,
)

from . import fixtures as F


def _write_lake_with_transcript(
    tmp_path: Path,
    *,
    source_id: str = "demo-meeting",
    transcript: str | None = None,
    write_metadata: bool = True,
) -> Path:
    raw_dir = tmp_path / "raw" / "meetings" / source_id
    raw_dir.mkdir(parents=True)
    if transcript is None:
        transcript = F.valid_transcript()
    (raw_dir / "transcript.txt").write_text(transcript, encoding="utf-8")
    if write_metadata:
        (raw_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "meeting_id": source_id,
                    "title": "Demo Meeting",
                    "date": "2026-05-20",
                    "source_type": "transcript",
                }
            ),
            encoding="utf-8",
        )
    return tmp_path


def test_check_transcript_path_mode_valid_exits_zero(tmp_path: Path) -> None:
    fixture = tmp_path / "demo.txt"
    fixture.write_text(F.valid_transcript(), encoding="utf-8")
    rc = cli_main(
        ["check-transcript", "--transcript-path", str(fixture)]
    )
    assert rc == 0


def test_check_transcript_path_mode_invalid_exits_one(tmp_path: Path) -> None:
    fixture = tmp_path / "bad.txt"
    fixture.write_text(F.encoding_corrupted_transcript(), encoding="utf-8")
    rc = cli_main(
        ["check-transcript", "--transcript-path", str(fixture)]
    )
    assert rc == 1


def test_check_transcript_missing_file_exits_two(tmp_path: Path) -> None:
    rc = cli_main(
        ["check-transcript", "--transcript-path", str(tmp_path / "nope.txt")]
    )
    assert rc == 2


def test_check_transcript_unreadable_non_utf8_exits_two(tmp_path: Path) -> None:
    fixture = tmp_path / "bad_bytes.txt"
    # 0xFF is not valid UTF-8.
    fixture.write_bytes(b"\xff\xfe\xfd" + b"some content here" * 50)
    rc = cli_main(
        ["check-transcript", "--transcript-path", str(fixture)]
    )
    assert rc == 2


def test_check_transcript_source_id_mode_writes_diagnostics(
    tmp_path: Path,
) -> None:
    lake = _write_lake_with_transcript(tmp_path)
    rc = cli_main(
        [
            "check-transcript",
            "--source-id",
            "demo-meeting",
            "--lake",
            str(lake),
        ]
    )
    assert rc == 0
    diag_dir = (
        lake / "store" / "processed" / "meetings" / "demo-meeting" / "diagnostics"
    )
    files = list(diag_dir.glob("transcript_quality_report__*.json"))
    assert len(files) == 1, files
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["artifact_type"] == "transcript_quality_report"
    assert payload["schema_version"] == "1.0.0"
    assert payload["has_errors"] is False


def test_check_transcript_rejects_combined_modes(tmp_path: Path) -> None:
    fixture = tmp_path / "demo.txt"
    fixture.write_text(F.valid_transcript(), encoding="utf-8")
    lake = _write_lake_with_transcript(tmp_path)
    stream = io.StringIO()
    result = run_check_transcript_cli(
        transcript_path=str(fixture),
        source_id="demo-meeting",
        lake=str(lake),
        stream=stream,
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in stream.getvalue()


def test_check_transcript_source_id_without_lake_errors(tmp_path: Path) -> None:
    stream = io.StringIO()
    result = run_check_transcript_cli(
        transcript_path=None,
        source_id="demo-meeting",
        lake=None,
        stream=stream,
    )
    assert result.exit_code == 2
    assert "--lake" in stream.getvalue()


def test_check_transcript_no_inputs_errors() -> None:
    stream = io.StringIO()
    result = run_check_transcript_cli(
        transcript_path=None,
        source_id=None,
        lake=None,
        stream=stream,
    )
    assert result.exit_code == 2


def test_check_transcript_resolves_source_record_raw_path(
    tmp_path: Path,
) -> None:
    """source_record.json::payload.raw_path takes precedence over the
    default raw/meetings/<id>/transcript.txt path."""
    lake = tmp_path
    raw_dir = lake / "raw" / "meetings" / "demo-meeting"
    raw_dir.mkdir(parents=True)
    custom_path = lake / "raw" / "meetings" / "demo-meeting" / "custom.txt"
    custom_path.write_text(F.valid_transcript(), encoding="utf-8")
    # metadata is required by the loader, but check-transcript reads
    # via source_record, so we just need a minimum source_record file.
    processed_dir = lake / "processed" / "meetings" / "demo-meeting"
    processed_dir.mkdir(parents=True)
    sr = {
        "artifact_type": "source_record",
        "payload": {"raw_path": "raw/meetings/demo-meeting/custom.txt"},
    }
    (processed_dir / "source_record.json").write_text(
        json.dumps(sr), encoding="utf-8"
    )
    rc = cli_main(
        [
            "check-transcript",
            "--source-id",
            "demo-meeting",
            "--lake",
            str(lake),
        ]
    )
    assert rc == 0


def test_enable_pre_flight_check_is_cli_only_default_false() -> None:
    """The flag must default to False and must not be readable from
    env vars. Setting the env var has no effect."""
    from spectrum_systems_core.data_lake.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args(
        [
            "process-meeting",
            "--lake",
            "/tmp/nope",
            "--meeting-id",
            "demo-meeting",
        ]
    )
    assert args.enable_pre_flight_check is False


def test_enable_pre_flight_check_env_var_has_no_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting `ENABLE_PRE_FLIGHT_CHECK=true` in env must NOT enable
    the gate (red team Pass 1 #8)."""
    monkeypatch.setenv("ENABLE_PRE_FLIGHT_CHECK", "true")
    from spectrum_systems_core.data_lake.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args(
        [
            "process-meeting",
            "--lake",
            "/tmp/nope",
            "--meeting-id",
            "demo-meeting",
        ]
    )
    assert args.enable_pre_flight_check is False


def test_process_meeting_pre_flight_halts_invalid_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red team Pass 2 #4: with --enable-pre-flight-check, an invalid
    transcript halts the run BEFORE any LLM call. We assert the
    extractor functions are never invoked."""
    lake = _write_lake_with_transcript(
        tmp_path,
        transcript=F.encoding_corrupted_transcript(),
    )

    invoked: list[str] = []

    def _spy(*_a, **_k):
        invoked.append("spy_called")
        raise AssertionError("extractor must not be reached on invalid transcript")

    import spectrum_systems_core.data_lake.cli as cli_mod

    monkeypatch.setattr(cli_mod, "process_meeting_llm", _spy)
    monkeypatch.setattr(cli_mod, "process_meeting", _spy)

    rc = cli_mod.main(
        [
            "process-meeting",
            "--lake",
            str(lake),
            "--meeting-id",
            "demo-meeting",
            "--enable-pre-flight-check",
        ]
    )
    assert rc == 1
    assert not invoked
    diag_dir = (
        lake / "store" / "processed" / "meetings" / "demo-meeting" / "diagnostics"
    )
    files = list(diag_dir.glob("transcript_quality_report__*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["has_errors"] is True


def test_process_meeting_pre_flight_proceeds_on_valid_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lake = _write_lake_with_transcript(tmp_path)

    called: list[str] = []

    def _spy_process_meeting(*, lake_root, meeting_id, workflows=None):
        called.append("process_meeting")

        class _R:
            pipeline_results: list = []
            promoted_workflows: list = []
            blocked_workflows: list = []
            markdown_paths: list = []
            agency_paths: list = []
            topic_paths: list = []
            index_path = Path("/tmp/idx.md")
            run_history_path = None
            experience_history_path = None
            eval_history_path = None
            run_note_paths: list = []

        return _R()

    import spectrum_systems_core.data_lake.cli as cli_mod

    monkeypatch.setattr(cli_mod, "process_meeting", _spy_process_meeting)
    monkeypatch.setattr(cli_mod, "_print_result", lambda _r: None)

    rc = cli_mod.main(
        [
            "process-meeting",
            "--lake",
            str(lake),
            "--meeting-id",
            "demo-meeting",
            "--enable-pre-flight-check",
        ]
    )
    assert rc == 0
    assert called == ["process_meeting"]


def test_process_meeting_without_flag_does_not_run_validator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default behavior: no flag → no validator call → no diagnostics."""
    lake = _write_lake_with_transcript(
        tmp_path,
        transcript=F.encoding_corrupted_transcript(),
    )

    import spectrum_systems_core.data_lake.cli as cli_mod

    monkeypatch.setattr(
        cli_mod,
        "process_meeting",
        lambda **_k: type(
            "R",
            (),
            {
                "pipeline_results": [],
                "promoted_workflows": [],
                "blocked_workflows": [],
                "markdown_paths": [],
                "agency_paths": [],
                "topic_paths": [],
                "index_path": Path("/tmp/idx.md"),
                "run_history_path": None,
                "experience_history_path": None,
                "eval_history_path": None,
                "run_note_paths": [],
            },
        )(),
    )
    monkeypatch.setattr(cli_mod, "_print_result", lambda _r: None)

    rc = cli_mod.main(
        [
            "process-meeting",
            "--lake",
            str(lake),
            "--meeting-id",
            "demo-meeting",
        ]
    )
    assert rc == 0
    diag_dir = (
        lake / "store" / "processed" / "meetings" / "demo-meeting" / "diagnostics"
    )
    assert not diag_dir.exists()
