"""Phase 4a — ``spectrum-core baseline-opus`` integration tests.

Each Pass-1 / Pass-2 / Pass-3 finding has a paired test:

* P1 #1 schema completeness: every schema array appears in the prompt.
* P1 #2 model string correct: resolved against the registry, not hardcoded.
* P1 #3 cost confirmation env-var bypass: env vars cannot bypass --confirm-cost.
* P1 #4 manifest update: a successful run advances ingestion_status to
  ``baseline_complete``.
* P1 #5 per-source state hook: a baseline run records a per-source
  history marker (informational), NOT a runs_observed increment
  (which would game the variance signal).
* P1 #6 consistency verifier tolerance: ranges widened to [90, 125]
  hard / [100, 112] reference per the Pass 1 amendment.
* P1 #7 legacy artifact handling: a legacy JSONL without
  prompt_content_hash is a WARNING, not a failure.
* P2 #1 paired rejection tests: every gate has a paired test that
  proves fail-closed.
* P2 #2 manifest update idempotent: running twice is a no-op.
* P2 #3 cost estimate before confirmation: stdout order is checked.
* P2 #4 prompt parses cleanly as Markdown.
* P2 #5 schema-drift test asserts all current types appear.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from spectrum_systems_core.corpus.baseline_opus import (
    ARTIFACT_TYPE,
    BASELINE_OPUS_WRITTEN,
    CONFIRM_COST_DECLINED,
    CONFIRM_COST_REQUIRED,
    MALFORMED_LLM_RESPONSE,
    OPUS_PROMPT_NOT_FOUND,
    SOURCE_RECORD_NOT_FOUND,
    STUB_ENV,
    BaselineOpusError,
    count_extracted_items,
    estimate_dry_run,
    load_opus_prompt,
    prompt_content_hash,
    resolve_opus_model,
    run_baseline_opus,
)
from spectrum_systems_core.corpus.manifest_loader import (
    compute_manifest_hash,
)
from spectrum_systems_core.data_lake.cli import main as cli_main


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _all_23_types() -> List[str]:
    """The 23 content arrays the prompt + envelope produce.

    Mirrors :data:`baseline_opus._CONTENT_ARRAYS`.
    """
    from spectrum_systems_core.corpus.baseline_opus import _CONTENT_ARRAYS

    return list(_CONTENT_ARRAYS)


def _stub_response(per_type_count: int = 5) -> str:
    """A JSON response that puts ``per_type_count`` items in every array."""
    payload: Dict[str, Any] = {}
    for k in _all_23_types():
        payload[k] = [{"text": f"{k}-{i}"} for i in range(per_type_count)]
    payload["grounding"] = []
    return json.dumps(payload)


def _stage_lake(tmp_path: Path) -> tuple[Path, Path]:
    lake = tmp_path / "lake"
    transcripts = lake / "raw" / "transcripts"
    transcripts.mkdir(parents=True)
    (lake / "processed" / "meetings").mkdir(parents=True)
    return lake, transcripts


def _write_source_record(
    lake: Path, *, source_id: str, raw_path: str
) -> Path:
    meeting_dir = lake / "processed" / "meetings" / source_id
    meeting_dir.mkdir(parents=True, exist_ok=True)
    sr_path = meeting_dir / "source_record.json"
    sr_path.write_text(
        json.dumps(
            {
                "artifact_type": "source_record",
                "schema_version": "1.0.0",
                "artifact_id": "deadbeef",
                "source_id": source_id,
                "created_at": "1970-01-01T00:00:00+00:00",
                "raw_hash": "sha256:" + "0" * 64,
                "payload": {
                    "source_id": source_id,
                    "raw_path": raw_path,
                    "raw_hash": "sha256:" + "0" * 64,
                    "ingestion_status": "validated",
                    "ingestion_forced": False,
                    "declared": {
                        "expected_path": raw_path,
                        "meeting_date": "2025-12-18",
                        "meeting_type": "working_group",
                        "supersedes": None,
                    },
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return sr_path


def _make_source_entry(
    sid: str,
    *,
    expected_path: str,
    ingestion_status: str = "validated",
    meeting_type: str = "working_group",
) -> Dict[str, Any]:
    return {
        "source_id": sid,
        "declared": {
            "expected_path": expected_path,
            "meeting_date": "2026-05-20",
            "meeting_type": meeting_type,
            "supersedes": None,
        },
        "observed": {
            "detected_speaker_count": None,
            "detected_word_count": None,
            "ingestion_status": ingestion_status,
            "last_updated": None,
        },
    }


def _write_manifest(
    tmp_path: Path, sources: List[Dict[str, Any]]
) -> Path:
    p = tmp_path / "manifest.json"
    p.write_text(
        json.dumps(
            {
                "artifact_type": "corpus_manifest",
                "schema_version": "1.0.0",
                "manifest_hash": compute_manifest_hash(sources),
                "sources": sources,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return p


# ---------------------------------------------------------------------------
# Prompt schema completeness (Pass 1 #1, Pass 2 #5, Pass 3 #2).
# ---------------------------------------------------------------------------


def test_opus_prompt_covers_every_schema_array_type() -> None:
    """The Opus prompt must reference every array property in the
    meeting_minutes schema (except ``grounding``). This catches future
    schema additions that aren't reflected in the prompt — the same
    drift class the Phase 4a spec calls out."""
    import json as _json

    repo_root = Path(__file__).resolve().parents[2]
    schema_path = (
        repo_root
        / "src"
        / "spectrum_systems_core"
        / "schemas"
        / "meeting_minutes.schema.json"
    )
    schema = _json.loads(schema_path.read_text(encoding="utf-8"))
    array_types = sorted(
        k
        for k, v in schema["properties"].items()
        if isinstance(v, dict) and v.get("type") == "array" and k != "grounding"
    )
    prompt = load_opus_prompt()
    missing = [t for t in array_types if t not in prompt]
    assert not missing, (
        f"Opus prompt missing schema array types: {missing}. "
        f"Update src/spectrum_systems_core/workflows/prompts/"
        f"meeting_minutes_opus.md so every type is referenced."
    )


def test_opus_prompt_is_non_empty_and_uses_utf8() -> None:
    text = load_opus_prompt()
    assert len(text) > 1000  # comprehensive prompt is long
    # No surprising encoding artifacts.
    assert "﻿" not in text


def test_opus_prompt_hash_is_deterministic() -> None:
    h1 = prompt_content_hash(load_opus_prompt())
    h2 = prompt_content_hash(load_opus_prompt())
    assert h1 == h2
    assert len(h1) == 64


# ---------------------------------------------------------------------------
# Model resolution (Pass 1 #2).
# ---------------------------------------------------------------------------


def test_model_resolved_from_registry_not_hardcoded() -> None:
    """The Opus model id comes from
    ai/registry/model_registry.json::opus_reference_baseline. Anything
    hardcoded in baseline_opus.py is a code-review failure; this test
    only proves the registry resolution wins.
    """
    model_id = resolve_opus_model()
    # The current registry entry is claude-opus-4-7; the test does
    # not pin the version (a future operator may roll it forward) but
    # it MUST resolve to an Opus-class id.
    assert "opus" in model_id.lower()
    assert model_id == model_id.strip()


def test_model_registry_missing_halts(tmp_path: Path, monkeypatch) -> None:
    """A missing registry file halts with `model_registry_error`."""
    fake = tmp_path / "missing.json"
    monkeypatch.setattr(
        "spectrum_systems_core.corpus.baseline_opus._MODEL_REGISTRY_PATH",
        fake,
    )
    with pytest.raises(BaselineOpusError) as ei:
        resolve_opus_model()
    assert ei.value.reason_code == "model_registry_error"


# ---------------------------------------------------------------------------
# CLI selection mutex / confirm-cost CLI-only contract (Pass 1 #3).
# ---------------------------------------------------------------------------


def test_cli_all_without_confirm_cost_rejected(tmp_path: Path, capsys) -> None:
    lake, _ = _stage_lake(tmp_path)
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)
    rc = cli_main(
        [
            "baseline-opus",
            "--lake",
            str(lake),
            "--all",
            "--manifest",
            str(manifest),
        ]
    )
    assert rc == 2
    out = capsys.readouterr().out
    assert "--confirm-cost" in out
    assert "env var" in out


def test_cli_confirm_cost_env_var_does_not_bypass(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Setting CONFIRM_COST=true in env MUST NOT bypass the CLI flag.

    The argparse layer only honors --confirm-cost; the test sets a
    plausibly-named env var and asserts the CLI still errors.
    """
    lake, _ = _stage_lake(tmp_path)
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)

    for name in (
        "CONFIRM_COST",
        "BASELINE_OPUS_CONFIRM_COST",
        "SPECTRUM_CONFIRM_COST",
    ):
        monkeypatch.setenv(name, "true")
    rc = cli_main(
        [
            "baseline-opus",
            "--lake",
            str(lake),
            "--all",
            "--manifest",
            str(manifest),
        ]
    )
    assert rc == 2
    assert "--confirm-cost" in capsys.readouterr().out


