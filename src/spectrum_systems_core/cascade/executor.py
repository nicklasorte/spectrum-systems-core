"""Phase 6 — Stage 2 cascade filter executor.

After Haiku has produced a `meeting_minutes` artifact, the cascade asks
Sonnet (or any configured filter model) whether each extracted item
should be kept or dropped. The result is a `meeting_minutes_filtered`
artifact whose `filtered_items` is a strict subset of the source's
payload arrays — the cascade NEVER invents or mutates items.

Design contract:

* The executor is PURE except for the LLM call. The LLM call goes
  through an abstract `api_client` callable so unit tests can swap in
  a deterministic stub.
* Per-chunk batching: items are grouped by their source chunk, and one
  filter API call evaluates all items from that chunk together.
* Conservative failure mode: if Sonnet's response fails JSON Schema
  validation, EVERY item in that chunk is KEPT (not dropped). This
  preserves recall at the cost of leaving false positives in the
  filtered output. Operators monitor
  `chunks_with_invalid_filter_response` on the
  `cascade_filter_log` to detect when this happens at scale.
* The `reason` field on each Haiku-extracted item is STRIPPED before
  being sent to the filter — the filter's decision must be
  independent of Haiku's extraction reasoning.
* `additionalProperties: false` is enforced on every artifact this
  module writes (via `validation.validate_artifact`).
* Idempotency: two calls with byte-identical inputs (transcript,
  source artifact, prompt, and a deterministic `api_client`) MUST
  produce identical filtered subsets. Real Sonnet calls are not
  strictly deterministic even at `temperature=0`; operators requiring
  strict reproducibility rely on the per-source variance budget from
  Phase 3 to gauge expected variation.
"""
from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

import jsonschema

from ..schemas import schema_path
from ..validation import validate_artifact


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

FILTERED_ARTIFACT_TYPE: str = "meeting_minutes_filtered"
FILTERED_SCHEMA_VERSION: str = "1.0.0"

CASCADE_FILTER_LOG_ARTIFACT_TYPE: str = "cascade_filter_log"
CASCADE_FILTER_LOG_SCHEMA_VERSION: str = "1.0.0"
CASCADE_FILTER_LOG_TTL_DAYS: int = 30

DEFAULT_CASCADE_FILTER_MODEL: str = "claude-sonnet-4-6"

# Pass-through marker recorded on every item from a chunk whose
# filter response failed schema validation.
FILTER_RESPONSE_INVALID_PASSTHROUGH: str = "invalid_response_passthrough"

# Per-chunk turn budget for turn_aggregate items. Items whose
# source_turn_ids list exceeds this are truncated before being sent
# to the filter; `truncation_count` on the cascade_filter_log records it.
_TURN_AGGREGATE_BUDGET: int = 10

# Verbatim-item context window around the source_quote (characters
# BEFORE and AFTER the quote). Kept small so the per-chunk prompt
# stays well within Sonnet's 200k context budget.
_VERBATIM_CONTEXT_PAD: int = 100

# Per-chunk output token budget for the filter. Sized to ~20-30 items
# per chunk; cascade items are 3-line JSON objects.
DEFAULT_PER_CHUNK_OUTPUT_TOKENS: int = 800

# Maximum items sent to the filter in a single API call. When a chunk
# accumulates more items than this (e.g. unlocated-grounding fallbacks
# bucketed into chunk 0), the chunk is split into sub-batches of at most
# this many items and one filter call is made per sub-batch. Sized to
# match `DEFAULT_PER_CHUNK_OUTPUT_TOKENS` (~30 items fits in 800 output
# tokens with margin). Without this cap, a 230-item bucket sent in one
# call produces a truncated response whose schema validation fails,
# triggering conservative pass-through on every item.
MAX_ITEMS_PER_FILTER_CALL: int = 30

# Approximate bytes/token ratio for the filter cost estimator. Mirrors
# `cost.estimator._BYTES_PER_TOKEN` so the two estimators stay in sync.
_BYTES_PER_TOKEN: int = 4

# Where the cascade filter prompt lives. Loaded once per run.
CASCADE_FILTER_PROMPT_PATH: Path = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "prompts"
    / "cascade_filter_sonnet.md"
)


# ---------------------------------------------------------------------------
# The 23 extraction array keys on the meeting_minutes payload that the
# cascade filters. Kept here as the single source of truth; the
# `meeting_minutes_filtered.schema.json::filtered_items` properties list
# is asserted equal to this in `tests/cascade/test_schema_round_trip.py`.
#
# Naming note: the spec calls this "the 22 array keys" but the
# meeting_minutes payload actually carries 23 extraction arrays after
# the Phase 5 / Phase 3 / Phase 2 / Phase 1 additions. We use the
# living count here and assert exhaustiveness in tests.
# ---------------------------------------------------------------------------
_EXTRACTION_ARRAY_KEYS: Tuple[str, ...] = (
    "decisions",
    "action_items",
    "open_questions",
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
    "issue_registry_entry",
    "position_statement",
    "dissent_or_objection",
    "agenda_item",
    "precedent_reference",
    "external_stakeholder_input",
    "glossary_definition",
    "procedural_ruling",
)


