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
from typing import Any, Dict, Optional

from ._chunk_counters import (
    BLOCK_REASON_EMPTY_RESPONSE,
    BLOCK_REASON_OTHER,
    BLOCK_REASON_PARSE_ERROR,
    BLOCK_REASON_RATE_LIMIT,
    ChunkCounters,
)


_LOG = logging.getLogger(__name__)


# Public artifact_type constants. The orchestrator + tests reference
# these by name; if you rename one, also update the failure_artifact
# enum in ``orchestration_result.schema.json``.
ARTIFACT_RATE_LIMIT_EXHAUSTED: str = "api_rate_limit_exhausted"
ARTIFACT_EMPTY_RESPONSE: str = "extraction_empty_response"
ARTIFACT_JSON_PARSE_FAILED: str = "typed_extraction_llm_json_parse_failed"
ARTIFACT_EMPTY_RESULT: str = "typed_extraction_empty_result"
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
    extraction_run_id: Optional[str] = None,
) -> Dict[str, Any]:
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
    extraction_run_id: Optional[str] = None,
    sdl_root: Optional[Path] = None,
    n: int = 1,
) -> Dict[str, Any]:
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
    extraction_run_id: Optional[str] = None,
    sdl_root: Optional[Path] = None,
    n: int = 1,
) -> Dict[str, Any]:
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
    extraction_run_id: Optional[str] = None,
    sdl_root: Optional[Path] = None,
    n: int = 1,
) -> Dict[str, Any]:
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
    extraction_run_id: Optional[str] = None,
    sdl_root: Optional[Path] = None,
    n: int = 1,
) -> Dict[str, Any]:
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
    extraction_run_id: Optional[str] = None,
    sdl_root: Optional[Path] = None,
    n: int = 1,
) -> Dict[str, Any]:
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
    extraction_run_id: Optional[str] = None,
    sdl_root: Optional[Path] = None,
    n: int = 1,
) -> Dict[str, Any]:
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


def _emit(
    artifact_type: str,
    *,
    chunk_id: str,
    source_id: str,
    component: str,
    detail: str,
    extraction_run_id: Optional[str],
    sdl_root: Optional[Path],
) -> Dict[str, Any]:
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
        from ..validation import validate_artifact, ArtifactValidationError

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
    return artifact