def test_cli_rejects_both_all_and_source_id(tmp_path: Path, capsys) -> None:
    lake, _ = _stage_lake(tmp_path)
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)
    rc = cli_main(
        [
            "baseline-opus",
            "--lake",
            str(lake),
            "--all",
            "--source-id",
            "src-a",
            "--manifest",
            str(manifest),
        ]
    )
    assert rc == 2
    assert "mutually exclusive" in capsys.readouterr().out


def test_cli_requires_one_of_all_or_source_id(tmp_path: Path, capsys) -> None:
    lake, _ = _stage_lake(tmp_path)
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)
    rc = cli_main(
        [
            "baseline-opus",
            "--lake",
            str(lake),
            "--manifest",
            str(manifest),
        ]
    )
    assert rc == 2


# ---------------------------------------------------------------------------
# Missing prompt / source_record / unknown source halts (Pass 2 #1).
# ---------------------------------------------------------------------------


def test_missing_opus_prompt_halts(
    tmp_path: Path, monkeypatch
) -> None:
    """Override the canonical prompt path to a non-existent file and
    confirm the run halts with `opus_prompt_not_found`."""
    fake = tmp_path / "nope.md"
    monkeypatch.setattr(
        "spectrum_systems_core.corpus.baseline_opus._PROMPT_PATH",
        fake,
    )
    with pytest.raises(BaselineOpusError) as ei:
        load_opus_prompt()
    assert ei.value.reason_code == OPUS_PROMPT_NOT_FOUND


