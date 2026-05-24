"""Phase 4.C cascade filter — per-item Sonnet adjudication of grounded items.

Stage 2 of the Phase 4 governance chain:

    raw extraction  →  grounding_gate (4.A)  →  cascade_filter (4.C)

The grounding gate already proved every item's ``source_quote`` is a
verbatim substring of the transcript. The cascade filter is the
precision pass on top of that: Sonnet adjudicates each grounded item
with ``{keep, drop, modify}``. ``drop`` removes a true-typed item that
was over-extracted; ``modify`` is a substring-tightening only — Sonnet
can shrink ``source_quote``/``text`` to a narrower verbatim span but
can never rephrase or add tokens.

The filter is fail-closed:

* a malformed Sonnet response → every item in the batch is DROPPED
  with reason ``SONNET_RESPONSE_INVALID``. We do NOT fall back to
  "keep" — that was the Phase 2.C cascade's mistake (every invalid
  batch leaked false positives back into the output).
* a ``modify`` decision whose ``modified_text`` is not a substring of
  the chunk after :func:`normalize_for_grounding` → that item is
  DROPPED with reason ``MODIFY_BROKE_GROUNDING``. Modify is a
  privilege the filter must EARN per call.
* a batch the operator could not run because ``max_batches`` was hit
  → every remaining item is DROPPED with
  ``MAX_BATCHES_EXCEEDED``. The cost cap is a hard limit; we never
  silently pass an item through because the operator ran out of
  budget.

Per-type disqualifiers come from the prompt at
``workflows/prompts/meeting_minutes_llm.md``: the ``DO NOT EXTRACT``
section is parsed at module load and rendered into the cascade
prompt so Sonnet's adjudication uses the SAME guidance the extraction
model saw. When the 4.B precision-guard section lands it will live in
the same prompt file and be picked up automatically.

This module is pure except for the ``api_client`` callable. Two calls
with byte-identical inputs and a deterministic api_client produce
identical :class:`CascadeResult` instances.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .grounding_gate import CLAIM_SHAPED_TYPES, normalize_for_grounding


# ---------------------------------------------------------------------------
# Constants — single source of truth for the cascade's model + cost cap.
# ---------------------------------------------------------------------------

CASCADE_FILTER_MODEL: str = "claude-sonnet-4-6"
"""The model id stamped onto every cascade artifact. Read this constant;
do NOT re-introduce string literals. Constant-discipline check in
``tests/test_cascade_filter.py`` greps the cascade module for any other
occurrence of the sonnet model name and asserts it lives in this
constant's definition only."""

CASCADE_FILTER_SCHEMA_VERSION: str = "1.0.0"
"""Initial version. Bump on any breaking change to the artifact shape
the script writers in this PR produce
(``cascade_filtered__*.json`` / ``cascade_audit__*.jsonl`` /
``cascade_filter_result__*.json``)."""

CASCADE_BATCH_SIZE: int = 10
"""Items per Sonnet adjudication call. Sized so the response stays
well under Sonnet's max_tokens (each per-item record is ~3 short
lines of JSON; 10 records ≈ 30 lines + framing)."""

CASCADE_MAX_BATCHES_DEFAULT: int = 30
"""Hard cap on Sonnet calls per source. 30 batches × 10 items =
300 items, comfortably above what a single transcript produces
after 4.A grounding. Operator can lower via ``--max-batches`` for
cost sensitivity."""

CASCADE_PROMPT_PATH: Path = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "prompts"
    / "cascade_filter.md"
)
"""Where the cascade prompt template lives. Loaded lazily so tests can
override and so a session that swaps the prompt mid-run picks up the
new content on the next call."""

MEETING_MINUTES_PROMPT_PATH: Path = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "prompts"
    / "meeting_minutes_llm.md"
)
"""Source of per-type disqualifier text. The ``DO NOT EXTRACT`` section
defines the over-extraction patterns Sonnet should also reject."""


class CascadeDecision(str, Enum):
    """Per-item adjudication outcome from Sonnet."""

    KEEP = "keep"
    DROP = "drop"
    MODIFY = "modify"


