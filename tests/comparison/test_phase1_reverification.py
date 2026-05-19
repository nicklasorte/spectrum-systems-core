"""Phase 1 (Step 1.7) — comparison engine re-verification + mixed-schema tests.

The comparison engine MUST:

1. Detect a Haiku artifact whose grounded items no longer match the
   current transcript and mark the comparison ``tainted: true``.
2. Halt with reason ``schema_version_mixed`` when the Haiku artifact
   and the Opus baseline declare different ``schema_version``s, UNLESS
   ``--allow-mixed-schema`` was passed (CLI-only).
3. Surface a ``grounding_rate`` field in the comparison output.
4. NEVER read the ``--allow-mixed-schema`` switch from environment
   variables or config files — the flag is per-invocation, opt-in.

These tests construct a small temp data-lake with a baseline, a haiku
artifact, a transcript, and (in the tampering case) a mutated artifact;
they then call ``cmp.run_comparison`` directly. No network access.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import compare_opus_haiku as cmp  # noqa: E402


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _seed_baseline(
    dl: Path,
    sid: str,
    *,
    schema_version: str = "1.4.0",
    text: str = "Hello world.",
) -> None:
    p = (
        dl / "store" / "processed" / "meetings" / sid
        / "reference_baselines" / "opus_reference_minutes.jsonl"
    )
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "extraction_type": "decisions",
                "ground_truth_text": text,
                "model_id": "claude-opus-4-6",
                "schema_version": schema_version,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _seed_transcript(dl: Path, sid: str, transcript: str) -> Path:
    meeting_dir = dl / "store" / "processed" / "meetings" / sid
    meeting_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = meeting_dir / "transcript.txt"
    transcript_path.write_text(transcript, encoding="utf-8")
    # Also write a minimal source_record.json so the resolver finds it.
    _write(
        meeting_dir / "source_record.json",
        {
            "artifact_id": "src-rec-1",
            "artifact_type": "source_record",
            "schema_version": "1.0.0",
            "payload": {
                "transcript_path": str(transcript_path),
            },
        },
    )
    return transcript_path


def _haiku_artifact(
    *,
    schema_version: str = "1.4.0",
    decisions: list | None = None,
) -> dict:
    return {
        "artifact_id": "art-1",
        "artifact_type": "meeting_minutes",
        "schema_version": schema_version,
        "status": "promoted",
        "created_at": "1970-01-01T00:00:00+00:00",
        "trace_id": "trace-haiku-1",
        "input_refs": [],
        "content_hash": "deadbeef",
        "payload": {
            "schema_version": schema_version,
            "title": "Test meeting",
            "summary": "",
            "decisions": decisions
            if decisions is not None
            else ["Hello world."],
            "action_items": [],
            "open_questions": [],
            "meeting_id": "src",
            "provenance": {
                "produced_by": "meeting_minutes_llm",
                "model_id": "claude-haiku-4-5",
            },
        },
    }


def test_grounded_haiku_artifact_passes_re_verification(tmp_path: Path):
    """A 1.4.0 haiku artifact whose every item still matches the current
    transcript byte-for-byte must NOT be tainted."""
    dl = tmp_path / "dl"
    sid = "src"
    transcript = "CHAIR: Hello world."
    _seed_transcript(dl, sid, transcript)
    _seed_baseline(dl, sid, schema_version="1.4.0")
    # Haiku item references a real transcript span with the correct
    # normalized offset.
    decisions = [
        {
            "text": "Hello world.",
            "grounding_mode": "verbatim",
            "source_quote": "Hello world.",
            "quote_offset_normalized": 6,
        }
    ]
    _write(
        dl / "store" / "processed" / "meetings" / sid
        / "meeting_minutes__llm-1.json",
        _haiku_artifact(decisions=decisions),
    )
    res = cmp.run_comparison(
        data_lake=dl, source_id=sid, dry_run=True
    )
    assert res["status"] == "success"
    assert res["tainted"] is False
    assert res["grounding_rate"] == 1.0
    assert res["re_verification"]["status"] == "ok"
    assert res["haiku_schema_version"] == "1.4.0"
    assert res["baseline_schema_version"] == "1.4.0"


def test_tampered_haiku_artifact_is_marked_tainted(tmp_path: Path):
    """The canonical tampering signal: the artifact claims to point at
    a quote that does not appear in the current transcript. The
    comparison must set tainted: true and surface the rejection reason
    codes."""
    dl = tmp_path / "dl"
    sid = "src"
    # The artifact references "Hello world." but the current transcript
    # is different (transcript mutation OR data-lake tampering).
    transcript = "CHAIR: Goodbye, everyone."
    _seed_transcript(dl, sid, transcript)
    _seed_baseline(dl, sid, schema_version="1.4.0", text="Goodbye")
    decisions = [
        {
            "text": "Hello world.",
            "grounding_mode": "verbatim",
            "source_quote": "Hello world.",
            "quote_offset_normalized": 6,
        }
    ]
    _write(
        dl / "store" / "processed" / "meetings" / sid
        / "meeting_minutes__llm-1.json",
        _haiku_artifact(decisions=decisions),
    )
    res = cmp.run_comparison(
        data_lake=dl, source_id=sid, dry_run=True
    )
    assert res["tainted"] is True
    assert res["re_verification"]["status"] == "tainted"
    assert "grounding_exact_text_not_in_transcript" in (
        res["re_verification"]["rejected_reason_codes"]
    )


def test_mixed_schema_halts_without_flag(tmp_path: Path):
    """Haiku schema_version 1.4.0 vs baseline schema_version 1.0.0
    must halt with schema_version_mixed by default."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_transcript(dl, sid, "CHAIR: Hello world.")
    _seed_baseline(dl, sid, schema_version="1.0.0", text="Hello")
    _write(
        dl / "store" / "processed" / "meetings" / sid
        / "meeting_minutes__llm-1.json",
        _haiku_artifact(schema_version="1.4.0"),
    )
    with pytest.raises(cmp.ComparisonError) as ei:
        cmp.run_comparison(data_lake=dl, source_id=sid, dry_run=True)
    assert ei.value.reason == "schema_version_mixed"


