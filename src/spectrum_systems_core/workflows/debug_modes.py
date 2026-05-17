"""Four observe-only diagnostic modes for the meeting_minutes_llm path.

Each mode attacks a separate hypothesis for why ``grounding_entries=0``
across every chunk in production while simulations pass. None of them
touches an eval, gate, schema or promotion path; none writes an
artifact; every one prints to stdout only.

* Mode 1 — :class:`RawResponsePrintingClient` wraps the real transport
  and prints the verbatim model response BEFORE the workflow parses it.
  Proves parser-vs-model: if the raw text carries grounding entries but
  ``grounding_entries=0`` after parsing, the parser drops them.
* Mode 2 — :func:`run_minimal_repro` makes ONE direct call with the
  REAL prompt file and 3 transcript turns, bypassing the framework.
  Proves framework-vs-prompt.
* Mode 3 — :func:`build_opus_vs_llm_diff` reconstructs (without calling)
  the meeting_minutes_llm and the Opus-baseline API parameters and
  diffs them. Proves call-configuration divergence — chiefly whether
  the transcript even reaches the model.
* Mode 4 — :func:`build_parser_isolation_report` feeds a known-good
  synthetic response through the REAL parser. Proves the parser in
  isolation: zero items out of a known-good input is a parser bug.

The helpers are deliberately importable and side-effect-free (they
return strings; the CLI does the printing) so the test-suite can drive
them with no data-lake, no network and no API key.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..data_lake.chunker import chunk_transcript
from . import meeting_minutes_llm as _mm
from .llm_client import AnthropicJSONClient, LLMClient, LLMClientError

# turn-id token as the grounded user message renders it, e.g. ``[t0007]``.
_TURN_ID_RE = re.compile(r"\[t\d{4}\]")

# Truncation budget for the verbatim raw response (Mode 1 spec).
_RAW_TRUNCATE = 3000

# Distinctive transcript prefix length used for the substring "is the
# transcript actually in the user message" probe (Mode 3). Long enough
# to be specific, short enough to survive a short fixture transcript.
_TRANSCRIPT_PROBE_CHARS = 200


# ---------------------------------------------------------------------------
# Mode 4 — the synthetic known-good response.
#
# Field names match the canonical prompt's documented item shapes
# (grounding: kind/text/source_turns; decisions: text/verb/source_turns;
# action_items: text/owner/due_date/source_turns) and every one of the
# parser's carried arrays is present (even if empty) so the parser sees
# a COMPLETE payload — a partial input would make a green test prove
# nothing. This dict is the single source of truth for the synthetic;
# tests import it so the input under test cannot drift from the spec.
# ---------------------------------------------------------------------------
SYNTHETIC_PARSER_TEST_RESPONSE: dict[str, Any] = {
    "grounding": [
        {
            "kind": "decision",
            "text": "The TIG will use the aggregate interference methodology.",
            "source_turns": ["t0045"],
        },
        {
            "kind": "action_item",
            "text": "Submit comments to the study plan by tomorrow.",
            "source_turns": ["t0090"],
        },
    ],
    "decisions": [
        {
            "text": "The TIG will use the aggregate interference methodology.",
            "verb": "approved",
            "source_turns": ["t0045"],
        }
    ],
    "action_items": [
        {
            "text": "Submit comments to the study plan by tomorrow.",
            "owner": "DiFrancisco, Michael",
            "due_date": None,
            "source_turns": ["t0090"],
        }
    ],
    "attendees": [],
    "topics": [],
    "claims": [],
    "open_questions": [],
    "commitments": [],
    "risks": [],
    "cross_references": [],
    "named_artifacts": [],
    "regulatory_references": [],
    "technical_parameters": [],
    "scheduled_events": [],
    "sentiment_indicators": [],
    "meeting_phases": [],
    "issue_registry_entry": [],
    "position_statement": [],
    "dissent_or_objection": [],
    "agenda_item": [],
    "precedent_reference": [],
    "external_stakeholder_input": [],
    "glossary_definition": [],
    "procedural_ruling": [],
}


def _contains_word(text: str, word: str) -> bool:
    return isinstance(text, str) and word in text


def _count_items(parsed: object) -> int:
    """Total items across every list value in a parsed payload."""
    if not isinstance(parsed, dict):
        return 0
    return sum(len(v) for v in parsed.values() if isinstance(v, list))


# ---------------------------------------------------------------------------
# Mode 1 — print the raw response BEFORE parsing.
# ---------------------------------------------------------------------------
class RawResponsePrintingClient:
    """Transport wrapper that prints the verbatim model response.

    The wrapped client is called unchanged and its response is returned
    UNCHANGED, so normal extraction / evals / promotion are byte
    identical — this only observes. The print happens inside
    ``__call__`` so it is structurally guaranteed to land BEFORE the
    workflow's ``_parse_llm_payload`` runs on that same response (the
    workflow parses only after the call returns). A retry is a second
    call and prints again, so EVERY API call is shown.

    ``total_chunks`` is informational only and may be set after
    construction (the workflow knows the post-truncation chunk count
    only after it has built this wrapper); the workflow makes ONE
    whole-transcript call rather than one call per chunk, so ``n`` is
    the API-call sequence number, not a chunk index.
    """

    def __init__(self, inner: LLMClient, total_chunks: int = 0):
        self._inner = inner
        self.total_chunks = total_chunks
        self.calls = 0

    def __call__(self, *, system: str, user: str) -> str:
        self.calls += 1
        raw = self._inner(system=system, user=user)
        text = raw if isinstance(raw, str) else str(raw)
        print(
            f"=== RAW API RESPONSE (chunk {self.calls}/{self.total_chunks}) ===",
            flush=True,
        )
        print(text[:_RAW_TRUNCATE], flush=True)
        print("=== END RAW RESPONSE ===", flush=True)
        print(
            f"raw_contains_grounding={_contains_word(text, 'grounding')} "
            f"raw_contains_decisions={_contains_word(text, 'decisions')}",
            flush=True,
        )
        return raw


# ---------------------------------------------------------------------------
# Mode 4 — feed the synthetic through the REAL parser/aggregator.
# ---------------------------------------------------------------------------
def build_parser_isolation_report() -> str:
    """Run :data:`SYNTHETIC_PARSER_TEST_RESPONSE` through the EXACT code
    path the workflow uses on a real model response.

    The real path in ``meeting_minutes_llm._extract`` is
    ``_parse_llm_payload(raw)`` followed by
    ``_fill_unclassified_decision_verbs(parsed["decisions"])``. Both run
    here so a parser regression — not a re-implementation — is what the
    counts measure. ``PASS`` only when the two grounding entries, the
    one decision and the one action_item all survive.
    """
    raw = json.dumps(SYNTHETIC_PARSER_TEST_RESPONSE)
    parsed = _mm._parse_llm_payload(raw)

    if parsed is None:
        return "\n".join(
            [
                "=== PARSER ISOLATION TEST ===",
                "input: 2 grounding entries, 1 decision, 1 action_item",
                "output_grounding_entries: 0",
                "output_decisions: 0",
                "output_action_items: 0",
                "output_items_total: 0",
                "parser_result: FAIL (parser returned None on known-good input)",
                "=== END PARSER TEST ===",
            ]
        )

    parsed["decisions"] = _mm._fill_unclassified_decision_verbs(
        parsed["decisions"]
    )

    g = len(parsed.get("grounding") or [])
    d = len(parsed.get("decisions") or [])
    a = len(parsed.get("action_items") or [])
    total = _count_items(parsed)
    ok = g == 2 and d == 1 and a == 1
    verdict = (
        "PASS (items preserved)" if ok else "FAIL (items dropped)"
    )
    return "\n".join(
        [
            "=== PARSER ISOLATION TEST ===",
            "input: 2 grounding entries, 1 decision, 1 action_item",
            f"output_grounding_entries: {g}",
            f"output_decisions: {d}",
            f"output_action_items: {a}",
            f"output_items_total: {total}",
            f"parser_result: {verdict}",
            "=== END PARSER TEST ===",
        ]
    )


# ---------------------------------------------------------------------------
# Mode 3 — diff the meeting_minutes_llm call against the Opus baseline
# call WITHOUT making either call.
# ---------------------------------------------------------------------------
def _resolve_opus_baseline_params() -> tuple[str, int]:
    """``(model_id, max_tokens)`` the Opus-reference-baseline workflow
    uses, read from the same registry file the workflow resolves at run
    time. ``create-opus-reference-baselines.yml`` reads
    ``models.opus_reference_baseline``;
    ``create_opus_reference_baselines._OPUS_MAX_TOKENS`` is 16384, the
    value also recorded in ``model_metadata.opus_reference_baseline
    .max_tokens`` — read from the registry here so this never drifts
    from the registry the workflow actually resolves.
    """
    raw = _mm._MODEL_REGISTRY_PATH.read_text(encoding="utf-8")
    reg = json.loads(raw)
    model = (reg.get("models") or {}).get("opus_reference_baseline")
    if not isinstance(model, str) or not model.strip():
        model = "<unresolved:models.opus_reference_baseline>"
    meta = (reg.get("model_metadata") or {}).get(
        "opus_reference_baseline"
    ) or {}
    mt = meta.get("max_tokens")
    max_tokens = mt if isinstance(mt, int) and not isinstance(mt, bool) else 16384
    return model.strip() if isinstance(model, str) else model, max_tokens


def build_opus_vs_llm_diff(*, transcript_text: str) -> str:
    """Reconstruct both API calls' parameters and diff them. No call is
    made — this only inspects how the two would be configured.

    The single most important line is ``user_message_contains_transcript``:
    if it is ``False`` for ``llm`` the transcript never reaches the
    model and that is the root cause. It is ALWAYS printed as a bool.
    """
    # --- meeting_minutes_llm side ----------------------------------
    try:
        llm_model, llm_max_tokens = _mm._resolve_extraction_model()
    except Exception as exc:  # noqa: BLE001 — diagnostics never crash
        llm_model, llm_max_tokens = (
            f"<unresolved:{type(exc).__name__}>",
            -1,
        )
    system_prompt = _mm._system_prompt()
    chunks = chunk_transcript(transcript_text)
    if chunks:
        llm_user = transcript_text + _mm._render_turn_block(chunks)
    else:
        llm_user = transcript_text

    # --- opus_reference_baseline side ------------------------------
    # The Opus baseline workflow reuses the SAME prompt file
    # (create_opus_reference_baselines._PROMPT_PATH IS
    # meeting_minutes_llm._PROMPT_PATH) and sends the RAW transcript as
    # the user message — no turn block.
    opus_model, opus_max_tokens = _resolve_opus_baseline_params()
    opus_system = system_prompt
    opus_user = transcript_text

    def _h(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]

    probe = transcript_text.strip()[:_TRANSCRIPT_PROBE_CHARS]
    llm_has_transcript = bool(probe) and probe in llm_user
    opus_has_transcript = bool(probe) and probe in opus_user
    llm_has_turn_ids = _TURN_ID_RE.search(llm_user) is not None
    opus_has_turn_ids = _TURN_ID_RE.search(opus_user) is not None

    def _first500(text: str) -> str:
        return text[:500].replace("\n", "\\n")

    return "\n".join(
        [
            "=== API CALL DIFF: meeting_minutes_llm vs opus_baseline ===",
            "model:",
            f"  llm:  {llm_model}",
            f"  opus: {opus_model}",
            "max_tokens:",
            f"  llm:  {llm_max_tokens}",
            f"  opus: {opus_max_tokens}",
            "system_prompt_length:",
            f"  llm:  {len(system_prompt)}",
            f"  opus: {len(opus_system)}",
            "system_prompt_hash:",
            f"  llm:  {_h(system_prompt)}",
            f"  opus: {_h(opus_system)}",
            "user_message_first_500_chars:",
            f"  llm:  {_first500(llm_user)}",
            f"  opus: {_first500(opus_user)}",
            "user_message_contains_transcript:",
            f"  llm:  {llm_has_transcript}",
            f"  opus: {opus_has_transcript}",
            "user_message_contains_turn_ids:",
            f"  llm:  {llm_has_turn_ids}",
            f"  opus: {opus_has_turn_ids}",
            "=== END DIFF ===",
        ]
    )


# ---------------------------------------------------------------------------
# Mode 2 — standalone minimal reproduction, bypassing the framework.
# ---------------------------------------------------------------------------
_MINIMAL_REPRO_TURNS = 3
_MINIMAL_REPRO_MAX_TOKENS = 4096


def _render_first_turns(transcript_text: str, n: int) -> str:
    """``[turn_id] SPEAKER: text`` for the first ``n`` chunked turns,
    using the SAME chunker the workflow uses so the turns are the real
    ones, not a hand-rolled approximation."""
    lines: list[str] = []
    for chunk in chunk_transcript(transcript_text)[:n]:
        speaker = chunk.get("speaker")
        who = f" {speaker}:" if speaker else ""
        lines.append(
            f"[{chunk['turn_id']}]{who} {chunk.get('text', '')}"
        )
    return "\n".join(lines)


def run_minimal_repro(
    *,
    transcript_text: str,
    client: LLMClient | None = None,
) -> str:
    """One direct API call: REAL prompt file verbatim as the system
    message, the first 3 real transcript turns as the user message,
    ``max_tokens=4096``. Prints the full raw response and the
    contains/items summary, then the caller exits — the framework, the
    chunked grounded prompt, the evals and the gate are all bypassed so
    this isolates "does the model, given this prompt, return items at
    all".

    ``client`` is injected by tests; production resolves the same
    registry model the workflow uses and constructs the real transport.
    A transport failure is reported (not raised) — a debug probe that
    crashes proves nothing.
    """
    system = _mm._system_prompt()
    turns = _render_first_turns(transcript_text, _MINIMAL_REPRO_TURNS)
    user = f"Extract from these 3 transcript turns:\n{turns}"

    active = client
    if active is None:
        try:
            model_id, _ = _mm._resolve_extraction_model()
        except Exception as exc:  # noqa: BLE001
            return (
                "=== MINIMAL REPRO ===\n"
                f"model_registry_error: {type(exc).__name__}: {exc}\n"
                "=== END MINIMAL REPRO ==="
            )
        active = AnthropicJSONClient(
            model=model_id, max_tokens=_MINIMAL_REPRO_MAX_TOKENS
        )

    try:
        raw = active(system=system, user=user)
    except LLMClientError as exc:
        return (
            "=== MINIMAL REPRO ===\n"
            f"llm_transport_error: {exc}\n"
            "=== END MINIMAL REPRO ==="
        )

    text = raw if isinstance(raw, str) else str(raw)
    try:
        doc = json.loads(_mm._strip_fence(text))
    except (json.JSONDecodeError, TypeError):
        doc = None
    items = _count_items(doc)

    return "\n".join(
        [
            "=== MINIMAL REPRO ===",
            "model_call: 1 (direct, framework bypassed)",
            "--- FULL RAW RESPONSE ---",
            text,
            "--- END RAW RESPONSE ---",
            f"contains_grounding={_contains_word(text, 'grounding')} "
            f"contains_decisions={_contains_word(text, 'decisions')} "
            f"items_in_response={items}",
            "=== END MINIMAL REPRO ===",
        ]
    )


__all__ = [
    "SYNTHETIC_PARSER_TEST_RESPONSE",
    "RawResponsePrintingClient",
    "build_parser_isolation_report",
    "build_opus_vs_llm_diff",
    "run_minimal_repro",
]
