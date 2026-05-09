from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from ..artifacts import Artifact, ArtifactStore, new_artifact
from ..context import build_context_bundle
from ..control import decide_control
from ..evals import run_required_evals
from ..promotion import promote_if_allowed


@dataclass
class WorkflowResult:
    context_bundle: Artifact
    meeting_minutes: Artifact
    eval_results: list[Artifact]
    control_decision: Artifact
    promoted: bool
    store: ArtifactStore = field(default_factory=ArtifactStore)


def _derive_trace_id(input_text: str) -> str:
    digest = hashlib.sha256(input_text.encode("utf-8")).hexdigest()
    return f"trace-{digest[:16]}"


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
    store = ArtifactStore()
    trace_id = _derive_trace_id(input_text)

    context_bundle = build_context_bundle(
        input_text=input_text,
        purpose="meeting_minutes",
        trace_id=trace_id,
    )
    store.put(context_bundle)

    payload = _extract_meeting_minutes(input_text)
    minutes = new_artifact(
        artifact_type="meeting_minutes",
        payload=payload,
        trace_id=trace_id,
        status="draft",
        input_refs=[context_bundle.artifact_id],
    )
    store.put(minutes)

    eval_results = run_required_evals(minutes)
    for r in eval_results:
        store.put(r)

    decision = decide_control(minutes, eval_results)
    store.put(decision)

    promote_if_allowed(store, minutes, decision)
    promoted = minutes.status == "promoted"

    return WorkflowResult(
        context_bundle=context_bundle,
        meeting_minutes=minutes,
        eval_results=eval_results,
        control_decision=decision,
        promoted=promoted,
        store=store,
    )