def test_mixed_schema_allowed_with_explicit_cli_flag(tmp_path: Path):
    """When the CLI flag is passed, mixed schema is allowed and the
    allow_mixed_schema=True is logged in the result."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_transcript(dl, sid, "CHAIR: Hello world.")
    _seed_baseline(dl, sid, schema_version="1.0.0", text="Hello")
    _write(
        dl / "store" / "processed" / "meetings" / sid
        / "meeting_minutes__llm-1.json",
        _haiku_artifact(schema_version="1.4.0"),
    )
    res = cmp.run_comparison(
        data_lake=dl,
        source_id=sid,
        dry_run=True,
        allow_mixed_schema=True,
    )
    assert res["status"] == "success"
    assert res["allow_mixed_schema"] is True
    assert res["haiku_schema_version"] == "1.4.0"
    assert res["baseline_schema_version"] == "1.0.0"


def test_allow_mixed_schema_is_cli_only_not_env_var(tmp_path: Path):
    """The CLI parser must not consult ALLOW_MIXED_SCHEMA — the env var
    has no effect, only the explicit ``--allow-mixed-schema`` flag."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_transcript(dl, sid, "CHAIR: Hello world.")
    _seed_baseline(dl, sid, schema_version="1.0.0", text="Hello")
    _write(
        dl / "store" / "processed" / "meetings" / sid
        / "meeting_minutes__llm-1.json",
        _haiku_artifact(schema_version="1.4.0"),
    )
    # Set the env var; without the CLI flag the halt must still fire.
    prev = os.environ.get("ALLOW_MIXED_SCHEMA")
    os.environ["ALLOW_MIXED_SCHEMA"] = "true"
    try:
        rc = cmp.main(
            argv=[
                "--data-lake", str(dl),
                "--source-id", sid,
                "--dry-run",
            ]
        )
    finally:
        if prev is None:
            os.environ.pop("ALLOW_MIXED_SCHEMA", None)
        else:
            os.environ["ALLOW_MIXED_SCHEMA"] = prev
    # Halt: main returns 1 on ComparisonError and writes a JSON
    # failure body to stdout.
    assert rc == 1


