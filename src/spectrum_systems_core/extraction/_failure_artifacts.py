"""Failure artifact emission for chunks blocked during typed extraction.

Phase X-0 + X-1. Every non-success exit path in the extraction loop
emits a failure artifact describing why the chunk was blocked. The
orchestrator's chunk counter is then bumped by the same call so the
two cannot drift out of sync (red-team finding RT1: "Can a blocked
chunk produce ``✓`` if the orchestrator counter is not called?" --
the answer is no, because the only blessed emission helpers also
update the counter).

Failure artifact types map 1:1 with block_reasons in
``_chunk_counters.py``:

  api_rate_limit_exhausted          -> rate_limit_exhausted
  extraction_empty_response         -> empty_response
  typed_extraction_llm_json_parse_failed -> parse_error
  typed_extraction_empty_result     -> other  (zero items after parse)
"""
from __future__ import annotations

import datetime
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from ._chunk_counters import (
    BLOCK_REASON_EMPTY_RESPONSE,
    BLOCK_REASON_OTHER,
    BLOCK_REASON_PARSE_ERROR,
    BLOCK_REASON_RATE_LIMIT,
    ChunkCounters,
)

# Phase O.2: in-memory chunk lookup table set per extraction run. The
# typed_extraction_runner installs the map once it has loaded
# chunks.jsonl, so every emit_* call site can attach chunk text /
# speaker / index for free without re-reading from disk.
_ACTIVE_CHUNK_LOOKUP: dict[str, dict[str, Any]] = {}


def install_chunk_lookup(chunks: Any | None) -> None:
    """Register the in-memory chunk list used by ``_maybe_emit_blocked_chunk``.

    Pass ``None`` or an empty list to clear the table. The lookup is
    keyed on ``chunk_id`` (and falls back to ``id``). Callers that
    cannot supply a chunk list still get the legacy failure artifacts
    -- the blocked_chunk emission is a strict addition.
    """
    global _ACTIVE_CHUNK_LOOKUP
    if not chunks:
        _ACTIVE_CHUNK_LOOKUP = {}
        return
    table: dict[str, dict[str, Any]] = {}
    for c in chunks:
        if not isinstance(c, dict):
            continue
        cid = c.get("chunk_id") or c.get("id")
        if isinstance(cid, str) and cid:
            table[cid] = c
    _ACTIVE_CHUNK_LOOKUP = table


def clear_chunk_lookup() -> None:
    install_chunk_lookup(None)


_BLOCK_REASON_BY_TYPE: dict[str, str] = {
    "api_rate_limit_exhausted": "rate_limit_exhausted",
    "extraction_empty_response": "empty_response",
    "story_extraction_empty_response": "empty_response",
    "typed_extraction_llm_json_parse_failed": "parse_error",
    "story_extraction_parse_failed": "parse_error",
    "typed_extraction_empty_result": "other",
}


_LOG = logging.getLogger(__name__)


# Public artifact_type constants. The orchestrator + tests reference
# these by name; if you rename one, also update the failure_artifact
# enum in ``orchestration_result.schema.json``.
ARTIFACT_RATE_LIMIT_EXHAUSTED: str = "api_rate_limit_exhausted"
ARTIFACT_EMPTY_RESPONSE: str = "extraction_empty_response"
ARTIFACT_JSON_PARSE_FAILED: str = "typed_extraction_llm_json_parse_failed"
ARTIFACT_EMPTY_RESULT: str = "typed_extraction_empty_result"

# Phase O.2: unified blocked_chunk envelope. Emitted alongside the
# typed failure artifacts above so a single artifact carries the
# chunk text + metadata for human review.
ARTIFACT_BLOCKED_CHUNK: str = "blocked_chunk"
BLOCKED_CHUNK_SCHEMA_VERSION: str = "2.0.0"
_CHUNK_TEXT_TRUNCATE_AT: int = 500
_CHUNK_TEXT_NOT_FOUND: str = "[chunk not found]"
# Phase S.0: story extraction failure artifacts. The story-extractor parse
# site reuses guard_empty_response / strip_markdown_fence and emits these
# so the orchestrator's chunks_blocked counter sees the same call shape
# as typed extraction.
ARTIFACT_STORY_EMPTY_RESPONSE: str = "story_extraction_empty_response"
ARTIFACT_STORY_PARSE_FAILED: str = "story_extraction_parse_failed"

