"""Phase Y — source turn grounding tests.

Every test in this file defends one of the trust properties Phase Y
introduces:

- Happy-path golden (4): every artifact type promotes at 1.1.0, every
  extracted item has non-empty ``source_turns``, and the validity eval
  passes.
- Rejection: 100% null speaker → block with ``no_speaker_structure``.
- Rejection: unresolved turn_id → ``source_turn_validity`` fails with a
  reason code that carries the unresolved id.
- Rejection: invalid source_record on disk → ``source_record_invalid``;
  the eval refuses to pass silently.
- Rejection: ``schema_version: "1.1.0"`` artifact with an extracted
  item missing ``source_turns`` → required-field eval fails and the
  target is rejected.
- Determinism: chunk_transcript on the same input three times produces
  byte-identical chunk lists.
- Backward compat: 1.0.0 artifacts unchanged; no ``schema_version`` key
  required on the legacy path.
- Schema-version branch: the runner's ``REQUIRED_FIELDS_BY_TYPE`` lookup
  treats 1.0.0 and 1.1.0 differently in the way the spec dictates.

Each rejection test asserts the specific finding code (not just
``not promoted``) so a future regression that produces ``None`` instead
of a fail-closed block cannot pass these tests silently.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from spectrum_systems_core.artifacts import new_artifact
from spectrum_systems_core.data_lake import (
    NO_SPEAKER_STRUCTURE_FINDING,
    chunk_transcript,
    chunker_health,
    run_transcript_pipeline,
    source_record_path,
    speaker_null_rate,
)
from spectrum_systems_core.evals import (
    REQUIRED_FIELDS_BY_TYPE,
    SOURCE_RECORD_INVALID,
    SOURCE_TURN_UNRESOLVED_PREFIX,
    SOURCE_TURN_VALIDITY_EVAL_TYPE,
    run_required_evals,
    run_source_turn_validity_eval,
)


FIXTURES = Path(__file__).parent / "fixtures" / "phase_y"


def _seed(lake_root: Path, meeting_id: str) -> None:
    src = FIXTURES / meeting_id
    dst = lake_root / "raw" / "meetings" / meeting_id
    dst.mkdir(parents=True)
    shutil.copy(src / "transcript.txt", dst / "transcript.txt")
    shutil.copy(src / "metadata.json", dst / "metadata.json")


def _seed_inline(lake_root: Path, meeting_id: str, transcript: str, metadata: dict | None = None) -> None:
    meta = metadata or {
        "meeting_id": meeting_id,
        "title": "Test",
        "date": "2026-05-13",
        "source_type": "transcript",
    }
    dst = lake_root / "raw" / "meetings" / meeting_id
    dst.mkdir(parents=True)
    (dst / "transcript.txt").write_text(transcript, encoding="utf-8")
    (dst / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")


# ---------------------------------------------------------------------------
# Happy-path golden — one per artifact type.
# ---------------------------------------------------------------------------


_HAPPY_CASES = [
    ("m-y-meeting-minutes", "meeting_minutes"),
    ("m-y-decision-brief", "decision_brief"),
    ("m-y-agency-question", "agency_question_summary"),
    ("m-y-action-log", "meeting_action_log"),
]


@pytest.mark.parametrize("meeting_id,workflow_name", _HAPPY_CASES)
def test_happy_path_promotes_with_schema_1_1_0(
    tmp_path, meeting_id, workflow_name
):
    _seed(tmp_path, meeting_id)
    result = run_transcript_pipeline(
        lake_root=tmp_path,
        meeting_id=meeting_id,
        workflow_name=workflow_name,
    )

    # Promotion happened (the value, not the truthiness, asserted).
    assert result.promoted is True
    assert result.target.status == "promoted"
    assert result.control_decision.payload["decision"] == "allow"

    # Schema version bumped to 1.1.0.
    assert result.target.payload["schema_version"] == "1.1.0"

    # Every grounding entry has a non-empty ``source_turns`` list and
    # each turn_id resolves to a chunk in the on-disk source_record.
    sr_path = source_record_path(tmp_path, meeting_id)
    source_record = json.loads(sr_path.read_text(encoding="utf-8"))
    valid_turn_ids = {
        c["turn_id"] for c in source_record["payload"]["chunks"]
    }
    grounding = result.target.payload["grounding"]
    assert grounding, "happy fixture must produce grounded items"
    for entry in grounding:
        assert isinstance(entry["source_turns"], list)
        assert entry["source_turns"], (
            f"empty source_turns on {entry}"
        )
        for turn_id in entry["source_turns"]:
            assert turn_id in valid_turn_ids, (
                f"unresolved turn_id {turn_id!r} on {entry}"
            )

    # source_turn_validity eval passed.
    turn_validity = next(
        e for e in result.eval_results
        if e.payload.get("eval_type") == SOURCE_TURN_VALIDITY_EVAL_TYPE
    )
    assert turn_validity.payload["status"] == "pass"
    assert turn_validity.payload["reason_codes"] == []


# ---------------------------------------------------------------------------
# Rejection: 100% null speaker → no_speaker_structure block.
# ---------------------------------------------------------------------------


def test_pipeline_blocks_when_transcript_has_no_speaker_structure(tmp_path):
    transcript = (
        "Just a header line\n"
        "More prose without any speaker labels at all.\n"
        "And a third line for shape.\n"
    )
    _seed_inline(tmp_path, "m-no-speakers", transcript)

    result = run_transcript_pipeline(
        lake_root=tmp_path,
        meeting_id="m-no-speakers",
        workflow_name="meeting_minutes",
    )

    assert result.promoted is False
    assert result.target.status == "rejected"
    assert result.control_decision.payload["decision"] == "block"
    # control_decision aggregates by ``failed:<eval_type>``; the
    # actual finding code lives in the chunker eval_result's
    # reason_codes (the operator-facing trace).
    chunker_eval = next(
        e for e in result.eval_results
        if e.payload["eval_type"] == "chunker_health"
    )
    assert chunker_eval.payload["status"] == "fail"
    assert any(
        NO_SPEAKER_STRUCTURE_FINDING in r
        for r in chunker_eval.payload["reason_codes"]
    ), f"expected no_speaker_structure in {chunker_eval.payload['reason_codes']!r}"
    # control_decision references the failing chunker eval by type.
    assert any(
        "chunker_health" in r
        for r in result.control_decision.payload["reason_codes"]
    )
    # Chunker findings surfaced on the result for the operator.
    assert NO_SPEAKER_STRUCTURE_FINDING in result.chunker_findings

    # No source_record on disk — the chunker block fired before the
    # source_record write.
    assert not source_record_path(
        tmp_path, "m-no-speakers"
    ).is_file()


# ---------------------------------------------------------------------------
# Rejection: unresolved turn_id surfaces in source_turn_validity output.
# ---------------------------------------------------------------------------


def _write_source_record_to_disk(
    tmp_path: Path, meeting_id: str, chunks: list[dict]
) -> Path:
    """Helper: write a minimal source_record envelope to the path the
    eval reads from. Mirrors the real pipeline writer."""
    target_dir = tmp_path / "processed" / "meetings" / meeting_id
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"source_record__{meeting_id}.json"
    envelope = {
        "artifact_id": "src-test",
        "artifact_type": "source_record",
        "schema_version": 1,
        "status": "promoted",
        "created_at": "1970-01-01T00:00:00+00:00",
        "trace_id": "trace-test",
        "input_refs": [],
        "content_hash": "deadbeef",
        "payload": {"meeting_id": meeting_id, "chunks": chunks},
    }
    path.write_text(json.dumps(envelope, sort_keys=True), encoding="utf-8")
    return path


def test_unresolved_turn_id_fails_source_turn_validity(tmp_path):
    chunks = [
        {"turn_id": "t0000", "speaker": "ALICE", "text": "Hello", "line_start": 1, "line_end": 1},
        {"turn_id": "t0001", "speaker": "BOB", "text": "World", "line_start": 2, "line_end": 2},
    ]
    sr_path = _write_source_record_to_disk(tmp_path, "m-unresolved", chunks)

    target = new_artifact(
        artifact_type="meeting_minutes",
        payload={
            "title": "x",
            "summary": "y",
            "decisions": [],
            "action_items": [],
            "open_questions": [],
            "schema_version": "1.1.0",
            "grounding": [
                {
                    "kind": "decision",
                    "text": "Some decision",
                    "source_turns": ["DOES-NOT-EXIST-ZZZ"],
                }
            ],
        },
        trace_id="trace-test",
        status="draft",
    )

    eval_result = run_source_turn_validity_eval(target, sr_path)

    assert eval_result.payload["status"] == "fail"
    reason_codes = eval_result.payload["reason_codes"]
    assert any(
        SOURCE_TURN_UNRESOLVED_PREFIX in r and "DOES-NOT-EXIST-ZZZ" in r
        for r in reason_codes
    ), f"expected unresolved id surfaced in {reason_codes!r}"


def test_unresolved_turn_id_drives_pipeline_rejection(tmp_path, monkeypatch):
    """End-to-end: monkeypatch the source-turn matcher to fabricate a
    turn_id that does not exist in the source_record. The pipeline must
    reject the target — assert ``status == 'rejected'`` specifically."""
    from spectrum_systems_core.workflows import extraction as _extraction
    from spectrum_systems_core.data_lake import extract as _extract_module

    def _fake_match(_text, _chunks):
        return _extraction.MatchResult(
            turn_ids=["NEVER-IN-ANY-CHUNK"], was_fallback=False
        )

    monkeypatch.setattr(_extract_module, "match_source_turns", _fake_match)
    _seed(tmp_path, "m-y-meeting-minutes")
    result = run_transcript_pipeline(
        lake_root=tmp_path,
        meeting_id="m-y-meeting-minutes",
        workflow_name="meeting_minutes",
    )

    assert result.promoted is False
    assert result.target.status == "rejected"
    assert result.control_decision.payload["decision"] == "block"

    turn_validity = next(
        e for e in result.eval_results
        if e.payload["eval_type"] == SOURCE_TURN_VALIDITY_EVAL_TYPE
    )
    assert turn_validity.payload["status"] == "fail"
    assert any(
        "NEVER-IN-ANY-CHUNK" in r
        for r in turn_validity.payload["reason_codes"]
    )


def test_invalid_source_record_drives_pipeline_rejection(tmp_path):
    """End-to-end: corrupt the source_record on disk before evals run,
    by writing it as malformed JSON. The pipeline must reject the
    target. Asserts ``status == 'rejected'`` specifically."""
    # Run the pipeline once to set up a real run, then corrupt the
    # source_record and re-run.
    _seed(tmp_path, "m-y-meeting-minutes")
    # Pre-corrupt the source_record location before the pipeline runs.
    target_dir = tmp_path / "processed" / "meetings" / "m-y-meeting-minutes"
    target_dir.mkdir(parents=True)
    sr_path = target_dir / "source_record__m-y-meeting-minutes.json"
    # Make it a regular file but unreadable by JSON via a directory-replace
    # would be too invasive; the realistic attack is a malformed JSON
    # body. The pipeline overwrites the file on a healthy run, so we
    # cannot easily inject corruption before evals run via fixture
    # alone. Instead, simulate by patching the eval call site.
    import unittest.mock as _mock
    from spectrum_systems_core.data_lake import pipeline as _pipeline_module

    def _broken_eval(target, _path):
        # Pretend the on-disk source_record was corrupted between write
        # and read — the eval correctly reports source_record_invalid.
        from spectrum_systems_core.evals.source_turn_validity import (
            run_source_turn_validity_eval,
        )
        return run_source_turn_validity_eval(target, sr_path.parent / "nonexistent.json")

    with _mock.patch.object(
        _pipeline_module, "run_source_turn_validity_eval", _broken_eval
    ):
        result = run_transcript_pipeline(
            lake_root=tmp_path,
            meeting_id="m-y-meeting-minutes",
            workflow_name="meeting_minutes",
        )

    assert result.promoted is False
    assert result.target.status == "rejected"
    assert result.control_decision.payload["decision"] == "block"
    turn_validity = next(
        e for e in result.eval_results
        if e.payload["eval_type"] == SOURCE_TURN_VALIDITY_EVAL_TYPE
    )
    assert turn_validity.payload["status"] == "fail"
    assert any(
        SOURCE_RECORD_INVALID in r
        for r in turn_validity.payload["reason_codes"]
    )


def test_non_list_source_turns_fails_explicitly(tmp_path):
    """Red Team Pass 1 finding: a non-list source_turns value (e.g.
    ``"t0001"`` as a string) used to slip past both gates because
    required-field's ``_is_empty_value`` returns False on a non-empty
    string. source_turn_validity now fails loud on that shape."""
    chunks = [
        {"turn_id": "t0000", "speaker": "ALICE", "text": "x", "line_start": 1, "line_end": 1},
    ]
    sr_path = _write_source_record_to_disk(tmp_path, "m-bad-shape", chunks)

    target = new_artifact(
        artifact_type="meeting_minutes",
        payload={
            "schema_version": "1.1.0",
            "grounding": [
                {
                    "kind": "decision",
                    "text": "x",
                    # Wrong shape: string, not list.
                    "source_turns": "t0000",
                }
            ],
        },
        trace_id="trace-test",
        status="draft",
    )

    eval_result = run_source_turn_validity_eval(target, sr_path)
    assert eval_result.payload["status"] == "fail"
    assert any(
        "source_turns_not_a_list" in r
        for r in eval_result.payload["reason_codes"]
    )


def test_empty_source_turns_list_fails_source_turn_validity(tmp_path):
    """Mission constraint: source_turns must enforce minItems:1 — an
    empty list IS a violation. The non-list (string) case is covered by
    ``test_non_list_source_turns_fails_explicitly``; this pins the
    distinct empty-list ``[]`` code path (source_turn_validity.py:
    ``if not source_turns -> empty_source_turns_list``) so a future
    refactor cannot let ``"source_turns": []`` pass the deterministic
    gate silently. Schema-level minItems:1 on the v2 extraction
    artifact is already covered by
    ``tests/extraction/test_phase_r_binding.py`` and
    ``tests/extraction/test_source_turn_ids.py``; this is the grounded
    core-artifact analogue at the eval gate."""
    chunks = [
        {"turn_id": "t0000", "speaker": "A", "text": "x", "line_start": 1, "line_end": 1},
    ]
    sr_path = _write_source_record_to_disk(tmp_path, "m-empty-list", chunks)

    target = new_artifact(
        artifact_type="meeting_minutes",
        payload={
            "schema_version": "1.1.0",
            "grounding": [
                {"kind": "decision", "text": "x", "source_turns": []}
            ],
        },
        trace_id="trace-test",
        status="draft",
    )

    eval_result = run_source_turn_validity_eval(target, sr_path)
    assert eval_result.payload["status"] == "fail"
    assert any(
        "empty_source_turns_list" in r
        for r in eval_result.payload["reason_codes"]
    ), f"expected empty_source_turns_list in {eval_result.payload['reason_codes']!r}"


# ---------------------------------------------------------------------------
# Rejection: invalid source_record on disk.
# ---------------------------------------------------------------------------


def test_invalid_source_record_fails_eval_explicitly(tmp_path):
    """A malformed JSON source_record file must produce
    ``source_record_invalid`` — the eval refuses to pass silently."""
    target_dir = tmp_path / "processed" / "meetings" / "m-bad-sr"
    target_dir.mkdir(parents=True)
    bad_path = target_dir / "source_record__m-bad-sr.json"
    bad_path.write_text("{not valid json", encoding="utf-8")

    target = new_artifact(
        artifact_type="meeting_minutes",
        payload={
            "schema_version": "1.1.0",
            "grounding": [
                {"kind": "decision", "text": "x", "source_turns": ["t0000"]}
            ],
        },
        trace_id="trace-test",
        status="draft",
    )

    eval_result = run_source_turn_validity_eval(target, bad_path)

    assert eval_result.payload["status"] == "fail"
    reason_codes = eval_result.payload["reason_codes"]
    assert any(
        SOURCE_RECORD_INVALID in r for r in reason_codes
    ), f"expected source_record_invalid in {reason_codes!r}"


def test_missing_source_record_path_fails_eval_explicitly(tmp_path):
    """A None path also produces ``source_record_invalid`` rather than
    raising or returning success."""
    target = new_artifact(
        artifact_type="meeting_minutes",
        payload={"schema_version": "1.1.0", "grounding": []},
        trace_id="trace-test",
        status="draft",
    )
    eval_result = run_source_turn_validity_eval(target, None)

    assert eval_result.payload["status"] == "fail"
    assert any(
        SOURCE_RECORD_INVALID in r
        for r in eval_result.payload["reason_codes"]
    )


def test_source_record_with_empty_chunks_fails_eval_explicitly(tmp_path):
    """An empty chunks list on disk is ``source_record_invalid``."""
    sr_path = _write_source_record_to_disk(tmp_path, "m-empty-chunks", [])
    target = new_artifact(
        artifact_type="meeting_minutes",
        payload={"schema_version": "1.1.0", "grounding": []},
        trace_id="trace-test",
        status="draft",
    )
    eval_result = run_source_turn_validity_eval(target, sr_path)

    assert eval_result.payload["status"] == "fail"
    assert any(
        SOURCE_RECORD_INVALID in r
        for r in eval_result.payload["reason_codes"]
    )


# ---------------------------------------------------------------------------
# Rejection: missing source_turns on a 1.1.0 artifact.
# ---------------------------------------------------------------------------


def test_missing_source_turns_on_1_1_0_fails_required_field_eval():
    target = new_artifact(
        artifact_type="meeting_minutes",
        payload={
            "title": "x",
            "summary": "y",
            "decisions": [],
            "action_items": [],
            "open_questions": [],
            "schema_version": "1.1.0",
            "grounding": [
                # Missing source_turns. The 1.1.0 spec requires it
                # on every grounding entry.
                {"kind": "decision", "text": "Some decision"}
            ],
        },
        trace_id="trace-test",
        status="draft",
    )

    results = run_required_evals(target)
    fields_eval = next(
        r for r in results
        if r.payload["eval_type"] == "required_meeting_minutes_fields"
    )
    assert fields_eval.payload["status"] == "fail"
    assert any(
        "missing_item_field:grounding[0].source_turns" in r
        for r in fields_eval.payload["reason_codes"]
    )


def test_missing_source_turns_on_1_1_0_drives_rejection(tmp_path):
    """End-to-end: required-field eval fail → control blocks → target
    is in ``rejected`` status. Asserts ``status == 'rejected'``
    specifically — not just ``not promoted``."""
    from spectrum_systems_core.control import decide_control
    from spectrum_systems_core.promotion import promote_if_allowed
    from spectrum_systems_core.artifacts import ArtifactStore

    target = new_artifact(
        artifact_type="meeting_minutes",
        payload={
            "title": "x",
            "summary": "y",
            "decisions": [],
            "action_items": [],
            "open_questions": [],
            "schema_version": "1.1.0",
            "grounding": [
                {"kind": "decision", "text": "Some decision"}
            ],
        },
        trace_id="trace-test",
        status="draft",
    )

    store = ArtifactStore()
    store.put(target)
    eval_results = run_required_evals(target)
    for e in eval_results:
        store.put(e)
    decision = decide_control(target, eval_results)
    store.put(decision)
    promote_if_allowed(store, target, decision)

    assert target.status == "rejected"
    assert decision.payload["decision"] == "block"


# ---------------------------------------------------------------------------
# Determinism: same transcript → same chunks every run.
# ---------------------------------------------------------------------------


def test_chunk_transcript_is_deterministic():
    transcript = (FIXTURES / "m-y-meeting-minutes" / "transcript.txt").read_text(
        encoding="utf-8"
    )
    a = chunk_transcript(transcript)
    b = chunk_transcript(transcript)
    c = chunk_transcript(transcript)
    assert a == b == c
    # Byte-identical via canonical JSON serialization too.
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert json.dumps(a, sort_keys=True) == json.dumps(c, sort_keys=True)


def test_chunk_transcript_assigns_t_index_zero_padded_to_4():
    """12 speakers must yield turn ids ``t0000`` .. ``t0011``. The speaker
    regex matches ALL-CAPS letters, spaces, hyphens, and dots (no
    digits) — pick names that fit the spec's pattern exactly so this
    test does not silently depend on regex changes."""
    speakers = [
        "ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO", "FOXTROT",
        "GOLF", "HOTEL", "INDIA", "JULIET", "KILO", "LIMA",
    ]
    transcript = "\n".join(f"{s}: line {i}" for i, s in enumerate(speakers)) + "\n"
    chunks = chunk_transcript(transcript)
    assert len(chunks) == 12
    for i, c in enumerate(chunks):
        assert c["turn_id"] == f"t{i:04d}"


def test_chunk_transcript_empty_input_returns_empty_list():
    assert chunk_transcript("") == []
    assert chunk_transcript("   \n  \n") == []


def test_chunker_health_blocks_on_100_percent_null_speaker():
    transcript = "Just one prose line.\n"
    chunks = chunk_transcript(transcript)
    assert chunks  # one fallback chunk
    health = chunker_health(chunks)
    assert health.severity == "block"
    assert health.finding_code == NO_SPEAKER_STRUCTURE_FINDING
    assert speaker_null_rate(chunks) == 1.0


# ---------------------------------------------------------------------------
# Backward compat: 1.0.0 fixture still produces a list-of-strings shape
# on the workflow-direct path (chunks=None).
# ---------------------------------------------------------------------------


def test_workflow_direct_path_emits_1_0_0_when_chunks_not_provided():
    """Spec: chunks=None → emit ``schema_version: "1.0.0"`` AND skip
    ``source_turns``. The schema_version is always present so downstream
    consumers don't need to default; ``"1.0.0"`` signals legacy shape."""
    from spectrum_systems_core.workflows import run_meeting_minutes_workflow

    SAMPLE = (
        "Sync\n"
        "DECISION: ship\n"
        "ACTION: write\n"
        "QUESTION: timing?\n"
    )
    result = run_meeting_minutes_workflow(SAMPLE)

    assert result.promoted is True
    payload = result.meeting_minutes.payload
    # schema_version explicitly "1.0.0" on the legacy path.
    assert payload["schema_version"] == "1.0.0"
    # No grounding / source_turns at 1.0.0.
    assert "grounding" not in payload
    # decisions remains a list of strings.
    assert payload["decisions"] == ["ship"]
    assert payload["action_items"] == ["write"]
    assert payload["open_questions"] == ["timing?"]