def extraction_array_keys() -> Tuple[str, ...]:
    """Return the tuple of payload array keys the cascade evaluates."""
    return _EXTRACTION_ARRAY_KEYS


# ---------------------------------------------------------------------------
# Errors and result types.
# ---------------------------------------------------------------------------


class CascadeError(RuntimeError):
    """Raised when the cascade cannot complete fail-closed.

    Carries `reason_code` so a caller pattern-matches a stable token
    rather than parsing a message string.
    """

    def __init__(self, reason_code: str, message: str = "") -> None:
        self.reason_code = reason_code
        super().__init__(message or reason_code)


@dataclass(frozen=True)
class FilterDecision:
    """One per-item keep/drop decision from the filter."""

    item_idx: int
    decision: str  # "keep" | "drop"
    reason: str


@dataclass
class _LogEntry:
    chunk_index: int
    item_idx: int
    extraction_type: str
    decision: str
    reason: str


@dataclass
class CascadeFilterResult:
    """Return value of `run_cascade_filter`.

    Fields:
      filtered_items: object with the 23 array keys; each value is a
        subset of the source `meeting_minutes` payload's items.
      filter_metadata: dict matching
        `meeting_minutes_filtered.schema.json::filter_metadata` exactly.
      filter_log_entries: list of `_LogEntry` records (used by
        `write_cascade_filter_log` to build the diagnostic artifact).
      total_filter_tokens: integer total of input+output tokens reported
        by the api_client (best-effort; 0 when the client did not report).
      total_filter_cost_usd: Decimal — best-effort cost of the filter
        pass (0 when pricing is unavailable for the filter model).
    """

    filtered_items: Dict[str, List[Any]]
    filter_metadata: Dict[str, Any]
    filter_log_entries: List[_LogEntry]
    total_filter_tokens: int
    total_filter_cost_usd: Decimal


# ---------------------------------------------------------------------------
# Prompt loading helpers.
# ---------------------------------------------------------------------------


def cascade_filter_prompt_content(
    path: Path | str | None = None,
) -> str:
    """Read the cascade filter prompt from disk.

    Reads on every call (no caching) so tests can swap the file
    between calls — mirrors `cost.estimator.load_cost_constants`.
    """
    p = Path(path) if path is not None else CASCADE_FILTER_PROMPT_PATH
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CascadeError(
            "cascade_filter_prompt_missing",
            f"cascade filter prompt not found at {p}",
        ) from exc


