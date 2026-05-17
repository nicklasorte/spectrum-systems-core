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
import re
import sys
from pathlib import Path

from ..artifacts import Artifact
from ..config import LLMConfigError, preflight_llm_config
from ..config.taxonomy import UNCLASSIFIED_DECISION_VERB
from ..data_lake.chunker import chunk_transcript
from ..validation import (
    ArtifactValidationError,
    SchemaNotFoundError,
    validate_artifact,
)
from ..evals import (
    EXTRACTION_NOT_IN_SOURCE,
    REGULATORY_VERB_EVAL_TYPE,
    STRICT_SCHEMA_EVAL_TYPE,
    VERB_NOT_CLASSIFIED_PREFIX,
    WITHIN_SOURCE_EVAL_TYPE,
    resolve_decision_verb,
    run_grounding_coverage_eval,
    run_llm_gt_coverage_eval,
    run_llm_nonempty_eval,
    run_llm_strict_schema_eval,
    run_llm_within_source_eval,
    run_source_turn_validity_eval_from_chunks,
    run_tlc_routed_eval,
)
from ._loop import run_governed_loop
from .llm_client import AnthropicJSONClient, LLMClient, LLMClientError
from .meeting_minutes import WorkflowResult

PRODUCED_BY = "meeting_minutes_llm"

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "meeting_minutes_llm.md"

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
# The meeting_minutes_llm workflow makes ONE model call over the whole
# transcript; the chunker's turn-chunks are how every extracted item is
# attributed back to a span of the transcript (the model emits a
# top-level ``grounding`` array of
# ``{"kind","text","source_turns":[turn_id,...]}``). The 34-chunk run
# blocks while a 3-chunk run promotes, so the operator needs to see
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

    # --- within_source: extraction_not_in_source:<key>:<text60> --------
    ws = _eval_payload(WITHIN_SOURCE_EVAL_TYPE)
    for rc in ws.get("reason_codes", []) or []:
        if not isinstance(rc, str) or not rc.startswith(
            EXTRACTION_NOT_IN_SOURCE
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


def _make_extract(
    *,
    client: LLMClient,
    meeting_id: str | None,
    model_id: str,
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

    def _extract(
        input_text: str, chunks: list[dict] | None = None
    ) -> dict:
        title = _derive_title(input_text)
        grounded = bool(chunks)

        # The user turn now carries the explicit extraction directive
        # (the request belongs here, not only in the system prompt),
        # then the raw transcript FIRST (verbatim evals bind to it),
        # then the turn-segmented block so the model can cite turn_ids.
        # Same chunks → same base user message (replay-stable).
        base_user = _build_user_message(input_text, chunks, grounded)
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
    debug_chunks: bool = False,
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
    """
    # Resolve the extraction model FIRST — before any artifact is
    # produced and regardless of whether the transport is injected (a
    # stub client still produces an artifact that must carry the
    # registry-resolved provenance.model_id). A misconfigured registry
    # HALTS here fail-closed rather than producing a blocked artifact
    # with the wrong / no model_id.
    model_id, max_tokens = _resolve_extraction_model()
    if client is None:
        preflight_llm_config(enabled=True, env=env)
    active_client: LLMClient = client or AnthropicJSONClient(
        model=model_id, max_tokens=max_tokens
    )
    extract = _make_extract(
        client=active_client, meeting_id=meeting_id, model_id=model_id
    )

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
]
