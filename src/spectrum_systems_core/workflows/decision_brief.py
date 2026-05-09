from __future__ import annotations

from dataclasses import dataclass, field

from ..artifacts import Artifact, ArtifactStore
from ._loop import run_governed_loop


@dataclass
class DecisionBriefResult:
    context_bundle: Artifact
    decision_brief: Artifact
    eval_results: list[Artifact]
    control_decision: Artifact
    promoted: bool
    store: ArtifactStore = field(default_factory=ArtifactStore)


def _extract_decision_brief(input_text: str) -> dict:
    lines = [ln.strip() for ln in input_text.splitlines() if ln.strip()]
    title = lines[0] if lines else "Untitled brief"

    context_lines: list[str] = []
    options: list[str] = []
    rationale_lines: list[str] = []
    recommendation = ""

    for line in lines[1:]:
        upper = line.upper()
        if upper.startswith("CONTEXT:"):
            context_lines.append(line.split(":", 1)[1].strip())
        elif upper.startswith("OPTION:"):
            options.append(line.split(":", 1)[1].strip())
        elif upper.startswith("RECOMMENDATION:"):
            recommendation = line.split(":", 1)[1].strip()
        elif upper.startswith("RATIONALE:"):
            rationale_lines.append(line.split(":", 1)[1].strip())

    return {
        "title": title,
        "context": " ".join(context_lines),
        "options": options,
        "recommendation": recommendation,
        "rationale": " ".join(rationale_lines),
    }


def run_decision_brief_workflow(input_text: str) -> DecisionBriefResult:
    run = run_governed_loop(
        input_text=input_text,
        artifact_type="decision_brief",
        extract=_extract_decision_brief,
    )
    return DecisionBriefResult(
        context_bundle=run.context_bundle,
        decision_brief=run.target,
        eval_results=run.eval_results,
        control_decision=run.control_decision,
        promoted=run.promoted,
        store=run.store,
    )
