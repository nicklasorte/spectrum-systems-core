from __future__ import annotations

from dataclasses import dataclass, field

from ..artifacts import Artifact, ArtifactStore
from ._loop import run_governed_loop
from .extraction import find_source_turns


@dataclass
class DecisionBriefResult:
    context_bundle: Artifact
    decision_brief: Artifact
    eval_results: list[Artifact]
    control_decision: Artifact
    promoted: bool
    store: ArtifactStore = field(default_factory=ArtifactStore)


def _build_base_payload(input_text: str) -> dict:
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


def _build_grounding_entries(
    payload: dict, chunks: list[dict]
) -> list[dict]:
    entries: list[dict] = []
    if payload["context"]:
        entries.append(
            {
                "kind": "context",
                "text": payload["context"],
                "source_turns": find_source_turns(payload["context"], chunks),
            }
        )
    for option_text in payload["options"]:
        entries.append(
            {
                "kind": "option",
                "text": option_text,
                "source_turns": find_source_turns(option_text, chunks),
            }
        )
    if payload["recommendation"]:
        entries.append(
            {
                "kind": "recommendation",
                "text": payload["recommendation"],
                "source_turns": find_source_turns(
                    payload["recommendation"], chunks
                ),
            }
        )
    if payload["rationale"]:
        entries.append(
            {
                "kind": "rationale",
                "text": payload["rationale"],
                "source_turns": find_source_turns(payload["rationale"], chunks),
            }
        )
    return entries


def _extract_decision_brief(
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


def run_decision_brief_workflow(
    input_text: str, *, chunks: list[dict] | None = None
) -> DecisionBriefResult:
    run = run_governed_loop(
        input_text=input_text,
        artifact_type="decision_brief",
        extract=_extract_decision_brief,
        chunks=chunks,
    )
    return DecisionBriefResult(
        context_bundle=run.context_bundle,
        decision_brief=run.target,
        eval_results=run.eval_results,
        control_decision=run.control_decision,
        promoted=run.promoted,
        store=run.store,
    )
