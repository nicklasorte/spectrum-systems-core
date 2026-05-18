"""Phase Y.8 — improvement-cycle harness driver.

Sequences Y.1 .. Y.7 over one transcript and records, per phase,
whether it is ``present`` / ``partial`` / ``missing`` / ``unknown``.
``overall_status`` is ``promoted`` ONLY when every phase is
``present``; the first non-present phase names ``blocking_phase`` and
every later phase is ``unknown`` (it never ran).

Pre-flight gate (runs BEFORE any phase): if an open PR exists on a
``correction/*`` branch that references this ``transcript_id``, the
cycle halts with ``prior_open_correction_pr`` and runs NO phase — a
candidate must never be mined against stale Haiku output that an
in-flight PR is about to change.

The ``improvement_cycle_result`` payload is validated against its
schema INSIDE this function before the artifact is returned, so a
shape regression fails here, in the cycle, not later when CI reloads
the file (Phase Y red-team Pass 1 #4).
"""
from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import Callable
from pathlib import Path

import jsonschema

from ..artifacts import Artifact, new_artifact

ARTIFACT_TYPE = "improvement_cycle_result"
SCHEMA_VERSION = "1.0.0"
PHASES: tuple[str, ...] = ("Y_1", "Y_2", "Y_3", "Y_4", "Y_5", "Y_6", "Y_7")

CORPUS_SUMMARY_ARTIFACT_TYPE = "corpus_improvement_summary"
CORPUS_SUMMARY_SCHEMA_VERSION = "1.0.0"

_SCHEMA_DIR = (
    Path(__file__).resolve().parents[3] / "contracts" / "schemas"
)
_SCHEMA_PATH = (
    _SCHEMA_DIR / "extraction" / "improvement_cycle_result.schema.json"
)
_CORPUS_SUMMARY_SCHEMA_PATH = (
    _SCHEMA_DIR / "harness" / "corpus_improvement_summary.schema.json"
)

# phase name -> () -> artifact_id (raises on failure).
PhaseFunc = Callable[[], str]
# transcript_id -> list of open correction/* PR identifiers.
OpenPrLookup = Callable[[str], list[str]]
Clock = Callable[[], str]


class ImprovementCycleError(RuntimeError):
    def __init__(self, message: str, *, reason_code: str):
        super().__init__(message)
        self.reason_code = reason_code


def _now() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _empty_phase_status() -> dict:
    return {
        p: {
            "status": "unknown",
            "artifact_id_or_none": None,
            "started_at": None,
            "finished_at": None,
            "error_or_none": None,
        }
        for p in PHASES
    }


def _validate(payload: dict) -> None:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(payload, schema)


def _validate_corpus_summary(payload: dict) -> None:
    schema = json.loads(
        _CORPUS_SUMMARY_SCHEMA_PATH.read_text(encoding="utf-8")
    )
    jsonschema.validate(payload, schema)


# transcript_id -> the per_transcript record dict for one transcript.
PerTranscriptRunner = Callable[[str], dict]
# () -> the corpus_ingest_summary payload, or None when not ingested.
CorpusIngestSummaryLoader = Callable[[], dict | None]

_PER_TRANSCRIPT_KEYS = (
    "transcript_id",
    "overall_status",
    "total_f1",
    "false_negative_count",
    "correction_candidates_produced",
    "blocking_phase",
    "error_or_none",
)


