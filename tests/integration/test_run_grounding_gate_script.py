"""Integration contract test for ``scripts/run_grounding_gate.py``.

Drives the script as a real subprocess against a temp data-lake (no
network, no API key). Defends the script's trust properties:

* artifact_type is always ``grounding_gate_*`` (never ``artifact_kind``);
* a clean input produces ``passed=True`` and writes all three artifacts;
* a single ungrounded item produces ``passed=False``, the ungrounded
  item lands in the JSONL audit file, the grounded artifact STILL
  exists with the item dropped;
* ``--disable-grounding-gate`` writes ONLY the bypass record (no
  grounded / ungrounded artifacts) with operator + timestamp;
* missing transcript exits 2 with no side effects;
* the JSON is canonical (sorted keys + trailing newline) so two
  runs over the same input produce byte-identical artifacts;
* the script discovers the most recent meeting_minutes__*.json when
  ``--extraction-artifact`` is omitted.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "run_grounding_gate.py"

SOURCE_ID = "fixture-source-id"


def _write_chunks_jsonl(path: Path, chunks: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"chunk_id": cid, "text": text}, sort_keys=True)
        for cid, text in sorted(chunks.items())
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_minimal_data_lake(
    tmp_path: Path,
    *,
    extraction_payload: dict,
    transcript_text: str = "so we will determine the band plan today",
    chunks: dict[str, str] | None = None,
    extraction_filename: str = "meeting_minutes__abc.json",
    wrap_envelope: bool = False,
) -> Path:
    data_lake = tmp_path / "data-lake"
    processed = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    raw = data_lake / "store" / "raw" / "meetings" / SOURCE_ID
    processed.mkdir(parents=True)
    raw.mkdir(parents=True)
    (raw / "source.txt").write_text(transcript_text, encoding="utf-8")
    if chunks:
        _write_chunks_jsonl(raw / "chunks.jsonl", chunks)
    # meeting_minutes is written FLAT (no envelope wrapper) per the
    # schema — title/summary/decisions/etc. live at the top level.
    flat_payload: dict = {
        "artifact_type": "meeting_minutes",
        "schema_version": "1.5.0",
        "title": "Fixture meeting",
        "summary": "Test fixture for grounding gate integration tests.",
        "decisions": extraction_payload.get("decisions", []),
        "action_items": extraction_payload.get("action_items", []),
        "open_questions": extraction_payload.get("open_questions", []),
    }
    # Drop in any other claim-shaped types the caller wanted.
    for k, v in extraction_payload.items():
        if k not in flat_payload:
            flat_payload[k] = v
    if wrap_envelope:
        # Production-pipeline shape: the meeting_minutes fields live
        # inside ``payload`` under the governed envelope.
        extraction_artifact: dict = {
            "artifact_id": "fixture-artifact-id",
            "artifact_type": "meeting_minutes",
            "schema_version": 1,
            "status": "promoted",
            "created_at": "1970-01-01T00:00:00+00:00",
            "trace_id": "fixture-trace-id",
            "input_refs": [],
            "content_hash": "fixture-hash",
            "payload": flat_payload,
        }
    else:
        extraction_artifact = flat_payload
    (processed / extraction_filename).write_text(
        json.dumps(extraction_artifact, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return data_lake


def _run(*argv: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    # Ensure src/ is importable when invoking the script as a file path.
    full_env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + full_env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, str(SCRIPT), *argv],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env=full_env,
    )


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_clean_input_passes_and_writes_all_three_artifacts(tmp_path: Path) -> None:
    payload = {
        "decisions": [
            {
                "text": "Determine band plan",
                "source_quote": "we will determine the band plan",
                "source_chunk_id": "c1",
            }
        ]
    }
    data_lake = _build_minimal_data_lake(
        tmp_path,
        extraction_payload=payload,
        chunks={"c1": "so we will determine the band plan today"},
    )
    cp = _run("--source-id", SOURCE_ID, "--data-lake", str(data_lake))
    assert cp.returncode == 0, cp.stdout + cp.stderr

    processed = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    grounded = list(processed.glob("grounded_items__*.json"))
    result_files = list(processed.glob("grounding_gate_result__*.json"))
    ungrounded = list((processed / "ungrounded_items").glob("*.jsonl")) if (processed / "ungrounded_items").exists() else []
    assert len(grounded) == 1, f"expected one grounded artifact, got {grounded}"
    assert len(result_files) == 1, f"expected one result artifact, got {result_files}"
    # ungrounded_items dir/file is created even when empty? The script
    # writes an empty file when there's nothing to record.
    if ungrounded:
        assert ungrounded[0].read_text(encoding="utf-8") == ""

    result_payload = json.loads(result_files[0].read_text(encoding="utf-8"))
    assert result_payload["artifact_type"] == "grounding_gate_result"
    assert "artifact_kind" not in result_payload
    assert result_payload["passed"] is True
    assert result_payload["total_items"] == 1
    assert result_payload["grounded_count"] == 1
    assert result_payload["ungrounded_count"] == 0


def test_ungrounded_item_separated_into_audit_jsonl(tmp_path: Path) -> None:
    payload = {
        "decisions": [
            {
                "text": "Good one",
                "source_quote": "we will determine the band plan",
                "source_chunk_id": "c1",
            },
            {
                "text": "Bad one — quote doesn't match",
                "source_quote": "this phrase is not in any chunk",
                "source_chunk_id": "c1",
            },
        ]
    }
    data_lake = _build_minimal_data_lake(
        tmp_path,
        extraction_payload=payload,
        chunks={"c1": "so we will determine the band plan today"},
    )
    cp = _run("--source-id", SOURCE_ID, "--data-lake", str(data_lake))
    # Returns 1 — at least one ungrounded item.
    assert cp.returncode == 1, cp.stdout + cp.stderr

    processed = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    grounded_path = next(processed.glob("grounded_items__*.json"))
    ungrounded_path = next((processed / "ungrounded_items").glob("*.jsonl"))
    result_path = next(processed.glob("grounding_gate_result__*.json"))

    grounded = json.loads(grounded_path.read_text(encoding="utf-8"))
    assert len(grounded["payload"]["decisions"]) == 1
    assert grounded["payload"]["decisions"][0]["text"] == "Good one"
    assert grounded["gate_passed"] is False

    # The ungrounded JSONL carries one record.
    ungrounded_lines = [
        json.loads(ln) for ln in ungrounded_path.read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    assert len(ungrounded_lines) == 1
    assert ungrounded_lines[0]["extraction_type"] == "decisions"
    assert ungrounded_lines[0]["reason"] == "not_substring"

    # The gate result records the failure.
    result_payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert result_payload["total_items"] == 2
    assert result_payload["grounded_count"] == 1
    assert result_payload["ungrounded_count"] == 1
    assert result_payload["gate_drop_rate"] == 0.5


# --------------------------------------------------------------------------
# Bypass path
# --------------------------------------------------------------------------


def test_disable_grounding_gate_writes_bypass_record_only(tmp_path: Path) -> None:
    payload = {"decisions": [{"text": "x", "source_quote": "anything", "source_chunk_id": "c1"}]}
    data_lake = _build_minimal_data_lake(tmp_path, extraction_payload=payload)

    cp = _run(
        "--source-id", SOURCE_ID,
        "--data-lake", str(data_lake),
        "--disable-grounding-gate",
        "--operator", "alice@example.com",
    )
    assert cp.returncode == 0, cp.stdout + cp.stderr

    processed = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    bypass_files = list(processed.glob("grounding_gate_bypass_record__*.json"))
    grounded_files = list(processed.glob("grounded_items__*.json"))
    assert len(bypass_files) == 1
    # Gate did not run — no grounded/ungrounded/result artifacts.
    assert grounded_files == []
    assert not (processed / "ungrounded_items").exists() or \
        list((processed / "ungrounded_items").glob("*.jsonl")) == []

    bypass = json.loads(bypass_files[0].read_text(encoding="utf-8"))
    assert bypass["artifact_type"] == "grounding_gate_bypass_record"
    assert "artifact_kind" not in bypass
    assert bypass["operator"] == "alice@example.com"
    assert bypass["source_id"] == SOURCE_ID
    assert "operator override" in bypass["reason"]


# --------------------------------------------------------------------------
# Precondition failures
# --------------------------------------------------------------------------


def test_missing_transcript_exits_2(tmp_path: Path) -> None:
    """Even with a valid extraction artifact, a missing transcript blocks."""
    data_lake = tmp_path / "data-lake"
    processed = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    processed.mkdir(parents=True)
    # Drop a minimal extraction so the script gets PAST the
    # "no extraction" check and into the transcript check.
    art = {
        "artifact_id": "x",
        "artifact_type": "meeting_minutes",
        "schema_version": "1.5.0",
        "payload": {},
    }
    (processed / "meeting_minutes__x.json").write_text(
        json.dumps(art) + "\n", encoding="utf-8"
    )
    # No transcript created.
    cp = _run("--source-id", SOURCE_ID, "--data-lake", str(data_lake))
    assert cp.returncode == 2, cp.stderr
    assert "transcript not found" in cp.stderr


def test_missing_extraction_artifact_exits_2(tmp_path: Path) -> None:
    data_lake = tmp_path / "data-lake"
    raw = data_lake / "store" / "raw" / "meetings" / SOURCE_ID
    raw.mkdir(parents=True)
    (raw / "source.txt").write_text("anything", encoding="utf-8")
    (data_lake / "store" / "processed" / "meetings" / SOURCE_ID).mkdir(parents=True)
    cp = _run("--source-id", SOURCE_ID, "--data-lake", str(data_lake))
    assert cp.returncode == 2
    assert "meeting_minutes" in cp.stderr.lower()


def test_missing_data_lake_exits_2(tmp_path: Path) -> None:
    cp = _run("--source-id", SOURCE_ID, "--data-lake", str(tmp_path / "nope"))
    assert cp.returncode == 2
    assert "data-lake not found" in cp.stderr


# --------------------------------------------------------------------------
# Artifact selection — content-aware (schema_version) tiebreaker.
#
# Regression for the production mtime-tie failure: when the data-lake is
# fresh-cloned in CI, git stamps EVERY checked-out file's mtime with the
# single clone timestamp. A pure-mtime sort then collapses onto
# Path.glob iteration order (filesystem-dependent), so the gate could
# pick a stale 1.4.0 artifact (no source_quote) over the current 1.5.0
# artifact and report 100% missing_source_quote even when the right
# artifact sits in the same directory. The fix sorts by
# (schema_version, mtime, name) descending so the Phase 4.A artifact
# wins on the content signal, mtime ties or not.
# --------------------------------------------------------------------------


def _stamp_mtime(path: Path, ts: float) -> None:
    os.utime(path, (ts, ts))


def test_find_latest_extraction_prefers_phase_4a_over_legacy_when_mtimes_tie(
    tmp_path: Path,
) -> None:
    """The production scenario: a 1.4.0 (legacy) and a 1.5.0 (Phase 4.A)
    artifact share a directory with IDENTICAL mtimes (git-clone
    collision). The selector must pick the 1.5.0 artifact on the
    schema_version signal."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from run_grounding_gate import _find_latest_extraction

    d = tmp_path / "processed"
    d.mkdir()
    legacy = d / "meeting_minutes__legacy-efd6ce63609a.json"
    phase4a = d / "meeting_minutes__phase4a-a01658daa307.json"
    legacy.write_text(
        json.dumps({
            "artifact_type": "meeting_minutes",
            "payload": {"schema_version": "1.4.0", "title": "legacy"},
        }),
        encoding="utf-8",
    )
    phase4a.write_text(
        json.dumps({
            "artifact_type": "meeting_minutes",
            "payload": {"schema_version": "1.5.0", "title": "phase 4a"},
        }),
        encoding="utf-8",
    )
    clone_ts = 1_700_000_000
    _stamp_mtime(legacy, clone_ts)
    _stamp_mtime(phase4a, clone_ts)

    result = _find_latest_extraction(d)
    assert result == phase4a, (
        f"selected the legacy artifact under mtime collision: {result}"
    )


