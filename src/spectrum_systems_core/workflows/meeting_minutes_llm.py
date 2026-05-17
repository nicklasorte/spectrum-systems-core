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
from ..config.taxonomy import UNCLASSIFIED_DECISION_VERB
from ..data_lake.chunker import chunk_transcript
from ..validation import (
    ArtifactValidationError,
    SchemaNotFoundError,
    validate_artifact,
)
from ..evals import (
    resolve_decision_verb,
    run_grounding_coverage_eval,
    run_llm_gt_coverage_eval,
    run_llm_nonempty_eval,
    run_llm_strict_schema_eval,
    run_llm_within_source_eval,
    run_source_turn_validity_eval_from_chunks,
)
from ._loop import run_governed_loop
from .llm_client import AnthropicJSONClient, LLMClient, LLMClientError
from .meeting_minutes import WorkflowResult

PRODUCED_BY = "meeting_minutes_llm"

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "meeting_minutes_llm.md"

# Header delimiting the turn-segmented transcript appended to the user
# message. The raw transcript is sent FIRST (so the verbatim
# within-source / nonempty evals, which bind to the raw input_text, are
# unaffected); the turn block is appended after this header purely so
# the model can cite turn_ids in ``grounding``.
_TURN_BLOCK_HEADER = (
    "\n\n=== TRANSCRIPT TURNS "
    '(cite these turn_ids in "grounding") ===\n'
)


def _render_turn_block(chunks: list[dict]) -> str:
    """Deterministic ``[turn_id] SPEAKER: text`` listing of the chunked
    transcript. Same chunks → same string (chunks are already a pure
    function of the transcript), so the grounded prompt stays
    replay-stable."""
    lines: list[str] = []
    for chunk in chunks:
        speaker = chunk.get("speaker")
        who = f" {speaker}:" if speaker else ""
        lines.append(
            f"[{chunk['turn_id']}]{who} {chunk.get('text', '')}"
        )
    return _TURN_BLOCK_HEADER + "\n".join(lines)


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


# The structured arrays the schema + Haiku prompt declare (the nine
# PR #123 added, plus the three schema_version 1.2.0 additions:
# claims, sentiment_indicators, meeting_phases). The parser carries
# every one through to the artifact so the strict-schema eval (which
# validates the WHOLE payload against meeting_minutes.schema.json)
# sees exactly what the model returned — nothing dropped, nothing
# invented. A model-emitted array NOT carried here would be silently
# discarded, which would make its prompt instruction dead and let a
# malformed item escape the fail-closed schema gate; that is why each
# new prompt array is added here in lock-step.
_STRUCTURED_ARRAYS = (
    "commitments",
    "risks",
    "claims",
    "cross_references",
    "attendees",
    "topics",
    "regulatory_references",
    "technical_parameters",
    "named_artifacts",
    "scheduled_events",
    "sentiment_indicators",
    "meeting_phases",
    # schema_version 1.3.0 additions (eight new cross-meeting arrays).
    # Added in lock-step with the prompt: a model-emitted value reaches
    # the artifact and is validated fail-closed by the strict-schema
    # eval (an explicit null or a malformed item blocks promotion,
    # never silently dropped — which would also make the new prompt
    # instructions dead).
    "issue_registry_entry",
    "position_statement",
    "dissent_or_objection",
    "agenda_item",
    "precedent_reference",
    "external_stakeholder_input",
    "glossary_definition",
    "procedural_ruling",
)

# The three legacy required arrays. Kept exactly fail-closed: a missing
# key or a non-list value is still ``None`` (block via schema_violation).
_LEGACY_ARRAYS = ("decisions", "action_items", "open_questions")

# artifact_type the assembled payload is validated against — the SAME
# constant the strict-schema eval uses (evals/llm_extraction.py
# ``_MEETING_MINUTES_TYPE``). The retry pre-check below MUST use the same
# validator so it can never accept a payload the in-loop gate would
# reject (no second control model — the eval stays authoritative).
_MEETING_MINUTES_TYPE = "meeting_minutes"

# Bounded re-prompt budget: one initial call plus at most ONE corrective
# retry. This addresses an imperfect FIRST Haiku response (a parse miss
# or a single schema-violating field) without weakening any gate — if
# the corrected response is still malformed, the last candidate is
# returned unchanged and the governed loop's fail-closed evals block it
# exactly as before. Bounded so a persistently-bad model cannot loop.
_MAX_LLM_ATTEMPTS = 2

_PARSE_FAIL_REASON = (
    "the response was not a single JSON object carrying the three "
    "required arrays decisions, action_items, open_questions"
)

