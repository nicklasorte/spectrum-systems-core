#!/usr/bin/env python3
"""Produce Opus *reference baseline* JSONL files from raw transcripts.

This is a reusable, triggerable workflow step — not a one-shot script.
For every raw transcript ``.docx`` in the data-lake it runs an Opus
model against the SAME canonical extraction system prompt the Haiku
pipeline uses (``workflows/prompts/meeting_minutes_llm.md``) and writes
one ``reference_baselines/opus_reference_minutes.jsonl`` per source.

Why a reference baseline (not ground truth, not a product artifact):
the lines are model-authored, unverified, and explicitly
``status: "reference_only"``. They exist so a human (or a future
eval) can compare the Haiku pipeline against a stronger model's read
of the same transcript. They are NEVER promoted, NEVER read back into
the governed loop, and NEVER mixed with product artifacts.

Fail-closed contract (every gate halts the run; nothing partial is
written):

* The canonical prompt file cannot be read  -> ``missing_extraction_prompt``
  (checked BEFORE any model call — a missing prompt can never reach the
  transport).
* ``source_record.json`` missing -> the deterministic, LLM-free
  Stage-1 ingestion (the EXACT pipeline functions
  ``_stage_transcript_into_meetings`` + ``SourceLoader.load``, never
  reimplemented) is run in-process to produce it, so the canonical
  source UUID this baseline needs is satisfied as a precondition
  instead of being an unstated external requirement. This is NOT a
  weakening of the gate: ingestion is deterministic and LLM-free, a
  present source_record is left untouched, and if ingestion cannot
  produce a record the run HALTs ``source_record_ingest_failed``.
  A source_record that is *present* but unreadable JSON, not a JSON
  object, or lacking a valid-UUID ``artifact_id`` still ->
  ``invalid_source_record`` (never silently re-ingested over). The
  UUID is never inferred. ``source_id`` is taken from ``--source-id``
  / the transcript slug and is deliberately NOT required on the
  record.
* The model transport fails -> ``llm_transport_error`` (no fallback to a
  weaker model, no partial file).
* The model returns non-JSON / a non-object / a content key whose value
  is not a list -> ``malformed_llm_response`` (the whole transcript's
  file is never written — no partial JSONL). Individual items are NEVER
  a halt: the canonical prompt lets the model return either a plain
  string OR a structured object for every type (``decisions`` items in
  particular arrive as ``{"text","verb","stakeholders","confidence",
  "rationale"}``), so ``extract_ground_truth_text`` tolerantly reads the
  best text field from any dict (priority field list, then the first
  string value, then ``str(item)``) and the full original item is kept
  verbatim in ``item_data`` so nothing is lost. Before the response-level
  halt the parser tries, in order: markdown-fence stripping, truncation
  back to the
  last balanced ``}`` (recovers a valid object followed by trailing
  prose/markdown), then ONE simplified "JSON only" retry seeded with
  just the first 200 chars of the failed response. Only when all three
  fail does the run halt — it never silently emits an empty extraction.
  A long transcript can need several thousand output tokens, so this
  workflow sets ``max_tokens`` explicitly (8192) to keep a valid
  response from truncating into invalid JSON in the first place.
* After writing, the artifact is shadowed by a ``.gitignore`` rule in
  the data-lake clone -> ``gitignore_blocks_artifact`` (we refuse to
  leave behind an artifact that can never be committed).

Determinism that matters here: ``pair_id`` is a UUID5 over
``opus-ref-{source_id}-{extraction_type}-{index}`` so two runs over the
same transcript text assign the same ids to the same items. The model
content itself is not guaranteed identical run-to-run (that is exactly
why ``--skip-existing`` is the default — a reference baseline is
written once and only regenerated deliberately).

The model string is NEVER hardcoded here. It arrives via ``--model``
(the workflow resolves it from ``ai/registry/model_registry.json`` at
run time and passes it through, and stamps it into every JSONL line as
``model_id`` so a past artifact keeps its exact model even after the
registry is rolled forward).

Test seam: ``create_baselines`` accepts an injected ``client`` callable
``(*, system: str, user: str) -> str`` — the SAME structural seam
``workflows/llm_client.py`` defines — so the suite runs with no API
key and no network.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Reused pipeline functions — NEVER reimplemented here. A drift in any
# of these would silently desync this workflow from the Haiku pipeline.
from spectrum_systems_core.ingestion.docx_extractor import (  # noqa: E402
    DocxExtractor,
)
from spectrum_systems_core.ingestion.source_loader import (  # noqa: E402
    SourceLoader,
)
from spectrum_systems_core.orchestration.pipeline_orchestrator import (  # noqa: E402
    _slugify,
    _stage_transcript_into_meetings,
)
from spectrum_systems_core.workflows import (  # noqa: E402
    meeting_minutes_llm as _mm_llm,
)
from spectrum_systems_core.workflows.llm_client import (  # noqa: E402
    AnthropicJSONClient,
    LLMClientError,
)

PRODUCED_BY = "opus_reference_baseline_workflow"

# Offline / test transport seam. When set, its value is returned
# verbatim as the model response instead of constructing the real
# Anthropic client — the SAME env-var seam pattern
# ``create_human_gt_pairs.py`` uses (``CREATE_HUMAN_GT_PAIRS_STUB_RESPONSE``)
# so the integration contract test can drive the full subprocess write
# path with no API key. It only activates when explicitly set, so it
# can never silently shadow a production run. It is a transport stub,
# NOT a model-string override — ``--model`` is still required and is
# what gets stamped into every line.
_STUB_ENV = "OPUS_REFERENCE_BASELINE_STUB_RESPONSE"

# Arbitrary but FROZEN namespace. Freezing it is the determinism
# contract: re-running over the same transcript must reproduce the same
# pair_ids. Changing this constant re-keys every future baseline, so it
# must never change once shipped.
_OPUS_REF_NAMESPACE = uuid.UUID("3f1c9d8e-2b6a-5c7d-9e0f-1a2b3c4d5e6f")

_RAW_TRANSCRIPTS_SUBPATH = ("store", "raw", "transcripts")
_OUTPUT_SUBDIR = "reference_baselines"
_OUTPUT_FILENAME = "opus_reference_minutes.jsonl"

# A full structured extraction of all ~15 types over a long (40k+ char)
# transcript can need 6-8k output tokens; the shared AnthropicJSONClient
# default (4000) silently truncates such a response into invalid JSON
# (the observed malformed_llm_response root cause). Opus reference
# baselines are not on the byte-determinism path, so a generous bound is
# safe here and is set explicitly rather than inherited. 16384 gives
# headroom for the ~35% tokenizer increase in the current Opus revision
# (41,454 char transcript × 35% overhead × 13+ extraction types).
_OPUS_MAX_TOKENS = 16384

# The canonical extraction prompt — resolved through the pipeline module
# so the path is the SINGLE source of truth. We do not re-derive or
# duplicate the prompt text; the file is read at run time.
_PROMPT_PATH: Path = _mm_llm._PROMPT_PATH

# Schema that defines every extraction type (PR #123). Extraction types
# are derived from THIS file's array properties so a schema change is
# the single place that adds a type — no parallel list to drift.
_MEETING_MINUTES_SCHEMA = (
    Path(_mm_llm.__file__).resolve().parents[1]
    / "schemas"
    / "meeting_minutes.schema.json"
)

# Declares every extraction type this workflow knows about. It no
# longer drives text extraction — ``extract_ground_truth_text`` reads
# the best text field tolerantly from any item shape — but it is still
# load-bearing for two reasons that must NOT regress:
#   1. ``extraction_types()`` raises ``unmapped_extraction_type`` for any
#      schema array property absent from this map, so a new schema type
#      can never be silently skipped.
#   2. ``compare_opus_haiku.py`` mirrors this map and
#      ``test_compare_opus_haiku.py`` asserts the two stay byte-equal —
#      changing a value here without mirroring it there breaks that
#      cross-script contract.
# ``None`` historically meant "plain string array"; the value is now
# only consulted by the two invariants above, never to gate an item.
_PRIMARY_TEXT_FIELD: Dict[str, Optional[str]] = {
    "decisions": None,
    "action_items": None,
    "open_questions": None,
    "commitments": "commitment_text",
    "risks": "risk_text",
    "cross_references": "ref_text",
    "attendees": "name",
    "topics": "title",
    "regulatory_references": "reference_text",
    "technical_parameters": "value",
    "named_artifacts": "name",
    "scheduled_events": "title",
    # 1.2.0 additions. Each maps to a schema-*required*, reliably
    # non-empty string so a schema-valid item never HALTs the baseline:
    # claim_text / phase_name carry minLength or enum constraints, and
    # text_preview is required with minLength 1.
    "claims": "claim_text",
    "sentiment_indicators": "text_preview",
    "meeting_phases": "phase_name",
    # 1.3.0 additions (eight new cross-meeting arrays). Same rule:
    # every value is a schema-*required* string with minLength 1, so
    # extraction_types() never raises unmapped_extraction_type and a
    # schema-valid item never HALTs the baseline (asserted in
    # tests/test_meeting_minutes_schema.py).
    "issue_registry_entry": "title",
    "position_statement": "position_text",
    "dissent_or_objection": "objection_text",
    "agenda_item": "title",
    "precedent_reference": "reference_text",
    "external_stakeholder_input": "input_text",
    "glossary_definition": "term",
    "procedural_ruling": "ruling_text",
}

# Retained only so ``compare_opus_haiku.py`` can mirror it and
# ``test_compare_opus_haiku.py`` can assert the two scripts stay in
# sync. ``extract_ground_truth_text`` no longer consults this map —
# structured items are read tolerantly via the priority field list.
_LEGACY_OBJECT_TEXT_FIELD: Dict[str, str] = {
    "action_items": "action",
    "open_questions": "question_text",
}

# Header date patterns, tried in order. First hit wins. Returns an
# ISO YYYY-MM-DD string. ``meeting_date`` is null when no header date is
# present — we never infer one from the slug or the clock.
_HEADER_SCAN_LINES = 15
_DATE_PATTERNS = (
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
    re.compile(r"\b(\d{4})/(\d{2})/(\d{2})\b"),
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)"),
)
_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November",
            "December",
        ],
        start=1,
    )
}
_MONTH_NAME_RE = re.compile(
    r"\b([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})\b"
)


class ReferenceBaselineError(RuntimeError):
    """A fail-closed halt. ``reason`` is a stable machine code."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _now_utc_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def load_extraction_prompt() -> str:
    """Read the canonical system prompt. Halt before any model call.

    This is the single source of truth shared with the Haiku pipeline.
    A read failure raises ``missing_extraction_prompt`` so a missing
    prompt can never reach the transport with a silent fallback.
    """
    try:
        text = _PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReferenceBaselineError(
            "missing_extraction_prompt",
            f"cannot read canonical extraction prompt at {_PROMPT_PATH}: "
            f"{exc}",
        ) from exc
    if not text.strip():
        raise ReferenceBaselineError(
            "missing_extraction_prompt",
            f"canonical extraction prompt at {_PROMPT_PATH} is empty",
        )
    return text