def test_find_latest_extraction_uses_mtime_within_same_schema_version(
    tmp_path: Path,
) -> None:
    """When schema versions tie, recency (mtime) still wins."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from run_grounding_gate import _find_latest_extraction

    d = tmp_path / "processed"
    d.mkdir()
    older = d / "meeting_minutes__older.json"
    newer = d / "meeting_minutes__newer.json"
    body = json.dumps({"payload": {"schema_version": "1.5.0"}})
    older.write_text(body, encoding="utf-8")
    newer.write_text(body, encoding="utf-8")
    _stamp_mtime(older, 1_700_000_000)
    _stamp_mtime(newer, 1_700_000_100)

    assert _find_latest_extraction(d) == newer


def test_find_latest_extraction_uses_filename_when_schema_and_mtime_tie(
    tmp_path: Path,
) -> None:
    """Pure-mtime sort under git-clone gave non-deterministic results
    across filesystems. With (schema_version, mtime, name) descending,
    two artifacts sharing schema_version AND mtime resolve by filename
    (the lexicographically later name wins). The total order matters
    more than which side wins — the test pins it so a future refactor
    that drops the tiebreaker is caught."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from run_grounding_gate import _find_latest_extraction

    d = tmp_path / "processed"
    d.mkdir()
    a_name = d / "meeting_minutes__a.json"
    b_name = d / "meeting_minutes__b.json"
    body = json.dumps({"payload": {"schema_version": "1.5.0"}})
    a_name.write_text(body, encoding="utf-8")
    b_name.write_text(body, encoding="utf-8")
    same_ts = 1_700_000_000
    _stamp_mtime(a_name, same_ts)
    _stamp_mtime(b_name, same_ts)

    # Descending name order means "b" > "a".
    assert _find_latest_extraction(d) == b_name