def run_improvement_cycle(
    *,
    transcript_id: str,
    phase_funcs: dict[str, PhaseFunc],
    open_pr_lookup: OpenPrLookup | None = None,
    cycle_id: str | None = None,
    clock: Clock | None = None,
) -> Artifact:
    now = clock or _now
    cid = cycle_id or str(uuid.uuid4())
    lookup = open_pr_lookup or (lambda _tid: [])

    open_prs = lookup(transcript_id)
    prior_open_pr_check = {
        "checked": True,
        "found_open_pr_id": open_prs[0] if open_prs else None,
    }

    phase_status = _empty_phase_status()

    if open_prs:
        # Pre-flight halt — NO phase runs. Every phase stays "unknown".
        payload = {
            "artifact_type": ARTIFACT_TYPE,
            "schema_version": SCHEMA_VERSION,
            "transcript_id": transcript_id,
            "cycle_id": cid,
            "phase_status": phase_status,
            "overall_status": "blocked",
            "blocking_phase": "preflight",
            "prior_open_pr_check": prior_open_pr_check,
        }
        _validate(payload)
        return new_artifact(
            artifact_type=ARTIFACT_TYPE,
            payload=payload,
            trace_id=f"cycle-{cid[:16]}",
            status="draft",
        )

    blocking_phase: str | None = None
    for phase in PHASES:
        if blocking_phase is not None:
            # An earlier phase failed; this one never ran.
            phase_status[phase]["status"] = "unknown"
            continue
        func = phase_funcs.get(phase)
        started = now()
        phase_status[phase]["started_at"] = started
        if func is None:
            phase_status[phase]["status"] = "missing"
            phase_status[phase]["finished_at"] = now()
            phase_status[phase]["error_or_none"] = "phase_func_not_provided"
            blocking_phase = phase
            continue
        try:
            artifact_id = func()
        except Exception as exc:  # noqa: BLE001 — record, never swallow
            phase_status[phase]["status"] = "missing"
            phase_status[phase]["finished_at"] = now()
            phase_status[phase]["error_or_none"] = (
                f"{type(exc).__name__}: {exc}"
            )
            blocking_phase = phase
            continue
        phase_status[phase]["status"] = "present"
        phase_status[phase]["artifact_id_or_none"] = str(artifact_id)
        phase_status[phase]["finished_at"] = now()

    overall = (
        "promoted"
        if all(phase_status[p]["status"] == "present" for p in PHASES)
        else "blocked"
    )
    payload = {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "transcript_id": transcript_id,
        "cycle_id": cid,
        "phase_status": phase_status,
        "overall_status": overall,
        "blocking_phase": blocking_phase,
        "prior_open_pr_check": prior_open_pr_check,
    }
    _validate(payload)
    return new_artifact(
        artifact_type=ARTIFACT_TYPE,
        payload=payload,
        trace_id=f"cycle-{cid[:16]}",
        status="draft",
    )


def _normalise_per_transcript(transcript_id: str, raw: dict) -> dict:
    """Coerce a per_transcript_runner result into the schema shape.

    Fail-closed: a runner that returns a malformed dict is recorded as
    an ``error`` for that transcript, never silently coerced into a
    misleading ``promoted``."""
    status = raw.get("overall_status")
    if status not in {"promoted", "blocked", "error"}:
        return {
            "transcript_id": transcript_id,
            "overall_status": "error",
            "total_f1": None,
            "false_negative_count": None,
            "correction_candidates_produced": None,
            "blocking_phase": None,
            "error_or_none": (
                f"malformed_per_transcript_record:overall_status="
                f"{status!r}"
            ),
        }
    return {
        "transcript_id": transcript_id,
        "overall_status": status,
        "total_f1": raw.get("total_f1"),
        "false_negative_count": raw.get("false_negative_count"),
        "correction_candidates_produced": raw.get(
            "correction_candidates_produced"
        ),
        "blocking_phase": raw.get("blocking_phase"),
        "error_or_none": raw.get("error_or_none"),
    }