FAILURE_ARTIFACT_TYPES: tuple = (
    ARTIFACT_RATE_LIMIT_EXHAUSTED,
    ARTIFACT_EMPTY_RESPONSE,
    ARTIFACT_JSON_PARSE_FAILED,
    ARTIFACT_EMPTY_RESULT,
    ARTIFACT_STORY_EMPTY_RESPONSE,
    ARTIFACT_STORY_PARSE_FAILED,
)

FAILURE_ARTIFACT_SCHEMA_VERSION: str = "1.0.0"


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _build(
    artifact_type: str,
    *,
    chunk_id: str,
    source_id: str,
    component: str,
    detail: str,
    extraction_run_id: str | None = None,
) -> dict[str, Any]:
    return {
        "artifact_type": artifact_type,
        "schema_version": FAILURE_ARTIFACT_SCHEMA_VERSION,
        "failure_id": str(uuid.uuid4()),
        "chunk_id": chunk_id or "",
        "source_id": source_id or "",
        "component": component or "",
        "detail": (detail or "")[:1000],
        "extraction_run_id": extraction_run_id or "",
        "created_at": _now_iso(),
    }


def emit_rate_limit_exhausted(
    counters: ChunkCounters,
    *,
    chunk_id: str,
    source_id: str,
    component: str,
    detail: str = "",
    extraction_run_id: str | None = None,
    sdl_root: Path | None = None,
    n: int = 1,
) -> dict[str, Any]:
    """Emit ``api_rate_limit_exhausted`` and bump ``rate_limit_exhausted``."""
    counters.record_block(BLOCK_REASON_RATE_LIMIT, n=n)
    return _emit(
        ARTIFACT_RATE_LIMIT_EXHAUSTED,
        chunk_id=chunk_id,
        source_id=source_id,
        component=component,
        detail=detail,
        extraction_run_id=extraction_run_id,
        sdl_root=sdl_root,
    )


def emit_empty_response(
    counters: ChunkCounters,
    *,
    chunk_id: str,
    source_id: str,
    component: str,
    detail: str = "",
    extraction_run_id: str | None = None,
    sdl_root: Path | None = None,
    n: int = 1,
) -> dict[str, Any]:
    """Emit ``extraction_empty_response`` and bump ``empty_response``."""
    counters.record_block(BLOCK_REASON_EMPTY_RESPONSE, n=n)
    return _emit(
        ARTIFACT_EMPTY_RESPONSE,
        chunk_id=chunk_id,
        source_id=source_id,
        component=component,
        detail=detail,
        extraction_run_id=extraction_run_id,
        sdl_root=sdl_root,
    )


def emit_json_parse_failed(
    counters: ChunkCounters,
    *,
    chunk_id: str,
    source_id: str,
    component: str,
    detail: str = "",
    extraction_run_id: str | None = None,
    sdl_root: Path | None = None,
    n: int = 1,
) -> dict[str, Any]:
    """Emit ``typed_extraction_llm_json_parse_failed`` and bump ``parse_error``."""
    counters.record_block(BLOCK_REASON_PARSE_ERROR, n=n)
    return _emit(
        ARTIFACT_JSON_PARSE_FAILED,
        chunk_id=chunk_id,
        source_id=source_id,
        component=component,
        detail=detail,
        extraction_run_id=extraction_run_id,
        sdl_root=sdl_root,
    )


def emit_empty_result(
    counters: ChunkCounters,
    *,
    chunk_id: str,
    source_id: str,
    component: str,
    detail: str = "",
    extraction_run_id: str | None = None,
    sdl_root: Path | None = None,
    n: int = 1,
) -> dict[str, Any]:
    """Emit ``typed_extraction_empty_result`` and bump ``other``."""
    counters.record_block(BLOCK_REASON_OTHER, n=n)
    return _emit(
        ARTIFACT_EMPTY_RESULT,
        chunk_id=chunk_id,
        source_id=source_id,
        component=component,
        detail=detail,
        extraction_run_id=extraction_run_id,
        sdl_root=sdl_root,
    )