def test_run_grounding_gate_picks_phase_4a_artifact_over_legacy(
    tmp_path: Path,
) -> None:
    """End-to-end: the script run against a directory carrying BOTH a
    legacy and a Phase 4.A artifact (identical mtimes) must run the
    gate against the 1.5.0 artifact — proven by the resulting
    grounded_count > 0 (the 1.5.0 item has source_quote and grounds
    cleanly). Picking the legacy artifact would yield
    missing_source_quote and grounded_count == 0."""
    data_lake = tmp_path / "data-lake"
    processed = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    raw = data_lake / "store" / "raw" / "meetings" / SOURCE_ID
    processed.mkdir(parents=True)
    raw.mkdir(parents=True)
    transcript = "so we will determine the band plan today"
    (raw / "source.txt").write_text(transcript, encoding="utf-8")
    _write_chunks_jsonl(raw / "chunks.jsonl", {"c1": transcript})

    legacy = processed / "meeting_minutes__legacy-efd6ce63609a.json"
    phase4a = processed / "meeting_minutes__phase4a-a01658daa307.json"
    legacy.write_text(
        json.dumps({
            "artifact_type": "meeting_minutes",
            "schema_version": "1.4.0",
            "title": "Legacy",
            "summary": "Legacy artifact — no source_quote field.",
            "decisions": [{"text": "old decision — no quote"}],
            "action_items": [],
            "open_questions": [],
        }),
        encoding="utf-8",
    )
    phase4a.write_text(
        json.dumps({
            "artifact_type": "meeting_minutes",
            "schema_version": "1.5.0",
            "title": "Phase 4.A",
            "summary": "Phase 4.A artifact with source_quote.",
            "decisions": [{
                "text": "Determine band plan",
                "source_quote": "we will determine the band plan",
                "source_chunk_id": "c1",
            }],
            "action_items": [],
            "open_questions": [],
        }),
        encoding="utf-8",
    )
    clone_ts = 1_700_000_000
    _stamp_mtime(legacy, clone_ts)
    _stamp_mtime(phase4a, clone_ts)

    cp = _run("--source-id", SOURCE_ID, "--data-lake", str(data_lake))
    assert cp.returncode == 0, cp.stdout + cp.stderr

    result_path = next(processed.glob("grounding_gate_result__*.json"))
    result_payload = json.loads(result_path.read_text(encoding="utf-8"))
    # Picking the 1.5.0 artifact: 1 item, 1 grounded, 0 ungrounded.
    # Picking the legacy 1.4.0 (the bug): 1 item, 0 grounded, 1
    # ungrounded with reason "missing_source_quote".
    assert result_payload["grounded_count"] == 1, (
        f"selector picked the legacy 1.4.0 artifact: {result_payload!r}"
    )
    assert result_payload["ungrounded_count"] == 0


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_two_runs_produce_byte_identical_artifacts(tmp_path: Path) -> None:
    payload = {
        "decisions": [
            {
                "text": "x",
                "source_quote": "we will determine the band plan",
                "source_chunk_id": "c1",
            },
            {
                "text": "y",
                "source_quote": "no match here at all",
                "source_chunk_id": "c1",
            },
        ]
    }
    data_lake = _build_minimal_data_lake(
        tmp_path,
        extraction_payload=payload,
        chunks={"c1": "so we will determine the band plan today"},
        # --run-id below selects the extraction by filename slug, so
        # align the fixture filename with the run_id we'll pass.
        extraction_filename="meeting_minutes__fixed.json",
    )
    processed = data_lake / "store" / "processed" / "meetings" / SOURCE_ID

    cp1 = _run("--source-id", SOURCE_ID, "--data-lake", str(data_lake), "--run-id", "fixed")
    # First run wrote files; capture them.
    artifacts_after_first = {
        p.name: p.read_bytes()
        for p in [
            processed / "grounded_items__fixed.json",
            processed / "grounding_gate_result__fixed.json",
            processed / "ungrounded_items" / "fixed.jsonl",
        ]
    }
    # Allow some real-time gap so any non-deterministic timestamp would be visible.
    time.sleep(0.01)

    cp2 = _run("--source-id", SOURCE_ID, "--data-lake", str(data_lake), "--run-id", "fixed")
    artifacts_after_second = {
        p.name: p.read_bytes()
        for p in [
            processed / "grounded_items__fixed.json",
            processed / "grounding_gate_result__fixed.json",
            processed / "ungrounded_items" / "fixed.jsonl",
        ]
    }
    assert cp1.returncode == cp2.returncode
    for name in artifacts_after_first:
        assert artifacts_after_first[name] == artifacts_after_second[name], (
            f"{name} drift between runs"
        )


