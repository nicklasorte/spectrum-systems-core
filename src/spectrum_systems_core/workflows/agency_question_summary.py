from __future__ import annotations

from dataclasses import dataclass, field

from ..artifacts import Artifact, ArtifactStore
from ._loop import run_governed_loop
from .extraction import find_source_turns


@dataclass
class AgencyQuestionSummaryResult:
    context_bundle: Artifact
    agency_question_summary: Artifact
    eval_results: list[Artifact]
    control_decision: Artifact
    promoted: bool
    store: ArtifactStore = field(default_factory=ArtifactStore)


def _build_base_payload(input_text: str) -> dict:
    lines = [ln.strip() for ln in input_text.splitlines() if ln.strip()]
    title = lines[0] if lines else "Untitled agency question"

    agency = ""
    question_lines: list[str] = []
    citations: list[str] = []
    summary_lines: list[str] = []

    for line in lines[1:]:
        upper = line.upper()
        if upper.startswith("AGENCY:"):
            agency = line.split(":", 1)[1].strip()
        elif upper.startswith("QUESTION:"):
            question_lines.append(line.split(":", 1)[1].strip())
        elif upper.startswith("CITATION:"):
            citations.append(line.split(":", 1)[1].strip())
        else:
            summary_lines.append(line)

    return {
        "title": title,
        "agency": agency,
        "question": " ".join(question_lines),
        "summary": " ".join(summary_lines),
        "citations": citations,
    }


def _build_grounding_entries(
    payload: dict, chunks: list[dict]
) -> list[dict]:
    entries: list[dict] = []
    if payload["agency"]:
        entries.append(
            {
                "kind": "agency",
                "text": payload["agency"],
                "source_turns": find_source_turns(payload["agency"], chunks),
            }
        )
    if payload["question"]:
        entries.append(
            {
                "kind": "question",
                "text": payload["question"],
                "source_turns": find_source_turns(payload["question"], chunks),
            }
        )
    for citation_text in payload["citations"]:
        entries.append(
            {
                "kind": "citation",
                "text": citation_text,
                "source_turns": find_source_turns(citation_text, chunks),
            }
        )
    return entries


def _extract_agency_question_summary(
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


def run_agency_question_summary_workflow(
    input_text: str, *, chunks: list[dict] | None = None
) -> AgencyQuestionSummaryResult:
    run = run_governed_loop(
        input_text=input_text,
        artifact_type="agency_question_summary",
        extract=_extract_agency_question_summary,
        chunks=chunks,
    )
    return AgencyQuestionSummaryResult(
        context_bundle=run.context_bundle,
        agency_question_summary=run.target,
        eval_results=run.eval_results,
        control_decision=run.control_decision,
        promoted=run.promoted,
        store=run.store,
    )