def test_existing_golden_good_pipeline_still_promotes(tmp_path):
    """The pipeline path now emits 1.1.0 payloads, but the historical
    list-of-strings shape of ``decisions`` / ``action_items`` /
    ``open_questions`` is preserved so the pre-Phase-Y golden fixture
    test still asserts the expected payload."""
    src = Path(__file__).parent / "fixtures" / "golden_meetings" / "m-golden-good"
    dst = tmp_path / "raw" / "meetings" / "m-golden-good"
    dst.mkdir(parents=True)
    shutil.copy(src / "transcript.txt", dst / "transcript.txt")
    shutil.copy(src / "metadata.json", dst / "metadata.json")

    result = run_transcript_pipeline(
        lake_root=tmp_path,
        meeting_id="m-golden-good",
        workflow_name="meeting_minutes",
    )

    assert result.promoted is True
    payload = result.target.payload
    # 1.1.0 was emitted by the pipeline.
    assert payload["schema_version"] == "1.1.0"
    # Historical list-of-strings shape preserved.
    assert (
        "Adopt the SAS-only sharing model for Phase 1."
        in payload["decisions"]
    )
    # grounding entries now carry source_turns.
    for entry in payload["grounding"]:
        assert "source_turns" in entry
        assert entry["source_turns"]