# --------------------------------------------------------------------------
# Envelope-wrapped extraction artifact (production pipeline shape)
# --------------------------------------------------------------------------


def test_envelope_wrapped_extraction_is_unwrapped_for_schema_validation(
    tmp_path: Path,
) -> None:
    """Pipeline-written meeting_minutes carry the full Artifact envelope
    on disk (``artifact_id``, ``content_hash``, ``created_at``,
    ``input_refs``, ``payload``, ``status``, ``trace_id``); the gate
    must unwrap ``payload`` before handing the FLAT shape to the
    schema validator. Without the unwrap the envelope keys trip the
    schema's ``additionalProperties: false`` and the gate exits 2.
    """
    payload = {
        "decisions": [
            {
                "text": "Determine band plan",
                "source_quote": "we will determine the band plan",
                "source_chunk_id": "c1",
            }
        ]
    }
    data_lake = _build_minimal_data_lake(
        tmp_path,
        extraction_payload=payload,
        chunks={"c1": "so we will determine the band plan today"},
        wrap_envelope=True,
    )
    cp = _run("--source-id", SOURCE_ID, "--data-lake", str(data_lake))
    assert cp.returncode == 0, cp.stdout + cp.stderr

    processed = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    grounded_path = next(processed.glob("grounded_items__*.json"))
    grounded = json.loads(grounded_path.read_text(encoding="utf-8"))
    # The grounded output keeps the envelope shape (payload + gate
    # metadata) so compare_opus_haiku.py can read it via the same
    # _load_payload_from_path helper it uses for the source artifact.
    assert "payload" in grounded
    assert grounded["payload"]["decisions"][0]["text"] == "Determine band plan"
    assert grounded["gate_passed"] is True

    result_path = next(processed.glob("grounding_gate_result__*.json"))
    result_payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert result_payload["artifact_type"] == "grounding_gate_result"
    assert result_payload["total_items"] == 1
    assert result_payload["grounded_count"] == 1
    # trace_id is read from the envelope-level field.
    assert result_payload.get("trace_id") == "fixture-trace-id"