class CascadeDropReason(str, Enum):
    """Why a specific item ended up dropped.

    ``SONNET_DROP`` is the normal precision-pass drop: Sonnet read
    the item, read the source quote, and concluded it was
    over-extracted. The other three are fail-closed safety drops.
    """

    SONNET_DROP = "sonnet_drop"
    SONNET_RESPONSE_INVALID = "sonnet_response_invalid"
    MODIFY_BROKE_GROUNDING = "modify_broke_grounding"
    MAX_BATCHES_EXCEEDED = "max_batches_exceeded"


@dataclass(frozen=True)
class CascadeItemResult:
    """One item's outcome — kept, dropped, or modified.

    ``final_item`` is None when ``decision`` is DROP; for KEEP it is
    the original item by reference; for MODIFY it is the original
    item with the ``source_quote`` (and ``text`` when applicable)
    tightened to the validated substring. The original is preserved
    on ``original_item`` regardless so the audit log can show both
    forms.
    """

    item_index: int
    extraction_type: str
    decision: CascadeDecision
    reason: str
    original_item: dict[str, Any]
    final_item: dict[str, Any] | None = None


@dataclass
class CascadeResult:
    """Outcome of running the cascade over one grounded artifact."""

    total_items: int
    kept_count: int
    dropped_count: int
    modified_count: int
    batches_used: int
    item_results: list[CascadeItemResult] = field(default_factory=list)
    bypassed: bool = False


# ---------------------------------------------------------------------------
# Prompt loading + per-type guidance parsing.
# ---------------------------------------------------------------------------


def load_cascade_prompt_template(path: Path | None = None) -> str:
    """Read the cascade prompt template from disk on every call."""
    p = path if path is not None else CASCADE_PROMPT_PATH
    return p.read_text(encoding="utf-8")


def load_meeting_minutes_prompt(path: Path | None = None) -> str:
    p = path if path is not None else MEETING_MINUTES_PROMPT_PATH
    return p.read_text(encoding="utf-8")


def parse_type_disqualifiers(
    meeting_minutes_prompt: str,
) -> dict[str, str]:
    """Extract the per-type disqualifier text from the extraction prompt.

    Phase 4.B (PR #247) added a per-type precision guard inside
    ``<!-- PRECISION_GUARD_4B_BEGIN -->`` /
    ``<!-- PRECISION_GUARD_4B_END -->`` markers. Each guard entry
    starts with ``**type** —`` and lists the over-extraction patterns
    Sonnet should drop. The parser uses these directly when present.

    When the markers are absent (legacy prompt, test fixture, or a
    future prompt rewrite), the parser falls back to the
    ``## DO NOT EXTRACT`` section and maps the named subsections to
    the claim-shaped types they reference via
    ``_DONOTEXTRACT_TYPE_KEYWORDS``. Worst case, every type gets a
    generic disqualifier so Sonnet always has SOME guidance.

    Two legacy marker pair names are also accepted for forward
    compatibility — a prompt may use ``PER_TYPE_PRECISION_GUARD_*``
    in place of ``PRECISION_GUARD_4B_*``. The parser tries both.
    """
    # Look for either marker pair — the 4.B-as-shipped names first.
    explicit: dict[str, str] = {}
    for begin, end in (
        ("PRECISION_GUARD_4B_BEGIN", "PRECISION_GUARD_4B_END"),
        ("PER_TYPE_PRECISION_GUARD_BEGIN", "PER_TYPE_PRECISION_GUARD_END"),
    ):
        guard_block = _extract_marker_block(
            meeting_minutes_prompt, begin, end
        )
        if guard_block:
            explicit = _parse_per_type_block(guard_block)
            if explicit:
                break

    # Parse the DO NOT EXTRACT section — used either as the sole source
    # (no 4.B block) or as the fallback for types Phase 4.B does not
    # explicitly enumerate (its 6-type list is intentionally narrower
    # than the cascade's 14 claim-shaped types).
    do_not_extract = _extract_section(
        meeting_minutes_prompt, "## DO NOT EXTRACT"
    )
    generic = (
        "Drop items the speaker is brainstorming, recapping prior "
        "decisions, restating the agenda, or stating conditional / "
        "speculative content. Drop meta-procedural meeting mechanics."
    )
    if not do_not_extract:
        # Worst case: every type gets a generic disqualifier; the
        # explicit 4.B entries (if any) take priority.
        return {t: explicit.get(t, generic) for t in CLAIM_SHAPED_TYPES}

    # Map each claim-shaped type to the most relevant DO NOT EXTRACT
    # subsections. The mapping is conservative: each type gets the
    # WHOLE DO NOT EXTRACT section as fallback context plus a
    # type-specific note when a subsection clearly references it.
    type_specific_notes = _type_specific_notes_from_donotextract(do_not_extract)
    out: dict[str, str] = {}
    for typ in CLAIM_SHAPED_TYPES:
        # Phase 4.B entry, when present, is the most precise signal —
        # it was authored from the actual haiku_only false-positive list.
        if typ in explicit:
            out[typ] = explicit[typ]
            continue
        specific = type_specific_notes.get(typ, "")
        if specific:
            out[typ] = (
                f"{specific}\n\n--- General over-extraction patterns ---\n\n"
                f"{do_not_extract.strip()}"
            )
        else:
            out[typ] = do_not_extract.strip()
    return out


