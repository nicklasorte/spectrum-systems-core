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

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3]
    / "contracts"
    / "schemas"
    / "extraction"
    / "improvement_cycle_result.schema.json"
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


__all__ = [
    "ARTIFACT_TYPE",
    "SCHEMA_VERSION",
    "PHASES",
    "ImprovementCycleError",
    "run_improvement_cycle",
]