# --------------------------------------------------------------------------
# Step summary
# --------------------------------------------------------------------------


def test_run_id_flag_selects_specific_artifact(tmp_path: Path) -> None:
    """When --run-id is provided, the script locates
    meeting_minutes__<run_id>.json directly and gates THAT artifact,
    even when another artifact would otherwise win the content-aware
    selector (same schema_version, later mtime / name)."""
    data_lake = tmp_path / "data-lake"
    processed = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    raw = data_lake / "store" / "raw" / "meetings" / SOURCE_ID
    processed.mkdir(parents=True)
    raw.mkdir(parents=True)
    transcript = "so we will determine the band plan today"
    (raw / "source.txt").write_text(transcript, encoding="utf-8")
    _write_chunks_jsonl(raw / "chunks.jsonl", {"c1": transcript})

    # Two 1.5.0 artifacts: the "winner" by content-aware selector
    # (lexicographically-later filename under mtime+schema_version tie)
    # carries a quote that DOES NOT ground; the targeted one carries a
    # quote that DOES ground. --run-id must pick the targeted one.
    targeted = processed / "meeting_minutes__a-targeted.json"
    selector_default = processed / "meeting_minutes__z-default-pick.json"
    targeted.write_text(
        json.dumps({
            "artifact_type": "meeting_minutes",
            "schema_version": "1.5.0",
            "title": "Targeted",
            "summary": "Targeted artifact — quote grounds cleanly.",
            "decisions": [{
                "text": "Determine band plan",
                "source_quote": "we will determine the band plan",
                "source_chunk_id": "c1",
            }],
            "action_items": [],
            "open_questions": [],
        }),
        encoding="utf-8",
    )
    selector_default.write_text(
        json.dumps({
            "artifact_type": "meeting_minutes",
            "schema_version": "1.5.0",
            "title": "Default pick",
            "summary": "Default-selected artifact — quote does NOT match.",
            "decisions": [{
                "text": "Wrong artifact",
                "source_quote": "this phrase is not in any chunk",
                "source_chunk_id": "c1",
            }],
            "action_items": [],
            "open_questions": [],
        }),
        encoding="utf-8",
    )
    clone_ts = 1_700_000_000
    _stamp_mtime(targeted, clone_ts)
    _stamp_mtime(selector_default, clone_ts)

    # First: confirm the BUG case — without --run-id the selector picks
    # the "z-default-pick" artifact (lexicographically later under
    # mtime+schema tie) and the gate finds the quote doesn't ground.
    cp_default = _run("--source-id", SOURCE_ID, "--data-lake", str(data_lake))
    assert cp_default.returncode == 1, cp_default.stdout + cp_default.stderr
    default_result = json.loads(
        next(processed.glob("grounding_gate_result__z-default-pick.json"))
        .read_text(encoding="utf-8")
    )
    assert default_result["grounded_count"] == 0
    assert default_result["ungrounded_count"] == 1

    # Now: with --run-id the script gates the TARGETED artifact instead.
    cp = _run(
        "--source-id", SOURCE_ID,
        "--data-lake", str(data_lake),
        "--run-id", "a-targeted",
    )
    assert cp.returncode == 0, cp.stdout + cp.stderr

    targeted_result_path = processed / "grounding_gate_result__a-targeted.json"
    assert targeted_result_path.is_file(), (
        f"--run-id should produce a result file keyed by run_id: "
        f"{list(processed.glob('grounding_gate_result__*.json'))}"
    )
    targeted_result = json.loads(targeted_result_path.read_text(encoding="utf-8"))
    assert targeted_result["grounded_count"] == 1
    assert targeted_result["ungrounded_count"] == 0
    # The gate result records WHICH input artifact it gated.
    assert "meeting_minutes__a-targeted.json" in targeted_result.get(
        "extraction_artifact_path", ""
    )