def test_pre_1_4_haiku_skips_re_verification(tmp_path: Path):
    """A legacy haiku artifact (pre-1.4.0) has no grounding fields to
    verify, so the re-verification status is ``skipped`` with reason
    ``grounding_pre_1_4`` and tainted stays False."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_transcript(dl, sid, "CHAIR: hi.")
    _seed_baseline(dl, sid, schema_version="1.3.0", text="approved the threshold")
    _write(
        dl / "store" / "processed" / "meetings" / sid
        / "meeting_minutes__llm-1.json",
        _haiku_artifact(
            schema_version="1.3.0",
            decisions=["approved the threshold"],
        ),
    )
    res = cmp.run_comparison(
        data_lake=dl, source_id=sid, dry_run=True
    )
    assert res["tainted"] is False
    assert res["re_verification"]["status"] == "skipped"
    assert res["re_verification"]["reason"] == "grounding_pre_1_4"


def test_grounding_rate_field_is_present_in_output(tmp_path: Path):
    """Red-team Pass 3 #4: the grounding_rate value must surface in the
    comparison output for downstream consumers — without it, no caller
    can detect a degraded extraction."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_transcript(dl, sid, "CHAIR: Hello world.")
    _seed_baseline(dl, sid, schema_version="1.4.0", text="Hello")
    decisions = [
        {
            "text": "Hello world.",
            "grounding_mode": "verbatim",
            "source_quote": "Hello world.",
            "quote_offset_normalized": 6,
        }
    ]
    _write(
        dl / "store" / "processed" / "meetings" / sid
        / "meeting_minutes__llm-1.json",
        _haiku_artifact(decisions=decisions),
    )
    res = cmp.run_comparison(
        data_lake=dl, source_id=sid, dry_run=True
    )
    assert "grounding_rate" in res
    assert isinstance(res["grounding_rate"], (int, float))


def test_re_verification_when_transcript_missing_is_skipped_not_halted(
    tmp_path: Path,
):
    """The re-verification is a defensive cross-check; the primary gate
    ran at promotion time. When the transcript is not on disk we surface
    a skip rather than halting the whole comparison."""
    dl = tmp_path / "dl"
    sid = "src"
    # Seed baseline but DO NOT seed a transcript.
    _seed_baseline(dl, sid, schema_version="1.4.0", text="Hello")
    meeting_dir = dl / "store" / "processed" / "meetings" / sid
    meeting_dir.mkdir(parents=True, exist_ok=True)
    decisions = [
        {
            "text": "Hello world.",
            "grounding_mode": "verbatim",
            "source_quote": "Hello world.",
            "quote_offset_normalized": 6,
        }
    ]
    _write(
        meeting_dir / "meeting_minutes__llm-1.json",
        _haiku_artifact(decisions=decisions),
    )
    res = cmp.run_comparison(
        data_lake=dl, source_id=sid, dry_run=True
    )
    assert res["tainted"] is False
    assert res["re_verification"]["status"] == "skipped"
    assert res["re_verification"]["reason"] == "transcript_unavailable"


def test_baseline_with_no_schema_version_is_treated_as_unknown(
    tmp_path: Path,
):
    """If the baseline rows do not stamp a schema_version, the
    mismatch cannot be detected (returns None). The comparison
    proceeds as if no mixed-schema check fired — this is the legacy
    code path for pre-Phase-1 baselines."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_transcript(dl, sid, "CHAIR: Hello world.")
    # Strip schema_version from the baseline.
    p = (
        dl / "store" / "processed" / "meetings" / sid
        / "reference_baselines" / "opus_reference_minutes.jsonl"
    )
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "extraction_type": "decisions",
                "ground_truth_text": "Hello",
                "model_id": "claude-opus-4-6",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _write(
        dl / "store" / "processed" / "meetings" / sid
        / "meeting_minutes__llm-1.json",
        _haiku_artifact(schema_version="1.4.0"),
    )
    res = cmp.run_comparison(
        data_lake=dl, source_id=sid, dry_run=True
    )
    assert res["status"] == "success"
    assert res["baseline_schema_version"] is None


def test_artifact_schema_version_reads_envelope_first():
    """Envelope-level schema_version wins over payload-level."""
    artifact = {
        "schema_version": "1.4.0",
        "payload": {"schema_version": "1.3.0"},
    }
    assert cmp._artifact_schema_version(artifact) == "1.4.0"


def test_artifact_schema_version_falls_back_to_payload():
    artifact = {"payload": {"schema_version": "1.3.0"}}
    assert cmp._artifact_schema_version(artifact) == "1.3.0"


def test_artifact_schema_version_returns_none_when_missing():
    artifact = {"payload": {}}
    assert cmp._artifact_schema_version(artifact) is None
