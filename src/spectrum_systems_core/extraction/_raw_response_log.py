"""Phase O.1: raw API response logger.

When ``RAW_RESPONSE_LOG_ENABLED=true`` is set in the environment, every
model call site that opts in will, before parsing, classify the raw
response and persist a small ``raw_api_response_log`` artifact under
``<sdl_root>/debug/raw_responses/<source_id>/<chunk_id>_<call_type>.json``.

Cost when disabled is bounded to a single boolean read evaluated once
at module import (cached for the lifetime of the process). The
disabled path returns immediately without building any artifact dict.

The logger is a debug surface, not a forensic record: writes are
best-effort and silently skipped on OSError. Schema validation runs
through the central ``validate_artifact`` helper so a malformed log
artifact is logged but not raised.
"""
from __future__ import annotations

import contextlib
import contextvars
import datetime
import json
import logging
import os
from pathlib import Path
from typing import Iterator, Optional

_LOG = logging.getLogger(__name__)


RAW_RESPONSE_LOG_ENABLED_ENV: str = "RAW_RESPONSE_LOG_ENABLED"
RAW_RESPONSE_LOG_MAX_CHARS_ENV: str = "RAW_RESPONSE_LOG_MAX_CHARS"
_DEFAULT_MAX_CHARS: int = 2000

# Refusal markers checked only when the response is NOT valid JSON.
# Lower-cased; matched as substrings.
_REFUSAL_MARKERS: tuple = (
    "i cannot",
    "i'm unable",
    "i am unable",
    "i don't",
    "i do not",
)

ALLOWED_CALL_TYPES: tuple = (
    "extraction",
    "story",
    "two_stage_stage1",
    "two_stage_stage2",
    "classifier",
    "other",
)

ALLOWED_RESPONSE_TYPES: tuple = (
    "empty",
    "fence_only",
    "valid_json",
    "malformed_json",
    "refusal",
    "truncated",
)

SCHEMA_VERSION: str = "1.0.0"


def _read_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "")
    if raw is None:
        return default
    return raw.strip().lower() in {"true", "1", "yes", "on"}


def _read_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        v = int(raw.strip())
    except (TypeError, ValueError):
        return default
    if v < 0:
        return default
    return v


# Evaluated once at import. The env var read is cheap, but doing it
# per-call would add a syscall to the hot path. Tests can monkeypatch
# ``_RAW_RESPONSE_LOG_ENABLED`` directly to flip the bit without
# touching the environment.
_RAW_RESPONSE_LOG_ENABLED: bool = _read_bool(RAW_RESPONSE_LOG_ENABLED_ENV)
_RAW_RESPONSE_LOG_MAX_CHARS: int = _read_int(
    RAW_RESPONSE_LOG_MAX_CHARS_ENV, _DEFAULT_MAX_CHARS,
)


def is_enabled() -> bool:
    """Public accessor for the cached enable flag."""
    return _RAW_RESPONSE_LOG_ENABLED


def max_chars() -> int:
    """Public accessor for the cached max-chars truncation budget."""
    return _RAW_RESPONSE_LOG_MAX_CHARS


def reload_from_env() -> None:
    """Re-read the env vars. Tests use this when they overwrite the env
    after module import. Production code does not call this."""
    global _RAW_RESPONSE_LOG_ENABLED, _RAW_RESPONSE_LOG_MAX_CHARS
    _RAW_RESPONSE_LOG_ENABLED = _read_bool(RAW_RESPONSE_LOG_ENABLED_ENV)
    _RAW_RESPONSE_LOG_MAX_CHARS = _read_int(
        RAW_RESPONSE_LOG_MAX_CHARS_ENV, _DEFAULT_MAX_CHARS,
    )


# Per-call context. Extractors set it via ``call_context`` so the
# API caller wrapper can look up chunk_id / source_id / sdl_root
# without changing every api_caller signature.
_CALL_CONTEXT: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "raw_response_log_call_context", default=None,
)


@contextlib.contextmanager
def call_context(
    *,
    chunk_id: str = "",
    source_id: str = "",
    sdl_root: Optional[Path] = None,
    call_type: str = "extraction",
    model: str = "",
) -> Iterator[None]:
    """Push per-call context that ``write_log_from_context`` reads.

    The context is reset on exit even on exception, so a leaking caller
    cannot poison the next chunk's log.
    """
    token = _CALL_CONTEXT.set(
        {
            "chunk_id": chunk_id or "",
            "source_id": source_id or "",
            "sdl_root": sdl_root,
            "call_type": call_type or "other",
            "model": model or "",
        }
    )
    try:
        yield
    finally:
        _CALL_CONTEXT.reset(token)


def current_context() -> dict:
    """Return a shallow copy of the active context, or an empty dict."""
    ctx = _CALL_CONTEXT.get()
    return dict(ctx) if isinstance(ctx, dict) else {}