def test_run_id_flag_exits_2_when_artifact_not_found(tmp_path: Path) -> None:
    """When --run-id names an artifact that doesn't exist, the script
    exits 2 (precondition failure) with an error naming the missing
    path AND the run_id, and writes nothing."""
    data_lake = tmp_path / "data-lake"
    processed = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    raw = data_lake / "store" / "raw" / "meetings" / SOURCE_ID
    processed.mkdir(parents=True)
    raw.mkdir(parents=True)
    (raw / "source.txt").write_text("any transcript", encoding="utf-8")
    # Drop a real artifact in so we know the failure is purely from the
    # --run-id mismatch (not from an empty directory).
    (processed / "meeting_minutes__exists.json").write_text(
        json.dumps({
            "artifact_type": "meeting_minutes",
            "schema_version": "1.5.0",
            "title": "Exists",
            "summary": "Real artifact.",
            "decisions": [],
            "action_items": [],
            "open_questions": [],
        }),
        encoding="utf-8",
    )

    cp = _run(
        "--source-id", SOURCE_ID,
        "--data-lake", str(data_lake),
        "--run-id", "does-not-exist",
    )
    assert cp.returncode == 2, cp.stdout + cp.stderr
    assert "does-not-exist" in cp.stderr
    assert "meeting_minutes__does-not-exist.json" in cp.stderr

    # No grounded / result / ungrounded artifacts written under that run_id.
    assert list(processed.glob("grounded_items__does-not-exist*")) == []
    assert list(processed.glob("grounding_gate_result__does-not-exist*")) == []