def _extract_marker_block(
    text: str, begin_marker: str, end_marker: str
) -> str:
    """Return the substring between two HTML-comment markers, or ''."""
    pattern = (
        rf"<!--\s*{re.escape(begin_marker)}\s*-->(.*?)"
        rf"<!--\s*{re.escape(end_marker)}\s*-->"
    )
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1).strip() if match else ""


def _parse_per_type_block(block: str) -> dict[str, str]:
    """Parse a per-type precision guard block.

    Two layouts are recognized — both produced by the same Phase 4.B
    contract:

    1. Bold-headed (as shipped in Phase 4.B / PR #247):

           **<extraction_type>** — <one-line summary>
           - bullet 1
           - bullet 2
           Trailing prose ...

    2. Markdown-headed (older drafts):

           ### <extraction_type>
           <disqualifier text>

    Returns a ``{type: text}`` dict for every block whose heading
    matches a known claim-shaped type. Unknown types are ignored so
    the parser is forward-compatible with extra types added later.
    """
    out: dict[str, str] = {}

    # Bold-headed pattern from Phase 4.B. Each entry begins on its
    # own line with ``**type** —`` (em-dash) and runs until the next
    # ``**type**`` or end of block.
    bold_pattern = re.compile(
        r"(?ms)^\*\*([a-z_]+)\*\*\s*[—\-–]\s*(.*?)(?=^\*\*[a-z_]+\*\*\s*[—\-–]|\Z)"
    )
    for match in bold_pattern.finditer(block):
        heading = match.group(1).strip().strip("`")
        body = match.group(2).strip()
        if heading in CLAIM_SHAPED_TYPES and body:
            # Re-attach the em-dash header for the rendered prompt so
            # the model sees the same one-liner intro Phase 4.B wrote.
            out[heading] = f"— {body}"

    if out:
        return out

    # Fallback: ``###`` heading-based block (older drafts, test
    # fixtures).
    parts = re.split(r"(?m)^###\s+", block)
    for part in parts[1:]:
        lines = part.splitlines()
        if not lines:
            continue
        heading = lines[0].strip().strip("`")
        body = "\n".join(lines[1:]).strip()
        if heading in CLAIM_SHAPED_TYPES and body:
            out[heading] = body
    return out


def _extract_section(text: str, heading: str) -> str:
    """Return the body of a Markdown section by its heading line.

    Stops at the next heading at the same or higher level.
    """
    lines = text.splitlines()
    out: list[str] = []
    in_section = False
    heading_level = heading.count("#", 0, heading.find(" "))
    for line in lines:
        stripped = line.lstrip()
        if not in_section:
            if line.strip() == heading.strip():
                in_section = True
            continue
        # End of section: another heading at same or higher level.
        if stripped.startswith("#"):
            new_level = len(stripped) - len(stripped.lstrip("#"))
            if 0 < new_level <= heading_level:
                break
        out.append(line)
    return "\n".join(out).strip()