# Deterministic, clearly delimited correction block appended to the
# user message on the one retry. Deterministic given the reason, so a
# stubbed (test) client stays replay-stable and the suite remains
# hermetic; a real model receives the precise schema error to self-fix.
_CORRECTION_HEADER = (
    "\n\n=== YOUR PREVIOUS RESPONSE WAS REJECTED — RETURN CORRECTED "
    "STRICT JSON ONLY ===\n"
    "It did not pass the meeting_minutes schema gate. Reason:\n"
    "{reason}\n"
    "Return the FULL corrected JSON object: every required key, STRICT "
    "JSON only, no prose, no code fences. Do not repeat the error above "
    "and do not add keys the schema does not define.\n"
)


def _schema_reject_reason(payload: dict) -> str | None:
    """Return why ``payload`` would fail the strict-schema gate, or
    ``None`` when it passes.

    Validates the SAME flat projection
    (``{"artifact_type": ..., **payload}``) against the SAME schema the
    in-loop ``run_llm_strict_schema_eval`` uses, so this producer-side
    pre-check can never green-light a payload the authoritative gate
    would block. Never raises (mirrors the eval's fail-closed contract):
    any unexpected validator error is itself a rejection reason, so a
    broken validator triggers a retry / block rather than a silent pass.

    When schema validation is disabled by the operator
    (``SCHEMA_VALIDATION_ENABLED=false``) ``validate_artifact`` returns
    without checking; this returns ``None`` (no retry) and the in-loop
    eval is bypassed by the same env var — behaviour is identical to the
    pre-retry code path, i.e. the deliberate operator bypass is honoured
    consistently in both places."""
    flat = {"artifact_type": _MEETING_MINUTES_TYPE, **payload}
    try:
        validate_artifact(flat, _MEETING_MINUTES_TYPE)
    except ArtifactValidationError as exc:
        return str(exc)
    except SchemaNotFoundError:
        return "meeting_minutes schema file not found"
    except Exception as exc:  # noqa: BLE001 — producer never raises
        return f"validator_error:{type(exc).__name__}"
    return None


