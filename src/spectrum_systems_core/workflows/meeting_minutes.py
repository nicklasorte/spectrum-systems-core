from __future__ import annotations

from dataclasses import dataclass, field

from ..artifacts import Artifact, ArtifactStore
from ._loop import run_governed_loop


@dataclass
class WorkflowResult:
    context_bundle: Artifact
    meeting_minutes: Artifact
    eval_results: list[Artifact]
    control_decision: Artifact
    promoted: bool
    store: ArtifactStore = field(default_factory=ArtifactStore)


def _extract_meeting_minutes(input_text: str) -> dict:
    lines = [ln.strip() for ln in input_text.splitlines() if ln.strip()]
    title = lines[0] if lines else "Untitled meeting"

    decisions: list[str] = []
    action_items: list[str] = []
    open_questions: list[str] = []
    summary_lines: list[str] = []

    for line in lines:
        upper = line.upper()
        if upper.startswith("DECISION:"):
            decisions.append(line.split(":", 1)[1].strip())
        elif upper.startswith("ACTION:"):
            action_items.append(line.split(":", 1)[1].strip())
        elif upper.startswith("QUESTION:"):
            open_questions.append(line.split(":", 1)[1].strip())
        else:
            summary_lines.append(line)

    summary = " ".join(summary_lines) if summary_lines else title

    return {
        "title": title,
        "summary": summary,
        "decisions": decisions,
        "action_items": action_items,
        "open_questions": open_questions,
    }


def run_meeting_minutes_workflow(input_text: str) -> WorkflowResult:
    run = run_governed_loop(
        input_text=input_text,
        artifact_type="meeting_minutes",
        extract=_extract_meeting_minutes,
    )
    return WorkflowResult(
        context_bundle=run.context_bundle,
        meeting_minutes=run.target,
        eval_results=run.eval_results,
        control_decision=run.control_decision,
        promoted=run.promoted,
        store=run.store,
    )