def run_corpus_improvement_cycle(
    *,
    transcript_ids: list[str],
    corpus_ingest_summary_loader: CorpusIngestSummaryLoader,
    per_transcript_runner: PerTranscriptRunner,
    open_pr_lookup: OpenPrLookup | None = None,
    cycle_id: str | None = None,
    clock: Clock | None = None,
) -> Artifact:
    """Phase Z.5 — run the improvement cycle over the whole corpus.

    The pre-flight gate is ALL-OR-NOTHING and runs BEFORE any
    per-transcript work:

      1. ``corpus_ingest_summary`` absent  -> ``corpus_not_ingested``
      2. its ``blocked > 0``               -> ``corpus_partially_blocked``
      3. an open ``correction/*`` PR for ANY transcript
                                           -> ``prior_open_correction_pr``

    On a pre-flight halt NO transcript runs (``per_transcript`` is
    ``[]`` — red-team Pass 2 #4). Past pre-flight, per-transcript
    errors are isolated: one transcript raising never cancels the
    others. ``corpus_f1`` is the mean ``total_f1`` over the present
    (promoted) transcripts, and is ``null`` with an explicit
    ``corpus_f1_null_reason`` when none completed (red-team Pass 1 #5
    — never a div-by-zero / NaN)."""
    now = clock or _now
    cid = cycle_id or str(uuid.uuid4())
    lookup = open_pr_lookup or (lambda _tid: [])

    def _emit(
        *,
        present: int,
        blocked: int,
        corpus_f1: float | None,
        corpus_f1_null_reason: str | None,
        preflight_halt_reason: str | None,
        per_transcript: list[dict],
    ) -> Artifact:
        payload = {
            "artifact_type": CORPUS_SUMMARY_ARTIFACT_TYPE,
            "schema_version": CORPUS_SUMMARY_SCHEMA_VERSION,
            "produced_at": now(),
            "cycle_id": cid,
            "total_transcripts": len(transcript_ids),
            "present": present,
            "blocked": blocked,
            "corpus_f1": corpus_f1,
            "corpus_f1_null_reason": corpus_f1_null_reason,
            "preflight_halt_reason": preflight_halt_reason,
            "per_transcript": per_transcript,
        }
        _validate_corpus_summary(payload)
        return new_artifact(
            artifact_type=CORPUS_SUMMARY_ARTIFACT_TYPE,
            payload=payload,
            trace_id=f"corpus-cycle-{cid[:16]}",
            status="draft",
        )

    # ---- pre-flight (all-or-nothing; no transcript runs) ----
    summary = corpus_ingest_summary_loader()
    if summary is None:
        return _emit(
            present=0, blocked=0, corpus_f1=None,
            corpus_f1_null_reason="preflight_halt:corpus_not_ingested",
            preflight_halt_reason="corpus_not_ingested",
            per_transcript=[],
        )
    if int(summary.get("blocked", 0)) > 0:
        blocked_ids = summary.get("blocked_ids", [])
        reason = f"corpus_partially_blocked:{sorted(blocked_ids)}"
        return _emit(
            present=0, blocked=0, corpus_f1=None,
            corpus_f1_null_reason=f"preflight_halt:{reason}",
            preflight_halt_reason=reason,
            per_transcript=[],
        )
    for tid in transcript_ids:
        if lookup(tid):
            reason = f"prior_open_correction_pr:{tid}"
            return _emit(
                present=0, blocked=0, corpus_f1=None,
                corpus_f1_null_reason=f"preflight_halt:{reason}",
                preflight_halt_reason=reason,
                per_transcript=[],
            )

    # ---- per-transcript run (errors isolated) ----
    per_transcript: list[dict] = []
    for tid in transcript_ids:
        try:
            raw = per_transcript_runner(tid)
            record = _normalise_per_transcript(tid, raw)
        except Exception as exc:  # noqa: BLE001 — isolate, never cancel
            record = {
                "transcript_id": tid,
                "overall_status": "error",
                "total_f1": None,
                "false_negative_count": None,
                "correction_candidates_produced": None,
                "blocking_phase": None,
                "error_or_none": f"{type(exc).__name__}: {exc}",
            }
        per_transcript.append(record)

    present_records = [
        r for r in per_transcript if r["overall_status"] == "promoted"
    ]
    present = len(present_records)
    blocked = len(per_transcript) - present

    f1s = [
        float(r["total_f1"])
        for r in present_records
        if isinstance(r["total_f1"], (int, float))
    ]
    if f1s:
        corpus_f1: float | None = sum(f1s) / len(f1s)
        corpus_f1_null_reason: str | None = None
    else:
        corpus_f1 = None
        corpus_f1_null_reason = "no_present_transcript_with_total_f1"

    return _emit(
        present=present,
        blocked=blocked,
        corpus_f1=corpus_f1,
        corpus_f1_null_reason=corpus_f1_null_reason,
        preflight_halt_reason=None,
        per_transcript=per_transcript,
    )


__all__ = [
    "ARTIFACT_TYPE",
    "SCHEMA_VERSION",
    "PHASES",
    "CORPUS_SUMMARY_ARTIFACT_TYPE",
    "CORPUS_SUMMARY_SCHEMA_VERSION",
    "ImprovementCycleError",
    "run_improvement_cycle",
    "run_corpus_improvement_cycle",
]