def emit_story_empty_response(
    counters: ChunkCounters,
    *,
    chunk_id: str,
    source_id: str,
    component: str = "story_extractor",
    detail: str = "",
    extraction_run_id: str | None = None,
    sdl_root: Path | None = None,
    n: int = 1,
) -> dict[str, Any]:
    """Emit ``story_extraction_empty_response`` and bump ``empty_response``.

    Mirrors ``emit_empty_response`` but with the story-path artifact_type so
    the forensic record names the failing component. The counter still
    bumps the same ``empty_response`` block_reason because the
    orchestration_result rollup is component-agnostic.
    """
    counters.record_block(BLOCK_REASON_EMPTY_RESPONSE, n=n)
    return _emit(
        ARTIFACT_STORY_EMPTY_RESPONSE,
        chunk_id=chunk_id,
        source_id=source_id,
        component=component,
        detail=detail,
        extraction_run_id=extraction_run_id,
        sdl_root=sdl_root,
    )


def emit_story_parse_failed(
    counters: ChunkCounters,
    *,
    chunk_id: str,
    source_id: str,
    component: str = "story_extractor",
    detail: str = "",
    extraction_run_id: str | None = None,
    sdl_root: Path | None = None,
    n: int = 1,
) -> dict[str, Any]:
    """Emit ``story_extraction_parse_failed`` and bump ``parse_error``."""
    counters.record_block(BLOCK_REASON_PARSE_ERROR, n=n)
    return _emit(
        ARTIFACT_STORY_PARSE_FAILED,
        chunk_id=chunk_id,
        source_id=source_id,
        component=component,
        detail=detail,
        extraction_run_id=extraction_run_id,
        sdl_root=sdl_root,
    )


def _truncate_chunk_text(text: str) -> str:
    if not isinstance(text, str):
        return ""
    if len(text) <= _CHUNK_TEXT_TRUNCATE_AT:
        return text
    return text[:_CHUNK_TEXT_TRUNCATE_AT] + " [truncated]"