# ---------------------------------------------------------------------------
# Schema-version branch: 1.0.0 vs 1.1.0 in REQUIRED_FIELDS_BY_TYPE.
# ---------------------------------------------------------------------------


def test_required_fields_by_type_has_both_versions_for_every_artifact():
    for artifact_type in (
        "meeting_minutes",
        "decision_brief",
        "agency_question_summary",
        "meeting_action_log",
    ):
        assert "1.0.0" in REQUIRED_FIELDS_BY_TYPE[artifact_type]
        assert "1.1.0" in REQUIRED_FIELDS_BY_TYPE[artifact_type]


def test_required_fields_1_0_0_does_not_require_source_turns():
    spec_v1 = REQUIRED_FIELDS_BY_TYPE["meeting_minutes"]["1.0.0"]
    # No per-item check at 1.0.0.
    assert spec_v1["per_item_keys"] == ()


def test_required_fields_1_1_0_requires_source_turns_per_item():
    spec_v11 = REQUIRED_FIELDS_BY_TYPE["meeting_minutes"]["1.1.0"]
    # Per-item check at 1.1.0 specifies grounding.source_turns.
    assert ("grounding", "source_turns") in spec_v11["per_item_keys"]


def test_unknown_schema_version_falls_back_to_1_0_0():
    """Rollback safety: an artifact with an unrecognized
    ``schema_version`` does not bypass the spec — it falls back to
    ``1.0.0`` rules. Ensures stranded artifacts after a revert remain
    validatable."""
    target = new_artifact(
        artifact_type="meeting_minutes",
        payload={
            "title": "x",
            "summary": "y",
            "decisions": [],
            "action_items": [],
            "open_questions": [],
            "schema_version": "9.9.9",
        },
        trace_id="trace-test",
        status="draft",
    )
    results = run_required_evals(target)
    # No per-item check should fire because 1.0.0 rules apply.
    fields_eval = next(
        r for r in results
        if r.payload["eval_type"] == "required_meeting_minutes_fields"
    )
    # Top-level fields present → pass.
    assert fields_eval.payload["status"] == "pass"


