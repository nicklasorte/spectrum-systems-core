"""Integration contract test for ``scripts/extract_opus_baseline.py``.

Satisfies the CLAUDE.md non-negotiable: a script that writes a pipeline
artifact must have an integration test that

  1. Uses ``tests/integration/fixtures.py`` factories (no hand-rolled
     source records).
  2. Writes artifacts to a real temp directory (not mocked).
  3. Calls the script via ``subprocess.run`` against the temp dir.
  4. Asserts the correct output on disk (not just the return code).

The model transport is replaced by the script's ``_STUB_ENV`` seam
(``EXTRACT_OPUS_BASELINE_STUB_RESPONSE``) so the full assemble ->
scaffold-metadata -> self-healing-ingest path runs with NO ``claude``
CLI and NO network. The stub returns a CONTENT-ONLY meeting_minutes
object (no top-level ``title``/``summary``) so the test also proves the
metadata-scaffolding step the script adds before ingest.
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

from tests.integration.fixtures import make_source_record

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "extract_opus_baseline.py"
SOURCE_ID = "test-source-opus-extract"

# Content-only model output (what ``claude -p`` returns): no
# ``title``/``summary`` — the script scaffolds those before ingest.
_CONTENT_ONLY = {
    "decisions": [
        "The TIG approved the 7 GHz downlink threshold.",
        {
            "text": "Adjacent-band allocation deferred to next cycle.",
            "verb": "deferred",
            # Hallucinated chunk id on an OPEN type — must be stripped
            # everywhere by the global self-heal.
            "source_chunk_id": "turn-7",
        },
    ],
    "action_items": [{"action": "NTIA to circulate the methodology."}],
    "open_questions": [
        {
            "question_id": "q1",
            "question_text": "What ERP cap applies to the FSS uplink?",
        }
    ],
    "technical_parameters": [
        {
            "param_id": "tp1",
            "parameter_name": "FSS uplink ERP cap",
            "value": "33 dBm/MHz",
            # Stray key on a CLOSED type — stripped by schema-driven heal.
            "reason": "stated by the chair",
            # Null optional the schema forbids null on — dropped (class 3).
            "source_turns": None,
        }
    ],
}


# Keyword-free transcript: no ceiling trigger (decision / action_item /
# open_question / claim / topics) fires, so the minimum-counts gate
# stays silent — the integration-level negative fixture.
_NO_KEYWORD_TRANSCRIPT = (
    "7 GHz SPD-SEAD Sync — Test Transcript\n\nAlice 00:01\nHello.\n"
)

# Same transcript with an agenda line: the validated topics trigger
# fires, so the gate now requires >=1 topics item in the extraction.
_AGENDA_TRANSCRIPT = (
    "7 GHz SPD-SEAD Sync — Test Transcript\n\nAlice 00:01\n"
    "Welcome; the agenda today covers the downlink study plan.\n"
)


def _seed(
    tmp_path: Path, transcript: str = _NO_KEYWORD_TRANSCRIPT
) -> tuple[Path, str]:
    data_lake = tmp_path / "data-lake"
    meeting_dir = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    meeting_dir.mkdir(parents=True)
    artifact_id = str(uuid.uuid4())
    (meeting_dir / "source_record.json").write_text(
        json.dumps(make_source_record(SOURCE_ID, artifact_id)),
        encoding="utf-8",
    )
    raw_dir = data_lake / "store" / "raw" / "meetings" / SOURCE_ID
    raw_dir.mkdir(parents=True)
    (raw_dir / "source.txt").write_text(transcript, encoding="utf-8")
    return data_lake, artifact_id


def _run(args: list[str], stub: str | None) -> subprocess.CompletedProcess[str]:
    env = None
    if stub is not None:
        import os

        env = dict(os.environ)
        env["EXTRACT_OPUS_BASELINE_STUB_RESPONSE"] = stub
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _out_path(data_lake: Path) -> Path:
    return (
        data_lake / "store" / "processed" / "meetings" / SOURCE_ID
        / "reference_baselines" / "opus_reference_minutes.jsonl"
    )


def _rows(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_extract_stub_scaffolds_and_ingests(tmp_path: Path) -> None:
    data_lake, artifact_id = _seed(tmp_path)
    result = _run(
        [
            "--source-id", SOURCE_ID,
            "--data-lake", str(data_lake),
            "--operator", "test-operator",
            "--work-dir", str(tmp_path / "work"),
        ],
        stub=json.dumps(_CONTENT_ONLY),
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    out_path = _out_path(data_lake)
    assert out_path.is_file()
    rows = _rows(out_path)
    # 2 decisions + 1 action_item + 1 open_question + 1 technical_parameter
    assert len(rows) == 5
    for row in rows:
        assert row["source_id"] == SOURCE_ID
        assert row["source_artifact_id"] == artifact_id
        assert row["model_id"] == "claude-opus-4-7"
        assert row["provenance"]["produced_by"] == (
            "opus_reference_baseline_workflow"
        )
        assert "artifact_kind" not in row

    # Self-heal proof: the hallucinated source_chunk_id was stripped from
    # the OPEN decisions type too (global strip), the stray reason was
    # stripped from the CLOSED technical_parameters type, and the null
    # source_turns was dropped.
    dec = [r for r in rows if r["extraction_type"] == "decisions"]
    for r in dec:
        assert "source_chunk_id" not in r["item_data"]
    tp = [r for r in rows if r["extraction_type"] == "technical_parameters"]
    assert tp
    for r in tp:
        assert "reason" not in r["item_data"]
        assert "source_turns" not in r["item_data"]


def test_extract_does_not_fabricate_missing_required_field(
    tmp_path: Path,
) -> None:
    """Class-4 guard: a missing/null REQUIRED field is NOT filled with a
    sentinel. ``position_statement`` requires ``agency``; omitting it must
    HALT ``schema_violation`` (exit 1), never ingest an invented value."""
    data_lake, _ = _seed(tmp_path)
    content = dict(_CONTENT_ONLY)
    content["position_statement"] = [
        {
            "position_id": "p1",
            "position_text": "We oppose the adjacent-band relaxation.",
            # 'agency' (required) deliberately absent.
        }
    ]
    result = _run(
        [
            "--source-id", SOURCE_ID,
            "--data-lake", str(data_lake),
            "--operator", "test-operator",
            "--work-dir", str(tmp_path / "work"),
        ],
        stub=json.dumps(content),
    )
    assert result.returncode == 1, (
        f"expected schema_violation halt; stdout={result.stdout!r}"
    )
    payload = json.loads(result.stdout)
    assert payload["reason"] == "schema_violation"
    # The invented sentinel must never appear on disk.
    out_path = _out_path(data_lake)
    assert not out_path.exists()
    # And the rejection message must reference the real missing field,
    # not a fabricated value.
    assert "agency" in payload["detail"]
    assert "Unknown" not in payload["detail"]


def test_extract_halts_on_missing_transcript(tmp_path: Path) -> None:
    data_lake = tmp_path / "data-lake"
    (data_lake / "store" / "processed" / "meetings" / SOURCE_ID).mkdir(
        parents=True
    )
    # No source.txt staged.
    result = _run(
        [
            "--source-id", SOURCE_ID,
            "--data-lake", str(data_lake),
            "--operator", "test-operator",
            "--work-dir", str(tmp_path / "work"),
        ],
        stub=json.dumps(_CONTENT_ONLY),
    )
    assert result.returncode == 2, result.stdout
    assert json.loads(result.stdout)["reason"] == "missing_transcript"


def test_extract_halts_on_truncated_topics(tmp_path: Path) -> None:
    """Topics A-wide gate, positive direction: the transcript's agenda
    keyword fires but the extraction has NO topics items — the exact
    structural-truncation failure mode the 2026-06-10 calibration found
    in the committed 7ghz-spd-sead-sync baselines. The script must HALT
    ``ceiling_minimum_counts_failed`` naming ``topics`` BEFORE the
    ingest commits anything."""
    data_lake, _ = _seed(tmp_path, transcript=_AGENDA_TRANSCRIPT)
    result = _run(
        [
            "--source-id", SOURCE_ID,
            "--data-lake", str(data_lake),
            "--operator", "test-operator",
            "--work-dir", str(tmp_path / "work"),
        ],
        stub=json.dumps(_CONTENT_ONLY),  # no "topics" array
    )
    assert result.returncode == 1, (
        f"expected minimum-counts halt; stdout={result.stdout!r}"
    )
    payload = json.loads(result.stdout)
    assert payload["reason"] == "ceiling_minimum_counts_failed"
    assert "topics" in payload["detail"]
    # Fail closed: the truncated extraction never reached the data-lake.
    assert not _out_path(data_lake).exists()


def test_extract_passes_with_topics_present(tmp_path: Path) -> None:
    """Topics keyword fires AND the extraction has a topics item — the
    gate passes and the topics rows land in the baseline."""
    data_lake, _ = _seed(tmp_path, transcript=_AGENDA_TRANSCRIPT)
    content = dict(_CONTENT_ONLY)
    content["topics"] = [
        {"topic_id": "top1", "title": "Downlink study plan review"}
    ]
    result = _run(
        [
            "--source-id", SOURCE_ID,
            "--data-lake", str(data_lake),
            "--operator", "test-operator",
            "--work-dir", str(tmp_path / "work"),
        ],
        stub=json.dumps(content),
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    rows = _rows(_out_path(data_lake))
    topics = [r for r in rows if r["extraction_type"] == "topics"]
    assert len(topics) == 1
    assert topics[0]["item_data"]["title"] == "Downlink study plan review"


def test_extract_negative_fixture_no_topics_keyword_no_block(
    tmp_path: Path,
) -> None:
    """Topics A-wide gate, negative direction: a transcript with NO
    agenda/topic keyword and an extraction with NO topics items must
    ingest cleanly — the trigger stays silent when the type is absent.
    (The 47-meeting calibration corpus could not prove this direction:
    every corpus meeting has topics.)"""
    data_lake, _ = _seed(tmp_path, transcript=_NO_KEYWORD_TRANSCRIPT)
    result = _run(
        [
            "--source-id", SOURCE_ID,
            "--data-lake", str(data_lake),
            "--operator", "test-operator",
            "--work-dir", str(tmp_path / "work"),
        ],
        stub=json.dumps(_CONTENT_ONLY),  # no "topics" array
    )
    assert result.returncode == 0, (
        f"negative fixture must not block; stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    rows = _rows(_out_path(data_lake))
    assert rows  # ingest actually happened
    assert not any(r["extraction_type"] == "topics" for r in rows)


def test_extract_resolves_pinned_model_from_registry(tmp_path: Path) -> None:
    data_lake, _ = _seed(tmp_path)
    result = _run(
        [
            "--source-id", SOURCE_ID,
            "--data-lake", str(data_lake),
            "--operator", "test-operator",
            "--work-dir", str(tmp_path / "work"),
        ],
        stub=json.dumps(_CONTENT_ONLY),
    )
    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout)["model"] == "claude-opus-4-7"
