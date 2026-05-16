"""Mutually-exclusive meeting-minutes workflow dispatch.

One transcript runs through EXACTLY ONE extractor. Which one is decided
here, structurally, by a single resolved boolean — not by convention,
not by config hygiene, not by two call sites that "shouldn't both fire".
There is one ``if/else``; only one branch can execute.

- ``llm_extraction_enabled`` resolves to ``False`` (default) → the
  existing deterministic regex ``meeting_minutes`` workflow runs. This
  is byte-for-byte the pre-existing path; this whole feature is a
  no-op for any consumer that does not opt in.
- ``llm_extraction_enabled`` resolves to ``True`` → the pre-run config
  gate runs first (``preflight_llm_config``): if the API key is
  missing it raises ``LLMConfigError`` (reason ``config_error``)
  BEFORE any artifact is produced. Only if it passes does the live-LLM
  workflow run. The regex workflow does NOT run.

Both arms stamp ``provenance.produced_by`` so a consumer can tell which
extractor produced an artifact from a field on the artifact, never from
prose: ``"meeting_minutes"`` for the regex arm, ``"meeting_minutes_llm"``
for the LLM arm. The regex arm's provenance is added here at the
dispatch boundary and does NOT touch the deterministic data-lake
pipeline (``run_transcript_pipeline``), so golden and validate-and-
baseline signals are unaffected.
"""
from __future__ import annotations

from pathlib import Path

from ..config import preflight_llm_config
from ._loop import run_governed_loop
from .llm_client import LLMClient
from .meeting_minutes import WorkflowResult, _extract_meeting_minutes
from .meeting_minutes_llm import run_meeting_minutes_llm_workflow

REGEX_PRODUCED_BY = "meeting_minutes"


class WorkflowDispatchError(RuntimeError):
    """Raised pre-run if dispatch cannot pick exactly one workflow.

    This is the "both workflows registered for the same transcript"
    guard. It is defensive: the dispatch is a single ``if/else`` so this
    cannot happen by normal flow, but an explicit pre-run raise means a
    future refactor that breaks the invariant fails loudly before any
    artifact exists, not silently with two competing artifacts.
    """


def _regex_extract_with_provenance(meeting_id: str | None):
    def _extract(input_text: str) -> dict:
        payload = _extract_meeting_minutes(input_text)
        payload["provenance"] = {"produced_by": REGEX_PRODUCED_BY}
        if meeting_id:
            payload["meeting_id"] = meeting_id
        return payload

    return _extract


def _run_regex_arm(
    input_text: str, *, meeting_id: str | None
) -> WorkflowResult:
    run = run_governed_loop(
        input_text=input_text,
        artifact_type="meeting_minutes",
        extract=_regex_extract_with_provenance(meeting_id),
    )
    return WorkflowResult(
        context_bundle=run.context_bundle,
        meeting_minutes=run.target,
        eval_results=run.eval_results,
        control_decision=run.control_decision,
        promoted=run.promoted,
        store=run.store,
    )


def run_meeting_minutes_dispatch(
    input_text: str,
    *,
    llm_enabled: bool,
    client: LLMClient | None = None,
    meeting_id: str | None = None,
    source_id: str | None = None,
    lake_root: str | Path | None = None,
    env=None,
) -> WorkflowResult:
    """Run exactly one meeting-minutes extractor for this transcript.

    ``llm_enabled`` is the already-resolved boolean (resolve it with
    :func:`spectrum_systems_core.config.llm_extraction_enabled` — kept
    out of here so dispatch has one job: pick the branch and run it).

    When ``llm_enabled`` is True, :func:`preflight_llm_config` runs
    first and may raise ``LLMConfigError`` (reason ``config_error``)
    BEFORE any artifact is produced. The regex arm is never reached in
    that case — mutual exclusion holds even on the error path.
    """
    if not isinstance(llm_enabled, bool):
        # Fail-closed on a non-boolean (e.g. a truthy string slipped in
        # from a config file) rather than guessing the operator's
        # intent about live model calls.
        raise WorkflowDispatchError(
            f"llm_enabled must be a bool, got {type(llm_enabled).__name__}; "
            "refusing to dispatch ambiguously between the LLM and regex "
            "extractors"
        )

    if llm_enabled:
        # Pre-run gate: halt before any artifact if the runtime cannot
        # actually make the live call. No silent fallback to regex.
        preflight_llm_config(enabled=True, env=env)
        return run_meeting_minutes_llm_workflow(
            input_text,
            client=client,
            meeting_id=meeting_id,
            source_id=source_id,
            lake_root=lake_root,
            env=env,
        )

    # Flag off: the deterministic regex path. The LLM workflow is not
    # constructed and the client is never touched.
    return _run_regex_arm(input_text, meeting_id=meeting_id)


__all__ = [
    "run_meeting_minutes_dispatch",
    "WorkflowDispatchError",
    "REGEX_PRODUCED_BY",
]