_DONOTEXTRACT_TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "decisions": (
        "Brainstorming",
        "Recapping prior decisions",
        "Conditional or speculative",
        "Repeated mentions",
    ),
    "action_items": (
        "Restating the agenda",
        "Meta-procedural",
        "Brainstorming",
        "Repeated mentions",
    ),
    "open_questions": (
        "Conditional or speculative",
        "Brainstorming",
    ),
    "commitments": (
        "Brainstorming",
        "Conditional or speculative",
        "Repeated mentions",
    ),
    "claims": (
        "Banding / numeric mentions",
        "Repeated mentions",
        "Brainstorming",
    ),
    "risks": ("Conditional or speculative", "Brainstorming"),
    "cross_references": ("Repeated mentions",),
    "regulatory_references": (
        "Banding / numeric mentions",
        "Repeated mentions",
    ),
    "issue_registry_entry": ("Repeated mentions", "Recapping prior decisions"),
    "position_statement": ("Quotes attributed to absent third parties",),
    "dissent_or_objection": ("Brainstorming", "Conditional or speculative"),
    "precedent_reference": ("Recapping prior decisions",),
    "external_stakeholder_input": (
        "Quotes attributed to absent third parties",
    ),
    "procedural_ruling": ("Meta-procedural",),
}


def _type_specific_notes_from_donotextract(donotextract_body: str) -> dict[str, str]:
    """Build a {type: specific_note} mapping from the DO NOT EXTRACT body.

    For each claim-shaped type, walk the keyword list and concatenate
    the matching ``### Subsection`` paragraphs from the body. Returns
    only types that matched at least one keyword.
    """
    subsections = _split_subsections(donotextract_body)
    out: dict[str, str] = {}
    for typ, keywords in _DONOTEXTRACT_TYPE_KEYWORDS.items():
        matched: list[str] = []
        for kw in keywords:
            for subsection_title, subsection_body in subsections.items():
                if kw in subsection_title:
                    matched.append(
                        f"**{subsection_title}**: {subsection_body}".strip()
                    )
        if matched:
            out[typ] = "\n\n".join(matched)
    return out


def _split_subsections(body: str) -> dict[str, str]:
    """Return {subsection_title: subsection_body} for ``### Title``-led parts."""
    parts = re.split(r"(?m)^###\s+", body)
    out: dict[str, str] = {}
    for part in parts[1:]:
        lines = part.splitlines()
        if not lines:
            continue
        title = lines[0].strip()
        body_text = "\n".join(lines[1:]).strip()
        out[title] = body_text
    return out


# Schema descriptions for the claim-shaped types. Loaded from the
# meeting_minutes schema at runtime so the cascade prompt picks up
# any future schema update automatically.

def load_type_definitions(
    schema_path: Path | None = None,
) -> dict[str, str]:
    """Load per-type definition strings from the meeting_minutes schema.

    Returns a ``{extraction_type: short_definition}`` dict. The
    definition is the type's ``description`` field on its array
    property in ``meeting_minutes.schema.json``; when absent, a
    plain-text fallback is used so the prompt always has SOMETHING.
    """
    if schema_path is None:
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "schemas"
            / "meeting_minutes.schema.json"
        )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    properties = schema.get("properties", {})
    out: dict[str, str] = {}
    for typ in CLAIM_SHAPED_TYPES:
        prop = properties.get(typ, {})
        desc = prop.get("description") or _FALLBACK_TYPE_DEFINITIONS.get(typ)
        if desc:
            # Keep the prompt readable: collapse run-on schema docstring
            # whitespace and truncate to the first ~250 chars.
            cleaned = " ".join(str(desc).split())
            out[typ] = cleaned[:250]
        else:
            out[typ] = typ.replace("_", " ")
    return out


_FALLBACK_TYPE_DEFINITIONS: dict[str, str] = {
    "decisions": "A binding choice the group made or ratified in this meeting.",
    "action_items": "A specific task assigned to a specific owner with a clear deliverable.",
    "open_questions": "An unresolved question explicitly flagged for follow-up.",
    "commitments": "A speaker's stated intent to do something, without a specific deadline.",
    "claims": "A factual or evaluative statement the speaker is asserting.",
    "risks": "A potential adverse outcome the speaker is flagging.",
    "cross_references": "A reference to another meeting, document, or external work product.",
    "regulatory_references": "A reference to a specific regulation, statute, or standard.",
    "issue_registry_entry": "A problem that needs resolution; explicitly named as unresolved.",
    "position_statement": "A speaker's stated position on a contested topic.",
    "dissent_or_objection": "A speaker's explicit objection to a proposed direction.",
    "precedent_reference": "A reference to what was decided in a prior meeting.",
    "external_stakeholder_input": "Input from a stakeholder not in this meeting.",
    "procedural_ruling": "A ruling on how the group will operate procedurally.",
}