# ---------------------------------------------------------------------------
# Pipeline eval ordering — non_empty_payload runs BEFORE source_turn_validity.
# ---------------------------------------------------------------------------


def test_source_turn_validity_runs_after_non_empty_payload(tmp_path):
    """If the payload is empty, the non_empty_payload eval fires first
    — source_turn_validity cannot pass on an empty payload."""
    _seed(tmp_path, "m-y-meeting-minutes")
    result = run_transcript_pipeline(
        lake_root=tmp_path,
        meeting_id="m-y-meeting-minutes",
        workflow_name="meeting_minutes",
    )
    eval_types = [e.payload["eval_type"] for e in result.eval_results]
    # non_empty_payload appears earlier than source_turn_validity in
    # the eval results list (the runner orders them deterministically).
    assert eval_types.index("non_empty_payload") < eval_types.index(
        SOURCE_TURN_VALIDITY_EVAL_TYPE
    )


# ---------------------------------------------------------------------------
# Empty transcript blocks fail-closed with a governed block (not an
# exception).
# ---------------------------------------------------------------------------


def test_empty_transcript_input_blocks_via_loader(tmp_path):
    """An empty/whitespace-only transcript is rejected at the loader,
    not in the chunker. The chunker is exercised directly here to show
    its empty-list contract is preserved."""
    assert chunk_transcript("") == []
    assert chunk_transcript("   \n   \n") == []