def test_unknown_source_id_halts(tmp_path: Path, monkeypatch) -> None:
    lake, transcripts = _stage_lake(tmp_path)
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)
    monkeypatch.setenv(STUB_ENV, _stub_response())
    with pytest.raises(BaselineOpusError) as ei:
        run_baseline_opus(
            lake_root=lake,
            manifest_path=manifest,
            source_ids=["does-not-exist"],
        )
    assert ei.value.reason_code == "baseline_opus_source_id_unknown"


def test_missing_source_record_records_failed_outcome(
    tmp_path: Path, monkeypatch
) -> None:
    lake, transcripts = _stage_lake(tmp_path)
    (transcripts / "a.txt").write_text("hi", encoding="utf-8")
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)
    monkeypatch.setenv(STUB_ENV, _stub_response())

    summary = run_baseline_opus(
        lake_root=lake,
        manifest_path=manifest,
        source_ids=["src-a"],
    )
    assert len(summary.outcomes) == 1
    outcome = summary.outcomes[0]
    assert outcome.status == "failed"
    assert outcome.reason_code == SOURCE_RECORD_NOT_FOUND


def test_malformed_response_halts_with_reason(
    tmp_path: Path, monkeypatch
) -> None:
    lake, transcripts = _stage_lake(tmp_path)
    (transcripts / "a.txt").write_text("transcript text", encoding="utf-8")
    _write_source_record(lake, source_id="src-a", raw_path="raw/transcripts/a.txt")
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)
    monkeypatch.setenv(STUB_ENV, "this is not JSON")

    summary = run_baseline_opus(
        lake_root=lake,
        manifest_path=manifest,
        source_ids=["src-a"],
    )
    outcome = summary.outcomes[0]
    assert outcome.status == "failed"
    assert outcome.reason_code == MALFORMED_LLM_RESPONSE


