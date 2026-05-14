from __future__ import annotations

from dataclasses import dataclass, field

from ..artifacts import Artifact, ArtifactStore
from ._loop import run_governed_loop
from .extraction import find_source_turns


@dataclass
class MeetingActionLogResult:
    context_bundle: Artifact
    meeting_action_log: Artifact
    eval_results: list[Artifact]
    control_decision: Artifact
    promoted: bool
    store: ArtifactStore = field(default_factory=ArtifactStore)


def _build_base_payload(input_text: str) -> dict:
    lines = [ln.strip() for ln in input_text.splitlines() if ln.strip()]
    title = lines[0] if lines else "Untitled action log"

    meeting_ref = ""
    actions: list[str] = []

    for line in lines[1:]:
        upper = line.upper()
        if upper.startswith("MEETING_REF:"):
            meeting_ref = line.split(":", 1)[1].strip()
        elif upper.startswith("ACTION:"):
            actions.append(line.split(":", 1)[1].strip())

    return {
        "title": title,
        "meeting_ref": meeting_ref,
        "actions": actions,
        "open_count": len(actions),
    }


def _build_grounding_entries(
    payload: dict, chunks: list[dict]
) -> list[dict]:
    entries: list[dict] = []
    if payload["meeting_ref"]:
        entries.append(
            {
                "kind": "meeting_ref",
                "text": payload["meeting_ref"],
                "source_turns": find_source_turns(
                    payload["meeting_ref"], chunks
                ),
            }
        )
    for action_text in payload["actions"]:
        entries.append(
            {
                "kind": "action_item",
                "text": action_text,
                "source_turns": find_source_turns(action_text, chunks),
            }
        )
    return entries


def _extract_meeting_action_log(
    input_text: str, chunks: list[dict] | None = None
) -> dict:
    """Phase Y: chunks=None → schema_version="1.0.0" (no source_turns);
    chunks provided → schema_version="1.1.0" with a grounding list."""
    payload = _build_base_payload(input_text)
    if chunks is None:
        payload["schema_version"] = "1.0.0"
        return payload
    payload["schema_version"] = "1.1.0"
    payload["grounding"] = _build_grounding_entries(payload, chunks)
    return payload


def run_meeting_action_log_workflow(
    input_text: str, *, chunks: list[dict] | None = None
) -> MeetingActionLogResult:
    run = run_governed_loop(
        input_text=input_text,
        artifact_type="meeting_action_log",
        extract=_extract_meeting_action_log,
        chunks=chunks,
    )
    return MeetingActionLogResult(
        context_bundle=run.context_bundle,
        meeting_action_log=run.target,
        eval_results=run.eval_results,
        control_decision=run.control_decision,
        promoted=run.promoted,
        store=run.store,
    )
