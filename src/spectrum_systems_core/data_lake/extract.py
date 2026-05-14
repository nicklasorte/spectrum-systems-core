"""Grounding-aware extractors for transcript-derived artifacts.

These wrap the existing deterministic extractors in workflows/* and add:
- meeting_id (from metadata)
- grounding spans (from transcript line scan)

Per-workflow grounding is a list of `(prefix, kind)` pairs in
`_GROUNDING_PREFIXES`. A single `_ground_by_prefix_table` walks the
transcript once per prefix. The wrapped extractor preserves the original
payload shape exactly, so tests in tests/test_loop_generality.py still
pass against the bare extractors.

Phase Y adds optional ``chunks`` plumbing: when ``build_grounded_payload``
is invoked with a non-None ``chunks`` argument, each grounding entry is
augmented with a ``source_turns`` list and the payload's
``schema_version`` is bumped to ``"1.1.0"``.
"""
from __future__ import annotations

from typing import Callable

from ..workflows.agency_question_summary import _extract_agency_question_summary
from ..workflows.decision_brief import _extract_decision_brief
from ..workflows.extraction import (
    SOURCE_MATCH_FALLBACK,
    match_source_turns,
)
from ..workflows.meeting_action_log import _extract_meeting_action_log
from ..workflows.meeting_minutes import _extract_meeting_minutes
from .grounding import GROUNDING_KEY, MEETING_ID_KEY, find_lines_with_prefix
from .loader import TranscriptInput

_GROUNDING_PREFIXES: dict[str, list[tuple[str, str]]] = {
    "meeting_minutes": [
        ("DECISION:", "decision"),
        ("ACTION:", "action_item"),
        ("QUESTION:", "open_question"),
    ],
    "decision_brief": [
        ("CONTEXT:", "context"),
        ("OPTION:", "option"),
        ("RECOMMENDATION:", "recommendation"),
        ("RATIONALE:", "rationale"),
    ],
    "agency_question_summary": [
        ("AGENCY:", "agency"),
        ("QUESTION:", "question"),
        ("CITATION:", "citation"),
    ],
    "meeting_action_log": [
        ("MEETING_REF:", "meeting_ref"),
        ("ACTION:", "action_item"),
    ],
}

_BASE_EXTRACTORS: dict[str, Callable[[str], dict]] = {
    "meeting_minutes": _extract_meeting_minutes,
    "decision_brief": _extract_decision_brief,
    "agency_question_summary": _extract_agency_question_summary,
    "meeting_action_log": _extract_meeting_action_log,
}


def _ground_by_prefix_table(
    transcript_input: TranscriptInput, table: list[tuple[str, str]]
) -> list[dict]:
    out: list[dict] = []
    for prefix, kind in table:
        for line_no, raw in find_lines_with_prefix(transcript_input, prefix):
            text = raw.split(":", 1)[1].strip() if ":" in raw else raw.strip()
            out.append({
                "kind": kind,
                "text": text,
                "source_excerpt": raw,
                "start_line": line_no,
                "end_line": line_no,
            })
    return out


# Compatibility shim: expose the old (extract, grounder) tuple shape used by
# pipeline.py and tests that monkeypatch the table.
GROUNDED_EXTRACTORS: dict[
    str, tuple[Callable[[str], dict], Callable[[TranscriptInput], list[dict]]]
] = {
    name: (
        _BASE_EXTRACTORS[name],
        (lambda ti, table=_GROUNDING_PREFIXES[name]: _ground_by_prefix_table(ti, table)),
    )
    for name in _BASE_EXTRACTORS
}


def supported_workflow(name: str) -> bool:
    return name in GROUNDED_EXTRACTORS


def attach_source_turns(
    grounding_entries: list[dict], chunks: list[dict]
) -> list[str]:
    """Add ``source_turns`` to each grounding entry in place. Returns the
    list of fallback finding codes the pipeline should record alongside
    the run (one ``source_match_fallback`` entry per fallback item).
    Exposed as a top-level function so the pipeline can call it without
    cracking apart the payload-builder return shape — keeps
    ``build_grounded_payload``'s ``-> dict`` contract intact for the
    test suite."""
    findings: list[str] = []
    for entry in grounding_entries:
        result = match_source_turns(entry.get("text", ""), chunks)
        entry["source_turns"] = result.turn_ids
        if result.was_fallback:
            findings.append(SOURCE_MATCH_FALLBACK)
    return findings


def build_grounded_payload(
    transcript_input: TranscriptInput,
    workflow_name: str,
    *,
    chunks: list[dict] | None = None,
) -> dict:
    """Run the workflow's deterministic extractor and attach grounding.

    When ``chunks`` is provided, each grounding entry gets a
    ``source_turns`` list and the payload's ``schema_version`` is
    bumped to ``"1.1.0"``. Fallback-match findings (one per item that
    fell back to ``t0000``) are NOT returned here — callers that need
    them call :func:`attach_source_turns` directly. The dict-only return
    shape is the historical contract that ``test_data_lake_grounding``
    and ``test_data_lake_fix_pass_3`` depend on.
    """
    if workflow_name not in GROUNDED_EXTRACTORS:
        raise ValueError(
            f"unsupported workflow_name {workflow_name!r}; "
            f"supported: {sorted(GROUNDED_EXTRACTORS)}"
        )
    base_extract, grounder = GROUNDED_EXTRACTORS[workflow_name]
    payload = dict(base_extract(transcript_input.transcript_text))
    payload[MEETING_ID_KEY] = transcript_input.meeting_id
    grounding_entries = grounder(transcript_input)
    payload[GROUNDING_KEY] = grounding_entries

    if chunks is not None:
        attach_source_turns(grounding_entries, chunks)
        payload["schema_version"] = "1.1.0"

    return payload