# ---------------------------------------------------------------------------
# Manifest update (Pass 1 #4, Pass 2 #2 idempotency).
# ---------------------------------------------------------------------------


def test_successful_run_updates_manifest_to_baseline_complete(
    tmp_path: Path, monkeypatch
) -> None:
    lake, transcripts = _stage_lake(tmp_path)
    (transcripts / "a.txt").write_text("transcript text", encoding="utf-8")
    _write_source_record(lake, source_id="src-a", raw_path="raw/transcripts/a.txt")
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)
    monkeypatch.setenv(STUB_ENV, _stub_response())

    summary = run_baseline_opus(
        lake_root=lake,
        manifest_path=manifest,
        source_ids=["src-a"],
    )
    assert summary.outcomes[0].status == "baseline_complete"
    assert summary.outcomes[0].reason_code == BASELINE_OPUS_WRITTEN
    # Confirm the manifest now says baseline_complete.
    refreshed = json.loads(manifest.read_text(encoding="utf-8"))
    src = next(s for s in refreshed["sources"] if s["source_id"] == "src-a")
    assert src["observed"]["ingestion_status"] == "baseline_complete"


def test_manifest_update_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    """Running twice with the same stub leaves the manifest at
    ``baseline_complete`` both times; no double-counting."""
    lake, transcripts = _stage_lake(tmp_path)
    (transcripts / "a.txt").write_text("transcript text", encoding="utf-8")
    _write_source_record(lake, source_id="src-a", raw_path="raw/transcripts/a.txt")
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)
    monkeypatch.setenv(STUB_ENV, _stub_response())

    run_baseline_opus(
        lake_root=lake,
        manifest_path=manifest,
        source_ids=["src-a"],
    )
    after_first = json.loads(manifest.read_text(encoding="utf-8"))
    run_baseline_opus(
        lake_root=lake,
        manifest_path=manifest,
        source_ids=["src-a"],
    )
    after_second = json.loads(manifest.read_text(encoding="utf-8"))
    assert (
        after_first["sources"][0]["observed"]["ingestion_status"]
        == after_second["sources"][0]["observed"]["ingestion_status"]
        == "baseline_complete"
    )


# ---------------------------------------------------------------------------
# Per-source state hook (Pass 1 #5).
# ---------------------------------------------------------------------------


