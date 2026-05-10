"""Tests for MinutesProcessor (Phase L.2).

All .docx fixtures are built in-memory using python-docx. No binary
fixtures, no LLM calls, no hard-coded UUIDs.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import jsonschema
import pytest
from docx import Document

from spectrum_systems_core.ingestion._paths import contracts_root
from spectrum_systems_core.ingestion.docx_extractor import DocxExtractor
from spectrum_systems_core.ingestion.minutes_processor import (
    MinutesProcessor,
    extract_meeting_date,
    extract_meeting_name,
)


def _write_docx(path: Path, paragraphs: list[str]) -> None:
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    doc.save(str(path))


def _make_data_lake(tmp_path: Path, *, with_minutes_dir: bool = True) -> Path:
    dl = tmp_path / "data-lake"
    if with_minutes_dir:
        (dl / "store" / "raw" / "minutes").mkdir(parents=True)
    (dl / "store" / "artifacts").mkdir(parents=True)
    return dl


@pytest.fixture
def data_lake(tmp_path: Path, monkeypatch) -> Path:
    dl = _make_data_lake(tmp_path)
    monkeypatch.setenv("DATA_LAKE_PATH", str(dl))
    monkeypatch.setenv("SDL_ROOT", str(dl / "store" / "artifacts"))
    return dl


def test_process_single_docx_produces_minutes_record(data_lake: Path) -> None:
    docx = data_lake / "store" / "raw" / "minutes" / (
        "7 GHz Downlink TIG Meeting - minutes 2-19-26.docx"
    )
    _write_docx(docx, ["Heading", "Body of the minutes goes here."])
    result = MinutesProcessor().process(str(docx), str(data_lake))
    assert result["status"] == "success", result
    assert result["meeting_date"] == "2026-02-19"
    assert "TIG Meeting" in result["meeting_name"]
    assert result["text_unit_count"] == 2
    assert result["character_count"] > 0
    artifact_path = Path(result["artifact_path"])
    assert artifact_path.is_file()
    record = json.loads(artifact_path.read_text())
    uuid.UUID(record["minutes_id"])
    assert record["schema_version"] == "1.0.0"
    assert record["provenance"]["produced_by"] == "MinutesProcessor"


def test_meeting_date_extracted_from_filename() -> None:
    cases = {
        "Meeting 2-19-26.docx": "2026-02-19",
        "Meeting 2-19-2026.docx": "2026-02-19",
        "Meeting 20260219.docx": "2026-02-19",
        "Meeting 20251218.docx": "2025-12-18",
        "Meeting 22Jan2026.docx": "2026-01-22",
        "Meeting 22-Jan-2026.docx": "2026-01-22",
    }
    for filename, expected in cases.items():
        assert extract_meeting_date(filename, "") == expected, filename


def test_meeting_date_extracted_from_content() -> None:
    text = "These are the minutes from January 22, 2026.\nMore body."
    assert extract_meeting_date("untitled.docx", text) == "2026-01-22"
    text2 = "Meeting on December 18, 2025"
    assert extract_meeting_date("untitled.docx", text2) == "2025-12-18"


def test_meeting_date_none_when_not_found() -> None:
    assert extract_meeting_date("notes.docx", "no date in here") is None
    # Month-only-with-year is intentionally NOT matched (no day-of-month).
    assert extract_meeting_date("Some Meeting Jan2026.docx", "") is None


def test_meeting_name_extracted_from_filename() -> None:
    name = extract_meeting_name(
        "7 GHz Downlink TIG Meeting - minutes 2-19-26.docx"
    )
    assert "TIG Meeting" in name
    # Date pattern stripped, "minutes" suffix dropped.
    assert "2-19-26" not in name
    assert "minutes" not in name.lower()


def test_minutes_record_schema_validates(data_lake: Path) -> None:
    docx = data_lake / "store" / "raw" / "minutes" / "Meeting 2-19-26.docx"
    _write_docx(docx, ["Some body content."])
    result = MinutesProcessor().process(str(docx), str(data_lake))
    assert result["status"] == "success"
    record = json.loads(Path(result["artifact_path"]).read_text())
    schema = json.loads(
        (
            contracts_root() / "schemas" / "ingestion" / "minutes_record.schema.json"
        ).read_text()
    )
    # Will raise on schema violation.
    jsonschema.Draft202012Validator(schema).validate(record)


def test_process_directory_empty_returns_empty_list(data_lake: Path) -> None:
    assert MinutesProcessor().process_directory(str(data_lake)) == []


def test_process_directory_missing_returns_empty_list(tmp_path: Path) -> None:
    # No store/raw/minutes/ subtree at all.
    dl = tmp_path / "empty_lake"
    dl.mkdir()
    assert MinutesProcessor().process_directory(str(dl)) == []


def test_process_never_raises(monkeypatch, tmp_path: Path) -> None:
    # Pass nonexistent file: must return failure, not raise.
    result = MinutesProcessor().process(
        "/does/not/exist.docx", str(tmp_path)
    )
    assert result["status"] == "failure"
    assert "file_not_found" in result["reason"]
    assert result["artifact_path"] is None


def test_process_directory_processes_all_docx(data_lake: Path) -> None:
    base = data_lake / "store" / "raw" / "minutes"
    _write_docx(base / "Meeting 2-19-26.docx", ["A"])
    _write_docx(base / "Meeting 3-19-26.docx", ["B"])
    results = MinutesProcessor().process_directory(str(data_lake))
    assert len(results) == 2
    dates = {r["meeting_date"] for r in results}
    assert dates == {"2026-02-19", "2026-03-19"}


def test_meeting_date_none_when_filename_unparseable(data_lake: Path) -> None:
    docx = data_lake / "store" / "raw" / "minutes" / "untitled_minutes.docx"
    # Body text also has no date pattern.
    _write_docx(docx, ["Just a body without any date in the first 500 chars."])
    result = MinutesProcessor().process(str(docx), str(data_lake))
    assert result["status"] == "success"
    assert result["meeting_date"] is None
    record = json.loads(Path(result["artifact_path"]).read_text())
    assert record["meeting_date"] is None


def test_uses_docx_extractor_does_not_reimplement(
    data_lake: Path, monkeypatch
) -> None:
    # If MinutesProcessor reimplemented .docx parsing, swapping the
    # extractor at construction time would have no effect on the result.
    docx = data_lake / "store" / "raw" / "minutes" / "Meeting 2-19-26.docx"
    _write_docx(docx, ["Body content here."])

    calls = {"count": 0}
    real = DocxExtractor()

    class _Wrapping:
        def extract(self, *args, **kwargs):
            calls["count"] += 1
            return real.extract(*args, **kwargs)

    result = MinutesProcessor(docx_extractor=_Wrapping()).process(
        str(docx), str(data_lake)
    )
    assert result["status"] == "success"
    assert calls["count"] == 1