# ---------------------------------------------------------------------------
# Prompt rendering.
# ---------------------------------------------------------------------------


def render_prompt(
    template: str,
    *,
    type_definitions: Mapping[str, str],
    type_disqualifiers: Mapping[str, str],
    items: Sequence[Mapping[str, Any]],
) -> str:
    """Substitute the three template placeholders.

    Uses literal substitution (not str.format) because the template
    body contains ``{`` and ``}`` characters in its JSON examples.
    """
    type_defs_rendered = "\n".join(
        f"- `{typ}`: {type_definitions.get(typ, '(no definition)')}"
        for typ in sorted(type_definitions)
    )
    type_disq_rendered = "\n\n".join(
        f"### `{typ}`\n{type_disqualifiers.get(typ, '(no per-type guidance)')}"
        for typ in sorted(type_disqualifiers)
    )
    items_rendered = json.dumps(list(items), sort_keys=True, indent=2)
    body = template.replace("{type_definitions}", type_defs_rendered)
    body = body.replace("{type_disqualifiers}", type_disq_rendered)
    body = body.replace("{items_json}", items_rendered)
    return body


# ---------------------------------------------------------------------------
# Response parsing.
# ---------------------------------------------------------------------------


def _parse_sonnet_response(
    raw: str, expected_count: int
) -> tuple[bool, str, list[dict[str, Any]]]:
    """Validate Sonnet's batch response shape.

    Expected: a JSON array of ``expected_count`` objects, each with
    ``item_index``, ``decision``, ``reason``, and (when decision is
    ``modify``) ``modified_text``. Indices must form a permutation of
    ``range(expected_count)`` — duplicates and missing entries both
    fail. Returns ``(ok, message, decisions)``; on failure ``decisions``
    is empty.
    """
    # Tolerate models that wrap the array in ```json ... ``` fences.
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        fence_match = re.match(
            r"^```(?:json)?\s*\n(.*?)\n```$", cleaned, re.DOTALL
        )
        if fence_match:
            cleaned = fence_match.group(1).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return False, f"json_decode_error: {exc.msg}", []

    if not isinstance(parsed, list):
        return False, "response is not a JSON array", []
    if len(parsed) != expected_count:
        return (
            False,
            f"expected {expected_count} entries, got {len(parsed)}",
            [],
        )

    seen: set[int] = set()
    decisions: list[dict[str, Any]] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            return False, "array entry is not an object", []
        item_index = entry.get("item_index")
        decision = entry.get("decision")
        reason = entry.get("reason")
        if not isinstance(item_index, int):
            return False, "item_index missing or not an int", []
        if item_index in seen:
            return False, f"duplicate item_index {item_index}", []
        if item_index < 0 or item_index >= expected_count:
            return (
                False,
                f"item_index {item_index} out of range",
                [],
            )
        if decision not in {"keep", "drop", "modify"}:
            return (
                False,
                f"decision {decision!r} not in keep/drop/modify",
                [],
            )
        if not isinstance(reason, str) or not reason.strip():
            return False, "reason missing or empty", []
        if decision == "modify":
            modified_text = entry.get("modified_text")
            if not isinstance(modified_text, str) or not modified_text.strip():
                return (
                    False,
                    "modify decision missing non-empty modified_text",
                    [],
                )
        seen.add(item_index)
        decisions.append(entry)

    decisions.sort(key=lambda e: e["item_index"])
    return True, "", decisions


# ---------------------------------------------------------------------------
# Modify re-grounding check.
# ---------------------------------------------------------------------------


def _modified_text_grounds(
    modified_text: str, chunk_text: str
) -> bool:
    """Return True iff modified_text is a substring of chunk_text after
    grounding normalization. Mirrors the Phase 4.A gate's check so a
    modify is held to the same trust property as the original quote.
    """
    if not modified_text or not chunk_text:
        return False
    return normalize_for_grounding(modified_text) in normalize_for_grounding(
        chunk_text
    )


# ---------------------------------------------------------------------------
# The single execution path.
# ---------------------------------------------------------------------------


# Type alias for the api_client callable. It receives the rendered
# prompt as ``user`` and returns the raw Sonnet response text. Tests
# inject deterministic stubs; production wires the Anthropic SDK.
ApiClient = Callable[..., str]