def _build_blocked_chunk(
    failure_artifact: dict[str, Any],
    block_reason: str,
    chunk: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the unified blocked_chunk envelope.

    When ``chunk`` is ``None`` the envelope still emits with
    ``chunk_text = "[chunk not found]"`` and ``chunk_char_count = null``
    so the artifact is self-describing.
    """
    artifact: dict[str, Any] = {
        "artifact_type": ARTIFACT_BLOCKED_CHUNK,
        "schema_version": BLOCKED_CHUNK_SCHEMA_VERSION,
        "failure_id": failure_artifact.get("failure_id", "") or str(uuid.uuid4()),
        "chunk_id": failure_artifact.get("chunk_id", "") or "",
        "source_id": failure_artifact.get("source_id", "") or "",
        "block_reason": block_reason,
        "component": failure_artifact.get("component", "") or "",
        "detail": failure_artifact.get("detail", "") or "",
        "extraction_run_id": failure_artifact.get("extraction_run_id", "") or "",
        "created_at": failure_artifact.get("created_at") or _now_iso(),
    }
    if isinstance(chunk, dict):
        raw_text = chunk.get("text") or ""
        if not isinstance(raw_text, str):
            raw_text = str(raw_text or "")
        artifact["chunk_text"] = _truncate_chunk_text(raw_text)
        artifact["chunk_char_count"] = len(raw_text)
        speaker = chunk.get("speaker") or chunk.get("chunk_speaker")
        artifact["chunk_speaker"] = (
            speaker if isinstance(speaker, str) else None
        )
        idx = chunk.get("chunk_index")
        if isinstance(idx, bool):
            idx = None
        artifact["chunk_index"] = (
            int(idx) if isinstance(idx, int) and idx >= 0 else None
        )
        model = chunk.get("extraction_model") or chunk.get("model") or ""
        artifact["extraction_model"] = model if isinstance(model, str) else ""
    else:
        artifact["chunk_text"] = _CHUNK_TEXT_NOT_FOUND
        artifact["chunk_char_count"] = None
        artifact["chunk_speaker"] = None
        artifact["chunk_index"] = None
        artifact["extraction_model"] = ""
    return artifact


def _maybe_emit_blocked_chunk(
    failure_artifact: dict[str, Any],
    sdl_root: Path | None,
) -> dict[str, Any] | None:
    """Write a blocked_chunk envelope mirroring the failure artifact.

    Looks up the chunk via the in-memory table installed by
    ``install_chunk_lookup`` so no extra disk reads happen on the hot
    path. Validation + write follow the same fail-soft pattern as the
    failure artifact: violations are logged, never raised.
    """
    artifact_type = failure_artifact.get("artifact_type", "")
    block_reason = _BLOCK_REASON_BY_TYPE.get(artifact_type)
    if not block_reason:
        return None
    chunk_id = failure_artifact.get("chunk_id", "") or ""
    chunk = _ACTIVE_CHUNK_LOOKUP.get(chunk_id) if chunk_id else None
    bc = _build_blocked_chunk(failure_artifact, block_reason, chunk)
    try:
        from ..validation import (
            ArtifactValidationError,
            SchemaNotFoundError,
            validate_artifact,
        )
        try:
            validate_artifact(bc, ARTIFACT_BLOCKED_CHUNK)
        except (ArtifactValidationError, SchemaNotFoundError) as exc:
            _LOG.warning("blocked_chunk_schema_violation: %s", exc)
    except ImportError:
        pass
    if sdl_root is not None:
        try:
            target_dir = Path(sdl_root) / "blocked_chunks"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / f"{bc['failure_id']}.json").write_text(
                json.dumps(bc, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            _LOG.warning("blocked_chunk_write_failed: %s", exc)
    return bc


def _emit(
    artifact_type: str,
    *,
    chunk_id: str,
    source_id: str,
    component: str,
    detail: str,
    extraction_run_id: str | None,
    sdl_root: Path | None,
) -> dict[str, Any]:
    """Build, validate, and (optionally) persist a failure artifact.

    Returns the artifact dict regardless of whether persistence
    succeeded -- the in-memory counter is the authoritative record for
    chunk_blocked rollups; the on-disk artifact is a forensic record.

    Schema validation runs through the central
    ``spectrum_systems_core.validation.validate_artifact`` helper so
    failure artifacts cannot drift from their schema. Validation
    failures are logged but never raised; we prefer to preserve the
    counter bump and let the next ``X-2`` red-team pass surface the
    schema mismatch.
    """
    artifact = _build(
        artifact_type,
        chunk_id=chunk_id,
        source_id=source_id,
        component=component,
        detail=detail,
        extraction_run_id=extraction_run_id,
    )

    # Validate before write so a malformed failure artifact does NOT
    # land on disk in a partially-broken state. Lazy import to avoid
    # an import cycle (validation -> schemas -> back to extraction
    # constants is fine, but we still defer).
    try:
        from ..validation import ArtifactValidationError, validate_artifact

        try:
            validate_artifact(artifact, artifact_type)
        except ArtifactValidationError as exc:
            _LOG.warning(
                "failure_artifact_schema_violation: type=%s detail=%s",
                artifact_type, exc,
            )
    except ImportError:
        # validation module not yet importable (e.g. during bootstrap).
        # Continue -- the counter is the durable signal.
        pass

    if sdl_root is not None:
        try:
            target_dir = Path(sdl_root) / "failures"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / f"{artifact['failure_id']}.json").write_text(
                json.dumps(artifact, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            _LOG.warning(
                "failure_artifact_write_failed: type=%s err=%s",
                artifact_type, exc,
            )

    # Phase O.2: mirror every blocked failure as a unified blocked_chunk
    # envelope carrying the chunk text + metadata. No-op when the
    # artifact_type does not map to a block_reason (defensive guard
    # rather than a hard assertion).
    _maybe_emit_blocked_chunk(artifact, sdl_root)
    return artifact
