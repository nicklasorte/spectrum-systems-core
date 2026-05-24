from __future__ import annotations

from dataclasses import dataclass, field

from ..artifacts import Artifact, ArtifactStore
from ._loop import run_governed_loop
from .extraction import find_source_turns


@dataclass
class WorkflowResult:
    context_bundle: Artifact
    meeting_minutes: Artifact
    eval_results: list[Artifact]
    control_decision: Artifact
    promoted: bool
    store: ArtifactStore = field(default_factory=ArtifactStore)


def _build_base_payload(input_text: str) -> dict:
    lines = [ln.strip() for ln in input_text.splitlines() if ln.strip()]
    title = lines[0] if lines else "Untitled meeting"

    decisions: list[str] = []
    action_items: list[dict] = []
    open_questions: list[str] = []
    summary_lines: list[str] = []

    for line in lines:
        upper = line.upper()
        if upper.startswith("DECISION:"):
            decisions.append(line.split(":", 1)[1].strip())
        elif upper.startswith("ACTION:"):
            action_items.append({"action": line.split(":", 1)[1].strip()})
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


def _build_grounding_entries(
    payload: dict, chunks: list[dict]
) -> list[dict]:
    """Build grounding entries with ``source_turns`` from the extracted
    item lists. Each (kind, text) pair becomes one grounding entry."""
    entries: list[dict] = []
    for kind, key in (
        ("decision", "decisions"),
        ("action_item", "action_items"),
        ("open_question", "open_questions"),
    ):
        for item in payload[key]:
            item_text = item["action"] if kind == "action_item" else item
            entries.append(
                {
                    "kind": kind,
                    "text": item_text,
                    "source_turns": find_source_turns(item_text, chunks),
                }
            )
    return entries


def _extract_meeting_minutes(
    input_text: str, chunks: list[dict] | None = None
) -> dict:
    """Deterministic meeting-minutes extractor.

    Phase Y spec: ``chunks=None`` (legacy path) emits
    ``schema_version: "1.0.0"`` and skips ``source_turns``. ``chunks``
    provided emits ``schema_version: "1.1.0"`` and a ``grounding`` list
    with one entry per extracted decision / action / question, each
    carrying a ``source_turns`` list. The schema_version key is always
    present so downstream consumers never have to default.
    """
    payload = _build_base_payload(input_text)
    if chunks is None:
        payload["schema_version"] = "1.0.0"
        return payload
    payload["schema_version"] = "1.1.0"
    payload["grounding"] = _build_grounding_entries(payload, chunks)
    return payload


def run_meeting_minutes_workflow(
    input_text: str, *, chunks: list[dict] | None = None
) -> WorkflowResult:
    run = run_governed_loop(
        input_text=input_text,
        artifact_type="meeting_minutes",
        extract=_extract_meeting_minutes,
        chunks=chunks,
    )
    return WorkflowResult(
        context_bundle=run.context_bundle,
        meeting_minutes=run.target,
        eval_results=run.eval_results,
        control_decision=run.control_decision,
        promoted=run.promoted,
        store=run.store,
    )