def write_log_from_context(
    raw: str,
    *,
    call_type_override: Optional[str] = None,
    model_override: Optional[str] = None,
) -> Optional[Path]:
    """Hot-path entry point used by api caller wrappers.

    Disabled-mode early-out happens here, before any context lookup.
    """
    if not _RAW_RESPONSE_LOG_ENABLED:
        return None
    ctx = current_context()
    return write_log(
        raw,
        chunk_id=ctx.get("chunk_id") or "",
        source_id=ctx.get("source_id") or "",
        model=model_override or ctx.get("model") or "",
        call_type=call_type_override or ctx.get("call_type") or "other",
        sdl_root=ctx.get("sdl_root"),
    )


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def classify_response(raw: object, max_chars_limit: Optional[int] = None) -> str:
    """Return one of ``ALLOWED_RESPONSE_TYPES`` for ``raw``.

    Classification order (the first rule that matches wins):

    1. ``len(raw) > max_chars`` -> ``truncated``.
    2. ``raw.strip() == ""``     -> ``empty``.
    3. Strip a leading ``` fence; if the stripped body is empty -> ``fence_only``.
    4. Parses as JSON object/array -> ``valid_json``.
    5. Looks like JSON but fails to parse, or starts with non-JSON char ->
       ``refusal`` if it contains a refusal marker, else ``malformed_json``.

    Valid JSON wins over refusal so a JSON response that happens to
    contain "I cannot" in a decision_text field is not misclassified.
    """
    limit = (
        max_chars_limit if isinstance(max_chars_limit, int) and max_chars_limit > 0
        else _RAW_RESPONSE_LOG_MAX_CHARS
    )
    if not isinstance(raw, str):
        raw = str(raw or "")

    if len(raw) > limit:
        return "truncated"

    stripped = raw.strip()
    if not stripped:
        return "empty"

    fence_stripped = stripped
    if fence_stripped.startswith("```"):
        if "\n" in fence_stripped:
            fence_stripped = fence_stripped.split("\n", 1)[1]
        else:
            fence_stripped = ""
        if fence_stripped.endswith("```"):
            fence_stripped = fence_stripped.rsplit("```", 1)[0]
        fence_stripped = fence_stripped.strip()
        if not fence_stripped:
            return "fence_only"

    if fence_stripped.startswith("{") or fence_stripped.startswith("["):
        try:
            json.loads(fence_stripped)
            return "valid_json"
        except (TypeError, ValueError):
            pass
        # Looks like JSON but failed to parse. Refusal markers only
        # checked here when the response is NOT valid JSON.
        low = stripped.lower()
        for marker in _REFUSAL_MARKERS:
            if marker in low:
                return "refusal"
        return "malformed_json"

    # Not obviously JSON. Check refusal markers.
    low = stripped.lower()
    for marker in _REFUSAL_MARKERS:
        if marker in low:
            return "refusal"
    return "malformed_json"


def build_log_artifact(
    raw: str,
    *,
    chunk_id: str,
    source_id: str,
    model: str,
    call_type: str,
    max_chars_limit: Optional[int] = None,
) -> dict:
    """Assemble the ``raw_api_response_log`` artifact dict."""
    if call_type not in ALLOWED_CALL_TYPES:
        call_type = "other"
    limit = (
        max_chars_limit if isinstance(max_chars_limit, int) and max_chars_limit > 0
        else _RAW_RESPONSE_LOG_MAX_CHARS
    )
    text = raw if isinstance(raw, str) else str(raw or "")
    preview = text[:limit]
    return {
        "artifact_type": "raw_api_response_log",
        "schema_version": SCHEMA_VERSION,
        "chunk_id": chunk_id or "",
        "source_id": source_id or "",
        "model": model or "",
        "call_type": call_type,
        "response_type": classify_response(text, max_chars_limit=limit),
        "raw_response_chars": len(text),
        "raw_response_preview": preview,
        "logged_at": _now_iso(),
    }


def _safe_filename_segment(value: str) -> str:
    """Strip path separators and trim a path segment to a reasonable length.

    The chunk_id and call_type form the filename. Untrusted inputs must
    not be able to escape the directory.
    """
    cleaned = (value or "").replace("/", "_").replace("\\", "_").strip()
    return cleaned[:200] or "unknown"


def write_log(
    raw: str,
    *,
    chunk_id: str,
    source_id: str,
    model: str,
    call_type: str,
    sdl_root: Optional[Path] = None,
) -> Optional[Path]:
    """Persist a raw_api_response_log artifact when logging is enabled.

    Returns the write path on success, ``None`` otherwise. Disabled-mode
    short-circuits at the very first check so the hot path stays free.
    """
    if not _RAW_RESPONSE_LOG_ENABLED:
        return None
    artifact = build_log_artifact(
        raw,
        chunk_id=chunk_id,
        source_id=source_id,
        model=model,
        call_type=call_type,
    )

    # Lightweight existence check: validate before writing so a broken
    # schema is caught in tests, but never raise -- this is a debug
    # surface, not a fail-closed gate.
    try:
        from ..validation import (
            ArtifactValidationError,
            SchemaNotFoundError,
            validate_artifact,
        )

        try:
            validate_artifact(artifact, "raw_api_response_log")
        except (ArtifactValidationError, SchemaNotFoundError) as exc:
            _LOG.warning("raw_api_response_log_schema_violation: %s", exc)
    except ImportError:
        pass

    if sdl_root is None:
        return None

    safe_source = _safe_filename_segment(source_id)
    safe_chunk = _safe_filename_segment(chunk_id)
    safe_call = _safe_filename_segment(call_type)
    target_dir = Path(sdl_root) / "debug" / "raw_responses" / safe_source
    target = target_dir / f"{safe_chunk}_{safe_call}.json"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(artifact, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return target
    except OSError as exc:
        _LOG.warning("raw_api_response_log_write_failed: %s", exc)
        return None


__all__ = [
    "ALLOWED_CALL_TYPES",
    "ALLOWED_RESPONSE_TYPES",
    "RAW_RESPONSE_LOG_ENABLED_ENV",
    "RAW_RESPONSE_LOG_MAX_CHARS_ENV",
    "SCHEMA_VERSION",
    "build_log_artifact",
    "call_context",
    "classify_response",
    "current_context",
    "is_enabled",
    "max_chars",
    "reload_from_env",
    "write_log",
    "write_log_from_context",
]
