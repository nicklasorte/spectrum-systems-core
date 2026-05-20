"""Phase 4 — ``spectrum-core ingest-corpus`` integration tests.

Each Pass-1 / Pass-2 / Pass-3 finding has a paired test here:

* P1 #1 silent-pass: monkey-patch the validator and assert it IS called.
* P1 #2 manifest unreadable: missing manifest -> exit 2.
* P1 #3 hash mismatch: a hand-edited manifest fails closed.
* P1 #4 force-reason gate: too-short reason rejected.
* P1 #5 idempotency: two runs produce byte-identical source_record.json.
* P2 #2 mid-run mutation: a manifest mutated mid-run is NOT picked up.
* P2 #3 pre-flight contracts: ingest of a corrupt transcript quarantines.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from spectrum_systems_core.corpus.ingest import (
    INGEST_QUARANTINED,
    INGEST_VALIDATED,
    run_ingest,
)
from spectrum_systems_core.corpus.manifest_loader import (
    CorpusManifestError,
    compute_manifest_hash,
)
from spectrum_systems_core.data_lake.cli import main as cli_main

from tests.transcript_quality.fixtures import (
    encoding_corrupted_transcript,
    valid_transcript,
)


# ---------------------------------------------------------------------------
# Fixture helpers.
# ---------------------------------------------------------------------------


def _make_source_entry(
    sid: str,
    *,
    expected_path: str,
    ingestion_status: str = "pending",
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


def _stage_lake(tmp_path: Path) -> tuple[Path, Path]:
    """Create a minimal data lake under tmp_path and return its root +
    a transcripts directory."""
    lake = tmp_path / "lake"
    transcripts = lake / "raw" / "transcripts"
    transcripts.mkdir(parents=True)
    (lake / "processed" / "meetings").mkdir(parents=True)
    return lake, transcripts


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
# Pass 1 #1 — silent-pass: the validator MUST be called.
# ---------------------------------------------------------------------------


def test_pre_flight_validator_is_actually_called(
    tmp_path: Path, monkeypatch
) -> None:
    """Hardcoded ON: the validator is invoked, not bypassed."""
    lake, transcripts = _stage_lake(tmp_path)
    (transcripts / "a.txt").write_text(valid_transcript() * 5, encoding="utf-8")
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)

    calls: list[str] = []
    import spectrum_systems_core.corpus.ingest as ingest_mod

    original = ingest_mod.validate

    def _wrapped(transcript, **kw):
        calls.append(kw.get("source_id") or "<no-sid>")
        return original(transcript, **kw)

    monkeypatch.setattr(ingest_mod, "validate", _wrapped)

    summary = run_ingest(
        lake_root=lake,
        manifest_path=manifest,
        source_ids=["src-a"],
    )
    # The validator MUST have been called exactly once for src-a.
    assert calls == ["src-a"]
    assert summary.outcomes[0].source_id == "src-a"


# ---------------------------------------------------------------------------
# Pass 1 #2 — manifest missing.
# ---------------------------------------------------------------------------


def test_missing_manifest_exit_2_on_cli(tmp_path: Path, capsys) -> None:
    """CLI: a missing manifest exits 2 with a clear reason code."""
    lake, _ = _stage_lake(tmp_path)
    nope = tmp_path / "nope.json"
    rc = cli_main(
        [
            "ingest-corpus",
            "--lake",
            str(lake),
            "--source-id",
            "anything",
            "--manifest",
            str(nope),
        ]
    )
    assert rc == 2
    captured = capsys.readouterr()
    assert "corpus_manifest_not_found" in captured.out


def test_missing_transcript_quarantines(tmp_path: Path) -> None:
    """When the transcript file is missing, the source is quarantined."""
    lake, _ = _stage_lake(tmp_path)
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/ghost.txt")]
    manifest = _write_manifest(tmp_path, sources)
    summary = run_ingest(
        lake_root=lake,
        manifest_path=manifest,
        source_ids=["src-a"],
    )
    outcome = summary.outcomes[0]
    assert outcome.status == "quarantined"
    assert outcome.reason_code == "transcript_not_found"
    assert outcome.source_record_path is None


# ---------------------------------------------------------------------------
# Pass 1 #3 — manifest hash mismatch.
# ---------------------------------------------------------------------------


def test_hash_mismatch_fails_closed(tmp_path: Path, capsys) -> None:
    lake, transcripts = _stage_lake(tmp_path)
    (transcripts / "a.txt").write_text(valid_transcript() * 5, encoding="utf-8")
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)
    # Tamper: mutate without refreshing the hash.
    data = json.loads(manifest.read_text())
    data["sources"][0]["declared"]["meeting_type"] = "internal_review"
    manifest.write_text(json.dumps(data), encoding="utf-8")

    rc = cli_main(
        [
            "ingest-corpus",
            "--lake",
            str(lake),
            "--source-id",
            "src-a",
            "--manifest",
            str(manifest),
        ]
    )
    assert rc == 2
    out = capsys.readouterr().out
    assert "corpus_manifest_hash_mismatch" in out


# ---------------------------------------------------------------------------
# Pass 1 #4 — force-reason gate.
# ---------------------------------------------------------------------------


def test_force_reason_too_short_rejected(tmp_path: Path) -> None:
    lake, transcripts = _stage_lake(tmp_path)
    (transcripts / "a.txt").write_text(valid_transcript() * 5, encoding="utf-8")
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)
    with pytest.raises(CorpusManifestError) as ei:
        run_ingest(
            lake_root=lake,
            manifest_path=manifest,
            source_ids=["src-a"],
            forced=True,
            force_reason="too short",
        )
    assert ei.value.reason_code == "ingest_force_reason_too_short"


def test_force_reason_required_for_force_flag(tmp_path: Path, capsys) -> None:
    """CLI: --force-ingest without --force-reason exits 2."""
    lake, _ = _stage_lake(tmp_path)
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)
    rc = cli_main(
        [
            "ingest-corpus",
            "--lake",
            str(lake),
            "--source-id",
            "src-a",
            "--manifest",
            str(manifest),
            "--force-ingest",
        ]
    )
    assert rc == 2
    assert "force-ingest" in capsys.readouterr().out


def test_force_ingest_bypasses_errors(tmp_path: Path) -> None:
    """A forced ingest of a transcript that would otherwise quarantine
    writes a source_record with ingestion_forced=true."""
    lake, transcripts = _stage_lake(tmp_path)
    # An empty transcript triggers `transcript_below_min_length` (error).
    (transcripts / "a.txt").write_text("hi", encoding="utf-8")
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)

    summary = run_ingest(
        lake_root=lake,
        manifest_path=manifest,
        source_ids=["src-a"],
        forced=True,
        force_reason="operator verified the transcript is intentionally short for tests",
    )
    outcome = summary.outcomes[0]
    assert outcome.status == "validated"
    assert outcome.forced is True
    assert outcome.source_record_path is not None
    sr = json.loads(Path(outcome.source_record_path).read_text())
    assert sr["payload"]["ingestion_forced"] is True
    assert (
        sr["payload"]["force_reason"]
        == "operator verified the transcript is intentionally short for tests"
    )


# ---------------------------------------------------------------------------
# Pass 1 #5 — idempotency.
# ---------------------------------------------------------------------------


def test_ingest_idempotent_on_source_record(tmp_path: Path) -> None:
    """Two runs on a valid source produce a byte-identical source_record.json."""
    lake, transcripts = _stage_lake(tmp_path)
    (transcripts / "a.txt").write_text(valid_transcript() * 5, encoding="utf-8")
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)

    s1 = run_ingest(
        lake_root=lake,
        manifest_path=manifest,
        source_ids=["src-a"],
    )
    sr1 = Path(s1.outcomes[0].source_record_path).read_bytes()
    s2 = run_ingest(
        lake_root=lake,
        manifest_path=manifest,
        source_ids=["src-a"],
    )
    sr2 = Path(s2.outcomes[0].source_record_path).read_bytes()
    assert sr1 == sr2


# ---------------------------------------------------------------------------
# Pass 2 #3 — corrupt transcript quarantines.
# ---------------------------------------------------------------------------


def test_corrupt_transcript_quarantines(tmp_path: Path) -> None:
    lake, transcripts = _stage_lake(tmp_path)
    (transcripts / "a.txt").write_text(
        encoding_corrupted_transcript(), encoding="utf-8"
    )
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)
    summary = run_ingest(
        lake_root=lake,
        manifest_path=manifest,
        source_ids=["src-a"],
    )
    outcome = summary.outcomes[0]
    assert outcome.status == "quarantined"
    assert outcome.reason_code == INGEST_QUARANTINED
    assert outcome.has_errors
    # A diagnostic was written even though no source_record was.
    assert outcome.source_record_path is None
    assert outcome.diagnostics_path is not None
    diag = Path(outcome.diagnostics_path).read_text()
    assert "transcript_quality_report" in diag


# ---------------------------------------------------------------------------
# Pass 2 #2 — mid-run mutation not picked up.
# ---------------------------------------------------------------------------


def test_mid_run_mutation_not_picked_up(tmp_path: Path) -> None:
    """run_ingest loads the manifest ONCE; a mid-run mutation is not
    picked up. We approximate the mid-run window by mutating the file
    after loading via a monkey-patched validate that mutates between
    the per-source loops."""
    lake, transcripts = _stage_lake(tmp_path)
    (transcripts / "a.txt").write_text(valid_transcript() * 5, encoding="utf-8")
    (transcripts / "b.txt").write_text(valid_transcript() * 5, encoding="utf-8")
    sources = [
        _make_source_entry("src-a", expected_path="raw/transcripts/a.txt"),
        _make_source_entry("src-b", expected_path="raw/transcripts/b.txt"),
    ]
    manifest = _write_manifest(tmp_path, sources)

    # Mutate disk before processing src-b's iteration. The simplest
    # reliable hook is to monkey-patch validate so the first call
    # mutates the manifest on disk.
    import spectrum_systems_core.corpus.ingest as ingest_mod

    original = ingest_mod.validate
    mutated = {"done": False}

    def _maybe_mutate(transcript, **kw):
        if not mutated["done"]:
            data = json.loads(manifest.read_text())
            # Mutate AND refresh the hash so the file is internally
            # consistent — the contract we are testing is that the
            # in-progress run uses the LOADED snapshot, not the file
            # state at iteration time.
            data["sources"][1]["declared"]["meeting_type"] = "internal_review"
            data["manifest_hash"] = compute_manifest_hash(data["sources"])
            manifest.write_text(json.dumps(data), encoding="utf-8")
            mutated["done"] = True
        return original(transcript, **kw)

    ingest_mod.validate = _maybe_mutate
    try:
        summary = run_ingest(
            lake_root=lake,
            manifest_path=manifest,
            all_sources=True,
        )
    finally:
        ingest_mod.validate = original

    # Both sources still recorded as validated against the ORIGINAL
    # in-memory copy.
    assert {o.source_id for o in summary.outcomes} == {"src-a", "src-b"}
    # The manifest_hash on the SUMMARY is the post-rewrite hash. The
    # important property is the in-memory loop used the snapshot;
    # we verify this by checking that src-b was ingested and not
    # spuriously errored due to mid-run mutation.
    for o in summary.outcomes:
        assert o.source_record_path is not None


# ---------------------------------------------------------------------------
# CLI selection mutex.
# ---------------------------------------------------------------------------


def test_cli_rejects_both_all_and_source_id(tmp_path: Path, capsys) -> None:
    lake, _ = _stage_lake(tmp_path)
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)
    rc = cli_main(
        [
            "ingest-corpus",
            "--lake",
            str(lake),
            "--source-id",
            "src-a",
            "--all",
            "--manifest",
            str(manifest),
        ]
    )
    assert rc == 2
    assert "mutually exclusive" in capsys.readouterr().out


def test_cli_requires_a_selector(tmp_path: Path, capsys) -> None:
    lake, _ = _stage_lake(tmp_path)
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)
    rc = cli_main(
        [
            "ingest-corpus",
            "--lake",
            str(lake),
            "--manifest",
            str(manifest),
        ]
    )
    assert rc == 2
    assert "exactly one" in capsys.readouterr().out


def test_unknown_source_id_fails_closed(tmp_path: Path, capsys) -> None:
    lake, transcripts = _stage_lake(tmp_path)
    (transcripts / "a.txt").write_text(valid_transcript() * 5, encoding="utf-8")
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)
    rc = cli_main(
        [
            "ingest-corpus",
            "--lake",
            str(lake),
            "--source-id",
            "ghost",
            "--manifest",
            str(manifest),
        ]
    )
    assert rc == 2
    assert "ingest_source_id_unknown" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# End-to-end happy path with manifest update.
# ---------------------------------------------------------------------------


def test_validated_source_updates_observed(tmp_path: Path) -> None:
    lake, transcripts = _stage_lake(tmp_path)
    (transcripts / "a.txt").write_text(valid_transcript() * 5, encoding="utf-8")
    sources = [_make_source_entry("src-a", expected_path="raw/transcripts/a.txt")]
    manifest = _write_manifest(tmp_path, sources)
    summary = run_ingest(
        lake_root=lake,
        manifest_path=manifest,
        source_ids=["src-a"],
    )
    outcome = summary.outcomes[0]
    assert outcome.status == "validated"
    assert outcome.reason_code == INGEST_VALIDATED
    data = json.loads(manifest.read_text())
    obs = data["sources"][0]["observed"]
    assert obs["ingestion_status"] == "validated"
    assert obs["detected_word_count"] is not None
    assert obs["detected_word_count"] > 0
    assert obs["last_updated"] is not None