def _parse_llm_payload(raw: str) -> dict | None:
    """Parse the model text into the full meeting_minutes content
    payload, or ``None`` if it is not a well-formed object carrying the
    three legacy array keys.

    Carry-through contract:

    * ``decisions`` / ``action_items`` / ``open_questions`` — required.
      A missing key or a non-list value is the fail-closed ``None``
      signal (caller emits a payload the strict-schema eval rejects).
      String items are stripped and empties dropped (UNCHANGED legacy
      behaviour). Non-string items (the structured object forms PR #123
      added via the schema ``oneOf``) are preserved VERBATIM so the
      strict-schema eval validates their real shape — they are never
      coerced to strings and never silently dropped.
    * The nine structured arrays — carried with ``.get(key, [])`` so an
      omitted key becomes ``[]`` (never absent, never ``null``). An
      EXPLICIT ``null`` from the model is preserved as-is (``None``) so
      the strict-schema eval blocks it with ``schema_violation`` rather
      than the parser silently patching a malformed response into
      something that passes (constitution: never invent, never repair).
    * ``stakeholders`` / ``confidence`` (architecture-review fields on a
      structured decision item) ride along untouched inside the
      preserved decision object — no injection, no defaulting here; the
      schema marks them optional and the model is instructed to emit
      them.

    We do NOT coerce, repair, or invent. A malformed response must
    block, not be patched into something that passes.
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
    for key in _LEGACY_ARRAYS:
        if key not in doc:
            return None
        value = doc[key]
        if not isinstance(value, list):
            return None
        cleaned: list = []
        for v in value:
            if isinstance(v, str):
                s = v.strip()
                if s:
                    cleaned.append(s)
            else:
                # Structured object (or any non-string): preserve
                # verbatim. The strict-schema eval validates the whole
                # payload against meeting_minutes.schema.json, so a bad
                # item is blocked there — never smuggled, never coerced.
                cleaned.append(v)
        out[key] = cleaned
    for key in _STRUCTURED_ARRAYS:
        # ``.get(key, [])``: omitted -> [] (never absent, never null);
        # present-and-null -> None (preserved, blocked by the schema
        # gate); present-and-list -> carried as-is.
        out[key] = doc.get(key, [])
    # Phase Y grounding array, carried verbatim with the same rule:
    # omitted -> [] ; explicit null -> None (schema gate blocks the
    # non-array). The caller drops this key on the ungrounded (1.0.0)
    # path so legacy payloads are byte-identical to before.
    out["grounding"] = doc.get("grounding", [])
    return out


def _fill_unclassified_decision_verbs(decisions: list) -> list:
    """Stamp the explicit ``UNCLASSIFIED_DECISION_VERB`` sentinel onto an
    object-form decision the model left without a classifiable verb.

    Why this exists (the 34-chunk block): the extraction prompt
    encourages the OBJECT decision form whenever stakeholders /
    confidence can be attributed. At scale the model emits many such
    object decisions and does not always supply a ``verb``; when the
    decision text also carries no taxonomy verb the regulatory_verb gate
    hard-blocks the whole run with ``verb_not_classified:__missing__``.
    The IDENTICAL decision in plain-string form has always promoted —
    the gate never required a verb for string decisions — so the block
    is an object-vs-string inconsistency, not a trust property.

    Scope is deliberately minimal and fail-closed-preserving:

    * Only dict items with a non-empty ``text`` are touched. String
      decisions and malformed items are returned verbatim (the strict
      schema / within-source evals own those, unchanged).
    * The fill fires ONLY when ``resolve_decision_verb`` — the EXACT
      function the regulatory_verb gate uses — returns ``None``, i.e.
      precisely the ``__missing__`` block case (no declared verb AND no
      taxonomy verb in text). A decision that CLAIMS a verb (recognised
      or garbage) is never overridden, so a hallucinated / mis-extracted
      verb still blocks: the hallucination-defence property is intact.
    * A decision whose text already yields a taxonomy verb is left as-is
      so the existing text-derived classification still applies.

    ``verb`` is schema-optional, so this has zero strict-schema-gate
    impact. Returns a NEW list — model output is never mutated in place.
    """
    out: list = []
    for item in decisions:
        if (
            isinstance(item, dict)
            and isinstance(item.get("text"), str)
            and item["text"].strip()
            and resolve_decision_verb(item) is None
        ):
            out.append({**item, "verb": UNCLASSIFIED_DECISION_VERB})
        else:
            out.append(item)
    return out


def _make_extract(
    *,
    client: LLMClient,
    meeting_id: str | None,
):
    def _base_payload(title: str, grounded: bool) -> dict:
        payload: dict = {
            "title": title,
            "summary": title,
            # Phase Y: grounded run emits 1.1.0 so the runner's
            # per-item grounding check + source_turn_validity apply;
            # the ungrounded path stays byte-identical at 1.0.0.
            "schema_version": "1.1.0" if grounded else "1.0.0",
            "provenance": {"produced_by": PRODUCED_BY},
        }
        if grounded:
            # word_level_timestamps is set by the chunker, NOT the
            # model (the prompt explicitly forbids the model from
            # emitting it). The current docx inputs carry no
            # word-level timing, so it is always False here; it is
            # only added on the grounded path so the ungrounded 1.0.0
            # payload stays byte-identical (additivity / rollback).
            payload["word_level_timestamps"] = False
        if meeting_id:
            payload["meeting_id"] = meeting_id
        return payload

    def _extract(
        input_text: str, chunks: list[dict] | None = None
    ) -> dict:
        title = _derive_title(input_text)
        grounded = bool(chunks)

        # The model is shown the raw transcript FIRST (verbatim evals
        # bind to it) then the turn-segmented block so it can cite
        # turn_ids. Same chunks → same base user message (replay-stable).
        base_user = (
            input_text + _render_turn_block(chunks)
            if grounded
            else input_text
        )
        system = _system_prompt()

        # Bounded re-prompt loop. Attempt 1 uses the base prompt and is
        # byte-identical to the pre-retry happy path. A parse miss or a
        # schema violation triggers ONE corrective retry with the precise
        # reason fed back. The gate is never weakened: an exhausted,
        # still-malformed run returns the last candidate and the governed
        # loop's fail-closed evals block it exactly as before.
        candidate = _base_payload(title, grounded)
        user = base_user
        for attempt in range(_MAX_LLM_ATTEMPTS):
            try:
                raw = client(system=system, user=user)
            except LLMClientError as exc:
                # Transport failure is NOT a malformed response: no
                # retry (unchanged). Emit a payload the strict-schema
                # eval rejects; record the cause for the debug report
                # (extra key, ignored by every eval).
                p = _base_payload(title, grounded)
                p["_llm_error"] = str(exc)
                return p

            parsed = _parse_llm_payload(raw)
            if parsed is None:
                # Non-object / missing-array: arrays deliberately absent
                # so the strict-schema eval fails closed if not corrected.
                candidate = _base_payload(title, grounded)
                candidate["_llm_raw"] = (raw or "")[:500]
                reason = _PARSE_FAIL_REASON
            else:
                if not grounded:
                    # Ungrounded (1.0.0) path: drop grounding so the
                    # payload is byte-identical to the pre-Phase-Y shape.
                    parsed.pop("grounding", None)
                # Option C: record the explicit indeterminate-verb
                # sentinel on any object-form decision the model left
                # unclassifiable, BEFORE schema validation and the
                # content hash. verb is schema-optional so the strict
                # schema gate is unaffected; this only converts a silent
                # regulatory_verb hard-block into an auditable field.
                parsed["decisions"] = _fill_unclassified_decision_verbs(
                    parsed["decisions"]
                )
                candidate = _base_payload(title, grounded)
                candidate.update(parsed)
                reason = _schema_reject_reason(candidate)
                if reason is None:
                    # Schema-valid on this attempt — done. No extra keys
                    # added, so a first-attempt success is byte-identical
                    # to the pre-retry behaviour (same content_hash).
                    return candidate

            # Malformed. Re-ask once with the precise reason fed back,
            # only while attempts remain (bounded by _MAX_LLM_ATTEMPTS).
            if attempt + 1 < _MAX_LLM_ATTEMPTS:
                user = base_user + _CORRECTION_HEADER.format(reason=reason)

        # Retry budget exhausted and still malformed: return the last
        # candidate UNCHANGED. Promotion is decided solely by the
        # governed loop's evals + control gate, which block it.
        return candidate

    return _extract


def run_meeting_minutes_llm_workflow(
    input_text: str,
    *,
    client: LLMClient | None = None,
    meeting_id: str | None = None,
    source_id: str | None = None,
    lake_root: str | Path | None = None,
    env=None,
    max_chunks: int | None = None,
) -> WorkflowResult:
    """Produce a promoted ``meeting_minutes`` artifact via a live model.

    ``client`` defaults to :class:`AnthropicJSONClient`; tests inject a
    deterministic stub so the suite runs with no API key. ``source_id``
    + ``lake_root`` are used only by the observe-only GT-coverage eval
    (Step 6); when absent it still emits a numeric ``coverage_percent``
    of ``0.0`` and passes (observe-only never blocks).

    ``max_chunks`` is a DEBUG-ONLY knob (default ``None`` = process the
    whole transcript). When set, only the first N chunks are kept AND
    the transcript fed to the model is truncated to the line span those
    chunks cover, so the model input — the real latency cost — shrinks
    and every transcript-bound eval (within-source, nonempty,
    source-turn-validity, grounding-coverage) stays consistent with the
    reduced chunk set. It exists only to iterate on the schema gate in
    ~30s instead of 10+ minutes; production runs leave it ``None`` so
    behaviour is byte-identical.

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

    # Phase Y: chunk the transcript so the model can attribute every
    # extracted item to specific turn_ids, and so the deterministic
    # source-turn-validity eval can reject a fabricated turn_id. A
    # whitespace-only transcript yields no chunks → the ungrounded
    # (1.0.0) path runs, exactly as before (rollback by construction).
    chunks = chunk_transcript(input_text)
    if (
        max_chunks is not None
        and max_chunks >= 0
        and len(chunks) > max_chunks
    ):
        # Debug-only fast path. Keep the first N chunks and truncate the
        # transcript to the line span they cover (chunks are contiguous
        # and line-ordered, so the first N span lines 1..last.line_end).
        # Truncating the TEXT — not just the chunk list — is what makes
        # the run fast: the model is shown input_text first, so a
        # smaller input_text is the actual latency win. Re-deriving the
        # text from the original lines keeps it a faithful prefix so the
        # verbatim within-source / nonempty evals still hold.
        chunks = chunks[:max_chunks]
        last_line = chunks[-1]["line_end"] if chunks else 0
        input_text = "\n".join(input_text.splitlines()[:last_line])
    grounded = bool(chunks)

    extra_evals = [
        run_llm_strict_schema_eval,
        functools.partial(run_llm_nonempty_eval, transcript_text=input_text),
        functools.partial(run_llm_within_source_eval, transcript_text=input_text),
        functools.partial(
            run_llm_gt_coverage_eval, source_id=source_id, lake_root=lake_root
        ),
    ]
    if grounded:
        # Same eval logic and same control authority as the data-lake
        # pipeline's Phase Y gate — only the source of the valid
        # turn-id set differs (in-memory chunks vs. on-disk
        # source_record). A fabricated turn_id or an unattributed
        # content item now blocks promotion through the one
        # decide_control gate.
        extra_evals.append(
            functools.partial(
                run_source_turn_validity_eval_from_chunks, chunks=chunks
            )
        )
        extra_evals.append(run_grounding_coverage_eval)

    run = run_governed_loop(
        input_text=input_text,
        artifact_type="meeting_minutes",
        extract=extract,
        chunks=chunks if grounded else None,
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