def test_run_id_flag_bypasses_content_aware_selector(tmp_path: Path) -> None:
    """The exact production scenario: a 1.5.0 artifact predates the
    VERBATIM SOURCE GROUNDING prompt section and has no source_quote
    fields, while a 1.4.0 artifact carries the older but quote-bearing
    payload. The content-aware selector picks the 1.5.0 artifact on
    schema_version, the gate reports 100% missing_source_quote, and
    the operator wants to gate the 1.4.0 artifact instead. --run-id
    targeting the 1.4.0 artifact must bypass the selector and run the
    gate against it.
    """
    data_lake = tmp_path / "data-lake"
    processed = data_lake / "store" / "processed" / "meetings" / SOURCE_ID
    raw = data_lake / "store" / "raw" / "meetings" / SOURCE_ID
    processed.mkdir(parents=True)
    raw.mkdir(parents=True)
    transcript = "so we will determine the band plan today"
    (raw / "source.txt").write_text(transcript, encoding="utf-8")
    _write_chunks_jsonl(raw / "chunks.jsonl", {"c1": transcript})

    # 1.5.0 artifact — newer schema, NO source_quote (the "bad" pick).
    no_quote_15 = processed / "meeting_minutes__no-quote-15.json"
    # 1.4.0 artifact — older schema, but with source_quote. Note we
    # build it as a 1.5.0-shaped payload too so the SCHEMA validator
    # accepts it; what we are simulating is "this is the artifact the
    # operator wants to gate" via --run-id, not a real 1.4.0 schema.
    target_with_quote = processed / "meeting_minutes__target-with-quote.json"
    no_quote_15.write_text(
        json.dumps({
            "artifact_type": "meeting_minutes",
            "schema_version": "1.5.0",
            "title": "Selector default",
            "summary": "Selector would pick this — missing source_quote.",
            "decisions": [{"text": "decision without a quote"}],
            "action_items": [],
            "open_questions": [],
        }),
        encoding="utf-8",
    )
    target_with_quote.write_text(
        json.dumps({
            "artifact_type": "meeting_minutes",
            "schema_version": "1.5.0",
            "title": "Operator-targeted",
            "summary": "The artifact the operator wants gated.",
            "decisions": [{
                "text": "Determine band plan",
                "source_quote": "we will determine the band plan",
                "source_chunk_id": "c1",
            }],
            "action_items": [],
            "open_questions": [],
        }),
        encoding="utf-8",
    )
    # Make the selector PREFER no_quote_15 by giving it a newer mtime.
    _stamp_mtime(target_with_quote, 1_700_000_000)
    _stamp_mtime(no_quote_15, 1_700_000_100)

    # Confirm the bug case first: without --run-id the selector picks
    # the no-quote artifact and the gate fails with missing_source_quote.
    cp_default = _run("--source-id", SOURCE_ID, "--data-lake", str(data_lake))
    assert cp_default.returncode == 1, cp_default.stdout + cp_default.stderr
    default_result = json.loads(
        (processed / "grounding_gate_result__no-quote-15.json")
        .read_text(encoding="utf-8")
    )
    assert default_result["grounded_count"] == 0
    # Confirm the failure reason matches the production scenario.
    failure_reasons = {f["reason"] for f in default_result.get("failures", [])}
    assert "missing_source_quote" in failure_reasons, default_result

    # Now: --run-id targeting the quote-bearing artifact bypasses the
    # content-aware selector and grounds cleanly.
    cp = _run(
        "--source-id", SOURCE_ID,
        "--data-lake", str(data_lake),
        "--run-id", "target-with-quote",
    )
    assert cp.returncode == 0, cp.stdout + cp.stderr
    targeted_result = json.loads(
        (processed / "grounding_gate_result__target-with-quote.json")
        .read_text(encoding="utf-8")
    )
    assert targeted_result["grounded_count"] == 1
    assert targeted_result["ungrounded_count"] == 0


def test_step_summary_includes_totals_and_failure_reasons(tmp_path: Path) -> None:
    payload = {
        "decisions": [
            {
                "text": "Bad",
                "source_quote": "this phrase is not in any chunk",
                "source_chunk_id": "c1",
            }
        ]
    }
    data_lake = _build_minimal_data_lake(
        tmp_path,
        extraction_payload=payload,
        chunks={"c1": "some other text"},
    )
    summary_path = tmp_path / "summary.md"
    cp = _run(
        "--source-id", SOURCE_ID,
        "--data-lake", str(data_lake),
        env={"GITHUB_STEP_SUMMARY": str(summary_path)},
    )
    assert cp.returncode == 1
    summary = summary_path.read_text(encoding="utf-8")
    assert "Phase 4.A grounding gate" in summary
    assert "total_items: 1" in summary
    assert "not_substring" in summary
    assert "RECALL COLLAPSE" in summary  # 0/1 grounded < 50%
