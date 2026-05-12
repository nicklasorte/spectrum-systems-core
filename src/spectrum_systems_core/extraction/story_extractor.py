"""StoryExtractor: chunks.jsonl -> candidates.jsonl via the Anthropic API.

Model: claude-haiku-4-5-20251001 at temperature=0 (FINDING-C-007).
Returns a structured JSON candidate per chunk; failures are recorded as
blocked candidates so they remain debuggable in candidates.jsonl rather
than crashing the pipeline (FINDING-C-002 / debuggability principle).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import jsonschema

_LOG = logging.getLogger(__name__)

from ..ingestion._paths import schema_path
from ._paths import find_processed_dir
# Phase S.0: reuse the Phase X resilience primitives so the story
# extraction parse path uses the same call order
# (guard_empty_response -> strip_markdown_fence -> json.loads). Import,
# never duplicate -- one implementation per primitive.
from ._chunk_counters import ChunkCounters
from ._failure_artifacts import emit_story_empty_response, emit_story_parse_failed
from ._resilience import EmptyResponseError, guard_empty_response, strip_markdown_fence


_COMPONENT_NAME = "story_extractor"
_COMPONENT_VERSION = "1.1.0"
_SCHEMA_VERSION = "1.1.0"
EXTRACTION_MODEL = "claude-haiku-4-5-20251001"
EXTRACTION_TEMPERATURE = 0
MAX_TOKENS = 1000

PROMPT_TEMPLATE = """You are a story extraction assistant. Your job is to find
potential stories in source text that could be used in speeches, reports, or
strategic narratives.

Analyze the following text chunk and extract zero or more story candidates.

Source ID: {source_id}
Chunk ID: {chunk_id}
Chunk index: {chunk_index}
Page numbers: {page_numbers}

Text:
{chunk_text}

Return a JSON array of story objects. If there are no stories, return an
empty array []. Always use array format even for a single story.
Do not return multiple separate JSON objects — wrap all stories in a
single array. No preamble, no markdown.

Each story object must match this exact structure:

{{
  "story_found": true,
  "source_excerpt": "verbatim quote from the text above, minimum 10 chars",
  "story_summary": "one sentence summary of the story",
  "possible_theme": "one phrase theme label",
  "tier_guess": "tier_1 or tier_2 or tier_3",
  "why_it_might_work": "one sentence on why this story resonates",
  "risk_flags": ["list of risk strings, can be empty array"],
  "source_turn_ids": ["array containing the Chunk ID above"]
}}

Rules:
- source_excerpt must be copied VERBATIM from the text above. Do not paraphrase.
- If no story exists in this chunk, return an empty array [].
- tier_1 = has a clear human moment, stakes, and narrative tension.
- tier_2 = interesting but needs development.
- tier_3 = background/context only.

SOURCE CITATION REQUIREMENT (mandatory):

For every item you extract, you MUST include the IDs of the specific
speaker-turn chunks from which you extracted it.

The chunk provided to you has a "Chunk ID" field above. Use that exact
ID in the source_turn_ids array of the extracted item.

Rules:
- If you cannot identify which chunks support an item: DO NOT include
  that item. Omit it entirely from the array.