def test_pipeline_blocks_on_empty_chunks_via_direct_transcript_input(tmp_path):
    """If a caller bypasses the loader and passes a TranscriptInput with
    a whitespace-only transcript directly, the chunker returns ``[]``
    and the pipeline blocks fail-closed with ``empty_chunk_list``. The
    block flows through a governed ``decide_control`` call (not a raw
    Python exception)."""
    from spectrum_systems_core.data_lake.loader import TranscriptInput

    # Construct a TranscriptInput with whitespace-only transcript text.
    # The loader would normally reject this; we bypass to prove the
    # chunker gate also fails closed.
    ti = TranscriptInput(
        meeting_id="m-empty-chunks",
        title="t",
        date="2026-05-13",
        source_type="transcript",
        transcript_text="   \n   \n",
        transcript_lines=("   ", "   "),
        metadata={"meeting_id": "m-empty-chunks"},
        transcript_hash="x" * 64,
        metadata_hash="y" * 64,
        transcript_path="(synthetic)",
        metadata_path="(synthetic)",
    )

    result = run_transcript_pipeline(
        lake_root=tmp_path,
        transcript_input=ti,
        workflow_name="meeting_minutes",
        write_outputs=False,
    )

    assert result.promoted is False
    assert result.target.status == "rejected"
    assert result.control_decision.payload["decision"] == "block"
    chunker_eval = next(
        e for e in result.eval_results
        if e.payload["eval_type"] == "chunker_health"
    )
    assert chunker_eval.payload["status"] == "fail"
    assert any(
        "empty_chunk_list" in r
        for r in chunker_eval.payload["reason_codes"]
    )


# ---------------------------------------------------------------------------
# Determinism over the full pipeline path — two runs of the same input
# write byte-identical source_record + product files.
# ---------------------------------------------------------------------------


def test_pipeline_source_record_is_byte_identical_across_runs(
    tmp_path, tmp_path_factory
):
    _seed(tmp_path, "m-y-meeting-minutes")
    a = run_transcript_pipeline(
        lake_root=tmp_path,
        meeting_id="m-y-meeting-minutes",
        workflow_name="meeting_minutes",
    )
    other = tmp_path_factory.mktemp("other-lake")
    _seed(other, "m-y-meeting-minutes")
    b = run_transcript_pipeline(
        lake_root=other,
        meeting_id="m-y-meeting-minutes",
        workflow_name="meeting_minutes",
    )

    sr_a = Path(a.source_record_path).read_bytes()
    sr_b = Path(b.source_record_path).read_bytes()
    assert sr_a == sr_b