def filter_items(
    grounded_items_by_type: Mapping[str, list[Mapping[str, Any]]],
    chunks_by_id: Mapping[str, str],
    type_definitions: Mapping[str, str],
    type_disqualifiers: Mapping[str, str],
    *,
    api_client: ApiClient | None = None,
    prompt_template: str | None = None,
    max_batches: int = CASCADE_MAX_BATCHES_DEFAULT,
    batch_size: int = CASCADE_BATCH_SIZE,
    disable_cascade: bool = False,
    model: str = CASCADE_FILTER_MODEL,
) -> CascadeResult:
    """Run the cascade over grounded items and return a CascadeResult.

    Args:
        grounded_items_by_type: the ``payload`` block from a
            ``grounded_items__<run_id>.json`` artifact, filtered to the
            14 claim-shaped types. Items at other keys are ignored.
        chunks_by_id: chunk text indexed by ``source_chunk_id``. Used
            to re-validate a ``modify`` decision's ``modified_text``.
            When an item omits ``source_chunk_id`` the chunk-keyed
            lookup falls back to the full transcript if a key
            ``"__full_transcript__"`` is present.
        type_definitions / type_disqualifiers: rendered into the prompt
            so Sonnet sees the SAME guidance the extractor saw.
        api_client: callable invoked once per batch. Signature is
            ``api_client(prompt: str, model: str) -> str``. Required
            unless ``disable_cascade=True``.
        prompt_template: optional override of the prompt template.
            Defaults to the on-disk template at
            :data:`CASCADE_PROMPT_PATH`.
        max_batches: hard cap. Items in batches beyond the cap are
            DROPPED with reason ``MAX_BATCHES_EXCEEDED`` so the cost
            cap never silently passes items through.
        batch_size: items per Sonnet call.
        disable_cascade: when True, every item is kept as a
            pass-through. ``CascadeResult.bypassed`` is set to True so
            the caller can write the bypass record. The api_client is
            NEVER called on this path.
        model: stamped onto Sonnet calls as the ``model`` kwarg. Tests
            pass an alternate id to verify the constant flows through.

    Returns: a fully-populated :class:`CascadeResult`. Never raises
    on per-batch failures — those manifest as drops in the audit log.
    """
    if prompt_template is None:
        prompt_template = load_cascade_prompt_template()

    flat: list[tuple[str, int, Mapping[str, Any]]] = []
    for typ in sorted(CLAIM_SHAPED_TYPES):
        items = grounded_items_by_type.get(typ) or []
        if not isinstance(items, list):
            continue
        for idx, item in enumerate(items):
            if isinstance(item, Mapping):
                flat.append((typ, idx, item))

    total_items = len(flat)

    if disable_cascade:
        # Bypass path: every item passes through as KEEP. No api_client
        # call is made so this works without an API key.
        item_results: list[CascadeItemResult] = []
        for global_idx, (typ, _orig_idx, item) in enumerate(flat):
            item_results.append(
                CascadeItemResult(
                    item_index=global_idx,
                    extraction_type=typ,
                    decision=CascadeDecision.KEEP,
                    reason="cascade_bypassed",
                    original_item=dict(item),
                    final_item=dict(item),
                )
            )
        return CascadeResult(
            total_items=total_items,
            kept_count=total_items,
            dropped_count=0,
            modified_count=0,
            batches_used=0,
            item_results=item_results,
            bypassed=True,
        )

    if api_client is None:
        raise ValueError(
            "api_client is required when disable_cascade=False"
        )

    item_results = []
    batches_used = 0
    kept = 0
    dropped = 0
    modified = 0

    for batch_start in range(0, total_items, batch_size):
        batch = flat[batch_start : batch_start + batch_size]
        if batches_used >= max_batches:
            # Cost cap reached. Every remaining item — including this
            # batch and all subsequent batches — is dropped with the
            # documented reason. Fail-closed: we never silently keep.
            for local_idx, (typ, _orig_idx, item) in enumerate(batch):
                item_results.append(
                    CascadeItemResult(
                        item_index=batch_start + local_idx,
                        extraction_type=typ,
                        decision=CascadeDecision.DROP,
                        reason=CascadeDropReason.MAX_BATCHES_EXCEEDED.value,
                        original_item=dict(item),
                        final_item=None,
                    )
                )
                dropped += 1
            continue

        # Build the prompt input items. Each entry carries the global
        # item index (the cascade's identifier), the extraction type,
        # the source_quote (already grounded by 4.A), and the full
        # item payload so Sonnet can read the text + grounding context.
        prompt_items: list[dict[str, Any]] = []
        for local_idx, (typ, _orig_idx, item) in enumerate(batch):
            prompt_items.append(
                {
                    "item_index": local_idx,
                    "extraction_type": typ,
                    "source_quote": item.get("source_quote", ""),
                    "source_chunk_id": item.get("source_chunk_id"),
                    "item": dict(item),
                }
            )

        rendered_prompt = render_prompt(
            prompt_template,
            type_definitions=type_definitions,
            type_disqualifiers=type_disqualifiers,
            items=prompt_items,
        )
        batches_used += 1
        try:
            raw_response = api_client(prompt=rendered_prompt, model=model)
        except TypeError:
            # Tolerate api_clients that take only one positional arg.
            raw_response = api_client(rendered_prompt)

        ok, message, decisions = _parse_sonnet_response(
            raw_response, expected_count=len(batch)
        )
        if not ok:
            # Fail-closed: every item in this batch is DROPPED, NOT
            # kept. The Phase 2.C cascade's mistake was the opposite —
            # an invalid response leaked false positives through.
            for local_idx, (typ, _orig_idx, item) in enumerate(batch):
                item_results.append(
                    CascadeItemResult(
                        item_index=batch_start + local_idx,
                        extraction_type=typ,
                        decision=CascadeDecision.DROP,
                        reason=(
                            f"{CascadeDropReason.SONNET_RESPONSE_INVALID.value}"
                            f": {message}"
                        ),
                        original_item=dict(item),
                        final_item=None,
                    )
                )
                dropped += 1
            continue

        for entry in decisions:
            local_idx = entry["item_index"]
            typ, _orig_idx, item = batch[local_idx]
            decision_str = entry["decision"]
            reason = entry["reason"]

            if decision_str == "keep":
                item_results.append(
                    CascadeItemResult(
                        item_index=batch_start + local_idx,
                        extraction_type=typ,
                        decision=CascadeDecision.KEEP,
                        reason=reason,
                        original_item=dict(item),
                        final_item=dict(item),
                    )
                )
                kept += 1
            elif decision_str == "drop":
                item_results.append(
                    CascadeItemResult(
                        item_index=batch_start + local_idx,
                        extraction_type=typ,
                        decision=CascadeDecision.DROP,
                        reason=f"{CascadeDropReason.SONNET_DROP.value}: {reason}",
                        original_item=dict(item),
                        final_item=None,
                    )
                )
                dropped += 1
            else:  # modify
                modified_text = entry["modified_text"]
                # Pull the chunk text the original item was grounded
                # against. When source_chunk_id is unknown, fall back
                # to the full transcript marker so the modify path is
                # still gated.
                chunk_id = item.get("source_chunk_id")
                chunk_text = ""
                if isinstance(chunk_id, str) and chunk_id in chunks_by_id:
                    chunk_text = chunks_by_id[chunk_id]
                elif "__full_transcript__" in chunks_by_id:
                    chunk_text = chunks_by_id["__full_transcript__"]
                if _modified_text_grounds(modified_text, chunk_text):
                    new_item = dict(item)
                    new_item["source_quote"] = modified_text
                    # When the item carries a text field that originally
                    # mirrored source_quote, the modify shrinks it too.
                    if isinstance(new_item.get("text"), str) and new_item[
                        "text"
                    ] == item.get("source_quote"):
                        new_item["text"] = modified_text
                    item_results.append(
                        CascadeItemResult(
                            item_index=batch_start + local_idx,
                            extraction_type=typ,
                            decision=CascadeDecision.MODIFY,
                            reason=reason,
                            original_item=dict(item),
                            final_item=new_item,
                        )
                    )
                    modified += 1
                else:
                    # Modify broke the grounding property — drop.
                    item_results.append(
                        CascadeItemResult(
                            item_index=batch_start + local_idx,
                            extraction_type=typ,
                            decision=CascadeDecision.DROP,
                            reason=(
                                f"{CascadeDropReason.MODIFY_BROKE_GROUNDING.value}"
                                f": modified_text {modified_text!r} is not a "
                                f"substring of the source chunk"
                            ),
                            original_item=dict(item),
                            final_item=None,
                        )
                    )
                    dropped += 1

    return CascadeResult(
        total_items=total_items,
        kept_count=kept,
        dropped_count=dropped,
        modified_count=modified,
        batches_used=batches_used,
        item_results=item_results,
        bypassed=False,
    )


