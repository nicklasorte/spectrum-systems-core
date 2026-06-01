"""Integration contract test for ``scripts/ingest_opus_baseline.py``.

Satisfies the CLAUDE.md non-negotiable: every script that writes a
pipeline artifact must have an integration test that

  1. Uses ``tests/integration/fixtures.py`` factories (no hand-rolled
     dicts) to produce the input artifacts.
  2. Writes artifacts to a real temp directory (not mocked).
  3. Calls the script via ``subprocess.run`` against the temp dir.
  4. Asserts the correct output on disk (not just the return code).

This file mirrors ``test_ingest_codex_baseline_contract.py``; differences
are limited to: the script-under-test, the output filename, the model
string source (registry's ``models.opus_reference_baseline``), the
produced_by tag, the Opus-namespace pair_id check, and the absence of an
``operator`` key in ``provenance``.
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

from tests.integration.fixtures import make_source_record

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ingest_opus_baseline.py"
SOURCE_ID = "test-source-opus-ingest"

# Representative meeting_minutes payload exercising the schema branches
# the canonical extraction prompt is allowed to emit. Same payload the
# codex contract test uses so the two ingests can be cross-checked.
_OPUS_INPUT = {
    "artifact_type": "meeting_minutes",
    "schema_version": "1.4.0",
    "title": "Synthetic Opus Baseline",
    "summary": "Synthetic Opus output for the ingest contract test.",
    "decisions": [
        "The TIG approved the 7 GHz downlink threshold.",
        {"text": "Adjacent-band allocation deferred to next cycle."},
    ],
    "action_items": [
        {"action": "NTIA to circulate the revised methodology."},
    ],
    "open_questions": [
        {
            "question_id": "q1",
            "question_text": "What ERP cap applies to the FSS uplink?",
        },
    ],
    "provenance": {"produced_by": "opus_local"},
}


def _seed_data_lake(tmp_path: Path) -> tuple[Path, str]:
    """Return ``(data_lake_root, source_artifact_id)`` with a real
    ``source_record.json`` produced by the integration factory."""
    data_lake = tmp_path / "data-lake"
    meeting_dir = (
        data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    )
    meeting_dir.mkdir(parents=True)
    source_artifact_id = str(uuid.uuid4())
    (meeting_dir / "source_record.json").write_text(
        json.dumps(make_source_record(SOURCE_ID, source_artifact_id)),
        encoding="utf-8",
    )
    return data_lake, source_artifact_id


def _write_input(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "opus_output.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
    )


def test_ingest_writes_jsonl_to_reference_baselines_dir(
    tmp_path: Path,
) -> None:
    data_lake, source_artifact_id = _seed_data_lake(tmp_path)
    input_file = _write_input(tmp_path, _OPUS_INPUT)

    result = _run(
        [
            "--input-file", str(input_file),
            "--source-id", SOURCE_ID,
            "--data-lake", str(data_lake),
            "--operator", "test-operator",
        ]
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    out_path = (
        data_lake / "store" / "processed" / "meetings" / SOURCE_ID
        / "reference_baselines" / "opus_reference_minutes.jsonl"
    )
    assert out_path.is_file(), f"expected JSONL at {out_path}"

    # Path mirror: opus baseline lives in the SAME directory as the
    # codex baseline would, and the directory is `reference_baselines/`
    # (no new directory structure).
    assert out_path.parent.name == "reference_baselines"

    text = out_path.read_text(encoding="utf-8")
    assert text.endswith("\n"), "JSONL must end with a single newline"
    # JSONL invariant: one JSON object per line.
    lines = text.rstrip("\n").split("\n")
    rows = [json.loads(line) for line in lines]
    assert len(rows) >= 4  # 2 decisions + 1 action_item + 1 open_question

    # Every row carries the same source_artifact_id from
    # source_record.json — the join key with the Codex baseline.
    for row in rows:
        assert row["source_id"] == SOURCE_ID
        assert row["source_artifact_id"] == source_artifact_id
        assert row["model_authored"] is True
        assert row["human_authored"] is False
        assert row["status"] == "reference_only"
        assert row["model_id"] == "claude-opus-4-7"
        assert row["provenance"]["produced_by"] == (
            "opus_reference_baseline_workflow"
        )
        # Opus shape MUST NOT carry an operator key in provenance
        # (codex shape does; opus shape does not).
        assert "operator" not in row["provenance"], (
            "Opus baseline provenance must not include an operator key "
            "— that is the codex shape"
        )
        # CLAUDE.md discipline — never artifact_kind.
        assert "artifact_kind" not in row

    # extraction_type populated across multiple types.
    etypes = {row["extraction_type"] for row in rows}
    assert {"decisions", "action_items", "open_questions"} <= etypes

    # Deterministic field order via sort_keys + minimal separators.
    for line, row in zip(lines, rows):
        assert line == json.dumps(
            row, sort_keys=True, separators=(",", ":")
        )


def test_ingest_rejects_schema_violation(tmp_path: Path) -> None:
    """A typo (``artifact_kind``) must HALT with ``schema_violation``,
    exit code 1, and write nothing."""
    data_lake, _ = _seed_data_lake(tmp_path)
    bad_input = {
        "artifact_kind": "meeting_minutes",  # typo: NOT artifact_type
        "schema_version": "1.4.0",
        "title": "Bad",
        "summary": "",
        "decisions": [],
        "action_items": [],
        "open_questions": [],
    }
    input_file = _write_input(tmp_path, bad_input)

    result = _run(
        [
            "--input-file", str(input_file),
            "--source-id", SOURCE_ID,
            "--data-lake", str(data_lake),
            "--operator", "test-operator",
        ]
    )
    assert result.returncode == 1, (
        f"expected exit 1, got {result.returncode}; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "failure"
    assert payload["reason"] == "schema_violation"

    out_path = (
        data_lake / "store" / "processed" / "meetings" / SOURCE_ID
        / "reference_baselines" / "opus_reference_minutes.jsonl"
    )
    assert not out_path.exists(), "no file may be written on halt"


def test_ingest_rejects_input_file_missing(tmp_path: Path) -> None:
    """A missing input file is exit code 2 (file-not-found, not a
    schema violation)."""
    data_lake, _ = _seed_data_lake(tmp_path)
    result = _run(
        [
            "--input-file", str(tmp_path / "nope.json"),
            "--source-id", SOURCE_ID,
            "--data-lake", str(data_lake),
            "--operator", "test-operator",
        ]
    )
    assert result.returncode == 2, result.stdout
    payload = json.loads(result.stdout)
    assert payload["reason"] == "input_file_not_found"


def test_ingest_rejects_invalid_json(tmp_path: Path) -> None:
    data_lake, _ = _seed_data_lake(tmp_path)
    input_file = tmp_path / "broken.json"
    input_file.write_text("{not valid json", encoding="utf-8")
    result = _run(
        [
            "--input-file", str(input_file),
            "--source-id", SOURCE_ID,
            "--data-lake", str(data_lake),
            "--operator", "test-operator",
        ]
    )
    assert result.returncode == 2, result.stdout
    payload = json.loads(result.stdout)
    assert payload["reason"] == "invalid_input_json"


def test_ingest_halts_when_already_ingested(tmp_path: Path) -> None:
    """Append-only data lake: second ingest with the file present
    halts ``already_ingested`` and does NOT overwrite."""
    data_lake, _ = _seed_data_lake(tmp_path)
    input_file = _write_input(tmp_path, _OPUS_INPUT)

    args = [
        "--input-file", str(input_file),
        "--source-id", SOURCE_ID,
        "--data-lake", str(data_lake),
        "--operator", "test-operator",
    ]
    first = _run(args)
    assert first.returncode == 0, first.stdout

    out_path = (
        data_lake / "store" / "processed" / "meetings" / SOURCE_ID
        / "reference_baselines" / "opus_reference_minutes.jsonl"
    )
    original_bytes = out_path.read_bytes()

    second = _run(args)
    assert second.returncode == 1, second.stdout
    payload = json.loads(second.stdout)
    assert payload["reason"] == "already_ingested"
    # File preserved byte-for-byte.
    assert out_path.read_bytes() == original_bytes


def test_ingest_halts_when_source_record_missing(tmp_path: Path) -> None:
    """No source_record.json -> ``missing_source_record`` halt, exit 1,
    no file written."""
    data_lake = tmp_path / "data-lake"
    # Create only the meeting dir — no source_record.json.
    (data_lake / "store" / "processed" / "meetings" / SOURCE_ID).mkdir(
        parents=True
    )
    input_file = _write_input(tmp_path, _OPUS_INPUT)
    result = _run(
        [
            "--input-file", str(input_file),
            "--source-id", SOURCE_ID,
            "--data-lake", str(data_lake),
            "--operator", "test-operator",
        ]
    )
    assert result.returncode == 1, result.stdout
    payload = json.loads(result.stdout)
    assert payload["reason"] == "missing_source_record"

    out_path = (
        data_lake / "store" / "processed" / "meetings" / SOURCE_ID
        / "reference_baselines" / "opus_reference_minutes.jsonl"
    )
    assert not out_path.exists()


def test_ingest_dry_run_validates_but_writes_nothing(
    tmp_path: Path,
) -> None:
    data_lake, _ = _seed_data_lake(tmp_path)
    input_file = _write_input(tmp_path, _OPUS_INPUT)
    result = _run(
        [
            "--input-file", str(input_file),
            "--source-id", SOURCE_ID,
            "--data-lake", str(data_lake),
            "--operator", "test-operator",
            "--dry-run",
        ]
    )
    assert result.returncode == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["total"] >= 4
    out_path = (
        data_lake / "store" / "processed" / "meetings" / SOURCE_ID
        / "reference_baselines" / "opus_reference_minutes.jsonl"
    )
    assert not out_path.exists()


def test_ingest_resolves_model_from_registry(tmp_path: Path) -> None:
    """No --model -> registry's models.opus_reference_baseline is
    stamped into every row."""
    data_lake, _ = _seed_data_lake(tmp_path)
    input_file = _write_input(tmp_path, _OPUS_INPUT)
    result = _run(
        [
            "--input-file", str(input_file),
            "--source-id", SOURCE_ID,
            "--data-lake", str(data_lake),
            "--operator", "test-operator",
        ]
    )
    assert result.returncode == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["model"] == "claude-opus-4-7"

    out_path = (
        data_lake / "store" / "processed" / "meetings" / SOURCE_ID
        / "reference_baselines" / "opus_reference_minutes.jsonl"
    )
    rows = [
        json.loads(line)
        for line in out_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert all(row["model_id"] == "claude-opus-4-7" for row in rows)


def test_ingest_accepts_wrapped_envelope_shape(tmp_path: Path) -> None:
    """Envelope shape (artifact_type + payload) and flat shape produce
    the same on-disk JSONL contents (modulo the timestamped
    ``created_at``)."""
    data_lake, _ = _seed_data_lake(tmp_path)
    wrapped = {
        "artifact_type": "meeting_minutes",
        "schema_version": "1.4.0",
        "payload": {
            "artifact_type": "meeting_minutes",
            "schema_version": "1.4.0",
            "title": "Wrapped",
            "summary": "Wrapped envelope test.",
            "decisions": ["a decision"],
            "action_items": [{"action": "a deliverable"}],
            "open_questions": [],
        },
    }
    input_file = _write_input(tmp_path, wrapped)
    result = _run(
        [
            "--input-file", str(input_file),
            "--source-id", SOURCE_ID,
            "--data-lake", str(data_lake),
            "--operator", "test-operator",
        ]
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    out_path = (
        data_lake / "store" / "processed" / "meetings" / SOURCE_ID
        / "reference_baselines" / "opus_reference_minutes.jsonl"
    )
    rows = [
        json.loads(line)
        for line in out_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 2
    etypes = sorted(r["extraction_type"] for r in rows)
    assert etypes == ["action_items", "decisions"]


def test_ingest_pair_id_uses_opus_namespace_not_codex(
    tmp_path: Path,
) -> None:
    """The frozen Opus namespace produces a pair_id that does NOT
    collide with the codex namespace for the same source/etype/index."""
    data_lake, _ = _seed_data_lake(tmp_path)
    input_file = _write_input(tmp_path, _OPUS_INPUT)
    result = _run(
        [
            "--input-file", str(input_file),
            "--source-id", SOURCE_ID,
            "--data-lake", str(data_lake),
            "--operator", "test-operator",
        ]
    )
    assert result.returncode == 0, result.stdout

    out_path = (
        data_lake / "store" / "processed" / "meetings" / SOURCE_ID
        / "reference_baselines" / "opus_reference_minutes.jsonl"
    )
    rows = [
        json.loads(line)
        for line in out_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    opus_namespace = uuid.UUID("3f1c9d8e-2b6a-5c7d-9e0f-1a2b3c4d5e6f")
    codex_namespace = uuid.UUID("4d2b8c7e-6f1a-5b3d-8e9f-2a3b4c5d6e7f")
    for row in rows:
        etype = row["extraction_type"]
        # Find the index by counting earlier rows of the same etype.
        idx = sum(
            1 for r in rows
            if r["extraction_type"] == etype
            and rows.index(r) < rows.index(row)
        )
        expected = str(
            uuid.uuid5(
                opus_namespace,
                f"opus-ref-{SOURCE_ID}-{etype}-{idx}",
            )
        )
        forbidden = str(
            uuid.uuid5(
                codex_namespace,
                f"codex-ref-{SOURCE_ID}-{etype}-{idx}",
            )
        )
        assert row["pair_id"] == expected, (
            f"pair_id={row['pair_id']!r} != expected Opus uuid5"
            f" {expected!r} for {etype} #{idx}"
        )
        assert row["pair_id"] != forbidden, (
            f"pair_id={row['pair_id']!r} collides with the codex "
            f"namespace value for the same item slot"
        )


def _read_rows(out_path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in out_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_ingest_self_heals_markdown_json_fence(tmp_path: Path) -> None:
    """Opus quirk (a): output wrapped in a ```json ... ``` markdown
    fence is parsed by slicing to the outermost braces, and ingests
    successfully."""
    data_lake, _ = _seed_data_lake(tmp_path)
    input_file = tmp_path / "fenced.json"
    input_file.write_text(
        "```json\n" + json.dumps(_OPUS_INPUT) + "\n```\n",
        encoding="utf-8",
    )
    result = _run(
        [
            "--input-file", str(input_file),
            "--source-id", SOURCE_ID,
            "--data-lake", str(data_lake),
            "--operator", "test-operator",
        ]
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    out_path = (
        data_lake / "store" / "processed" / "meetings" / SOURCE_ID
        / "reference_baselines" / "opus_reference_minutes.jsonl"
    )
    assert out_path.is_file()
    assert len(_read_rows(out_path)) >= 4


def test_ingest_self_heals_preamble_prose(tmp_path: Path) -> None:
    """Opus quirk (b): leading preamble prose before the JSON object is
    stripped by the outermost-brace slice."""
    data_lake, _ = _seed_data_lake(tmp_path)
    input_file = tmp_path / "preamble.json"
    input_file.write_text(
        "Now I have everything needed to extract the meeting minutes.\n\n"
        + json.dumps(_OPUS_INPUT),
        encoding="utf-8",
    )
    result = _run(
        [
            "--input-file", str(input_file),
            "--source-id", SOURCE_ID,
            "--data-lake", str(data_lake),
            "--operator", "test-operator",
        ]
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    out_path = (
        data_lake / "store" / "processed" / "meetings" / SOURCE_ID
        / "reference_baselines" / "opus_reference_minutes.jsonl"
    )
    assert len(_read_rows(out_path)) >= 4


def test_ingest_self_heals_hallucinated_source_chunk_id(
    tmp_path: Path,
) -> None:
    """Opus quirk (c): a ``source_chunk_id`` key invented on a
    ``technical_parameters`` item (whose schema item-type sets
    ``additionalProperties: false`` and does NOT declare the key) would
    otherwise HALT schema_violation. The recursive strip removes it and
    the ingest succeeds; the stored item_data no longer carries the key.
    """
    data_lake, _ = _seed_data_lake(tmp_path)
    payload = dict(_OPUS_INPUT)
    payload["technical_parameters"] = [
        {
            "param_id": "tp1",
            "parameter_name": "FSS uplink ERP cap",
            "value": "33 dBm/MHz",
            "source_chunk_id": "turn-42",  # hallucinated, schema rejects
        }
    ]
    input_file = _write_input(tmp_path, payload)
    result = _run(
        [
            "--input-file", str(input_file),
            "--source-id", SOURCE_ID,
            "--data-lake", str(data_lake),
            "--operator", "test-operator",
        ]
    )
    assert result.returncode == 0, (
        f"expected self-heal to succeed; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    out_path = (
        data_lake / "store" / "processed" / "meetings" / SOURCE_ID
        / "reference_baselines" / "opus_reference_minutes.jsonl"
    )
    rows = _read_rows(out_path)
    tp_rows = [r for r in rows if r["extraction_type"] == "technical_parameters"]
    assert tp_rows, "technical_parameters row must be present"
    for r in tp_rows:
        assert "source_chunk_id" not in r["item_data"], (
            "the hallucinated key must be stripped from item_data"
        )


def test_ingest_still_halts_on_real_schema_violation_after_strip(
    tmp_path: Path,
) -> None:
    """Fail-closed is preserved: stripping ``source_chunk_id`` does not
    rescue a genuinely malformed artifact. A wrong-typed required field
    (``decisions`` as an object, not a list) still HALTs
    schema_violation and writes nothing."""
    data_lake, _ = _seed_data_lake(tmp_path)
    bad = dict(_OPUS_INPUT)
    bad["decisions"] = {"source_chunk_id": "x", "text": "not a list"}
    input_file = _write_input(tmp_path, bad)
    result = _run(
        [
            "--input-file", str(input_file),
            "--source-id", SOURCE_ID,
            "--data-lake", str(data_lake),
            "--operator", "test-operator",
        ]
    )
    assert result.returncode == 1, (
        f"expected schema_violation halt; stdout={result.stdout!r}"
    )
    assert json.loads(result.stdout)["reason"] == "schema_violation"
    out_path = (
        data_lake / "store" / "processed" / "meetings" / SOURCE_ID
        / "reference_baselines" / "opus_reference_minutes.jsonl"
    )
    assert not out_path.exists()


def test_ingest_halts_invalid_json_when_no_braces(tmp_path: Path) -> None:
    """A truncated / prose-only extraction with no balanced braces halts
    ``invalid_input_json`` (exit 2), never a silent empty ingest."""
    data_lake, _ = _seed_data_lake(tmp_path)
    input_file = tmp_path / "prose.json"
    input_file.write_text(
        "I was unable to read the transcript file.", encoding="utf-8"
    )
    result = _run(
        [
            "--input-file", str(input_file),
            "--source-id", SOURCE_ID,
            "--data-lake", str(data_lake),
            "--operator", "test-operator",
        ]
    )
    assert result.returncode == 2, result.stdout
    assert json.loads(result.stdout)["reason"] == "invalid_input_json"


def test_ingest_pair_id_is_deterministic_per_input(
    tmp_path: Path,
) -> None:
    """Re-deriving build_opus_records over the same input produces the
    SAME pair_id values regardless of --operator."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    sys.path.insert(0, str(REPO_ROOT / "src"))
    import ingest_opus_baseline as iob  # noqa: WPS433

    types = iob.extraction_types()
    rows_a = iob.build_opus_records(
        payload=_OPUS_INPUT,
        types=types,
        source_id=SOURCE_ID,
        source_artifact_id="not-actually-used-for-pair-id",
        model="claude-opus-4-7",
        meeting_date=None,
        created_at="1970-01-01T00:00:00+00:00",
    )
    rows_b = iob.build_opus_records(
        payload=_OPUS_INPUT,
        types=types,
        source_id=SOURCE_ID,
        source_artifact_id="not-actually-used-for-pair-id",
        model="claude-opus-4-7",
        meeting_date=None,
        created_at="1970-01-01T00:00:00+00:00",
    )
    assert [r["pair_id"] for r in rows_a] == [r["pair_id"] for r in rows_b]
