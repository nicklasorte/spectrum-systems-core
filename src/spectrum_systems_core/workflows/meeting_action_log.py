from __future__ import annotations

from dataclasses import dataclass, field

from ..artifacts import Artifact, ArtifactStore
from ._loop import run_governed_loop


@dataclass
class MeetingActionLogResult:
    context_bundle: Artifact
    meeting_action_log: Artifact
    eval_results: list[Artifact]
    control_decision: Artifact
    promoted: bool
    store: ArtifactStore = field(default_factory=ArtifactStore)


def _extract_meeting_action_log(input_text: str) -> dict:
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


def run_meeting_action_log_workflow(input_text: str) -> MeetingActionLogResult:
    run = run_governed_loop(
        input_text=input_text,
        artifact_type="meeting_action_log",
        extract=_extract_meeting_action_log,
    )
    return MeetingActionLogResult(
        context_bundle=run.context_bundle,
        meeting_action_log=run.target,
        eval_results=run.eval_results,
        control_decision=run.control_decision,
        promoted=run.promoted,
        store=run.store,
    )