# ---------------------------------------------------------------------------
# Result → artifact payloads.
# ---------------------------------------------------------------------------


def cascade_filtered_payload(
    grounded_envelope: Mapping[str, Any],
    result: CascadeResult,
) -> dict[str, Any]:
    """Build the ``cascade_filtered__<run_id>.json`` payload.

    Carries only KEEP and MODIFY items in their original payload
    arrays, in their original extraction-type bucket. Non-claim-shaped
    keys in the grounded payload pass through unchanged so the
    downstream comparison can read the artifact as a meeting_minutes
    payload.
    """
    grounded_payload = grounded_envelope.get("payload", {}) or {}
    out_payload: dict[str, Any] = {}
    # Pass through non-claim-shaped keys.
    for k, v in grounded_payload.items():
        if k not in CLAIM_SHAPED_TYPES:
            out_payload[k] = v
        else:
            out_payload[k] = []
    # Re-bucket kept + modified items by extraction_type, preserving
    # the order they appeared in item_results (already global-index
    # sorted by construction).
    for r in result.item_results:
        if r.decision in (CascadeDecision.KEEP, CascadeDecision.MODIFY):
            out_payload.setdefault(r.extraction_type, []).append(
                r.final_item if r.final_item is not None else r.original_item
            )

    return {
        "artifact_type": "cascade_filtered",
        "schema_version": CASCADE_FILTER_SCHEMA_VERSION,
        "source_id": grounded_envelope.get("source_id"),
        "run_id": grounded_envelope.get("run_id"),
        "source_grounded_artifact": grounded_envelope.get(
            "source_extraction_artifact"
        ),
        "filter_model": CASCADE_FILTER_MODEL,
        "bypassed": result.bypassed,
        "payload": out_payload,
    }


