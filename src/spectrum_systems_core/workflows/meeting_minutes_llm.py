"""Live-LLM meeting-minutes extraction workflow.

This is the first workflow in the repo that makes a live model call.
The constitution defers live calls; this workflow scopes exactly ONE
live-call SITE in, behind a default-off feature flag, reusing the
existing governed loop, envelope, control function and promotion gate.
No new module, no second control model, no second envelope. On the
grounded path a transcript larger than one token-budget-safe call is
processed as deterministic contiguous chunk batches (one call per
batch) whose parsed payloads are aggregated into ONE payload the
unchanged evals judge — still one workflow, one loop, one gate.

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
import re
import sys
from pathlib import Path

from ..artifacts import Artifact, compute_content_hash
from ..config import LLMConfigError, preflight_llm_config
from ..config.taxonomy import UNCLASSIFIED_DECISION_VERB
from ..data_lake.chunker import chunk_transcript

# NB: the glossary loader is imported lazily inside
# ``_prepend_glossary_block`` so a disabled run NEVER pays the import
# cost. The lazy-import contract is asserted by
# ``tests/glossary/test_production_wiring.py::test_disabled_path_does_not_import_loader``
# (subprocess + sys.modules introspection). Do not move these imports
# back to module scope without breaking that test.
from ..evals import (
    EXTRACTION_NOT_IN_SOURCE,
    REGULATORY_VERB_EVAL_TYPE,
    STRICT_SCHEMA_EVAL_TYPE,
    VERB_NOT_CLASSIFIED_PREFIX,
    WITHIN_SOURCE_EVAL_TYPE,
    WITHIN_SOURCE_WARN_PREFIX,
    resolve_decision_verb,
    route_within_source_eval,
    run_grounding_coverage_eval,
    run_llm_gt_coverage_eval,
    run_llm_nonempty_eval,
    run_llm_strict_schema_eval,
    run_llm_within_source_eval,
    run_source_turn_validity_eval_from_chunks,
    run_tlc_routed_eval,
)
from ..validation import (
    ArtifactValidationError,
    SchemaNotFoundError,
    validate_artifact,
)
from ._loop import run_governed_loop
from .llm_client import AnthropicJSONClient, LLMClient, LLMClientError
from .meeting_minutes import WorkflowResult

PRODUCED_BY = "meeting_minutes_llm"

# Provenance key the demoted-within_source warn codes land on. The
# meeting_minutes schema's ``provenance`` object permits additional
# keys (no additionalProperties:false there — same reason model_id is
# schema-additive), so this is a non-breaking addition the correction
# miner can read off the promoted artifact.
WITHIN_SOURCE_WARNINGS_PROVENANCE_KEY = "within_source_warnings"


def _routed_within_source_eval(
    target: Artifact, *, transcript_text: str
) -> Artifact:
    """Standalone within_source eval, then route the result.

    ``run_llm_within_source_eval`` is unchanged and still runs for
    EVERY type (the eval is never removed or skipped). The result is
    then passed through ``route_within_source_eval``: a HIGH_STAKES (or
    mixed / unparseable) miss is returned untouched and still blocks; a
    STANDARD-only miss is demoted to ``status == "warn"`` so
    ``decide_control`` logs it instead of blocking. The routing lives
    here (the call site), not inside ``llm_extraction``: ``tlc_router``
    imports ``llm_extraction``, so importing the router back into
    ``llm_extraction`` would be an import cycle.
    """
    return route_within_source_eval(
        run_llm_within_source_eval(target, transcript_text)
    )


_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "meeting_minutes_llm.md"


import contextlib as _contextlib


@_contextlib.contextmanager
def _make_override_prompt_cm(prompt_text):
    """Optionally swap ``_system_prompt`` for the duration of one run.

    Phase 5: the production CLI uses this to inject the Opus prompt
    when ``--model sonnet-unconstrained`` or ``--model opus`` is set.
    A ``None`` ``prompt_text`` is a pass-through context manager so the
    default ``--model haiku`` path is byte-identical to pre-Phase-5
    behaviour (no swap, no restore overhead, no observable change).

    The override mechanism is intentionally the same one
    ``pipeline.governed_pipeline_run._override_prompt`` uses so the
    correction miner and the CLI cannot drift on injection mechanics.
    Restoration on exception is guaranteed by the contextlib
    machinery.
    """
    if prompt_text is None:
        yield
        return
    import sys as _sys

    this_module = _sys.modules[__name__]
    original = getattr(this_module, "_system_prompt", None)
    setattr(this_module, "_system_prompt", lambda: prompt_text)
    try:
        yield
    finally:
        if original is None:
            try:
                delattr(this_module, "_system_prompt")
            except AttributeError:
                pass
        else:
            setattr(this_module, "_system_prompt", original)

# Single source of truth for the extraction model string. NEVER hardcode
# a model literal in this module — the value below is a PATH to the
# registry file, not a model id. P8-A swapped Haiku → Sonnet here because
# the 21-type / ~6500-token constraint prompt exceeds Haiku's reliable
# instruction-following ceiling at 34-chunk full-transcript scale.
_MODEL_REGISTRY_PATH = (
    Path(__file__).resolve().parents[3]
    / "ai"
    / "registry"
    / "model_registry.json"
)
_EXTRACTION_REGISTRY_KEY = "meeting_minutes_extraction"
_MODEL_REGISTRY_ERROR = "model_registry_error"


def _resolve_extraction_model() -> tuple[str, int]:
    """Resolve ``(model_id, max_tokens)`` for meeting_minutes extraction.

    The only source is ``ai/registry/model_registry.json`` →
    ``meeting_minutes_extraction``. Fail-closed and HALT-not-infer: a
    missing file, malformed JSON, a missing entry, a missing/blank
    ``model_id`` or a non-positive-int ``max_tokens`` raises
    :class:`LLMConfigError` BEFORE any artifact is produced. There is no
    hardcoded model fallback by design — a misconfigured registry must
    stop the run, never silently substitute a different model (that is
    the exact "artifact where model_id doesn't match the registry entry
    actually used" failure the mission's red team forbids).
    """
    try:
        raw = _MODEL_REGISTRY_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise LLMConfigError(
            f"cannot read model registry at {_MODEL_REGISTRY_PATH}: {exc}",
            reason_code=_MODEL_REGISTRY_ERROR,
        ) from exc
    try:
        registry = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMConfigError(
            f"model registry at {_MODEL_REGISTRY_PATH} is not valid "
            f"JSON: {exc}",
            reason_code=_MODEL_REGISTRY_ERROR,
        ) from exc
    entry = (
        registry.get(_EXTRACTION_REGISTRY_KEY)
        if isinstance(registry, dict)
        else None
    )
    if not isinstance(entry, dict):
        raise LLMConfigError(
            f"model registry has no '{_EXTRACTION_REGISTRY_KEY}' object",
            reason_code=_MODEL_REGISTRY_ERROR,
        )
    model_id = entry.get("model_id")
    if not isinstance(model_id, str) or not model_id.strip():
        raise LLMConfigError(
            f"'{_EXTRACTION_REGISTRY_KEY}.model_id' is missing or not a "
            "non-empty string",
            reason_code=_MODEL_REGISTRY_ERROR,
        )
    max_tokens = entry.get("max_tokens")
    # bool is an int subclass — reject it explicitly so ``true`` in the
    # registry cannot pass as a token budget.
    if (
        not isinstance(max_tokens, int)
        or isinstance(max_tokens, bool)
        or max_tokens <= 0
    ):
        raise LLMConfigError(
            f"'{_EXTRACTION_REGISTRY_KEY}.max_tokens' is missing or not "
            "a positive integer",
            reason_code=_MODEL_REGISTRY_ERROR,
        )
    return model_id.strip(), max_tokens

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
    """Deterministic turn-ID look-up table for grounding citations.

    Format: ``[turn_id] SPEAKER (lines N-M)`` — text body deliberately
    excluded. The raw transcript is already in the user message, so the
    model reads text from there and cites turn_ids from this table. The
    old ``[turn_id] SPEAKER: text`` format caused
    extraction_within_source_required failures: the model included the
    ``[t0066]`` prefix from this block in extracted items, whose
    normalized form is not a substring of the raw transcript (which has
    no turn-ID tokens). Same chunks → same string; replay-stable.
    """
    lines: list[str] = []
    for chunk in chunks:
        speaker = chunk.get("speaker")
        who = f" {speaker}" if speaker else ""
        line_start = chunk.get("line_start", "?")
        line_end = chunk.get("line_end", "?")
        lines.append(
            f"[{chunk['turn_id']}]{who} (lines {line_start}-{line_end})"
        )
    return _TURN_BLOCK_HEADER + "\n".join(lines)


# Architectural fix (grounding_entries=0 root cause): the entire ~6500
# token instruction set lived in the SYSTEM prompt while the USER turn
# carried ONLY data (raw transcript + turn block) with NO request. With
# no task framing in the user turn and a system prompt whose dominant,
# repeated message is "empty arrays are correct/safe; any mistake blocks
# the whole artifact; when in doubt emit nothing", BOTH Haiku and Sonnet
# consistently took the safe degenerate path and returned the all-empty
# object — so every content array AND ``grounding`` came back empty,
# model-independently. This is not a model-capability problem; it is the
# missing request. These constants put the actual extraction directive
# where it belongs (the user turn) and frame the transcript + turn
# block. They are PROSE ONLY — they restate the system prompt's binding
# rules (verbatim, grounding-required, empty-only-when-genuinely-absent)
# and relax none of them, so no eval / schema / taxonomy / control / test
# changes. Deterministic constants → replay-stable for the stub suite.
# Must NOT contain any ``[tNNNN]`` token (would pollute a stub's
# turn-id scan) and must keep the raw transcript BEFORE the turn block
# (the system prompt's source-attribution section binds to that order).
_USER_TASK_HEADER = (
    "TASK: Extract the structured meeting minutes from the transcript "
    "below, following the system instructions exactly. Return the single "
    "STRICT JSON object with every schema key present. Extract every "
    "decision, action item, open question, and structured item the "
    "transcript actually records. Do NOT return empty arrays for "
    "categories the transcript clearly contains; an empty array is "
    "correct ONLY for a category genuinely absent from THIS transcript, "
    "never as a blanket safe default. The raw transcript follows, then a "
    '"=== TRANSCRIPT TURNS ===" lookup table of turn_ids.\n\n'
)

_USER_TASK_FOOTER_GROUNDED = (
    "\n\n=== END OF INPUT ===\n"
    "Now output the single STRICT JSON object only (no prose, no code "
    "fences). For EVERY item you emit in any array you MUST add the "
    'matching entry to the top-level "grounding" array with its real '
    "source_turns read from the TURN block above; never emit an empty "
    '"grounding" when any content array is non-empty.'
)

_USER_TASK_FOOTER_UNGROUNDED = (
    "\n\n=== END OF INPUT ===\n"
    "Now output the single STRICT JSON object only (no prose, no code "
    "fences), with every schema key present."
)


def _build_user_message(
    input_text: str, chunks: list[dict] | None, grounded: bool
) -> str:
    """Assemble the user-turn message: an explicit extraction directive,
    the raw transcript, the turn block (grounded only), and a closing
    imperative. The raw transcript stays before the turn block so the
    system prompt's verbatim / source-attribution rules are unaffected;
    only the missing request is added. Deterministic given the same
    chunks, so stub-backed tests stay replay-stable."""
    if grounded and chunks:
        return (
            _USER_TASK_HEADER
            + input_text
            + _render_turn_block(chunks)
            + _USER_TASK_FOOTER_GROUNDED
        )
    return _USER_TASK_HEADER + input_text + _USER_TASK_FOOTER_UNGROUNDED


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

# Deterministic chunk-batch size for the grounded extraction path.
#
# Root cause this addresses: the workflow used to make ONE model call
# over the WHOLE transcript. ``bbdaf4d`` / the model_registry note
# proved ``max_tokens=16384`` non-truncating only at ~34-chunk
# full-transcript scale. A 138-chunk single call's response exceeds
# that budget; ``AnthropicJSONClient`` raises
# ``llm_output_truncated:max_tokens`` and the producer returns a
# no-arrays base payload, so ``required_meeting_minutes_fields`` +
# ``regulatory_verb`` block the whole run. Splitting the ordered chunk
# list into contiguous batches of at most ``_CHUNKS_PER_BATCH`` keeps
# every batch's model OUTPUT (the truncation axis) well under the
# proven-safe 34-chunk ceiling, then the per-batch parsed payloads are
# aggregated into one payload the UNCHANGED evals judge. Same input ->
# same contiguous batches -> replay-stable. An empty batch is valid: it
# contributes empty arrays to the aggregate, never a block.
_CHUNKS_PER_BATCH = 25


def _slice_transcript_for_batch(
    transcript_lines: list[str], batch: list[dict]
) -> str:
    """Verbatim transcript slice covering ``batch``'s line span.

    The model is shown only this slice (plus the batch's turn block) so
    its output stays bounded. ``line_start`` / ``line_end`` are 1-based
    inclusive (the chunker contract); the slice is a faithful verbatim
    substring of the full transcript, so every item the model extracts
    from it is still a verbatim substring of the full ``input_text`` the
    within-source / nonempty evals bind to. Mirrors the ``max_chunks``
    truncation idiom already used in the workflow."""
    if not batch:
        return ""
    start = batch[0].get("line_start", 1)
    end = batch[-1].get("line_end", len(transcript_lines))
    return "\n".join(transcript_lines[max(0, start - 1):end])

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


# --- Per-chunk debug report (observe-only; --debug-chunks) --------------
#
# The meeting_minutes_llm workflow makes one model call per chunk
# batch and aggregates the parsed payloads; the chunker's turn-chunks
# are how every extracted item is attributed back to a span of the
# transcript (the model emits a top-level ``grounding`` array of
# ``{"kind","text","source_turns":[turn_id,...]}``). The aggregated
# payload is judged by the evals once, so the operator needs to see
# WHICH chunk produced each blocking item. This builder decomposes the
# already-computed eval_results back onto the chunks the items cite.
#
# It is strictly observe-only: a pure function of
# (payload, chunks, eval_results) that never raises, never mutates its
# inputs, and whose output never reaches the control gate. The
# authoritative pass/fail is still the eval_results it reads — this only
# explains them per chunk; it can neither relax nor tighten any gate.

_DECISION_KINDS = frozenset({"decision", "decisions"})
_ACTION_KINDS = frozenset({"action_item", "action_items", "action"})
_ITEM_TEXT_KEYS = ("text", "action", "question_text")
_SCHEMA_PATH_RE = re.compile(r"path=\[(?P<body>.*)\]\s*$")
_PATH_ELEM_RE = re.compile(r"'(?P<s>[^']*)'|(?P<i>\d+)")


def _dbg_norm(text: object) -> str:
    """Lowercase, collapse whitespace runs, strip. Same shape as the
    within-source eval's match algorithm so grounding text and the
    truncated text the evals report line up."""
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", text).strip().lower()


def _has_any_content(payload: dict) -> bool:
    """True if any legacy or structured content array on the payload is a
    non-empty list. Used only by the debug report to distinguish "model
    returned the all-empty object" from "no content arrays present at
    all" (a base payload from a transport / parse failure). Observe-only;
    never consulted by any gate."""
    for key in (*_LEGACY_ARRAYS, *_STRUCTURED_ARRAYS):
        value = payload.get(key)
        if isinstance(value, list) and len(value) > 0:
            return True
    return False


def _item_primary_text(item: object) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in _ITEM_TEXT_KEYS:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def _parse_schema_path(reason_code: str) -> list[object]:
    """Best-effort extraction of the JSON path the strict-schema eval
    surfaces as ``... at path=['decisions', 3, 'verb']``. Returns the
    parsed elements (str / int) in order, or ``[]`` when the code has no
    path tail (a structural pre-check such as ``not_a_list:<k>``)."""
    m = _SCHEMA_PATH_RE.search(reason_code)
    if m is None:
        return []
    out: list[object] = []
    for em in _PATH_ELEM_RE.finditer(m.group("body")):
        if em.group("s") is not None:
            out.append(em.group("s"))
        else:
            out.append(int(em.group("i")))
    return out


def build_chunk_debug_report(
    *,
    payload: object,
    chunks: list[dict] | None,
    eval_results: list,
) -> str:
    """Deterministic per-chunk decomposition of the LLM run's evals.

    Renders, for every chunk in order::

        CHUNK {n}/{total} [turn_id=...]: decisions={d} action_items={a}
          regulatory_verb_issues: [...]
          within_source_issues: [...]
          schema_issues: [...]

    plus a final ``UNATTRIBUTED`` block for any blocking item that does
    not map to a chunk (a fabricated turn_id, or a payload-global schema
    violation) — so a failure is never silently dropped from the view.

    Never raises: any unexpected shape degrades to an empty / best-effort
    line rather than crashing the run (this is debug output, and the
    control gate is unaffected regardless)."""
    try:
        return _build_chunk_debug_report(payload, chunks, eval_results)
    except Exception as exc:  # noqa: BLE001 — debug output never crashes the run
        return (
            "=== CHUNK DEBUG (meeting_minutes_llm) ===\n"
            f"debug_report_error: {type(exc).__name__}: {exc}"
        )


def _build_chunk_debug_report(
    payload: object,
    chunks: list[dict] | None,
    eval_results: list,
) -> str:
    header = "=== CHUNK DEBUG (meeting_minutes_llm) ==="
    if not chunks:
        return f"{header}\nno chunks (ungrounded path)"

    payload = payload if isinstance(payload, dict) else {}
    total = len(chunks)

    # turn_id -> 1-based chunk position. Chunks are already ordered; the
    # chunker guarantees a string turn_id on every chunk.
    turn_to_pos: dict[str, int] = {}
    pos_turn: list[str] = []
    for i, chunk in enumerate(chunks):
        tid = chunk.get("turn_id") if isinstance(chunk, dict) else None
        tid = tid if isinstance(tid, str) else f"<chunk{i}>"
        pos_turn.append(tid)
        turn_to_pos.setdefault(tid, i + 1)

    grounding = payload.get("grounding")
    grounding = grounding if isinstance(grounding, list) else []

    # Per-chunk accumulators (index 1..total). Index 0 == UNATTRIBUTED.
    n_buckets = total + 1
    dec_count = [0] * n_buckets
    act_count = [0] * n_buckets
    regverb: list[list[str]] = [[] for _ in range(n_buckets)]
    within: list[list[str]] = [[] for _ in range(n_buckets)]
    schema: list[list[str]] = [[] for _ in range(n_buckets)]

    # Grounding index: norm(text) -> sorted unique chunk positions, plus
    # per-(kind) counts attributed to each position.
    text_to_pos: dict[str, list[int]] = {}
    for g in grounding:
        if not isinstance(g, dict):
            continue
        kind = g.get("kind")
        st = g.get("source_turns")
        st = st if isinstance(st, list) else []
        positions = sorted(
            {turn_to_pos[t] for t in st if isinstance(t, str) and t in turn_to_pos}
        )
        gnorm = _dbg_norm(g.get("text"))
        if gnorm and positions:
            text_to_pos.setdefault(gnorm, [])
            for p in positions:
                if p not in text_to_pos[gnorm]:
                    text_to_pos[gnorm].append(p)
        targets = positions if positions else [0]
        for p in targets:
            if kind in _DECISION_KINDS:
                dec_count[p] += 1
            elif kind in _ACTION_KINDS:
                act_count[p] += 1

    def _positions_for_text(reported: str) -> list[int]:
        """Map an eval-reported (truncated) item text back to chunk
        positions via the grounding array. The eval truncates to a
        prefix; grounding carries the item text 'exactly as emitted',
        so an exact-or-prefix normalized match is the link."""
        rnorm = _dbg_norm(reported)
        if not rnorm:
            return []
        if rnorm in text_to_pos:
            return list(text_to_pos[rnorm])
        hits: list[int] = []
        for gnorm, positions in text_to_pos.items():
            if gnorm.startswith(rnorm) or rnorm.startswith(gnorm):
                for p in positions:
                    if p not in hits:
                        hits.append(p)
        return sorted(hits)

    def _eval_payload(eval_type: str) -> dict:
        for e in eval_results:
            ep = getattr(e, "payload", None)
            if isinstance(ep, dict) and ep.get("eval_type") == eval_type:
                return ep
        return {}

    # --- regulatory_verb: verb_not_classified:<verb>|decision[<i>]:<t> -
    rv = _eval_payload(REGULATORY_VERB_EVAL_TYPE)
    decisions = payload.get("decisions")
    decisions = decisions if isinstance(decisions, list) else []
    for rc in rv.get("reason_codes", []) or []:
        if not isinstance(rc, str) or not rc.startswith(
            VERB_NOT_CLASSIFIED_PREFIX
        ):
            continue  # warns / unclassified notes never block — skip
        rest = rc[len(VERB_NOT_CLASSIFIED_PREFIX):]
        verb, sep, loc = rest.partition("|decision[")
        if not sep:
            # Global form (e.g. __decisions_not_a_list__) — unattributed.
            regverb[0].append(verb)
            continue
        idx_str, _, reported = loc.partition("]:")
        full_text = reported
        if idx_str.isdigit():
            di = int(idx_str)
            if 0 <= di < len(decisions):
                full_text = _item_primary_text(decisions[di]) or reported
        positions = _positions_for_text(full_text)
        for p in positions or [0]:
            regverb[p].append(verb)

    # --- within_source: extraction_not_in_source:<key>:<text60> OR the
    # demoted within_source_warn:<key>:<text60> form (a STANDARD-lane
    # miss is logged, not blocked — but the operator must still see it
    # attributed to its producing chunk; observability is never gated).
    ws = _eval_payload(WITHIN_SOURCE_EVAL_TYPE)
    for rc in ws.get("reason_codes", []) or []:
        if not isinstance(rc, str) or not (
            rc.startswith(EXTRACTION_NOT_IN_SOURCE)
            or rc.startswith(WITHIN_SOURCE_WARN_PREFIX)
        ):
            continue
        parts = rc.split(":", 2)
        key = parts[1] if len(parts) > 1 else "?"
        text = parts[2] if len(parts) > 2 else ""
        positions = _positions_for_text(text)
        label = f"{key}:{text}"
        for p in positions or [0]:
            within[p].append(label)

    # --- strict_schema: schema_violation:... [at path=[...]] ----------
    ss = _eval_payload(STRICT_SCHEMA_EVAL_TYPE)
    for rc in ss.get("reason_codes", []) or []:
        if not isinstance(rc, str):
            continue
        path = _parse_schema_path(rc)
        # Trim the leading "schema_violation:" so the message is the
        # operator-facing part (jsonschema text carries the offending
        # enum value verbatim, e.g. "'foo' is not one of [...]").
        msg = rc.split(":", 1)[1] if ":" in rc else rc
        positions: list[int] = []
        if path and isinstance(path[0], str):
            akey = path[0]
            arr = payload.get(akey)
            if (
                len(path) >= 2
                and isinstance(path[1], int)
                and isinstance(arr, list)
                and 0 <= path[1] < len(arr)
            ):
                item = arr[path[1]]
                if akey == "grounding" and isinstance(item, dict):
                    st = item.get("source_turns")
                    st = st if isinstance(st, list) else []
                    positions = sorted(
                        {
                            turn_to_pos[t]
                            for t in st
                            if isinstance(t, str) and t in turn_to_pos
                        }
                    )
                elif isinstance(item, dict) and isinstance(
                    item.get("source_turns"), list
                ):
                    positions = sorted(
                        {
                            turn_to_pos[t]
                            for t in item["source_turns"]
                            if isinstance(t, str) and t in turn_to_pos
                        }
                    )
                else:
                    positions = _positions_for_text(
                        _item_primary_text(item)
                    )
        for p in positions or [0]:
            schema[p].append(msg)

    def _fmt(values: list[str]) -> str:
        return json.dumps(values, ensure_ascii=False)

    lines = [
        header,
        f"chunks_total={total} grounding_entries={len(grounding)}",
    ]
    # Auto-debug rule (CLAUDE.md): a base payload produced by a transport
    # / truncation failure (``_llm_error``) or a parse miss (``_llm_raw``)
    # has NO content and NO grounding, so the line above reads
    # ``grounding_entries=0`` with zero explanation — the exact "block a
    # new engineer could not explain from the artifact alone" the rule
    # forbids. Surface the captured cause here so the session, not an
    # Actions-log dig, is the debugger. Observe-only: the report never
    # reaches the control gate, so this relaxes nothing.
    llm_error = payload.get("_llm_error")
    if isinstance(llm_error, str) and llm_error:
        lines.append(f"LLM_TRANSPORT_FAILURE: {llm_error}")
    llm_raw = payload.get("_llm_raw")
    if isinstance(llm_raw, str) and llm_raw:
        lines.append(
            "LLM_RESPONSE_UNPARSED (first 500 chars of model output): "
            + llm_raw
        )
    if (
        len(grounding) == 0
        and not isinstance(llm_error, str)
        and not isinstance(llm_raw, str)
        and not _has_any_content(payload)
    ):
        # Parsed cleanly, transport fine — the model itself returned the
        # all-empty object. Name that explicitly so it is never confused
        # with a transport / parse failure.
        lines.append(
            "LLM_RETURNED_EMPTY: model response parsed but every content "
            "array and grounding came back empty"
        )
    for p in range(1, total + 1):
        lines.append(
            f"CHUNK {p}/{total} [turn_id={pos_turn[p - 1]}]: "
            f"decisions={dec_count[p]} action_items={act_count[p]}"
        )
        lines.append(f"  regulatory_verb_issues: {_fmt(regverb[p])}")
        lines.append(f"  within_source_issues: {_fmt(within[p])}")
        lines.append(f"  schema_issues: {_fmt(schema[p])}")
    if dec_count[0] or act_count[0] or regverb[0] or within[0] or schema[0]:
        lines.append(
            "UNATTRIBUTED (no resolvable grounding -> chunk): "
            f"decisions={dec_count[0]} action_items={act_count[0]}"
        )
        lines.append(f"  regulatory_verb_issues: {_fmt(regverb[0])}")
        lines.append(f"  within_source_issues: {_fmt(within[0])}")
        lines.append(f"  schema_issues: {_fmt(schema[0])}")
    return "\n".join(lines)


def _prepend_glossary_block(
    *,
    user_message: str,
    batch_text: str,
    glossary,
    tokens_counter,
) -> str:
    """Prepend a Terminology block to ``user_message`` when ``glossary``
    matches against ``batch_text``.

    The block is matched against the batch text the model is actually
    shown — not the full transcript — so the terminology relevant to
    THIS batch is what gets surfaced. Matches are capped by the loader's
    default (``DEFAULT_MAX_TERMS``); the truncation count is rendered
    inside the block. ``tokens_counter['added']`` accumulates the
    formatted-block token count across all batches; the production
    wiring reads this value and stamps it into ``extraction_config``.

    When ``glossary`` is ``None`` (the disable flag was passed) the
    function is a pure pass-through and the user message is byte-
    identical to a run before this seam existed — the additivity /
    rollback property. The lazy import below is deliberate: a disabled
    run NEVER loads ``glossary.loader`` (proven by the sys.modules
    test referenced at the top of this module).
    """
    if glossary is None:
        return user_message
    from ..glossary.loader import (
        DEFAULT_MAX_TERMS as _GLOSSARY_DEFAULT_MAX_TERMS,
        count_glossary_tokens as _count_glossary_tokens,
        format_terminology_block as _format_terminology_block,
    )

    matched, truncated = glossary.match(
        batch_text, max_terms=_GLOSSARY_DEFAULT_MAX_TERMS
    )
    if not matched:
        return user_message
    block = _format_terminology_block(
        matched, truncated, version_hash=glossary.version_hash
    )
    if tokens_counter is not None:
        tokens_counter["added"] = tokens_counter.get("added", 0) + (
            _count_glossary_tokens(block)
        )
    return f"{block}\n\n{user_message}"


def _make_extract(
    *,
    client: LLMClient,
    meeting_id: str | None,
    model_id: str,
    glossary=None,
    glossary_tokens_counter=None,
):
    def _base_payload(title: str, grounded: bool) -> dict:
        payload: dict = {
            "title": title,
            "summary": title,
            # Phase Y: grounded run emits 1.1.0 so the runner's
            # per-item grounding check + source_turn_validity apply;
            # the ungrounded path stays byte-identical at 1.0.0.
            "schema_version": "1.1.0" if grounded else "1.0.0",
            # provenance.model_id is the registry-resolved extraction
            # model actually used for THIS run, recorded so a past
            # artifact keeps its exact model after the registry rolls
            # forward and so the unit gate can assert
            # artifact.model_id == registry entry. The meeting_minutes
            # schema's ``provenance`` object permits additional keys
            # (no additionalProperties:false there), so this is schema
            # additive — the strict-schema eval still passes.
            "provenance": {
                "produced_by": PRODUCED_BY,
                "model_id": model_id,
            },
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

    def _run_batch(
        *, system: str, base_user: str, title: str, grounded: bool
    ) -> tuple[dict, bool]:
        """One model call (plus the bounded corrective retry) for a
        single batch's user message.

        Returns ``(candidate, ok)``. ``ok`` is ``True`` only when the
        response parsed AND passed the producer-side schema pre-check
        for this batch; ``candidate`` is then the schema-valid parsed
        payload whose arrays the caller aggregates. ``ok`` is ``False``
        on a transport failure (no retry — unchanged) or an exhausted,
        still-malformed response; ``candidate`` is then the diagnostic
        base / last payload (carrying ``_llm_error`` / ``_llm_raw``)
        that the caller returns AS-IS so the governed loop's unchanged
        fail-closed evals block the whole run with the cause visible.

        The loop body is byte-identical to the pre-batching single-call
        logic — only the early returns now carry an explicit ``ok``
        flag so the caller can distinguish "mergeable" from "fail the
        whole run".
        """
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
                return p, False

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
                    return candidate, True

            # Malformed. Re-ask once with the precise reason fed back,
            # only while attempts remain (bounded by _MAX_LLM_ATTEMPTS).
            if attempt + 1 < _MAX_LLM_ATTEMPTS:
                user = base_user + _CORRECTION_HEADER.format(reason=reason)

        # Retry budget exhausted and still malformed: return the last
        # candidate UNCHANGED. Promotion is decided solely by the
        # governed loop's evals + control gate, which block it.
        return candidate, False

    def _extract(
        input_text: str, chunks: list[dict] | None = None
    ) -> dict:
        title = _derive_title(input_text)
        grounded = bool(chunks)
        system = _system_prompt()

        if not grounded or len(chunks) <= _CHUNKS_PER_BATCH:
            # Single pass: ungrounded, OR a chunk count that already fits
            # the proven-safe token budget. The user message and the one
            # model call are byte-identical to the pre-batching path
            # (same content_hash), so small runs and the existing hermetic
            # suite are completely unaffected by batching.
            base_user = _build_user_message(input_text, chunks, grounded)
            base_user = _prepend_glossary_block(
                user_message=base_user,
                batch_text=input_text,
                glossary=glossary,
                tokens_counter=glossary_tokens_counter,
            )
            candidate, _ = _run_batch(
                system=system,
                base_user=base_user,
                title=title,
                grounded=grounded,
            )
            return candidate

        # Grounded AND over the single-call budget: split the ordered
        # chunk list into contiguous batches, one model call per batch,
        # and aggregate the parsed arrays into ONE payload. The governed
        # loop runs every (unchanged) eval ONCE on this aggregated
        # payload — so required_meeting_minutes_fields / regulatory_verb
        # judge the whole-transcript aggregate, never a per-batch
        # response, and an empty batch is valid (it contributes nothing).
        transcript_lines = input_text.splitlines()
        aggregated = _base_payload(title, grounded)
        for key in (*_LEGACY_ARRAYS, *_STRUCTURED_ARRAYS):
            aggregated[key] = []
        aggregated["grounding"] = []

        for start in range(0, len(chunks), _CHUNKS_PER_BATCH):
            batch = chunks[start:start + _CHUNKS_PER_BATCH]
            batch_text = _slice_transcript_for_batch(
                transcript_lines, batch
            )
            base_user = _build_user_message(batch_text, batch, True)
            base_user = _prepend_glossary_block(
                user_message=base_user,
                batch_text=batch_text,
                glossary=glossary,
                tokens_counter=glossary_tokens_counter,
            )
            candidate, ok = _run_batch(
                system=system,
                base_user=base_user,
                title=title,
                grounded=True,
            )
            if not ok:
                # Fail closed for the WHOLE run, exactly as the
                # single-call path does today: return this batch's
                # diagnostic candidate (carrying _llm_error / _llm_raw
                # or the schema-invalid content) so the governed loop's
                # unchanged evals block it and the cause is visible. A
                # partial aggregation is NEVER promoted as if it were
                # the whole transcript.
                return candidate
            # ok is True ⇒ _schema_reject_reason passed ⇒ every carried
            # array is a valid list per meeting_minutes.schema.json.
            # _run_batch already stamped the verb sentinel per batch, so
            # the aggregate inherits it. Contiguous, non-overlapping
            # batches over disjoint slices ⇒ no cross-batch duplication.
            for key in (*_LEGACY_ARRAYS, *_STRUCTURED_ARRAYS, "grounding"):
                aggregated[key].extend(candidate.get(key, []))

        return aggregated

    return _extract


def _emit_single_chunk_debug(
    *,
    run,
    raw_response: str | None,
    print_context: bool,
) -> None:
    """Print the ``--single-chunk`` debug block.

    Emits, in order: the verbatim raw model response, every
    ``eval_result`` produced for the single retained chunk, and — only
    when ``print_context`` — the first 1000 characters of the context
    bundle the model was given (its ``payload.source_text``, which is
    exactly what is prepended to the model's user message). The last
    block answers the operative debugging question: did the transcript
    content actually reach the API call?

    Observe-only: it reads ``run`` and prints. It never mutates the
    artifact, the eval_results, the control decision, or the exit code,
    so a run with ``single_chunk=False`` is byte-identical to one
    before this knob existed (additivity / rollback).
    """
    out = sys.stdout
    print("=== SINGLE CHUNK RAW MODEL RESPONSE ===", file=out)
    print(
        raw_response
        if raw_response is not None
        else "(no model response captured)",
        file=out,
    )
    print("=== END RAW MODEL RESPONSE ===", file=out)
    print("=== SINGLE CHUNK EVAL RESULTS ===", file=out)
    for er in run.eval_results:
        payload = er.payload if isinstance(er.payload, dict) else {}
        print(
            json.dumps(payload, indent=2, sort_keys=True, default=str),
            file=out,
        )
    print("=== END EVAL RESULTS ===", file=out)
    if print_context:
        cb = run.context_bundle
        src = ""
        if cb is not None and isinstance(cb.payload, dict):
            src = str(cb.payload.get("source_text", ""))
        print(
            "=== SINGLE CHUNK CONTEXT BUNDLE (first 1000 chars) ===",
            file=out,
        )
        print(src[:1000], file=out)
        print("=== END CONTEXT BUNDLE ===", file=out)
    out.flush()


def run_meeting_minutes_llm_workflow(
    input_text: str,
    *,
    client: LLMClient | None = None,
    meeting_id: str | None = None,
    source_id: str | None = None,
    lake_root: str | Path | None = None,
    env=None,
    max_chunks: int | None = None,
    debug_chunks: bool = False,
    print_raw_response: bool = False,
    single_chunk: bool = False,
    print_context: bool = False,
    glossary=None,
    glossary_tokens_counter=None,
    model_id_override: str | None = None,
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

    ``debug_chunks`` (default ``False``) is a DEBUG-ONLY observability
    knob. When ``True`` a per-chunk decomposition of the run's evals is
    printed to stdout BEFORE the control decision aggregates them, so an
    operator can see WHICH chunk produced each blocking decision /
    action / verb / schema item. It is strictly observe-only: it does
    not touch the payload, the content_hash, the eval_results or the
    decision, so a run with ``debug_chunks=False`` is byte-identical to
    one before this knob existed (additivity / rollback).

    ``print_raw_response`` (default ``False``) is a DEBUG-ONLY
    observability knob (Mode 1). When ``True`` the active transport is
    wrapped so the verbatim model response is printed to stdout BEFORE
    ``_parse_llm_payload`` runs on it. The wrapper returns the response
    UNCHANGED, so extraction / evals / promotion are byte-identical to a
    run with it off — it only observes, exactly like ``debug_chunks``.

    ``single_chunk`` (default ``False``) is a DEBUG-ONLY knob. When
    ``True`` the transcript is chunked normally, then ONLY the single
    chunk with the most characters in its ``text`` is kept and the
    model input is reduced to exactly that chunk's text — so the debug
    cycle is one API call and it is easy to see exactly what the model
    receives and returns. It prints a ``SINGLE CHUNK MODE:`` header
    (chunk position / original total / turn_id / char count), the
    verbatim raw model response, and every eval_result for that chunk.
    It legitimately changes the artifact (a different, smaller input),
    so unlike ``debug_chunks`` it is NOT byte-identical to a full run;
    it takes precedence over ``max_chunks`` when both are set. The
    exit-code semantics are unchanged (promotion still decided solely
    by the governed loop's evals + control gate).

    ``print_context`` (default ``False``) only has an effect together
    with ``single_chunk``: it additionally prints the first 1000
    characters of the context bundle the model was given so an operator
    can confirm the transcript content is actually present in the API
    call. It is observe-only and changes nothing.
    """
    # Resolve the extraction model FIRST — before any artifact is
    # produced and regardless of whether the transport is injected (a
    # stub client still produces an artifact that must carry the
    # registry-resolved provenance.model_id). A misconfigured registry
    # HALTS here fail-closed rather than producing a blocked artifact
    # with the wrong / no model_id.
    #
    # Phase 5: `model_id_override` (Sonnet wiring) takes precedence over
    # the registry so the production CLI's `--model` flag can pin a
    # specific model without editing model_registry.json. The override
    # path keeps the registry's max_tokens (the transport budget is a
    # property of the prompt+pipeline, not the model family). A blank
    # / None override falls through to the registry, byte-identical to
    # pre-Phase-5 behaviour.
    registry_model_id, max_tokens = _resolve_extraction_model()
    if model_id_override is not None:
        if not isinstance(model_id_override, str) or not model_id_override.strip():
            raise LLMConfigError(
                "model_id_override must be a non-empty string when provided",
                reason_code=_MODEL_REGISTRY_ERROR,
            )
        model_id = model_id_override.strip()
    else:
        model_id = registry_model_id
    if client is None:
        preflight_llm_config(enabled=True, env=env)
    active_client: LLMClient = client or AnthropicJSONClient(
        model=model_id, max_tokens=max_tokens
    )
    # --single-chunk capture seam. DEBUG ONLY. When set we wrap the
    # resolved transport so the EXACT bytes the model returned (raw
    # response) can be printed verbatim after the run. The wrapper is
    # installed ONLY under single_chunk — when off, active_client is the
    # unmodified transport and the run is byte-identical to before this
    # knob existed (additivity / rollback).
    _capture: dict[str, str] = {}
    if single_chunk:
        _inner_client = active_client

        def _capturing_client(*, system: str, user: str) -> str:
            raw = _inner_client(system=system, user=user)
            # Single-chunk mode is one API call per attempt; last-write
            # -wins captures the final response — the one that produced
            # the returned artifact — if a corrective retry fired.
            _capture["system"] = system
            _capture["user"] = user
            _capture["raw"] = raw
            return raw

        active_client = _capturing_client

    # Mode 1 raw-response printer. Wraps whatever the active transport
    # now is (the single-chunk capturing client when both are set), so
    # the verbatim response is printed BEFORE _parse_llm_payload runs.
    # Pass-through: returns the response UNCHANGED.
    _raw_printer = None
    if print_raw_response:
        from .debug_modes import RawResponsePrintingClient

        _raw_printer = RawResponsePrintingClient(active_client)
        active_client = _raw_printer

    extract = _make_extract(
        client=active_client,
        meeting_id=meeting_id,
        model_id=model_id,
        glossary=glossary,
        glossary_tokens_counter=glossary_tokens_counter,
    )

    # Phase Y: chunk the transcript so the model can attribute every
    # extracted item to specific turn_ids, and so the deterministic
    # source-turn-validity eval can reject a fabricated turn_id. A
    # whitespace-only transcript yields no chunks → the ungrounded
    # (1.0.0) path runs, exactly as before (rollback by construction).
    chunks = chunk_transcript(input_text)
    if single_chunk and chunks:
        # --single-chunk DEBUG path. Take the ONE chunk with the most
        # characters in its text and run extraction on only it, so the
        # whole debug cycle is a single API call. ``max`` over the index
        # range returns the LOWEST index on a size tie, so the same
        # transcript always selects the same chunk (determinism).
        original_total = len(chunks)
        best_idx = max(
            range(len(chunks)),
            key=lambda i: len(chunks[i].get("text", "")),
        )
        best_chunk = chunks[best_idx]
        chunks = [best_chunk]
        # The model is shown ``input_text`` first, so reducing it to the
        # selected chunk's text is the actual single-API-call shrink AND
        # keeps every transcript-bound eval (within-source, nonempty,
        # source-turn-validity, grounding-coverage) self-consistent with
        # the one retained chunk.
        input_text = best_chunk.get("text", "")
        print(
            f"SINGLE CHUNK MODE: chunk {best_idx + 1}/{original_total} "
            f"turn_id={best_chunk.get('turn_id')} "
            f"chars={len(best_chunk.get('text', ''))}",
            file=sys.stdout,
            flush=True,
        )
    elif (
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
    if _raw_printer is not None:
        # Record the post-truncation chunk count so the printed banner
        # is honest about the chunk scope the run covers (the grounded
        # path now makes one call per chunk batch; the raw printer
        # prints each batch response in order).
        _raw_printer.total_chunks = len(chunks)

    extra_evals = [
        run_llm_strict_schema_eval,
        functools.partial(run_llm_nonempty_eval, transcript_text=input_text),
        functools.partial(
            _routed_within_source_eval, transcript_text=input_text
        ),
        functools.partial(
            run_llm_gt_coverage_eval, source_id=source_id, lake_root=lake_root
        ),
        # P8-A TLC routing. Appended AFTER the four LLM evals above so it
        # is purely additive — none of those is removed or weakened. It
        # classifies each extracted item by type lane and re-runs the
        # per-lane eval subset, folding the outcomes into ONE combined
        # eval_result the same decide_control gate blocks on. Because it
        # only CALLS the unmodified eval functions, it can add a fail
        # signal but never relax one (HIGH_STAKES keeps the full set;
        # STANDARD gets within_source + strict_schema).
        functools.partial(run_tlc_routed_eval, transcript_text=input_text),
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

    debug_hook = None
    if debug_chunks:
        # Closure captures the post-truncation chunk list actually fed
        # to the model so the report's chunk set matches the run's.
        report_chunks = chunks if grounded else None

        def debug_hook(target: Artifact, eval_results: list) -> None:
            print(
                build_chunk_debug_report(
                    payload=target.payload,
                    chunks=report_chunks,
                    eval_results=eval_results,
                ),
                file=sys.stdout,
                flush=True,
            )

    run = run_governed_loop(
        input_text=input_text,
        artifact_type="meeting_minutes",
        extract=extract,
        chunks=chunks if grounded else None,
        extra_evals=extra_evals,
        debug_hook=debug_hook,
    )

    if run.promoted:
        # Step 5: copy the demoted within_source warn codes from the
        # control_decision (their authoritative source) onto the
        # promoted artifact's provenance so a consumer reading only the
        # promoted JSON still sees that STANDARD items were extracted
        # non-verbatim. This is a post-promotion provenance stamp, not
        # a content change: it is gated on promotion, deterministic
        # given the inputs (sorted codes), schema-additive (provenance
        # permits extra keys), and content_hash is recomputed so the
        # envelope stays integrity-consistent before the data-lake
        # writer serialises it. It mirrors the in-place status mutation
        # promote_if_allowed already performs on the same envelope.
        warn_codes = list(
            run.control_decision.payload.get("within_source_warnings") or []
        )
        if warn_codes and isinstance(run.target.payload, dict):
            provenance = run.target.payload.get("provenance")
            if not isinstance(provenance, dict):
                provenance = {}
                run.target.payload["provenance"] = provenance
            provenance[WITHIN_SOURCE_WARNINGS_PROVENANCE_KEY] = warn_codes
            run.target.content_hash = compute_content_hash(
                run.target.payload
            )

    if single_chunk:
        _emit_single_chunk_debug(
            run=run,
            raw_response=_capture.get("raw"),
            print_context=print_context,
        )

    return WorkflowResult(
        context_bundle=run.context_bundle,
        meeting_minutes=run.target,
        eval_results=run.eval_results,
        control_decision=run.control_decision,
        promoted=run.promoted,
        store=run.store,
    )


__all__ = [
    "run_meeting_minutes_llm_workflow",
    "build_chunk_debug_report",
    "PRODUCED_BY",
    "_make_override_prompt_cm",
]
