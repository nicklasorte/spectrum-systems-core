"""Live-LLM meeting-minutes extraction workflow.

This is the first workflow in the repo that makes a live model call.
The constitution defers live calls; this workflow scopes exactly ONE
in, behind a default-off feature flag, reusing the existing governed
loop, envelope, control function and promotion gate. No new module,
no second control model, no second envelope.

Shape parity with the regex ``meeting_minutes`` workflow is deliberate:
the payload is ``{title, summary, decisions[], action_items[],
open_questions[], schema_version, provenance, meeting_id?}`` so the
same required-field eval, the same regulatory-verb eval, the same
``WorkflowResult`` and the same data-lake writer all apply unchanged.
The LLM only supplies the three content arrays; ``title`` / ``summary``
are derived deterministically from the transcript so a malformed model
response cannot masquerade as a titled artifact.

Fail-closed contract: a transport error, a non-JSON response, a
non-object response, or a response missing the required arrays does NOT
fall back to the regex extractor or to a text-mode guess. It produces a
payload whose strict-schema eval fails with ``schema_violation`` so the
control gate blocks and the artifact is never promoted.
"""
from __future__ import annotations

import functools
import json
from pathlib import Path

from ..artifacts import Artifact
from ..config import preflight_llm_config
from ..evals import (
    run_llm_gt_coverage_eval,
    run_llm_nonempty_eval,
    run_llm_strict_schema_eval,
    run_llm_within_source_eval,
)
from ._loop import run_governed_loop
from .llm_client import AnthropicJSONClient, LLMClient, LLMClientError
from .meeting_minutes import WorkflowResult

PRODUCED_BY = "meeting_minutes_llm"

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "meeting_minutes_llm.md"


def _system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _strip_fence(text: str) -> str:
    body = (text or "").strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
    if body.endswith("```"):
        body = body.rsplit("```", 1)[0]
    return body.strip()


def _derive_title(input_text: str) -> str:
    for line in input_text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return "Untitled meeting"


def _parse_llm_payload(raw: str) -> dict | None:
    """Parse the model text into ``{decisions, action_items,
    open_questions}`` of string lists, or ``None`` if it is not a
    well-formed object with all three array keys.

    ``None`` is the fail-closed signal: the caller emits a payload that
    the strict-schema eval rejects. We do NOT coerce, repair, or invent
    — a malformed response must block, not be patched into something
    that passes.
    """
    body = _strip_fence(raw)
    if not body:
        return None
    try:
        doc = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(doc, dict):
        return None
    out: dict = {}
    for key in ("decisions", "action_items", "open_questions"):
        if key not in doc:
            return None
        value = doc[key]
        if not isinstance(value, list):
            return None
        # Keep only clean string items; the strict-schema eval still
        # runs on the result, so dropping a non-string here cannot
        # smuggle a bad item past the gate — it only avoids crashing.
        out[key] = [v.strip() for v in value if isinstance(v, str) and v.strip()]
    return out


def _make_extract(
    *,
    client: LLMClient,
    meeting_id: str | None,
):
    def _extract(input_text: str) -> dict:
        title = _derive_title(input_text)
        payload: dict = {
            "title": title,
            "summary": title,
            "schema_version": "1.0.0",
            "provenance": {"produced_by": PRODUCED_BY},
        }
        if meeting_id:
            payload["meeting_id"] = meeting_id

        try:
            raw = client(system=_system_prompt(), user=input_text)
        except LLMClientError as exc:
            # Fail-closed: no text-mode fallback, no regex fallback. Emit
            # a payload the strict-schema eval rejects; record the cause
            # for the debug report (extra key, ignored by every eval).
            payload["_llm_error"] = str(exc)
            return payload

        parsed = _parse_llm_payload(raw)
        if parsed is None:
            # Malformed/non-object/missing-array → block via
            # schema_violation. The arrays are deliberately absent so
            # the strict-schema eval fails closed.
            payload["_llm_raw"] = (raw or "")[:500]
            return payload

        payload.update(parsed)
        return payload

    return _extract


def run_meeting_minutes_llm_workflow(
    input_text: str,
    *,
    client: LLMClient | None = None,
    meeting_id: str | None = None,
    source_id: str | None = None,
    lake_root: str | Path | None = None,
    env=None,
) -> WorkflowResult:
    """Produce a promoted ``meeting_minutes`` artifact via a live model.

    ``client`` defaults to :class:`AnthropicJSONClient`; tests inject a
    deterministic stub so the suite runs with no API key. ``source_id``
    + ``lake_root`` are used only by the observe-only GT-coverage eval
    (Step 6); when absent it still emits a numeric ``coverage_percent``
    of ``0.0`` and passes (observe-only never blocks).

    The four LLM-scoped evals are appended via ``extra_evals`` so they
    flow through the SAME control / promotion gate as the required
    evals — a failure blocks promotion fail-closed.

    Pre-run halt at this entry point too: when no ``client`` is
    injected (the real Anthropic client will be constructed),
    :func:`preflight_llm_config` runs BEFORE any artifact is produced,
    so a missing ``ANTHROPIC_API_KEY`` halts with ``config_error``
    rather than producing a blocked artifact. An injected client IS the
    configured transport, so the env check is skipped for it (keeps the
    test suite hermetic and key-free).
    """
    if client is None:
        preflight_llm_config(enabled=True, env=env)
    active_client: LLMClient = client or AnthropicJSONClient()
    extract = _make_extract(client=active_client, meeting_id=meeting_id)

    extra_evals = [
        run_llm_strict_schema_eval,
        functools.partial(run_llm_nonempty_eval, transcript_text=input_text),
        functools.partial(run_llm_within_source_eval, transcript_text=input_text),
        functools.partial(
            run_llm_gt_coverage_eval, source_id=source_id, lake_root=lake_root
        ),
    ]

    run = run_governed_loop(
        input_text=input_text,
        artifact_type="meeting_minutes",
        extract=extract,
        extra_evals=extra_evals,
    )
    return WorkflowResult(
        context_bundle=run.context_bundle,
        meeting_minutes=run.target,
        eval_results=run.eval_results,
        control_decision=run.control_decision,
        promoted=run.promoted,
        store=run.store,
    )


__all__ = ["run_meeting_minutes_llm_workflow", "PRODUCED_BY"]