def cascade_audit_records(result: CascadeResult) -> list[dict[str, Any]]:
    """Build the JSONL audit records — one per cascade item result."""
    out: list[dict[str, Any]] = []
    for r in result.item_results:
        record: dict[str, Any] = {
            "item_index": r.item_index,
            "extraction_type": r.extraction_type,
            "decision": r.decision.value,
            "reason": r.reason,
            "original_item": r.original_item,
        }
        if r.final_item is not None and r.final_item != r.original_item:
            record["final_item"] = r.final_item
        out.append(record)
    return out


def cascade_filter_result_payload(
    result: CascadeResult,
    *,
    source_id: str,
    run_id: str,
    grounded_artifact_path: str | None = None,
) -> dict[str, Any]:
    """Build the ``cascade_filter_result__<run_id>.json`` summary."""
    drop_rate = (
        result.dropped_count / result.total_items if result.total_items else 0.0
    )
    return {
        "artifact_type": "cascade_filter_result",
        "schema_version": CASCADE_FILTER_SCHEMA_VERSION,
        "source_id": source_id,
        "run_id": run_id,
        "filter_model": CASCADE_FILTER_MODEL,
        "grounded_artifact_path": grounded_artifact_path,
        "total_items": result.total_items,
        "kept_count": result.kept_count,
        "dropped_count": result.dropped_count,
        "modified_count": result.modified_count,
        "batches_used": result.batches_used,
        "cascade_drop_rate": drop_rate,
        "bypassed": result.bypassed,
    }


def cascade_bypass_record(
    *,
    source_id: str,
    run_id: str,
    grounded_artifact_path: str,
    operator: str,
    timestamp: str,
    reason: str = "operator override via --disable-cascade",
) -> dict[str, Any]:
    return {
        "artifact_type": "cascade_bypass_record",
        "schema_version": CASCADE_FILTER_SCHEMA_VERSION,
        "source_id": source_id,
        "run_id": run_id,
        "grounded_artifact_path": grounded_artifact_path,
        "operator": operator or "unknown",
        "timestamp": timestamp,
        "reason": reason,
        "filter_model": CASCADE_FILTER_MODEL,
    }