- Never invent or guess chunk IDs.
- A single item may cite multiple chunk_ids if it spans multiple turns.
- source_turn_ids must contain at least one valid chunk_id.
"""


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _execution_fingerprint(chunk_id: str, source_excerpt: str) -> str:
    seed = f"{chunk_id}|{source_excerpt}|{_COMPONENT_NAME}:{_COMPONENT_VERSION}"
    return "sha256:" + _sha256_hex(seed.encode("utf-8"))


def _failure(reason: str) -> Dict[str, Any]:
    return {"status": "failure", "candidates": [], "reason": reason}


def _parse_story_response(raw: str) -> List[Dict[str, Any]]:
    """Parse a story extraction response that may arrive in three shapes.

    Format A — JSON array (correct):
        [{"story_found": true, ...}, {"story_found": true, ...}]

    Format B — single JSON object (one story, no array):
        {"story_found": true, ...}

    Format C — concatenated JSON objects (NDJSON-style, no separator):
        {"story_found": true, ...}
        {"story_found": true, ...}

    Returns a list of parsed JSON values in all cases. Raises
    ``json.JSONDecodeError`` if none of the formats yield any value.
    """
    stripped = raw.strip()

    if stripped.startswith("["):
        return json.loads(stripped)

    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
            return [obj]
        except json.JSONDecodeError:
            pass

        results: List[Dict[str, Any]] = []
        decoder = json.JSONDecoder()
        pos = 0
        while pos < len(stripped):
            stripped_from_pos = stripped[pos:].lstrip()
            if not stripped_from_pos:
                break
            pos += len(stripped[pos:]) - len(stripped_from_pos)
            try:
                obj, end_pos = decoder.raw_decode(stripped, pos)
            except json.JSONDecodeError:
                break
            results.append(obj)
            pos = end_pos

        if results:
            return results

    raise json.JSONDecodeError("Unrecognized story response format", stripped, 0)


def _make_blocked_skeleton(
    chunk: Dict[str, Any],
    *,
    source_id: str,
    source_family: str,
    block_reason: str,
) -> Dict[str, Any]:
    """Skeleton blocked candidate that does NOT need to satisfy the schema.

    Blocked candidates are written to candidates.jsonl for debuggability but
    bypass schema validation. They preserve chunk_id, source, and the block
    reason so a human can locate the original chunk in chunks.jsonl.
    """
    return {
        "story_id": str(uuid.uuid4()),
        "source_id": source_id,
        "source_family": source_family,
        "chunk_id": chunk["chunk_id"],
        "unit_ids": list(chunk.get("unit_ids", [])),
        "page_numbers": list(chunk.get("page_numbers", [])),
        "extraction_model": EXTRACTION_MODEL,
        "extraction_temperature": EXTRACTION_TEMPERATURE,
        "grounded": False,
        "grounded_unit_ids": [],
        "status": "blocked",
        "superseded_by": None,
        "created_at": _now_iso(),
        "block_reason": block_reason,
    }


class StoryExtractor:
    """Extract structured story candidates from chunks via the Anthropic API."""

    def __init__(self, api_caller: Optional[Callable[[str], str]] = None):
        # Tests inject api_caller; production uses the real Anthropic client.
        self._api_caller = api_caller
        # Phase S.0 counter: every chunk produces exactly one outcome
        # (success, story_extraction_empty_response, story_extraction_parse_failed,
        # api_error, or schema_violation). The counter is per-run, surfaced
        # on the return dict so the orchestrator can roll it into
        # ``chunks_blocked``.
        self.last_run_counters: ChunkCounters = ChunkCounters()

    def extract_from_source(
        self, source_id: str, repo_root: str
    ) -> Dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        processed_dir, source_family = find_processed_dir(
            repo_root_path, source_id
        )
        if processed_dir is None or source_family is None:
            return _failure("chunks_not_found")
        chunks_path = processed_dir / "stories" / "chunks.jsonl"
        if not chunks_path.is_file():
            return _failure("chunks_not_found")

        chunks: List[Dict[str, Any]] = []
        try:
            with chunks_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    chunks.append(json.loads(line))
        except (OSError, json.JSONDecodeError) as exc:
            return _failure(f"chunks_unreadable: {exc}")
        if not chunks:
            return _failure("chunks_empty")

        if self._api_caller is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                return _failure("api_key_missing")
            try:
                self._api_caller = self._build_default_api_caller()
            except ImportError as exc:
                return _failure(f"anthropic_sdk_missing: {exc}")

        try:
            schema = json.loads(
                schema_path("story_candidate").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError) as exc:
            return _failure(f"schema_unreadable: {exc}")
        validator = jsonschema.Draft202012Validator(schema)

        all_records: List[Dict[str, Any]] = []
        ok_candidates: List[Dict[str, Any]] = []

        valid_chunk_ids = {c["chunk_id"] for c in chunks if "chunk_id" in c}

        # Phase S.0: fresh counter per source so multi-source callers do
        # not accumulate across runs. Attempts are bumped once per chunk
        # submitted to the API.
        counters = ChunkCounters()
        counters.record_attempt(len(chunks))
        # ``_resolve_sdl_root`` is not available here without growing this
        # module's dependency surface; the orchestrator persists failure
        # artifacts via a follow-up call when sdl_root is known. We pass
        # ``sdl_root=None`` so emission still bumps the counter (the
        # authoritative record) and logs the failure type even when on-
        # disk persistence is unavailable from this layer.
        failure_sdl_root = None

        for chunk in chunks:
            prompt = PROMPT_TEMPLATE.format(
                source_id=source_id,
                chunk_id=chunk.get("chunk_id"),
                chunk_index=chunk.get("chunk_index"),
                page_numbers=chunk.get("page_numbers", []),
                chunk_text=chunk.get("text", ""),
            )
            chunk_id_str = str(chunk.get("chunk_id") or "")
            try:
                response_text = self._api_caller(prompt)
            except Exception as exc:  # broad: API can raise many error types
                all_records.append(
                    _make_blocked_skeleton(
                        chunk,
                        source_id=source_id,
                        source_family=source_family,
                        block_reason=f"api_error: {type(exc).__name__}: {exc}",
                    )
                )
                continue

            # Phase S.0: identical call order to typed extraction
            # (typed_extraction_runner._parse_json_response_strict):
            #   guard_empty_response -> strip_markdown_fence -> json.loads.
            # An empty / whitespace-only response or a fence-only response
            # halts this chunk and counts it under ``empty_response``. A
            # non-empty response that still fails to parse counts under
            # ``parse_error``. Both keep the chunk debuggable via a
            # blocked candidate in candidates.jsonl AND via a
            # failure artifact in <sdl_root>/failures/.
            try:
                raw = guard_empty_response(response_text, chunk_id_str)
                stripped = strip_markdown_fence(raw)
                if not stripped:
                    raise EmptyResponseError(
                        f"Markdown fence wrapped no JSON content for chunk {chunk_id_str}"
                    )
            except EmptyResponseError as exc:
                emit_story_empty_response(
                    counters,
                    chunk_id=chunk_id_str,
                    source_id=source_id,
                    detail=str(exc),
                    sdl_root=failure_sdl_root,
                )
                all_records.append(
                    _make_blocked_skeleton(
                        chunk,
                        source_id=source_id,
                        source_family=source_family,
                        block_reason=f"empty_response: {exc}",
                    )
                )
                continue

            try:
                parsed_list = _parse_story_response(stripped)
            except (TypeError, json.JSONDecodeError) as exc:
                emit_story_parse_failed(
                    counters,
                    chunk_id=chunk_id_str,
                    source_id=source_id,
                    detail=f"{type(exc).__name__}: {exc}",
                    sdl_root=failure_sdl_root,
                )
                all_records.append(
                    _make_blocked_skeleton(
                        chunk,
                        source_id=source_id,
                        source_family=source_family,
                        block_reason=f"json_parse_error: {exc}",
                    )
                )
                continue

            if not isinstance(parsed_list, list):
                emit_story_parse_failed(
                    counters,
                    chunk_id=chunk_id_str,
                    source_id=source_id,
                    detail="response was not a JSON array",
                    sdl_root=failure_sdl_root,
                )
                all_records.append(
                    _make_blocked_skeleton(
                        chunk,
                        source_id=source_id,
                        source_family=source_family,
                        block_reason="json_parse_error: response was not a JSON array",
                    )
                )
                continue

            chunk_produced_candidate = False
            for parsed in parsed_list:
                if not isinstance(parsed, dict):
                    _LOG.warning(
                        "story_missing_required_fields: item is not a JSON "
                        "object (chunk_id=%s)",
                        chunk.get("chunk_id"),
                    )
                    continue

                if not parsed.get("story_found"):
                    # No story in this item — skip silently. Not an error.
                    continue

                raw_turn_ids = parsed.get("source_turn_ids")
                if not isinstance(raw_turn_ids, list) or not raw_turn_ids:
                    _LOG.warning(
                        "story_missing_required_fields: source_turn_ids missing "
                        "(chunk_id=%s)",
                        chunk.get("chunk_id"),
                    )
                    continue
                turn_ids = [str(t) for t in raw_turn_ids if isinstance(t, str)]
                if not turn_ids:
                    _LOG.warning(
                        "story_missing_required_fields: source_turn_ids empty "
                        "(chunk_id=%s)",
                        chunk.get("chunk_id"),
                    )
                    continue
                invalid_turn_ids = [t for t in turn_ids if t not in valid_chunk_ids]
                if invalid_turn_ids:
                    for bad in invalid_turn_ids:
                        _LOG.warning(
                            "extraction_invalid_source_turns: %s not in chunks", bad
                        )
                    source_turn_validation = "invalid"
                else:
                    source_turn_validation = "verified"

                candidate = self._assemble_candidate(
                    parsed,
                    chunk,
                    source_id=source_id,
                    source_family=source_family,
                    source_turn_ids=turn_ids,
                    source_turn_validation=source_turn_validation,
                )

                try:
                    validator.validate(candidate)
                except jsonschema.ValidationError as exc:
                    blocked = _make_blocked_skeleton(
                        chunk,
                        source_id=source_id,
                        source_family=source_family,
                        block_reason=f"schema_violation: {exc.message}",
                    )
                    # Preserve raw fields so a human can debug what the model said.
                    blocked["raw_extraction"] = parsed
                    all_records.append(blocked)
                    continue

                all_records.append(candidate)
                ok_candidates.append(candidate)
                chunk_produced_candidate = True

            if chunk_produced_candidate:
                counters.record_success(1)

        # Always overwrite (FINDING from RT2: appending produces duplicates).
        out_path = processed_dir / "stories" / "candidates.jsonl"
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", encoding="utf-8") as fh:
                for record in all_records:
                    fh.write(
                        json.dumps(record, sort_keys=True, separators=(",", ":"))
                        + "\n"
                    )
        except OSError as exc:
            return _failure(f"write_error: {exc}")

        self.last_run_counters = counters
        return {
            "status": "success",
            "candidates": ok_candidates,
            "all_records": all_records,
            "reason": "",
            "chunks_attempted": counters.chunks_attempted,
            "chunks_succeeded": counters.chunks_succeeded,
            "chunks_blocked": counters.chunks_blocked,
            "block_reasons": dict(counters.block_reasons),
            "stage_status": counters.stage_status(),
        }

    def _assemble_candidate(
        self,
        parsed: Dict[str, Any],
        chunk: Dict[str, Any],
        *,
        source_id: str,
        source_family: str,
        source_turn_ids: List[str],
        source_turn_validation: str,
    ) -> Dict[str, Any]:
        source_excerpt = parsed.get("source_excerpt") or ""
        return {
            "schema_version": _SCHEMA_VERSION,
            "story_id": str(uuid.uuid4()),
            "source_id": source_id,
            "source_family": source_family,
            "chunk_id": chunk["chunk_id"],
            "source_turn_ids": list(source_turn_ids),
            "source_turn_validation": source_turn_validation,
            "unit_ids": list(chunk.get("unit_ids", [])),
            "page_numbers": list(chunk.get("page_numbers", [])),
            "source_excerpt": source_excerpt,
            "story_summary": parsed.get("story_summary") or "",
            "possible_theme": parsed.get("possible_theme") or "",
            "tier_guess": parsed.get("tier_guess") or "tier_3",
            "why_it_might_work": parsed.get("why_it_might_work") or "",
            "risk_flags": list(parsed.get("risk_flags") or []),
            "storyworthy_score": {
                "five_second_moment": 0,
                "stakes": 0,
                "central_question": 0,
                "vulnerability": 0,
                "narrative_compression": 0,
                "total": 0,
            },
            "storyworthy_verdict": "reject",
            "extraction_model": EXTRACTION_MODEL,
            "extraction_temperature": EXTRACTION_TEMPERATURE,
            "grounded": False,
            "grounded_unit_ids": [],
            "status": "candidate",
            "superseded_by": None,
            "created_at": _now_iso(),
            "provenance": {
                "produced_by": {
                    "component": _COMPONENT_NAME,
                    "version": _COMPONENT_VERSION,
                },
                "input_artifact_ids": [chunk["chunk_id"]],
                "execution_fingerprint_hash": _execution_fingerprint(
                    chunk["chunk_id"], source_excerpt
                ),
            },
        }

    def _build_default_api_caller(self) -> Callable[[str], str]:
        import anthropic  # imported lazily so tests can run without it

        client = anthropic.Anthropic()

        def _call(prompt: str) -> str:
            message = client.messages.create(
                model=EXTRACTION_MODEL,
                max_tokens=MAX_TOKENS,
                temperature=EXTRACTION_TEMPERATURE,
                messages=[{"role": "user", "content": prompt}],
            )
            # Anthropic SDK returns a list of content blocks; join text blocks.
            parts: List[str] = []
            for block in message.content:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    parts.append(text)
            return "\n".join(parts)

        return _call