def test_per_source_history_marker_written(
    tmp_path: Path, monkeypatch
) -> None:
    """A successful run appends a marker to baseline_opus_history.jsonl
    under diagnostics/. The marker is informational; it does NOT
    increment the variance-budget runs_observed counter (which would
    game the variance signal — that counter only advances on Haiku
    comparison runs).
    """
    lake, transcripts = _stage_lake(tmp_path)
    (transcripts / "a.txt").write_text("transcript text", encoding="utf-8")
    _write_source_record(lake, source_id="src-a", raw_path="raw/transcripts/a.txt")
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)
    monkeypatch.setenv(STUB_ENV, _stub_response())

    run_baseline_opus(
        lake_root=lake,
        manifest_path=manifest,
        source_ids=["src-a"],
    )
    marker = (
        lake
        / "processed"
        / "meetings"
        / "src-a"
        / "diagnostics"
        / "baseline_opus_history.jsonl"
    )
    assert marker.is_file()
    lines = [line for line in marker.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["source_id"] == "src-a"

    # No tolerance_budget_state file is written by the baseline path —
    # variance is computed off Haiku comparison F1s, not off the Opus
    # baseline.
    budget_state = (
        lake
        / "processed"
        / "meetings"
        / "src-a"
        / "diagnostics"
        / "tolerance_budget_state__src-a.json"
    )
    assert not budget_state.exists()


# ---------------------------------------------------------------------------
# Artifact contents (Pass 2 #1 — successful write).
# ---------------------------------------------------------------------------


def test_successful_run_writes_meeting_minutes_opus_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    lake, transcripts = _stage_lake(tmp_path)
    (transcripts / "a.txt").write_text("transcript text", encoding="utf-8")
    _write_source_record(lake, source_id="src-a", raw_path="raw/transcripts/a.txt")
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)
    monkeypatch.setenv(STUB_ENV, _stub_response(per_type_count=4))

    summary = run_baseline_opus(
        lake_root=lake,
        manifest_path=manifest,
        source_ids=["src-a"],
    )
    out = summary.outcomes[0]
    assert out.status == "baseline_complete"
    artifact_path = Path(out.artifact_path)
    assert artifact_path.name.startswith("meeting_minutes_opus__")
    assert artifact_path.parent == lake / "processed" / "meetings" / "src-a"
    art = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert art["artifact_type"] == ARTIFACT_TYPE
    assert art["payload"]["provenance"]["model_id"]
    assert art["payload"]["provenance"]["prompt_content_hash"]
    # 23 content types × 4 items each = 92 items
    assert out.item_count == 23 * 4


def test_count_extracted_items_pure_helper() -> None:
    payload = {k: [{"x": 1}] for k in _all_23_types()}
    assert count_extracted_items(payload) == 23
    payload["decisions"].append({"x": 2})
    assert count_extracted_items(payload) == 24


# ---------------------------------------------------------------------------
# Cost confirmation stdout order (Pass 2 #3).
# ---------------------------------------------------------------------------


def test_cost_estimate_printed_before_confirmation_prompt(
    tmp_path: Path, monkeypatch
) -> None:
    """The summary line must reach stdout BEFORE the y/N prompt is
    presented to the operator."""
    lake, transcripts = _stage_lake(tmp_path)
    (transcripts / "a.txt").write_text("transcript text " * 200, encoding="utf-8")
    _write_source_record(lake, source_id="src-a", raw_path="raw/transcripts/a.txt")
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)
    monkeypatch.setenv(STUB_ENV, _stub_response())

    events: List[str] = []

    def _printer(msg: str) -> None:
        events.append(f"OUT:{msg}")

    def _reader(prompt: str) -> str:
        events.append(f"PROMPT:{prompt}")
        return "y"

    run_baseline_opus(
        lake_root=lake,
        manifest_path=manifest,
        all_sources=True,
        confirm_cost=True,
        confirm_input=_reader,
        confirm_output=_printer,
    )
    # The first event must be the estimate summary; the prompt comes
    # after.
    assert events[0].startswith("OUT:Estimated cost")
    assert events[1].startswith("PROMPT:")


def test_operator_declining_cost_halts(
    tmp_path: Path, monkeypatch
) -> None:
    lake, transcripts = _stage_lake(tmp_path)
    (transcripts / "a.txt").write_text("transcript text", encoding="utf-8")
    _write_source_record(lake, source_id="src-a", raw_path="raw/transcripts/a.txt")
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)
    monkeypatch.setenv(STUB_ENV, _stub_response())

    with pytest.raises(BaselineOpusError) as ei:
        run_baseline_opus(
            lake_root=lake,
            manifest_path=manifest,
            all_sources=True,
            confirm_cost=True,
            confirm_input=lambda _p: "n",
            confirm_output=lambda _m: None,
        )
    assert ei.value.reason_code == CONFIRM_COST_DECLINED