def cascade_filter_prompt_content_hash(
    path: Path | str | None = None,
) -> str:
    """sha256 of the cascade filter prompt content."""
    return hashlib.sha256(
        cascade_filter_prompt_content(path).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# Item-count helpers.
# ---------------------------------------------------------------------------


def items_in_artifact_count(
    artifact_or_payload: Mapping[str, Any],
) -> int:
    """Count items across the 23 extraction arrays in a meeting_minutes
    artifact (full envelope) OR a bare payload dict.

    Tolerant input: callers pass either form. The threshold check on
    the CLI calls this against the source artifact envelope; tests
    call it against a synthetic payload.
    """
    payload = artifact_or_payload
    if isinstance(payload, Mapping) and "payload" in payload and isinstance(
        payload.get("payload"), Mapping
    ):
        payload = payload["payload"]  # type: ignore[assignment]
    total = 0
    for key in _EXTRACTION_ARRAY_KEYS:
        v = payload.get(key) if isinstance(payload, Mapping) else None
        if isinstance(v, list):
            total += len(v)
    return total


# ---------------------------------------------------------------------------
# Filter response validation.
# ---------------------------------------------------------------------------


_FILTER_RESPONSE_SCHEMA_PATH: Path = (
    Path(__file__).resolve().parent / "cascade_filter_response.schema.json"
)


def _load_filter_response_schema() -> Dict[str, Any]:
    """Load the cascade filter response schema.

    The response schema is a top-level JSON array (not an envelope), so
    it lives in the cascade module directory instead of the central
    `schemas/` registry — the CI gate
    `tests/ci/test_phase_x_schemas.py` requires every file in
    `schemas/` to be a versioned artifact envelope, which a per-call
    payload schema is not.
    """
    return json.loads(
        _FILTER_RESPONSE_SCHEMA_PATH.read_text(encoding="utf-8")
    )


def _validate_filter_response(
    raw: Any, expected_item_count: int
) -> Tuple[bool, str, List[FilterDecision]]:
    """Validate one chunk's filter response.

    Returns `(ok, message, decisions)`. On `ok == False`, callers MUST
    pass-through every item in the chunk (conservative failure mode)
    and increment `chunks_with_invalid_filter_response`.
    """
    schema = _load_filter_response_schema()
    try:
        jsonschema.Draft202012Validator(schema).validate(raw)
    except jsonschema.ValidationError as exc:
        return (False, f"schema_invalid: {exc.message}", [])

    # Beyond JSON Schema: every input item must appear exactly once.
    seen: Dict[int, FilterDecision] = {}
    for entry in raw:
        idx = int(entry["item_idx"])
        if idx in seen:
            return (
                False,
                f"duplicate_item_idx: {idx} appears twice",
                [],
            )
        seen[idx] = FilterDecision(
            item_idx=idx,
            decision=str(entry["decision"]),
            reason=str(entry["reason"]),
        )
    if set(seen.keys()) != set(range(expected_item_count)):
        missing = sorted(set(range(expected_item_count)) - set(seen.keys()))
        extra = sorted(set(seen.keys()) - set(range(expected_item_count)))
        return (
            False,
            (
                f"item_idx_mismatch: missing={missing!r} extra={extra!r} "
                f"expected_item_count={expected_item_count}"
            ),
            [],
        )
    decisions = [seen[i] for i in range(expected_item_count)]
    return (True, "", decisions)


# ---------------------------------------------------------------------------
# Per-item prompt construction.
# ---------------------------------------------------------------------------


def _build_chunk_payload_for_filter(
    chunk_text: str,
    items: Sequence[Tuple[str, Dict[str, Any]]],
    turn_index: Mapping[int, str],
) -> Tuple[List[Dict[str, Any]], int]:
    """Build the per-item payload for one chunk's filter call.

    Returns `(stripped_items, truncation_count)`. Each `stripped_items`
    entry carries:
      * `item_idx`: position in the input list
      * `extraction_type`: e.g. "decisions"
      * The full item EXCEPT the `reason` field (stripped — see
        module docstring for why)
      * Grounding: `source_quote` (+ context window) for verbatim items,
        or `source_turn_ids` (+ rendered turn text) for turn_aggregate
        items. Verbatim items also keep their `quote_offset_normalized`
        if present so the filter can locate the quote in the chunk.

    `turn_index` is the {turn_id -> turn_text} mapping; the cascade
    uses it to render up to `_TURN_AGGREGATE_BUDGET` turns inline for
    turn_aggregate items. Any turn_id absent from the index renders as
    an empty string (the filter still sees the id list so it can reason
    about coverage).
    """
    out: List[Dict[str, Any]] = []
    truncation_count = 0
    for idx, (etype, raw_item) in enumerate(items):
        if not isinstance(raw_item, dict):
            # Legacy string-form items: pass through with no grounding.
            stripped: Dict[str, Any] = {
                "item_idx": idx,
                "extraction_type": etype,
                "item": raw_item,
            }
            out.append(stripped)
            continue

        item_copy = {k: v for k, v in raw_item.items() if k != "reason"}

        grounding_mode = item_copy.get("grounding_mode")
        if grounding_mode == "verbatim":
            quote = item_copy.get("source_quote")
            if isinstance(quote, str) and quote:
                # Locate the quote in the chunk if possible; emit a
                # padded substring so the filter sees the local context.
                pos = chunk_text.find(quote)
                if pos >= 0:
                    start = max(0, pos - _VERBATIM_CONTEXT_PAD)
                    end = min(
                        len(chunk_text),
                        pos + len(quote) + _VERBATIM_CONTEXT_PAD,
                    )
                    item_copy["_chunk_context"] = chunk_text[start:end]
        elif grounding_mode == "turn_aggregate":
            turn_ids = item_copy.get("source_turn_ids") or []
            if isinstance(turn_ids, list) and len(turn_ids) > _TURN_AGGREGATE_BUDGET:
                truncated_ids = list(turn_ids[:_TURN_AGGREGATE_BUDGET])
                more = len(turn_ids) - _TURN_AGGREGATE_BUDGET
                rendered = [
                    turn_index.get(int(t), "") for t in truncated_ids
                ]
                rendered.append(f"[... {more} more turns truncated ...]")
                item_copy["_turn_text"] = rendered
                truncation_count += 1
            elif isinstance(turn_ids, list):
                item_copy["_turn_text"] = [
                    turn_index.get(int(t), "") for t in turn_ids
                ]

        stripped = {
            "item_idx": idx,
            "extraction_type": etype,
            "item": item_copy,
        }
        out.append(stripped)
    return out, truncation_count


def _render_filter_prompt(
    prompt_template: str,
    chunk_text: str,
    payload_items: Sequence[Dict[str, Any]],
) -> str:
    """Substitute the two template variables in the cascade prompt.

    The template uses `<chunk_text>` and
    `<items_json_without_reason_field>` placeholders. We use simple
    string replacement (NOT `str.format`) because the prompt contains
    `{` characters that would break a format-string substitution.
    """
    body = prompt_template.replace("<chunk_text>", chunk_text)
    body = body.replace(
        "<items_json_without_reason_field>",
        json.dumps(list(payload_items), sort_keys=True, indent=2),
    )
    return body


# ---------------------------------------------------------------------------
# Chunk index helpers — group items by source chunk and build the
# turn_id -> turn_text map.
# ---------------------------------------------------------------------------


@dataclass
class _ChunkAssignment:
    """One item's assignment to a chunk.

    `original_payload_index` is the item's index within
    `source_payload[etype]` so we can splice the kept items back into a
    deterministic order. `chunk_index` is the chunk the item belongs to
    (the smallest chunk whose text overlaps the item's grounding).
    """

    extraction_type: str
    original_payload_index: int
    chunk_index: int
    item: Dict[str, Any]


def _normalize_chunks(
    chunks: Sequence[Mapping[str, Any] | str],
) -> List[Tuple[int, str]]:
    """Coerce chunks (mapping OR string) to `[(chunk_index, text), ...]`."""
    out: List[Tuple[int, str]] = []
    for idx, ch in enumerate(chunks):
        if isinstance(ch, Mapping):
            text = ch.get("text") or ""
        else:
            text = str(ch)
        out.append((idx, text))
    return out


def _build_turn_to_chunk_map(
    chunks: Sequence[Mapping[str, Any] | str],
) -> Dict[str, int]:
    """Build a `{turn_id -> chunk_index}` map from chunks that carry one.

    The production chunker (`data_lake/chunker.py::chunk_transcript`)
    emits one chunk per speaker turn with `turn_id` like `"t0042"`.
    Without this map, a `turn_aggregate` item (topics, attendees,
    meeting_phases, etc.) cannot be routed to its source chunk — the
    cascade would bucket every such item into chunk 0, producing the
    pathological `chunks_evaluated=1` / `items_dropped=0` failure mode
    when a real artifact carries many turn_aggregate items.

    Keys are stored in both the original `turn_id` string form
    (e.g. `"t0042"`) AND a bare-integer string form (e.g. `"42"`) so
    items emitting `source_turn_ids` as integers (per the
    meeting_minutes schema) match the same map as items emitting
    `"t0042"` strings.

    Bare-string chunks (no mapping form) contribute nothing.
    """
    out: Dict[str, int] = {}
    for idx, ch in enumerate(chunks):
        if not isinstance(ch, Mapping):
            continue
        tid = ch.get("turn_id")
        if isinstance(tid, str) and tid:
            out.setdefault(tid, idx)
            # Also index the bare-integer form: "t0042" -> "42".
            stripped = tid.lstrip("t").lstrip("0") or "0"
            out.setdefault(stripped, idx)
        elif isinstance(tid, int):
            out.setdefault(str(tid), idx)
            out.setdefault(f"t{tid:04d}", idx)
    return out


def _assign_items_to_chunks(
    source_payload: Mapping[str, Any],
    chunks: Sequence[Tuple[int, str]],
    turn_to_chunk: Optional[Mapping[str, int]] = None,
) -> List[_ChunkAssignment]:
    """Assign each extraction item to its source chunk.

    For verbatim items: the chunk whose text contains `source_quote`.
    For turn_aggregate items: the chunk whose `turn_id` matches the
    item's first `source_turn_ids` entry (via `turn_to_chunk`). The
    match is a substring search for verbatim items — chunks are
    normally contiguous transcript spans, so the first match is the
    correct one.

    Items with no grounding (or whose grounding cannot be located) are
    assigned to chunk 0. The filter still gets them — they are just
    grouped together. A real Phase 1 production artifact carries
    grounding on every item, so this fallback only fires on synthetic /
    legacy artifacts.
    """
    turn_map = turn_to_chunk or {}
    assignments: List[_ChunkAssignment] = []
    for etype in _EXTRACTION_ARRAY_KEYS:
        items = source_payload.get(etype)
        if not isinstance(items, list):
            continue
        for orig_idx, raw_item in enumerate(items):
            if isinstance(raw_item, dict):
                chunk_idx = _locate_chunk_for_item(raw_item, chunks, turn_map)
            else:
                # Legacy string-form item. No grounding — bucket to
                # chunk 0 so the filter still sees it.
                chunk_idx = 0
            assignments.append(
                _ChunkAssignment(
                    extraction_type=etype,
                    original_payload_index=orig_idx,
                    chunk_index=chunk_idx,
                    item=raw_item if isinstance(raw_item, dict) else {"text": raw_item},
                )
            )
    return assignments


def _locate_chunk_for_item(
    item: Mapping[str, Any],
    chunks: Sequence[Tuple[int, str]],
    turn_to_chunk: Optional[Mapping[str, int]] = None,
) -> int:
    """Return the index of the chunk that grounds this item.

    Routing rules:
    * `turn_aggregate` items → the chunk whose `turn_id` matches the
      first `source_turn_ids` entry. Both string (`"t0042"`) and
      integer (`42`) forms are accepted because the schema declares
      `source_turn_ids` as integers but some upstream paths emit the
      `t0042` string form.
    * `verbatim` items → the first chunk whose text contains
      `source_quote` as a substring.
    * Items whose grounding cannot be located → chunk 0 (fallback so
      the filter still sees them).
    """
    if not chunks:
        return 0

    grounding_mode = item.get("grounding_mode")

    if grounding_mode == "turn_aggregate" and turn_to_chunk:
        turn_ids = item.get("source_turn_ids") or item.get("source_turns") or []
        if isinstance(turn_ids, list):
            for tid in turn_ids:
                if isinstance(tid, (str, int)):
                    chunk_idx = turn_to_chunk.get(str(tid))
                    if chunk_idx is not None:
                        return chunk_idx
        return 0

    quote = item.get("source_quote")
    if isinstance(quote, str) and quote:
        for idx, text in chunks:
            if quote in text:
                return idx

    # Verbatim items whose quote could not be located may carry a
    # secondary turn-id signal (some upstream paths attach
    # `source_turn_ids` even on verbatim items). Try it before falling
    # back to chunk 0 so a paraphrase-near-miss still routes to the
    # right chunk.
    if turn_to_chunk:
        turn_ids = item.get("source_turn_ids") or item.get("source_turns") or []
        if isinstance(turn_ids, list):
            for tid in turn_ids:
                if isinstance(tid, (str, int)):
                    chunk_idx = turn_to_chunk.get(str(tid))
                    if chunk_idx is not None:
                        return chunk_idx
    return 0


def _build_turn_index(
    turn_records: Optional[Sequence[Mapping[str, Any]]],
) -> Dict[int, str]:
    """Build a {turn_id -> text} index from a turn records list.

    The caller (production CLI) loads the chunked transcript's
    `chunks.jsonl` and concatenates each chunk's `turns` array into
    one flat list; the resulting list is what we index. Synthetic test
    fixtures pass a small list directly. `None` produces an empty
    index — the filter still works, it just has no turn text to splice
    into turn_aggregate items.
    """
    if not turn_records:
        return {}
    out: Dict[int, str] = {}
    for rec in turn_records:
        if not isinstance(rec, Mapping):
            continue
        tid = rec.get("turn_id")
        text = rec.get("text") or rec.get("turn_text")
        if tid is None or not isinstance(text, str):
            continue
        try:
            out[int(tid)] = text
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# The single execution path.
# ---------------------------------------------------------------------------


def run_cascade_filter(
    *,
    source_artifact: Mapping[str, Any],
    chunks: Sequence[Mapping[str, Any] | str],
    api_client: Callable[..., Any],
    filter_model: str = DEFAULT_CASCADE_FILTER_MODEL,
    filter_prompt: Optional[str] = None,
    filter_prompt_path: Path | str | None = None,
    turn_records: Optional[Sequence[Mapping[str, Any]]] = None,
    per_chunk_output_tokens: int = DEFAULT_PER_CHUNK_OUTPUT_TOKENS,
    cost_constants_path: Path | str | None = None,
    clock: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(
        datetime.timezone.utc
    ),
) -> CascadeFilterResult:
    """Run the Stage 2 cascade against one `meeting_minutes` source.

    Inputs:
      source_artifact: the full Haiku-produced `meeting_minutes`
        envelope (or a bare payload dict — both forms are accepted).
      chunks: the chunked transcript the source was extracted from.
        Each chunk is either `{"text": "...", "turns": [...]}` or a
        bare string. Used both to ground items to their source chunk
        and to splice transcript context into the per-item filter
        prompt.
      api_client: callable invoked once per chunk. Signature:
        `api_client(system: str, user: str) -> str | dict`. Returns
        either the raw JSON-string response or a `{"text": "...",
        "input_tokens": int, "output_tokens": int}` dict. The dict
        form lets tests assert on actual token usage; the bare-string
        form is what real Anthropic SDK clients return after the JSON
        is extracted.
      filter_model: model id stamped onto the filtered artifact's
        `filter_metadata.filter_model`.
      filter_prompt: optional override of the prompt text (tests).
      filter_prompt_path: optional override of the prompt path.
        Defaults to `CASCADE_FILTER_PROMPT_PATH`. Both ignored when
        `filter_prompt` is given.
      turn_records: optional flat list of turn records — used to
        render turn_aggregate items.
      per_chunk_output_tokens: max_tokens parameter passed to
        `api_client` (best-effort; clients that ignore it still work).
      cost_constants_path: optional override for the cost constants
        file (tests).
      clock: injectable clock — tests pin it for deterministic
        timestamps.

    Returns a `CascadeFilterResult`. Does NOT write anything to disk;
    `write_filtered_artifact` and `write_cascade_filter_log` do that.

    Determinism: with a deterministic `api_client` (returning the same
    string for the same `(system, user)` pair), two calls produce
    byte-identical results. Real LLM calls are not strictly
    deterministic even at temperature=0; this is documented in the
    module docstring.
    """
    started_at = clock()

    # ---- Phase 6 prompt + payload normalisation ----------------------
    if filter_prompt is None:
        prompt_text = cascade_filter_prompt_content(filter_prompt_path)
        prompt_hash = cascade_filter_prompt_content_hash(filter_prompt_path)
        prompt_path_used = str(filter_prompt_path or CASCADE_FILTER_PROMPT_PATH)
    else:
        prompt_text = filter_prompt
        prompt_hash = hashlib.sha256(
            prompt_text.encode("utf-8")
        ).hexdigest()
        prompt_path_used = str(filter_prompt_path or "<inline>")

    if isinstance(source_artifact, Mapping) and isinstance(
        source_artifact.get("payload"), Mapping
    ):
        source_payload: Mapping[str, Any] = source_artifact["payload"]  # type: ignore[assignment]
    else:
        source_payload = source_artifact

    chunks_normalized = _normalize_chunks(chunks)
    turn_index = _build_turn_index(turn_records)
    turn_to_chunk = _build_turn_to_chunk_map(chunks)

    # ---- Build chunk -> [item assignment] groupings ------------------
    assignments = _assign_items_to_chunks(
        source_payload, chunks_normalized, turn_to_chunk
    )

    # Group by chunk_index, preserving original payload order so the
    # filtered subset reads in the same order as the source.
    by_chunk: Dict[int, List[_ChunkAssignment]] = {}
    for a in assignments:
        by_chunk.setdefault(a.chunk_index, []).append(a)

    # Pre-seed the filtered_items output with empty arrays for EVERY
    # extraction key. A missing key in the input becomes an empty array
    # in the output — never absent — so a downstream reader does not
    # have to defend against a key-absence vs empty-array distinction.
    filtered_items: Dict[str, List[Any]] = {
        k: [] for k in _EXTRACTION_ARRAY_KEYS
    }

    log_entries: List[_LogEntry] = []
    chunks_evaluated = 0
    chunks_with_invalid_filter_response = 0
    truncation_count = 0
    total_filter_tokens = 0

    # Track which items the cascade KEPT so we can splice them back
    # into filtered_items in original payload order at the end.
    kept_indices: Dict[str, List[int]] = {k: [] for k in _EXTRACTION_ARRAY_KEYS}

    # Empty extraction: short-circuit to an empty cascade. Still counts
    # as "0 chunks evaluated" so the metadata is consistent.
    no_items = not any(by_chunk.values())

    # ---- Per-chunk filter loop ---------------------------------------
    for chunk_idx in sorted(by_chunk):
        chunk_assignments = by_chunk[chunk_idx]
        # Chunk text (best-effort). A chunk_index out of range happens
        # when the source artifact was produced against a different
        # chunking — fall back to empty string; the filter still gets
        # the items and their per-item grounding context window.
        chunk_text = ""
        for idx, text in chunks_normalized:
            if idx == chunk_idx:
                chunk_text = text
                break

        chunks_evaluated += 1
        chunk_had_invalid_subbatch = False

        # Split the chunk into sub-batches of at most
        # MAX_ITEMS_PER_FILTER_CALL items. A single API call asked to
        # judge more than ~30 items is highly likely to truncate its
        # JSON output and fail schema validation, which would force the
        # cascade into conservative pass-through for every item in the
        # bucket (the failure mode observed on the Dec 18 transcript:
        # 230 items in chunk 0 → one truncated response → 0 drops).
        for sub_start in range(
            0, len(chunk_assignments), MAX_ITEMS_PER_FILTER_CALL
        ):
            sub_assignments = chunk_assignments[
                sub_start : sub_start + MAX_ITEMS_PER_FILTER_CALL
            ]

            items_for_filter: List[Tuple[str, Dict[str, Any]]] = [
                (a.extraction_type, a.item) for a in sub_assignments
            ]
            payload_items, chunk_trunc = _build_chunk_payload_for_filter(
                chunk_text, items_for_filter, turn_index
            )
            truncation_count += chunk_trunc

            user_message = _render_filter_prompt(
                prompt_text, chunk_text, payload_items
            )

            try:
                client_response = api_client(
                    system=prompt_text,
                    user=user_message,
                    model=filter_model,
                    max_tokens=per_chunk_output_tokens,
                )
            except TypeError:
                # Tolerate api_clients that do not accept `model` /
                # `max_tokens` kwargs. The stub in tests/cascade/ uses
                # the 2-arg form for clarity.
                client_response = api_client(
                    system=prompt_text, user=user_message
                )

            # Accept either a bare JSON string OR a `{text, input_tokens,
            # output_tokens}` dict. Real Anthropic SDK returns the dict
            # form; the test stubs return strings.
            response_text: str
            if isinstance(client_response, Mapping):
                response_text = str(client_response.get("text") or "")
                total_filter_tokens += int(
                    client_response.get("input_tokens", 0) or 0
                )
                total_filter_tokens += int(
                    client_response.get("output_tokens", 0) or 0
                )
            else:
                response_text = str(client_response)

            try:
                parsed = json.loads(response_text)
            except json.JSONDecodeError as exc:
                ok = False
                message = f"json_decode_error: {exc.msg}"
                decisions: List[FilterDecision] = []
            else:
                ok, message, decisions = _validate_filter_response(
                    parsed, expected_item_count=len(sub_assignments)
                )

            if not ok:
                # Conservative pass-through for this sub-batch only:
                # every item in the sub-batch is KEPT (not dropped) and
                # logged with the failure reason so a reviewer can trace
                # which sub-batches fell through. Other sub-batches of
                # the same chunk still get their real decisions.
                chunk_had_invalid_subbatch = True
                for local_idx, a in enumerate(sub_assignments):
                    kept_indices[a.extraction_type].append(
                        a.original_payload_index
                    )
                    log_entries.append(
                        _LogEntry(
                            chunk_index=chunk_idx,
                            item_idx=sub_start + local_idx,
                            extraction_type=a.extraction_type,
                            decision=FILTER_RESPONSE_INVALID_PASSTHROUGH,
                            reason=message,
                        )
                    )
                continue

            # Apply per-item decisions for this sub-batch.
            for d in decisions:
                a = sub_assignments[d.item_idx]
                if d.decision == "keep":
                    kept_indices[a.extraction_type].append(
                        a.original_payload_index
                    )
                log_entries.append(
                    _LogEntry(
                        chunk_index=chunk_idx,
                        item_idx=sub_start + d.item_idx,
                        extraction_type=a.extraction_type,
                        decision=d.decision,
                        reason=d.reason,
                    )
                )

        if chunk_had_invalid_subbatch:
            chunks_with_invalid_filter_response += 1

    # Splice kept items back into filtered_items in original order.
    for etype in _EXTRACTION_ARRAY_KEYS:
        idxs = sorted(set(kept_indices.get(etype, [])))
        source_list = source_payload.get(etype)
        if not isinstance(source_list, list):
            continue
        filtered_items[etype] = [source_list[i] for i in idxs if 0 <= i < len(source_list)]

    items_kept_count = sum(len(v) for v in filtered_items.values())
    items_in_count = items_in_artifact_count(source_payload)
    items_dropped_count = max(0, items_in_count - items_kept_count)

    # Best-effort cost estimate from the cost constants file. The
    # estimator is imported lazily so a no-key dry-run does not pull
    # in the cost module.
    total_filter_cost_usd = Decimal("0")
    try:
        from ..cost.estimator import estimate_extraction_cost

        if total_filter_tokens > 0:
            # Apportion the recorded tokens: half input, half output is
            # a conservative blend (output tokens dominate Sonnet cost).
            # Tests assert the result is non-negative and within the
            # 30% cost-estimator tolerance.
            total_filter_cost_usd = estimate_extraction_cost(
                total_filter_tokens * 2,  # bytes ~= tokens * 4; /2 here
                filter_model,
                output_tokens=total_filter_tokens // 2,
                constants_path=cost_constants_path,
            )
    except Exception:  # noqa: BLE001 — cost is diagnostic, never blocks
        total_filter_cost_usd = Decimal("0")

    completed_at = clock()

    filter_metadata = {
        "filter_model": filter_model,
        "filter_prompt_path": prompt_path_used,
        "filter_prompt_content_hash": prompt_hash,
        "items_kept_count": int(items_kept_count),
        "items_dropped_count": int(items_dropped_count),
        "chunks_evaluated": int(chunks_evaluated),
        "chunks_with_invalid_filter_response": int(
            chunks_with_invalid_filter_response
        ),
        "truncation_count": int(truncation_count),
        "filter_started_at": started_at.isoformat(),
        "filter_completed_at": completed_at.isoformat(),
    }

    _ = no_items  # documented short-circuit; metadata is still emitted

    return CascadeFilterResult(
        filtered_items=filtered_items,
        filter_metadata=filter_metadata,
        filter_log_entries=log_entries,
        total_filter_tokens=total_filter_tokens,
        total_filter_cost_usd=total_filter_cost_usd,
    )


# ---------------------------------------------------------------------------
# Writers — produce the on-disk artifacts.
# ---------------------------------------------------------------------------


def _meeting_dir(
    data_lake_path: Path | str, source_id: str, store_root_segment: bool = True
) -> Path:
    """Return the per-meeting directory.

    `store_root_segment` toggles whether `store/` is part of the path.
    The Phase 2 invocation log uses `store/processed/`; the cascade
    writes the FILTERED PRODUCT under the same processed/meetings tree
    the regular meeting_minutes writer uses
    (`processed_meeting_dir`), and writes the LOG diagnostic under
    `store/processed/meetings/<id>/diagnostics/` for parity with
    `pipeline_invocation_log`.
    """
    root = Path(data_lake_path)
    parts = [root]
    if store_root_segment:
        parts.append(Path("store"))
    parts.append(Path("processed") / "meetings" / source_id)
    out = parts[0]
    for p in parts[1:]:
        out = out / p
    return out


def _build_filtered_envelope(
    *,
    result: CascadeFilterResult,
    source_artifact_path: str,
    extraction_config: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    envelope: Dict[str, Any] = {
        "artifact_type": FILTERED_ARTIFACT_TYPE,
        "schema_version": FILTERED_SCHEMA_VERSION,
        "source_artifact_path": source_artifact_path,
        "filter_metadata": dict(result.filter_metadata),
        "filtered_items": {
            k: list(v) for k, v in result.filtered_items.items()
        },
    }
    if extraction_config is not None:
        envelope["extraction_config"] = dict(extraction_config)
    return envelope


def write_filtered_artifact(
    *,
    data_lake_path: Path | str,
    source_id: str,
    source_artifact_path: str,
    result: CascadeFilterResult,
    extraction_config: Optional[Mapping[str, Any]] = None,
    timestamp_suffix: Optional[str] = None,
) -> Tuple[Path, Dict[str, Any]]:
    """Write the `meeting_minutes_filtered` artifact and return its path.

    Validates the envelope against
    `meeting_minutes_filtered.schema.json` before writing — fail-closed
    on any schema violation.

    Filename: `meeting_minutes_filtered__<timestamp>.json` (the `__`
    separator matches the data lake contract for processed product
    artifacts). The timestamp suffix defaults to a UTC ISO seconds
    string with `:` replaced by `-` for filesystem friendliness.

    The filtered product artifact lives under
    `<data_lake_path>/store/processed/meetings/<source_id>/` — the same
    directory the regular `meeting_minutes__*.json` writer uses, which
    is also where `compare_opus_haiku._meeting_dir` looks for both the
    source and the cascade-filtered artifact via `--use-cascade-output`.
    """
    envelope = _build_filtered_envelope(
        result=result,
        source_artifact_path=source_artifact_path,
        extraction_config=extraction_config,
    )
    validate_artifact(envelope, FILTERED_ARTIFACT_TYPE)

    if timestamp_suffix is None:
        timestamp_suffix = (
            datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds")
            .replace(":", "-")
        )

    out_dir = (
        Path(data_lake_path)
        / "store"
        / "processed"
        / "meetings"
        / source_id
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{FILTERED_ARTIFACT_TYPE}__{timestamp_suffix}.json"
    out_path = out_dir / filename
    out_path.write_text(
        json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return out_path, envelope


def write_cascade_filter_log(
    *,
    data_lake_path: Path | str,
    source_id: str,
    source_artifact_path: str,
    filtered_artifact_path: str,
    result: CascadeFilterResult,
    timestamp_suffix: Optional[str] = None,
) -> Tuple[Path, Dict[str, Any]]:
    """Write the `cascade_filter_log` diagnostic and return its path.

    Validates the envelope before writing. The log lives under
    `<data_lake>/store/processed/meetings/<source_id>/diagnostics/`
    — same family as `pipeline_invocation_log__*.json`. Lifecycle:
    never promoted, never indexed, 30-day TTL (the reconciler is shared
    with the invocation log family; see CLAUDE.md).
    """
    if timestamp_suffix is None:
        timestamp_suffix = (
            datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds")
            .replace(":", "-")
        )

    started_at = result.filter_metadata["filter_started_at"]
    completed_at = result.filter_metadata["filter_completed_at"]

    summary = {
        "items_in": int(
            result.filter_metadata["items_kept_count"]
            + result.filter_metadata["items_dropped_count"]
        ),
        "items_kept": int(result.filter_metadata["items_kept_count"]),
        "items_dropped": int(result.filter_metadata["items_dropped_count"]),
        "chunks_evaluated": int(result.filter_metadata["chunks_evaluated"]),
        "chunks_with_invalid_filter_response": int(
            result.filter_metadata["chunks_with_invalid_filter_response"]
        ),
        "truncation_count": int(result.filter_metadata["truncation_count"]),
        "total_filter_tokens": int(result.total_filter_tokens),
        "total_filter_cost_usd": str(result.total_filter_cost_usd),
        "started_at": started_at,
        "completed_at": completed_at,
    }

    decisions_out: List[Dict[str, Any]] = []
    for entry in result.filter_log_entries:
        decisions_out.append(
            {
                "chunk_index": int(entry.chunk_index),
                "item_idx": int(entry.item_idx),
                "extraction_type": str(entry.extraction_type),
                "decision": str(entry.decision),
                "reason": str(entry.reason),
            }
        )

    log_envelope = {
        "artifact_type": CASCADE_FILTER_LOG_ARTIFACT_TYPE,
        "schema_version": CASCADE_FILTER_LOG_SCHEMA_VERSION,
        "source_artifact_path": source_artifact_path,
        "filtered_artifact_path": filtered_artifact_path,
        "summary": summary,
        "decisions": decisions_out,
    }
    validate_artifact(log_envelope, CASCADE_FILTER_LOG_ARTIFACT_TYPE)

    out_dir = (
        Path(data_lake_path)
        / "store"
        / "processed"
        / "meetings"
        / source_id
        / "diagnostics"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"cascade_filter_log__{timestamp_suffix}.json"
    out_path = out_dir / filename
    out_path.write_text(
        json.dumps(log_envelope, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return out_path, log_envelope