def extraction_types() -> List[str]:
    """The extraction types, derived from the meeting_minutes schema.

    Every array property except ``grounding`` (Phase Y meta, not an
    extracted content category). A schema array that has no entry in
    ``_PRIMARY_TEXT_FIELD`` is a hard error: it means a new type was
    added to the schema without teaching this workflow how to read its
    text, and we refuse to silently skip it.
    """
    try:
        schema = json.loads(
            _MEETING_MINUTES_SCHEMA.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ReferenceBaselineError(
            "missing_extraction_schema",
            f"cannot read meeting_minutes schema at "
            f"{_MEETING_MINUTES_SCHEMA}: {exc}",
        ) from exc
    props = schema.get("properties", {})
    types: List[str] = []
    for key, spec in props.items():
        if key == "grounding":
            continue
        if isinstance(spec, dict) and spec.get("type") == "array":
            if key not in _PRIMARY_TEXT_FIELD:
                raise ReferenceBaselineError(
                    "unmapped_extraction_type",
                    f"schema array property {key!r} has no "
                    f"_PRIMARY_TEXT_FIELD mapping — add one before "
                    f"running so the type is not silently skipped",
                )
            types.append(key)
    return types


def _ensure_source_record(
    data_lake: Path, source_id: str, transcript_text: str
) -> None:
    """Make the canonical ingestion record exist before resolution.

    The Opus reference baseline is independent of the Haiku *extraction*
    pipeline, but it still needs the stable transcript UUID the
    *ingestion* stage stamps into ``source_record.json``. When the
    data-lake holds the raw transcript but no source_record yet (the
    transcript was never run through the pipeline — the exact
    ``missing_source_record`` halt that made this workflow fail on every
    run before any Opus call), run ONLY the deterministic, LLM-free
    Stage-1 ingestion so the precondition is satisfied in-process.

    The pipeline functions are REUSED verbatim, never reimplemented:
    ``_stage_transcript_into_meetings`` (raw transcript text ->
    ``raw/meetings/<sid>/source.txt`` + ``metadata.json``) then
    ``SourceLoader.load`` (-> ``processed/meetings/<sid>/
    source_record.json`` with a fresh valid-UUID ``artifact_id``).
    Neither calls a model, so the Opus baseline stays independent of
    the Haiku extraction path.

    Fail-closed is preserved, not weakened:

    * An already-present source_record is left UNTOUCHED — a present but
      corrupt record must still trip ``invalid_source_record`` in
      :func:`_resolve_source_artifact_id`, never be silently
      overwritten.
    * If staging or ingestion fails, the run HALTs
      ``source_record_ingest_failed`` (no partial output) and the
      unchanged resolver still HALTs on a missing/invalid UUID.
    """
    sr_path = (
        data_lake
        / "store"
        / "processed"
        / "meetings"
        / source_id
        / "source_record.json"
    )
    if sr_path.is_file():
        return  # present -> resolver/validator owns it (gate unchanged)

    store_root = data_lake / "store"
    with tempfile.TemporaryDirectory() as td:
        txt_path = Path(td) / f"{source_id}.txt"
        txt_path.write_text(transcript_text, encoding="utf-8")
        stage = _stage_transcript_into_meetings(
            txt_path=txt_path,
            source_id=source_id,
            store_root=store_root,
        )
    if stage.get("status") != "success":
        raise ReferenceBaselineError(
            "source_record_ingest_failed",
            f"could not stage {source_id!r} into raw/meetings for "
            f"Stage-1 ingestion: {stage.get('reason')!r}",
        )

    # SourceLoader resolves the data-lake from DATA_LAKE_PATH; point it
    # at the SAME tree the script operates on (--data-lake is
    # authoritative) and restore the prior value afterwards.
    prev = os.environ.get("DATA_LAKE_PATH")
    os.environ["DATA_LAKE_PATH"] = str(data_lake)
    try:
        result = SourceLoader().load(source_id, str(store_root))
    finally:
        if prev is None:
            os.environ.pop("DATA_LAKE_PATH", None)
        else:
            os.environ["DATA_LAKE_PATH"] = prev

    if result.get("status") != "success":
        raise ReferenceBaselineError(
            "source_record_ingest_failed",
            f"Stage-1 ingestion failed for {source_id!r}: "
            f"{result.get('reason')!r} — no source_record produced, "
            f"no partial file written",
        )
    if not sr_path.is_file():
        raise ReferenceBaselineError(
            "source_record_ingest_failed",
            f"Stage-1 ingestion reported success for {source_id!r} but "
            f"wrote no source_record.json at {sr_path}",
        )


def _resolve_source_artifact_id(data_lake: Path, source_id: str) -> str:
    """slug -> stable transcript UUID via source_record.json.

    ``artifact_id`` is the ONLY field this read requires: it must be
    present and a valid UUID string. The record's ``source_id`` is
    deliberately NOT required here. The source slug is already
    authoritative — it arrives via ``--source-id`` / the transcript
    filename and is what every output path is keyed on — so
    re-requiring it on the artifact is redundant, and a strict schema
    check would reject every record the ingestion pipeline actually
    writes (``source_loader.py`` nests ``source_id`` under ``payload``,
    not at the top level). Missing file, unreadable JSON, a non-object
    body, or a missing / non-UUID ``artifact_id`` halts — the UUID is
    never inferred.
    """
    sr_path = (
        data_lake
        / "store"
        / "processed"
        / "meetings"
        / source_id
        / "source_record.json"
    )
    if not sr_path.is_file():
        raise ReferenceBaselineError(
            "missing_source_record",
            f"no source_record.json at {sr_path} — cannot resolve "
            f"source_artifact_id for {source_id!r}",
        )
    try:
        record = json.loads(sr_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReferenceBaselineError(
            "invalid_source_record",
            f"source_record.json at {sr_path} is unreadable/!json: {exc}",
        ) from exc
    if not isinstance(record, dict):
        raise ReferenceBaselineError(
            "invalid_source_record",
            f"source_record.json at {sr_path} is "
            f"{type(record).__name__}, expected a JSON object",
        )
    artifact_id = record.get("artifact_id")
    if not isinstance(artifact_id, str):
        raise ReferenceBaselineError(
            "invalid_source_record",
            f"source_record.json at {sr_path} has no string "
            f"artifact_id (got {type(artifact_id).__name__})",
        )
    try:
        uuid.UUID(artifact_id)
    except ValueError as exc:
        raise ReferenceBaselineError(
            "invalid_source_record",
            f"source_record.json at {sr_path} artifact_id "
            f"{artifact_id!r} is not a valid UUID: {exc}",
        ) from exc
    return artifact_id


def _read_transcript_text(docx_path: Path) -> str:
    """Plain text from a raw transcript .docx via the pipeline extractor.

    Writes to a throwaway tempfile so nothing is ever written under the
    data-lake ``raw/`` tree (the data-lake contract forbids core
    writing to raw/).
    """
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "transcript.txt"
        result = DocxExtractor().extract(
            str(docx_path), output_path=str(out)
        )
        if result.get("status") != "success":
            raise ReferenceBaselineError(
                "transcript_unreadable",
                f"docx extraction failed for {docx_path}: "
                f"{result.get('reason')!r}",
            )
        text = out.read_text(encoding="utf-8")
    if not text.strip():
        raise ReferenceBaselineError(
            "transcript_unreadable",
            f"transcript {docx_path} extracted to empty text",
        )
    return text


def _meeting_date_from_header(text: str) -> Optional[str]:
    """ISO date from the transcript header, or None. Never inferred."""
    head = "\n".join(text.splitlines()[:_HEADER_SCAN_LINES])
    for pat in _DATE_PATTERNS:
        m = pat.search(head)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                return datetime.date(y, mo, d).isoformat()
            except ValueError:
                continue
    m = _MONTH_NAME_RE.search(head)
    if m:
        month = _MONTHS.get(m.group(1).lower())
        if month:
            try:
                return datetime.date(
                    int(m.group(3)), month, int(m.group(2))
                ).isoformat()
            except ValueError:
                return None
    return None


def _strip_fence(text: str) -> str:
    """Strip a markdown code fence the model may have wrapped the JSON in.

    Handles both the multi-line ` ```json\\n{...}\\n``` ` shape and the
    degenerate single-line ` ```{...}``` ` shape (no newline after the
    opening fence — drop the three backticks, never the body, so a
    fenced-but-recoverable response is not turned into empty text).
    This MUST run before any ``json.loads`` (Fix 1).
    """
    body = (text or "").strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else body[3:]
    if body.endswith("```"):
        body = body.rsplit("```", 1)[0]
    return body.strip()


def parse_response(raw: str) -> Dict[str, Any]:
    """Parse the model text into a JSON object, or HALT.

    A non-JSON / non-object response is a fail-closed halt — we never
    coerce, repair, or partially accept it.
    """
    body = _strip_fence(raw)
    if not body:
        raise ReferenceBaselineError(
            "malformed_llm_response", "model returned empty text"
        )
    try:
        doc = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ReferenceBaselineError(
            "malformed_llm_response",
            f"model response is not valid JSON: {exc}",
        ) from exc
    if not isinstance(doc, dict):
        raise ReferenceBaselineError(
            "malformed_llm_response",
            f"model response JSON is {type(doc).__name__}, not an object",
        )
    return doc


# Exact wording of the second-attempt repair instruction. The model is
# given ONLY the first 200 chars of the failed response (never the whole
# broken blob) plus a hard "JSON only" directive.
_SECOND_ATTEMPT_INSTRUCTION = (
    "The previous response was not valid JSON. Return ONLY a valid "
    "JSON object with the same keys. No explanation, no markdown, "
    "no preamble. Start with { and end with }."
)
_FAILED_RESPONSE_CONTEXT_CHARS = 200


def _warn(message: str) -> None:
    """Loud, non-fatal signal. stderr only — stdout stays pure JSON.

    The workflow tees stdout into the run summary and ``json.loads`` it,
    so every diagnostic MUST go to stderr or it corrupts that parse.
    """
    print(f"WARN: {message}", file=sys.stderr)


def _log_parse_debug(
    source_id: str, raw: str, exc: json.JSONDecodeError
) -> None:
    """Step A: dump the head/tail of the raw response + the error site.

    Enough to diagnose truncation-vs-prose from the log alone, without
    echoing the (potentially huge) full response.
    """
    head = raw[:500]
    tail = raw[-500:] if len(raw) > 500 else ""
    _warn(
        f"malformed_llm_response_debug for {source_id}: parse error "
        f"{exc.msg} at line {exc.lineno} column {exc.colno} "
        f"(char {exc.pos}) of a {len(raw)}-char response"
    )
    _warn(f"  raw[:500] for {source_id}: {head!r}")
    if tail:
        _warn(f"  raw[-500:] for {source_id}: {tail!r}")


def _truncate_at_last_brace(body: str, err_pos: int) -> Optional[str]:
    """Step B helper: the prefix of ``body`` up to the last ``}`` before
    the parse error, or ``None`` if there is no such ``}``.

    ``err_pos`` is a code-point index into ``body`` (the exact string
    handed to ``json.loads``). ``str`` slicing is code-point based, so
    cutting at the index of a ``}`` can never split a multi-byte
    character into invalid UTF-8 — the cut is on a structural ``}``
    boundary, not a raw byte offset. A ``}`` that sits inside an
    unterminated string just yields a substring that fails
    ``json.loads`` on the next attempt; nothing invalid is ever
    accepted.
    """
    cut = body.rfind("}", 0, max(err_pos, 0))
    if cut == -1:
        return None
    return body[: cut + 1]


def parse_response_with_recovery(
    *,
    raw: str,
    client: Callable[..., str],
    system: str,
    user: str,
    source_id: str,
) -> Dict[str, Any]:
    """Fix 1+3: fence-strip, parse, and on a JSON parse failure attempt
    (B) truncation at the last ``}`` then (C) one simplified retry, else
    (D) HALT ``malformed_llm_response``. Never returns an empty/silent
    extraction on failure.

    Fence stripping (Fix 1) runs before EVERY ``json.loads`` here — the
    first attempt, the truncation attempt, and the retry — so a fenced
    response can never reach ``json.loads`` unstripped.
    """
    body = _strip_fence(raw)
    if not body:
        raise ReferenceBaselineError(
            "malformed_llm_response", "model returned empty text"
        )
    # ``except ... as exc`` unbinds ``exc`` at block exit in Python 3,
    # so the error is copied into a function-scoped name that survives
    # for the recovery steps below.
    first_err: Optional[json.JSONDecodeError] = None
    try:
        doc = json.loads(body)
    except json.JSONDecodeError as exc:
        first_err = exc
    else:
        if not isinstance(doc, dict):
            # Valid JSON but the wrong shape — truncation/retry cannot
            # turn a list/scalar into the required object. Fail closed
            # immediately rather than burning a retry that cannot help.
            raise ReferenceBaselineError(
                "malformed_llm_response",
                f"model response JSON is {type(doc).__name__}, not an "
                f"object",
            )
        return doc

    # Reached only when ``json.loads`` raised (the ``else`` returns or
    # raises), so ``first_err`` is always set here.
    assert first_err is not None

    # Step A — debug the failure before attempting any recovery.
    _log_parse_debug(source_id, raw, first_err)

    # Step B — truncation: reuse the longest valid JSON object prefix
    # that ends on a ``}`` before the error.
    truncated = _truncate_at_last_brace(body, first_err.pos)
    if truncated is not None:
        try:
            doc = json.loads(truncated)
        except json.JSONDecodeError:
            doc = None
        if isinstance(doc, dict):
            _warn(
                f"truncated_response_used: parsed {len(truncated)} "
                f"chars of {len(raw)} char response for {source_id}"
            )
            return doc

    # Step C — one simplified retry. Only the FIRST 200 chars of the
    # failed response are sent back (never the whole broken blob), plus
    # the original transcript so the model can actually redo the work.
    retry_user = (
        f"{_SECOND_ATTEMPT_INSTRUCTION}\n\n"
        f"First {_FAILED_RESPONSE_CONTEXT_CHARS} characters of the "
        f"previous (invalid) response, for reference only:\n"
        f"{raw[:_FAILED_RESPONSE_CONTEXT_CHARS]}\n\n"
        f"---\nRe-extract from this transcript:\n{user}"
    )
    try:
        raw2 = client(system=system, user=retry_user)
    except LLMClientError as exc:
        raise ReferenceBaselineError(
            "llm_transport_error",
            f"second (repair) model call failed for {source_id}: {exc} "
            f"— no fallback model, no partial file written",
        ) from exc

    body2 = _strip_fence(raw2)
    if body2:
        try:
            doc = json.loads(body2)
        except json.JSONDecodeError:
            doc = None
        if isinstance(doc, dict):
            _warn(
                f"second_attempt_used: recovered a valid JSON object on "
                f"the simplified retry for {source_id}"
            )
            return doc

    # Step D — every recovery path exhausted. HALT loudly; never a
    # silent empty extraction.
    raise ReferenceBaselineError(
        "malformed_llm_response",
        f"model response is not valid JSON for {source_id} after "
        f"fence-strip, truncation, and one simplified retry "
        f"(original error: {first_err})",
    )


# Text fields tried, in priority order, when an item is a structured
# object. This single list covers every extraction type — the canonical
# prompt lets the model return a structured object for ANY type, so the
# reader must not be keyed on a per-type field. The order is the union
# of every type's schema-required primary text field, most-specific
# first, so e.g. a ``risk`` object resolves on ``risk_text`` before any
# generic ``name``/``title`` fallback could shadow it.
_GROUND_TRUTH_TEXT_FIELDS = (
    "text",
    "question_text",
    "commitment_text",
    "risk_text",
    "reference_text",
    "parameter_name",
    # 1.3.0 type-specific primary text fields, listed before the
    # generic name/title fallbacks so e.g. a position_statement
    # resolves on position_text rather than a uuid id appearing first
    # in the model's object.
    "position_text",
    "objection_text",
    "input_text",
    "ruling_text",
    "term",
    "name",
    "title",
    "phase_name",
    "reference",
)


def extract_ground_truth_text(item: Any, extraction_type: str) -> str:
    """Best-effort ``ground_truth_text`` for one item. NEVER halts.

    The canonical extraction prompt lets the model return, for every
    type, either a plain verbatim string OR a structured object (e.g. a
    ``decisions`` item arriving as ``{"text","verb","stakeholders",
    "confidence","rationale"}``). A structured item is a valid response,
    not a malformed one, so this function tolerantly extracts the best
    text it can and the caller keeps the full original item verbatim in
    ``item_data`` — no information is lost and no real item is dropped.

    Resolution order:

    1. A plain string is returned as-is.
    2. For a dict, the first present, non-empty *string* field from
       ``_GROUND_TRUTH_TEXT_FIELDS`` (priority order) wins.
    3. Else the first non-empty string value anywhere in the dict.
    4. Else (no string content / a non-dict, non-string item) the
       ``str()`` of the item — a last-resort, never-empty fallback so
       the line is still written rather than silently dropped.

    ``extraction_type`` is accepted for call-site symmetry and so a
    future per-type override has a seam; the tolerant logic above is
    deliberately type-agnostic because the prompt's object form is.
    """
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for field in _GROUND_TRUTH_TEXT_FIELDS:
            val = item.get(field)
            if isinstance(val, str) and val:
                return val
        for val in item.values():
            if isinstance(val, str) and val:
                return val
    return str(item)


def build_records(
    *,
    parsed: Dict[str, Any],
    types: List[str],
    source_id: str,
    source_artifact_id: str,
    model: str,
    meeting_date: Optional[str],
    created_at: str,
) -> List[Dict[str, Any]]:
    """Build every JSONL record for one transcript, or HALT.

    Individual items NEVER halt: ``extract_ground_truth_text`` reads any
    string-or-object item tolerantly (the canonical prompt allows a
    structured object for every type) and the full original item is
    preserved verbatim in ``item_data``. The only halt here is a content
    KEY whose value is not a list (a response-shape error, not an item
    error). All records are built in memory first, so any halt — here or
    in the upstream response parse — means the transcript's JSONL is
    never written (no partial output).
    """
    records: List[Dict[str, Any]] = []
    for etype in types:
        value = parsed.get(etype)
        if value is None:
            # Absent key == no items of this type. The prompt says
            # every key should be present, but an omitted key is a
            # valid empty category, not a malformed response.
            continue
        if not isinstance(value, list):
            raise ReferenceBaselineError(
                "malformed_llm_response",
                f"{etype!r} is {type(value).__name__}, expected a list",
            )
        for index, item in enumerate(value):
            ground_truth_text = extract_ground_truth_text(item, etype)
            # Keep the full original item verbatim so nothing the model
            # returned is lost. A dict is stored as-is; a string (or any
            # non-dict) is wrapped as ``{"text": item}`` so ``item_data``
            # is always a JSON object with the original value recoverable.
            item_data = item if isinstance(item, dict) else {"text": item}
            pair_id = str(
                uuid.uuid5(
                    _OPUS_REF_NAMESPACE,
                    f"opus-ref-{source_id}-{etype}-{index}",
                )
            )
            records.append(
                {
                    "pair_id": pair_id,
                    "source_id": source_id,
                    "source_artifact_id": source_artifact_id,
                    "extraction_type": etype,
                    "ground_truth_text": ground_truth_text,
                    "item_data": item_data,
                    "human_authored": False,
                    "model_authored": True,
                    "model_id": model,
                    "verified": False,
                    "status": "reference_only",
                    "provenance": {"produced_by": PRODUCED_BY},
                    "schema_version": "1.0.0",
                    "meeting_date": meeting_date,
                    "created_at": created_at,
                }
            )
    return records


def _jsonl_path(data_lake: Path, source_id: str) -> Path:
    return (
        data_lake
        / "store"
        / "processed"
        / "meetings"
        / source_id
        / _OUTPUT_SUBDIR
        / _OUTPUT_FILENAME
    )


def _write_jsonl(path: Path, records: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(r, sort_keys=True, separators=(",", ":"))
        for r in records
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _is_git_worktree(path: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _assert_not_gitignored(data_lake: Path, abs_path: Path) -> None:
    """Refuse to leave behind a committed-never artifact.

    Mirrors ``scripts/_gitignore_audit.py``'s negation handling:
    ``git check-ignore -v`` returns rc=0 for ANY matched pattern
    including ``!`` un-ignore patterns, so a matched ``!``-pattern means
    the path is NOT ignored.
    """
    if not _is_git_worktree(data_lake):
        # Dev checkout / temp dir without a git repo — nothing can be
        # gitignored, so the guard is vacuously satisfied.
        return
    try:
        rel = abs_path.relative_to(data_lake)
    except ValueError:
        rel = abs_path
    result = subprocess.run(
        [
            "git", "-C", str(data_lake), "check-ignore", "-v",
            "--no-index", str(rel),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 1:
        return  # not ignored
    if result.returncode == 0:
        line = result.stdout.strip()
        try:
            head, _ = line.split("\t", 1)
            pattern = head.rsplit(":", 1)[1]
        except (IndexError, ValueError):
            pattern = ""
        if pattern.startswith("!"):
            return  # matched an un-ignore negation -> not ignored
        raise ReferenceBaselineError(
            "gitignore_blocks_artifact",
            f"{rel} is ignored by data-lake .gitignore rule: {line} "
            f"— git cannot re-include a file whose parent dir is "
            f"excluded, so add BOTH '!**/processed/**/' (re-include "
            f"the directory chain) and "
            f"'!**/processed/**/{_OUTPUT_SUBDIR}/{_OUTPUT_FILENAME}' "
            f"before committing",
        )
    raise ReferenceBaselineError(
        "gitignore_blocks_artifact",
        f"git check-ignore returned rc={result.returncode} for {rel}: "
        f"{result.stderr.strip()}",
    )


def _inventory_transcripts(
    data_lake: Path, source_id: Optional[str]
) -> List[Path]:
    """Raw transcript .docx files. Mirrors the pipeline's minutes filter.

    Files whose name contains 'minutes' are skipped (they belong in
    store/raw/minutes/ — running them as transcripts would produce the
    wrong baseline), exactly as PipelineOrchestrator does.
    """
    tdir = data_lake.joinpath(*_RAW_TRANSCRIPTS_SUBPATH)
    if not tdir.is_dir():
        raise ReferenceBaselineError(
            "missing_transcripts_dir",
            f"no raw transcripts directory at {tdir}",
        )
    docs = sorted(
        (
            p
            for p in tdir.iterdir()
            if p.is_file()
            and p.suffix.lower() == ".docx"
            and "minutes" not in p.name.lower()
        ),
        key=lambda p: p.name,
    )
    if source_id is not None:
        docs = [p for p in docs if _slugify(p.stem) == source_id]
    return docs


def create_baselines(
    *,
    data_lake: Path,
    source_id: Optional[str],
    dry_run: bool,
    skip_existing: bool,
    model: str,
    client: Optional[Callable[..., str]] = None,
    system_prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """Orchestrate the run. Returns a summary dict; raises on any halt.

    ``client`` defaults to :class:`AnthropicJSONClient` constructed with
    the passed ``model``; tests inject a stub. ``system_prompt`` is
    loaded ONCE up front (before any client construction) so a missing
    prompt halts before the model is ever contacted.
    """
    if not model or not model.strip():
        raise ReferenceBaselineError(
            "missing_model",
            "--model is required and must be non-empty (the workflow "
            "resolves it from ai/registry/model_registry.json)",
        )
    model = model.strip()

    # Loaded before the client exists -> a missing prompt provably
    # cannot reach the transport.
    prompt = system_prompt if system_prompt is not None else (
        load_extraction_prompt()
    )
    types = extraction_types()

    if client is not None:
        active_client: Callable[..., str] = client
    else:
        stub = os.environ.get(_STUB_ENV)
        if stub is not None:
            def _stub_client(*, system: str, user: str) -> str:  # noqa: ARG001
                return stub
            active_client = _stub_client
        else:
            active_client = AnthropicJSONClient(
                model=model, max_tokens=_OPUS_MAX_TOKENS
            )

    transcripts = _inventory_transcripts(data_lake, source_id)

    per_transcript: List[Dict[str, Any]] = []
    for docx_path in transcripts:
        sid = _slugify(docx_path.stem)
        out_path = _jsonl_path(data_lake, sid)

        if skip_existing and out_path.is_file():
            per_transcript.append(
                {
                    "source_id": sid,
                    "status": "skipped",
                    "reason": "exists",
                    "total": 0,
                    "by_type": {},
                    "output_path": str(out_path),
                }
            )
            continue

        transcript_text = _read_transcript_text(docx_path)
        meeting_date = _meeting_date_from_header(transcript_text)
        # Self-heal the unstated precondition: a transcript present in
        # the data-lake but never ingested has no source_record.json.
        # Produce it deterministically (LLM-free Stage-1) before
        # resolving the stable UUID, instead of halting on every run.
        _ensure_source_record(data_lake, sid, transcript_text)
        source_artifact_id = _resolve_source_artifact_id(data_lake, sid)

        try:
            raw = active_client(system=prompt, user=transcript_text)
        except LLMClientError as exc:
            raise ReferenceBaselineError(
                "llm_transport_error",
                f"model transport failed for {sid}: {exc} — no "
                f"fallback model, no partial file written",
            ) from exc

        parsed = parse_response_with_recovery(
            raw=raw,
            client=active_client,
            system=prompt,
            user=transcript_text,
            source_id=sid,
        )
        records = build_records(
            parsed=parsed,
            types=types,
            source_id=sid,
            source_artifact_id=source_artifact_id,
            model=model,
            meeting_date=meeting_date,
            created_at=_now_utc_iso(),
        )

        # An empty object ``{}`` (or every type-array empty) is valid
        # JSON, so it survives parsing and produces ZERO records. That is
        # not a malformed-response halt, but a silent empty baseline on a
        # content-bearing transcript is exactly the failure this task
        # forbids — make it loud (stderr, non-fatal).
        if not records:
            _warn(
                f"empty_extraction for {sid}: model returned a valid "
                f"JSON object with zero items across all {len(types)} "
                f"extraction types on a {len(transcript_text)}-char "
                f"transcript — the baseline will be empty. This is not "
                f"a halt (an empty object is structurally valid) but is "
                f"suspicious; inspect the raw response."
            )

        by_type: Dict[str, int] = {}
        for r in records:
            by_type[r["extraction_type"]] = (
                by_type.get(r["extraction_type"], 0) + 1
            )

        if dry_run:
            per_transcript.append(
                {
                    "source_id": sid,
                    "status": "dry_run",
                    "reason": "",
                    "total": len(records),
                    "by_type": by_type,
                    "output_path": str(out_path),
                }
            )
            continue

        _write_jsonl(out_path, records)
        _assert_not_gitignored(data_lake, out_path)
        per_transcript.append(
            {
                "source_id": sid,
                "status": "written",
                "reason": "",
                "total": len(records),
                "by_type": by_type,
                "output_path": str(out_path),
            }
        )

    return {
        "status": "success",
        "model": model,
        "dry_run": dry_run,
        "skip_existing": skip_existing,
        "transcripts": len(transcripts),
        "per_transcript": per_transcript,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-lake", required=True)
    parser.add_argument(
        "--source-id",
        default=None,
        help="Process only this slug. Omit to process all transcripts.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written; write/commit nothing.",
    )
    parser.add_argument(
        "--skip-existing",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip sources that already have the baseline JSONL "
        "(default true; pass --no-skip-existing to regenerate).",
    )
    parser.add_argument(
        "--model",
        required=True,
        help="Model string, resolved by the workflow from "
        "ai/registry/model_registry.json. No default — the script "
        "never hardcodes a model.",
    )
    args = parser.parse_args(argv)

    # Mobile workflow_dispatch inputs frequently arrive with a trailing
    # space pasted from a phone keyboard; strip every string arg.
    for attr in vars(args):
        val = getattr(args, attr)
        if isinstance(val, str):
            setattr(args, attr, val.strip())

    source_id = args.source_id or None
    data_lake = Path(args.data_lake)
    if not data_lake.is_dir():
        print(
            json.dumps(
                {
                    "status": "failure",
                    "reason": "data_lake_not_a_directory",
                    "detail": str(data_lake),
                },
                indent=2,
                sort_keys=True,
            )
        )
        print(
            f"FAIL: --data-lake is not a directory: {data_lake}",
            file=sys.stderr,
        )
        return 2

    try:
        result = create_baselines(
            data_lake=data_lake,
            source_id=source_id,
            dry_run=args.dry_run,
            skip_existing=args.skip_existing,
            model=args.model,
        )
    except ReferenceBaselineError as exc:
        print(
            json.dumps(
                {
                    "status": "failure",
                    "reason": exc.reason,
                    "detail": exc.detail,
                },
                indent=2,
                sort_keys=True,
            )
        )
        print(f"FAIL: {exc.reason} — {exc.detail}", file=sys.stderr)
        return 2

    print(json.dumps(result, indent=2, sort_keys=True))
    # Human-readable per-transcript summary: source_id | total | by type
    for t in result["per_transcript"]:
        by_type = t.get("by_type") or {}
        by_type_str = (
            ", ".join(f"{k}={by_type[k]}" for k in sorted(by_type))
            or "-"
        )
        print(
            f"{t['source_id']} | {t['status']} | total={t['total']} | "
            f"{by_type_str}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