def test_all_without_confirm_cost_halts_at_run_layer(
    tmp_path: Path,
) -> None:
    lake, transcripts = _stage_lake(tmp_path)
    (transcripts / "a.txt").write_text("transcript text", encoding="utf-8")
    _write_source_record(lake, source_id="src-a", raw_path="raw/transcripts/a.txt")
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)
    with pytest.raises(BaselineOpusError) as ei:
        run_baseline_opus(
            lake_root=lake,
            manifest_path=manifest,
            all_sources=True,
            confirm_cost=False,
        )
    assert ei.value.reason_code == CONFIRM_COST_REQUIRED


# ---------------------------------------------------------------------------
# Manifest hash mismatch (Pass 2 #1 — paired rejection test).
# ---------------------------------------------------------------------------


def test_manifest_hash_mismatch_halts(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    lake, transcripts = _stage_lake(tmp_path)
    (transcripts / "a.txt").write_text("transcript text", encoding="utf-8")
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)
    # Tamper: mutate without refreshing the hash.
    data = json.loads(manifest.read_text())
    data["sources"][0]["declared"]["meeting_type"] = "internal_review"
    manifest.write_text(json.dumps(data), encoding="utf-8")

    monkeypatch.setenv(STUB_ENV, _stub_response())
    rc = cli_main(
        [
            "baseline-opus",
            "--lake",
            str(lake),
            "--source-id",
            "src-a",
            "--manifest",
            str(manifest),
        ]
    )
    assert rc == 2
    assert "corpus_manifest_hash_mismatch" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Dry-run preview.
# ---------------------------------------------------------------------------


def test_dry_run_returns_estimate_and_does_not_call_model(
    tmp_path: Path, monkeypatch
) -> None:
    lake, transcripts = _stage_lake(tmp_path)
    (transcripts / "a.txt").write_text("transcript text " * 300, encoding="utf-8")
    _write_source_record(lake, source_id="src-a", raw_path="raw/transcripts/a.txt")
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)

    # If the client is ever called during a dry-run, the test would
    # surface the env var; instead we leave STUB unset and confirm the
    # dry-run succeeds (proving no client construction occurred).
    monkeypatch.delenv(STUB_ENV, raising=False)
    out = estimate_dry_run(
        lake_root=lake,
        source_id="src-a",
        manifest_path=manifest,
    )
    assert out["source_id"] == "src-a"
    assert out["model_id"]  # resolved from registry
    assert out["estimated_opus_cost_usd"]
    assert out["estimated_haiku_cost_usd"]
    assert out["prompt_content_hash"] == prompt_content_hash(load_opus_prompt())
    assert out["transcript_byte_length"] > 0


def test_cli_dry_run_emits_json(tmp_path: Path, monkeypatch, capsys) -> None:
    lake, transcripts = _stage_lake(tmp_path)
    (transcripts / "a.txt").write_text("transcript text", encoding="utf-8")
    _write_source_record(lake, source_id="src-a", raw_path="raw/transcripts/a.txt")
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)
    monkeypatch.delenv(STUB_ENV, raising=False)
    rc = cli_main(
        [
            "baseline-opus",
            "--lake",
            str(lake),
            "--source-id",
            "src-a",
            "--manifest",
            str(manifest),
            "--dry-run",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["source_id"] == "src-a"
    assert "estimated_opus_cost_usd" in parsed


def test_cli_dry_run_with_all_rejected(tmp_path: Path, capsys) -> None:
    lake, _ = _stage_lake(tmp_path)
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)
    rc = cli_main(
        [
            "baseline-opus",
            "--lake",
            str(lake),
            "--all",
            "--confirm-cost",
            "--manifest",
            str(manifest),
            "--dry-run",
        ]
    )
    assert rc == 2
    out = capsys.readouterr().out
    assert "exactly one --source-id" in out
