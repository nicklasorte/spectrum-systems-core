"""Run the typed extraction pipeline for a single source.

Phase M3.0 + M3.1. Glue code that:
1. Loads chunks.jsonl for ``source_id`` from the processed tree.
2. Classifies each chunk via ``ChunkClassifier`` (with regulatory-verb
   fallback).
3. Routes classified chunks to the three typed extractors.
4. Merges results into a ``meeting_extraction`` artifact and writes
   it atomically under ``<SDL_ROOT>/extractions/``.

This module is invoked from both the CLI (``spectrum-core extract-typed``)
and ``PipelineOrchestrator.run_typed_extraction``. It is deliberately
side-effect-bounded: it reads chunks + glossary, writes one artifact, and
returns a summary dict. Failures degrade to a ``status="failure"`` dict
with a ``reason`` field; never raises.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from ..agenda import (
    AgendaReferenceError,
    apply_phase_w_if_enabled,
    make_phase_w_agenda_resolver,
)
from ..evals.source_turn_orphan import (
    aggregate_source_turn_reports as _aggregate_source_turn_reports,
)
from ..evals.source_turn_orphan import (
    compute_source_turn_report as _compute_source_turn_report,
)
from ..glossary.chunk_position import (
    POSITION_MIDDLE,
    attention_block_for_position,
)
from ..glossary.few_shot_loader import (
    build_few_shot_block,
    load_few_shot_examples,
)
from ..glossary.glossary_builder import load_versioned_glossary
from ..glossary.term_injector import (
    build_terminology_block,
    find_matching_terms,
)
from ..health.finding import HealthFinding
from ..verification.model_registry import ModelRegistry
from ..verification.pipeline_integration import (
    VerificationIncompleteError,
    apply_phase_v_if_enabled,
)
from ._chunk_counters import ChunkCounters
from ._failure_artifacts import (
    emit_rate_limit_exhausted,
    install_chunk_lookup,
)
from ._prompt_blocks import REGULATORY_TAXONOMY_BLOCK
from ._raw_response_log import write_log_from_context as _write_raw_response_log
from ._resilience import (
    MAX_CONCURRENT_HAIKU_CALLS,
    EmptyResponseError,
    call_with_backoff,
    guard_empty_response,
    strip_markdown_fence,
)
from .action_item_extractor import ActionItemExtractor
from .chunk_classifier import ChunkClassifier
from .chunk_metadata_gate import (
    format_report_for_log as _format_chunk_metadata_report,
)
from .chunk_metadata_gate import (
    validate_chunk_metadata as _validate_chunk_metadata,
)
from .claim_extractor import ClaimExtractor
from .classification_cache import ClassificationCache
from .decision_extractor import DecisionExtractor
from .extraction_merger import ExtractionMerger
from .generalization_checker import scan_items as _scan_overgeneralization
from .glossary_manager import GlossaryManager
from .population_rates import (
    RATE_WARN_THRESHOLD as _POPULATION_RATE_WARN_THRESHOLD,
)
from .population_rates import (
    below_threshold_fields as _below_pop_threshold_fields,
)
from .population_rates import (
    compute_population_rates as _compute_population_rates,
)

_LOG = logging.getLogger(__name__)

_SOURCE_FAMILIES = (
    "meetings", "books", "comments", "working_papers", "notes",
)

# Component keys for the typed-extraction pipeline. Order matches the four
# components constructed in ``run_typed_extraction``.
_COMPONENT_KEYS: tuple = ("classifier", "decision", "claim", "action_item")

# Token budget for default Anthropic callers. The classifier returns a tiny
# JSON object; the three extractors return small lists of items. 2000 covers
# both with headroom.
_DEFAULT_MAX_TOKENS = 2000

# Phase P3-A T-2: extraction-mode rollback. Setting
# EXTRACTION_MODE=single_pass skips the chunk-classification step and
# never writes a chunk_classifications artifact -- a regression triage
# can grep for the artifact's absence to confirm the rollback took
# effect on disk.
EXTRACTION_MODE_TWO_STAGE: str = "two_stage"
EXTRACTION_MODE_SINGLE_PASS: str = "single_pass"
_EXTRACTION_MODE_ENV: str = "EXTRACTION_MODE"
_VALID_EXTRACTION_MODES = frozenset(
    {EXTRACTION_MODE_TWO_STAGE, EXTRACTION_MODE_SINGLE_PASS}
)


def _resolve_extraction_mode() -> str:
    """Read ``EXTRACTION_MODE`` from env; default ``two_stage``.

    An unrecognised value falls back to ``two_stage`` and is logged so
    a typo never silently produces an unexpected single-pass run.
    """
    raw = os.environ.get(_EXTRACTION_MODE_ENV, "").strip().lower()
    if not raw:
        return EXTRACTION_MODE_TWO_STAGE
    if raw in _VALID_EXTRACTION_MODES:
        return raw
    _LOG.warning(
        "extraction_mode_invalid: %s=%r -> falling back to %s",
        _EXTRACTION_MODE_ENV, raw, EXTRACTION_MODE_TWO_STAGE,
    )
    return EXTRACTION_MODE_TWO_STAGE


# Phase P3-A T-3: glossary-version pinning. GLOSSARY_VERSION=latest reads
# the highest-numbered file in the glossary root; GLOSSARY_VERSION=<N>
# pins to ``spectrum_glossary_v<N>.json`` so a regression can be bisected
# against a prior glossary without editing the live glossary file.
_GLOSSARY_VERSION_ENV: str = "GLOSSARY_VERSION"
_GLOSSARY_VERSION_LATEST: str = "latest"
_GLOSSARY_FILENAME_RE = re.compile(r"^spectrum_glossary_v(?P<n>\d+)\.json$")


def _resolve_pinned_glossary_path(
    glossary_root: Path | None,
) -> Path | None:
    """Return the on-disk glossary artifact path the runner should load.

    Resolution order:
      1. ``GLOSSARY_VERSION=<integer>`` -> ``spectrum_glossary_v<N>.json``
         (file must exist; missing file falls back to ``latest`` with
         a warning so a typo doesn't silently load a different version).
      2. ``GLOSSARY_VERSION=latest`` or unset -> the highest-numbered
         file matching ``spectrum_glossary_v<N>.json`` in the root.
      3. None when the glossary_root is missing entirely.
    """
    if glossary_root is None or not glossary_root.is_dir():
        return None
    raw = os.environ.get(_GLOSSARY_VERSION_ENV, "").strip().lower()
    candidates: list[tuple] = []
    for path in glossary_root.iterdir():
        m = _GLOSSARY_FILENAME_RE.match(path.name)
        if m:
            candidates.append((int(m.group("n")), path))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    by_version = {n: p for n, p in candidates}
    latest_n, latest_path = candidates[-1]
    if not raw or raw == _GLOSSARY_VERSION_LATEST:
        return latest_path
    try:
        pinned = int(raw)
    except ValueError:
        _LOG.warning(
            "glossary_version_invalid: %s=%r -> falling back to latest (v%d)",
            _GLOSSARY_VERSION_ENV, raw, latest_n,
        )
        return latest_path
    if pinned in by_version:
        return by_version[pinned]
    _LOG.warning(
        "glossary_version_pinned_missing: %s=%d not on disk -> falling back to latest (v%d)",
        _GLOSSARY_VERSION_ENV, pinned, latest_n,
    )
    return latest_path


def _parse_json_response_strict(text: str, chunk_id: str = "") -> dict[str, Any]:
    """X-1 call order: guard_empty_response -> strip_markdown_fence -> json.loads.

    Raises:
      EmptyResponseError: text was empty / whitespace only.
      json.JSONDecodeError: text was non-empty but did not parse as JSON
        even after fence stripping. The caller is expected to emit the
        ``typed_extraction_llm_json_parse_failed`` failure artifact and
        bump the orchestrator's ``parse_error`` counter.
      TypeError: parsed value was not a JSON object (the runner only
        accepts ``dict``).

    No silent ``return {}`` -- the legacy behaviour swallowed every
    failure mode behind the same empty-dict and made it impossible
    for the orchestrator to count blocked chunks. Callers MUST handle
    the exceptions and emit the matching failure artifact.
    """
    raw = guard_empty_response(text, chunk_id)
    stripped = strip_markdown_fence(raw)
    if not stripped:
        # Strip-to-empty after a non-empty input means the model wrote
        # only a fence and no content. Treat as empty (X-1 fail-closed).
        raise EmptyResponseError(
            f"Markdown fence wrapped no JSON content for chunk {chunk_id}"
        )
    parsed = json.loads(stripped)
    if not isinstance(parsed, dict):
        raise TypeError(
            f"expected JSON object, got {type(parsed).__name__} "
            f"for chunk {chunk_id}"
        )
    return parsed


def _parse_json_response(text: str) -> dict[str, Any]:
    """Legacy tolerant parser. Returns ``{}`` on any failure.

    Retained for the existing ``_build_anthropic_caller`` callers and
    the offline test path. New code paths must use
    ``_parse_json_response_strict`` so the orchestrator can count
    blocked chunks per X-0 / X-1.

    Tolerates a narrative prefix / suffix around a JSON object by
    falling back to the outermost ``{...}`` span when strict parsing
    fails. The strict variant deliberately does NOT do this so the
    failure mode can be counted as ``parse_error``.
    """
    try:
        return _parse_json_response_strict(text)
    except EmptyResponseError:
        return {}
    except (TypeError, json.JSONDecodeError):
        pass

    # Narrative-prefix fallback (legacy tolerance).
    stripped = (text or "").strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if 0 <= start < end:
        try:
            parsed = json.loads(stripped[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except (TypeError, json.JSONDecodeError):
            pass
    _LOG.warning(
        "typed_extraction_llm_json_parse_failed: head=%r",
        (text or "")[:200],
    )
    return {}


def _build_anthropic_caller(
    model: str, max_tokens: int = _DEFAULT_MAX_TOKENS
) -> Callable[[str], dict[str, Any]]:
    """Build a Haiku api_caller returning the parsed JSON response.

    The returned callable hands the prompt to ``anthropic.Anthropic()``, joins
    text blocks from the response, and parses JSON. On network/SDK error or
    JSON-parse failure it logs a warning and returns ``{}`` so the caller's
    existing ``isinstance(resp, dict)`` guard yields an empty result instead
    of raising.

    Lazy import of ``anthropic`` so tests + offline runs that never invoke
    this helper do not require the SDK at import time.
    """
    import anthropic

    client = anthropic.Anthropic()

    def _call(prompt: str) -> dict[str, Any]:
        # X-0 part A: every Haiku call goes through call_with_backoff so
        # a transient RateLimitError does not silently produce {} (and
        # hence zero items, which X-1's empty-items gate would then
        # have to catch downstream).
        def _send() -> Any:
            return client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )

        try:
            message = call_with_backoff(_send)
        except anthropic.RateLimitError as exc:
            # Backoff exhausted -- re-raise so the runner emits the
            # api_rate_limit_exhausted failure artifact and bumps the
            # rate_limit_exhausted counter. Do NOT swallow.
            _LOG.warning(
                "typed_extraction_rate_limit_exhausted: %s",
                exc,
            )
            raise
        except Exception as exc:  # noqa: BLE001 - SDK error surface
            _LOG.warning(
                "typed_extraction_llm_call_failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            return {}
        parts: list[str] = []
        for block in getattr(message, "content", []) or []:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        raw_text = "\n".join(parts)
        # Phase O.1: optional raw-response log. Disabled-mode is a
        # single boolean read at module load, so the hot path stays
        # free when RAW_RESPONSE_LOG_ENABLED is false.
        _write_raw_response_log(raw_text, model_override=model)
        return _parse_json_response(raw_text)

    return _call


def _build_anthropic_batch_classifier_caller(
    model: str, max_tokens: int = 400,
) -> Callable[[str], dict[str, Any]]:
    """Sync caller for the batch classifier.

    Differs from ``_build_anthropic_caller`` in two ways:
    1. Returns ``{"text": <raw response>}`` (no JSON parsing) -- the
       batch response is line-delimited, not JSON.
    2. Uses a smaller ``max_tokens`` because the batch response is
       always a few lines per chunk (one classification per line).

    Falls back to ``{"text": ""}`` on any error so the classifier's
    fallback path triggers and per-chunk classify() runs.
    """
    import anthropic

    client = anthropic.Anthropic()

    def _call(prompt: str) -> dict[str, Any]:
        def _send() -> Any:
            return client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )

        try:
            message = call_with_backoff(_send)
        except anthropic.RateLimitError as exc:
            _LOG.warning(
                "typed_extraction_batch_classifier_rate_limit_exhausted: %s",
                exc,
            )
            raise
        except Exception as exc:
            _LOG.warning(
                "typed_extraction_batch_classifier_call_failed: %s: %s",
                type(exc).__name__, exc,
            )
            return {"text": ""}
        parts: list[str] = []
        for block in getattr(message, "content", []) or []:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        raw_text = "\n".join(parts)
        _write_raw_response_log(
            raw_text, model_override=model, call_type_override="classifier",
        )
        return {"text": raw_text}

    return _call


def _build_anthropic_async_batch_classifier_caller(
    model: str, max_tokens: int = 400,
) -> Callable[[str], Awaitable[dict[str, Any]]]:
    """Async caller for the batch classifier (uses AsyncAnthropic).

    The returned coroutine accepts a prompt string and returns
    ``{"text": <response>}``. Errors degrade to ``{"text": ""}`` so the
    batch classifier triggers its per-chunk fallback path.
    """
    import asyncio
    import random as _random

    import anthropic

    client = anthropic.AsyncAnthropic()

    async def _acall(prompt: str) -> dict[str, Any]:
        # X-0 part A: async equivalent of call_with_backoff. Retry on
        # RateLimitError only; non-rate-limit exceptions surface on the
        # first try. After max_retries the exception re-raises so the
        # runner can emit the rate-limit failure artifact.
        last_exc: Exception | None = None
        from ._resilience import MAX_RETRIES as _MAX_RETRIES
        for attempt in range(_MAX_RETRIES):
            try:
                message = await client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=0,
                    messages=[{"role": "user", "content": prompt}],
                )
                break
            except anthropic.RateLimitError as exc:
                last_exc = exc
                if attempt == _MAX_RETRIES - 1:
                    _LOG.warning(
                        "typed_extraction_async_batch_classifier_rate_limit_exhausted: %s",
                        exc,
                    )
                    raise
                wait = (2 ** attempt) + _random.uniform(0, 1)
                await asyncio.sleep(wait)
            except Exception as exc:
                _LOG.warning(
                    "typed_extraction_async_batch_classifier_call_failed: %s: %s",
                    type(exc).__name__, exc,
                )
                return {"text": ""}
        else:  # pragma: no cover - the for/else triggers only if loop exits cleanly
            assert last_exc is not None
            raise last_exc

        parts: list[str] = []
        for block in getattr(message, "content", []) or []:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        raw_text = "\n".join(parts)
        _write_raw_response_log(
            raw_text, model_override=model, call_type_override="classifier",
        )
        return {"text": raw_text}

    return _acall


def _resolve_api_callers(
    injected: dict[str, Callable[[str], dict[str, Any]]] | None,
) -> dict[str, Callable[[str], dict[str, Any]]]:
    """Return a callers dict with real Haiku callers filling missing keys.

    - Injected callers are preserved (so unit tests are unaffected).
    - Missing keys are lazy-built against ``ChunkClassifier.MODEL_ID``
      (all four components share the same Haiku model id).
    - The ``classifier`` key gets a *batch* caller that returns the raw
      response text (not parsed JSON) -- ``ChunkClassifier.batch_classify``
      expects ``{"text": ...}``.
    - If ``ANTHROPIC_API_KEY`` is unset or the SDK is missing, the missing
      keys are left absent and each component falls back to its offline
      ``_default_api_caller``. A warning is logged so the run records the
      degraded mode.
    """
    callers: dict[str, Callable[[str], dict[str, Any]]] = dict(injected or {})
    missing = [k for k in _COMPONENT_KEYS if k not in callers]
    if not missing:
        return callers
    if not os.environ.get("ANTHROPIC_API_KEY"):
        _LOG.warning(
            "typed_extraction_offline_mode: ANTHROPIC_API_KEY unset; "
            "components %s will classify every chunk as off_topic",
            missing,
        )
        return callers
    # Build per-component callers. ``_build_anthropic_caller`` is invoked
    # for every non-classifier missing key, matching the legacy behaviour.
    # The classifier gets a batch caller (raw text response) instead.
    # An ImportError from ANY builder causes us to drop the partially
    # built dict and return only the injected callers, preserving the
    # "SDK missing -> all components offline" invariant.
    try:
        new_callers: dict[str, Callable[[str], dict[str, Any]]] = {}
        # X-0: never hardcode the model id. ModelRegistry.get("extraction")
        # is the single source of truth -- callers pin via
        # ``<SDL_ROOT>/config/model_registry.json`` and missing config
        # falls back to the documented default (claude-haiku-4-5-*).
        extraction_model = _resolve_extraction_model_id()
        for key in missing:
            if key == "classifier":
                new_callers[key] = _build_anthropic_batch_classifier_caller(
                    extraction_model
                )
            else:
                new_callers[key] = _build_anthropic_caller(
                    extraction_model
                )
    except ImportError as exc:
        _LOG.warning(
            "typed_extraction_anthropic_sdk_missing: %s -- "
            "continuing with offline defaults",
            exc,
        )
        return callers
    callers.update(new_callers)
    return callers


def _resolve_extraction_model_id() -> str:
    """Resolve the extraction model id via ``ModelRegistry.get("extraction")``.

    Falls back to ``ChunkClassifier.MODEL_ID`` only if the registry
    raises (which it should not -- ``ModelRegistry`` has a built-in
    default). The fallback exists so a misconfigured SDL_ROOT cannot
    take the whole extraction stack down.
    """
    try:
        sdl_root_env = os.environ.get("SDL_ROOT", "").strip() or None
        return ModelRegistry(sdl_root_env).get("extraction")["model"]
    except Exception as exc:  # noqa: BLE001 - belt + braces
        _LOG.warning(
            "typed_extraction_model_registry_unreadable: %s -> "
            "falling back to ChunkClassifier.MODEL_ID",
            exc,
        )
        return ChunkClassifier.MODEL_ID


def _resolve_async_classifier_caller(
    injected: Callable[[str], Awaitable[dict[str, Any]]] | None,
) -> Callable[[str], Awaitable[dict[str, Any]]] | None:
    """Build an async classifier caller for the batch path.

    Returns None when ``ANTHROPIC_API_KEY`` is unset or the anthropic SDK
    is missing. In that case the runner falls back to the synchronous
    classifier path (which itself falls back to per-chunk and ultimately
    the offline ``off_topic`` default).
    """
    if injected is not None:
        return injected
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        return _build_anthropic_async_batch_classifier_caller(
            _resolve_extraction_model_id()
        )
    except ImportError as exc:
        _LOG.warning(
            "typed_extraction_async_anthropic_sdk_missing: %s -- "
            "falling back to sync classifier path",
            exc,
        )
        return None


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _resolve_store_root(data_lake: str | None = None) -> Path | None:
    raw = data_lake or os.environ.get("DATA_LAKE_PATH") or ""
    if not raw:
        return None
    p = Path(raw)
    if not p.exists():
        return None
    return p / "store"


def _resolve_sdl_root(data_lake: str | None = None) -> Path | None:
    env_sdl = os.environ.get("SDL_ROOT", "").strip()
    if env_sdl:
        return Path(env_sdl)
    store = _resolve_store_root(data_lake)
    if store is None:
        return None
    return store / "artifacts"


def _resolve_glossary_root(sdl_root: Path | None) -> Path | None:
    env_glossary = os.environ.get("SDL_GLOSSARY", "").strip()
    if env_glossary:
        return Path(env_glossary)
    if sdl_root is not None:
        return sdl_root.parent / "glossary" if sdl_root.name == "artifacts" else sdl_root / "glossary"
    return None


def _find_chunks_path(store_root: Path, source_id: str) -> Path | None:
    for family in _SOURCE_FAMILIES:
        p = store_root / "processed" / family / source_id / "stories" / "chunks.jsonl"
        if p.is_file():
            return p
    return None


def _find_source_artifact_id(store_root: Path, source_id: str) -> str | None:
    for family in _SOURCE_FAMILIES:
        sr_path = store_root / "processed" / family / source_id / "source_record.json"
        if sr_path.is_file():
            try:
                data = json.loads(sr_path.read_text(encoding="utf-8"))
                aid = data.get("artifact_id")
                if isinstance(aid, str) and aid:
                    return aid
            except (OSError, json.JSONDecodeError):
                return None
    return None


def _load_chunks(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def _meeting_extraction_path(sdl_root: Path, source_artifact_id: str) -> Path:
    return sdl_root / "extractions" / f"{source_artifact_id}_meeting_extraction.json"


def _chunk_classifications_path(sdl_root: Path, source_artifact_id: str) -> Path:
    return (
        sdl_root / "extractions" /
        f"{source_artifact_id}_chunk_classifications.json"
    )


def _build_chunk_classifications_artifact(
    *,
    source_artifact_id: str,
    source_id: str,
    extraction_run_id: str,
    extraction_mode: str,
    classifications: Sequence[dict[str, Any]],
    extraction_path_breakdown: dict[str, int],
    off_topic_rate: float,
    router_model: str,
) -> dict[str, Any]:
    """Build the Phase P3-A T-2 aggregate artifact.

    The artifact is forensic provenance for the routing step. It is
    NOT consumed by the eval gate (the gate reads the same rates off
    the meeting_extraction artifact); the aggregate exists so an
    operator triaging a coverage regression can inspect every
    classification call without crawling individual chunk records.
    """
    per_chunk: list[dict[str, Any]] = []
    for cls in classifications or []:
        if not isinstance(cls, dict):
            continue
        per_chunk.append({
            "chunk_id": str(cls.get("chunk_id") or ""),
            "classification": cls.get("classification") or "off_topic",
            "confidence": cls.get("confidence"),
            "regulatory_verb_fallback_applied": bool(
                cls.get("regulatory_verb_fallback_applied")
            ),
        })
    return {
        "artifact_type": "chunk_classifications",
        "schema_version": "1.0.0",
        "source_artifact_id": source_artifact_id,
        "source_id": source_id,
        "extraction_run_id": extraction_run_id,
        "created_at": _now_iso(),
        "extraction_mode": extraction_mode,
        "chunk_count": len(per_chunk),
        "classifications": per_chunk,
        "extraction_path_breakdown": dict(extraction_path_breakdown),
        "off_topic_skip_count": int(extraction_path_breakdown.get("off_topic", 0)),
        "off_topic_rate": float(off_topic_rate),
        "router_model": router_model or ChunkClassifier.MODEL_ID,
    }


def _write_chunk_classifications_artifact(
    artifact: dict[str, Any],
    sdl_root: Path,
    source_artifact_id: str,
) -> Path | None:
    """Atomic write the chunk_classifications artifact. Returns path or None.

    Validation failures are logged but do not abort the run -- the
    artifact is provenance, not a contract product. The runner
    itself never reads the file back during the same process.
    """
    try:
        from ..validation import ArtifactValidationError, validate_artifact
        try:
            validate_artifact(artifact, "chunk_classifications")
        except ArtifactValidationError as exc:
            _LOG.warning(
                "chunk_classifications_schema_violation: %s", exc,
            )
    except ImportError:
        pass
    target = _chunk_classifications_path(sdl_root, source_artifact_id)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(
            json.dumps(artifact, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(target)
        return target
    except OSError as exc:
        _LOG.warning("chunk_classifications_write_failed: %s", exc)
        return None


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Best-effort RateLimitError detection without forcing an SDK import."""
    try:
        import anthropic  # noqa: WPS433 -- runtime check
        return isinstance(exc, anthropic.RateLimitError)
    except ImportError:
        # No SDK installed; nothing to compare against. Fall back to the
        # class name so we still catch rate-limit-shaped errors raised
        # in tests via stubs.
        return type(exc).__name__ == "RateLimitError"


def _emit_rate_limit_for_all(
    chunks: Sequence[dict[str, Any]],
    *,
    counters: ChunkCounters,
    source_id: str,
    component: str,
    detail: str,
    extraction_run_id: str,
    sdl_root: Path | None,
) -> None:
    """Emit one api_rate_limit_exhausted artifact per chunk that was
    submitted but blocked, and bump ``rate_limit_exhausted`` accordingly.

    Used when the classifier batch path exhausts retries and the runner
    cannot tell which subset of chunks were affected -- the whole
    submitted batch is counted as blocked, which is the conservative
    fail-closed choice (RT1 finding: blocked chunks must NEVER be
    silently treated as ``off_topic``).
    """
    for chunk in chunks:
        cid = ""
        if isinstance(chunk, dict):
            cid = str(chunk.get("chunk_id") or chunk.get("id") or "")
        emit_rate_limit_exhausted(
            counters,
            chunk_id=cid,
            source_id=source_id,
            component=component,
            detail=detail,
            extraction_run_id=extraction_run_id,
            sdl_root=sdl_root,
        )


def _orchestration_result_path(sdl_root: Path, run_id: str) -> Path:
    return sdl_root / "orchestration" / f"{run_id}_extraction.json"


def _write_orchestration_result(
    counters: ChunkCounters,
    *,
    run_id: str,
    source_id: str,
    sdl_root: Path | None,
    phase_w_extras: dict[str, Any] | None = None,
) -> Path | None:
    """Serialise the counter into the orchestration_result artifact.

    The artifact is validated via ``validate_artifact`` before write so
    a malformed result cannot land on disk. Validation failures are
    logged and the artifact is still written so forensic evidence is
    preserved -- the strict invariant is "the in-memory counter is
    authoritative", and the artifact is a forensic mirror.

    ``phase_w_extras`` (Phase W integration wiring) carries the
    optional ``glossary_injection_summary``, ``binding_tuple_call_count``,
    and ``scope_overgeneralization_count`` fields. They are
    additive -- existing artifacts written before Phase W remain
    schema-valid because the new properties are declared optional in
    ``orchestration_result.schema.json``.
    """
    artifact: dict[str, Any] = {
        "artifact_type": "orchestration_result",
        "schema_version": "1.0.0",
        "run_id": run_id,
        "source_id": source_id,
        "stage_status": counters.stage_status(),
        "created_at": _now_iso(),
        **counters.as_dict(),
    }
    if phase_w_extras:
        for key, value in phase_w_extras.items():
            if value is None:
                continue
            artifact[key] = value
    try:
        from ..validation import ArtifactValidationError, validate_artifact
        try:
            validate_artifact(artifact, "orchestration_result")
        except ArtifactValidationError as exc:
            _LOG.warning(
                "orchestration_result_schema_violation: %s", exc,
            )
    except ImportError:
        pass

    if sdl_root is None:
        return None
    target = _orchestration_result_path(sdl_root, run_id)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(artifact, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return target
    except OSError as exc:
        _LOG.warning("orchestration_result_write_failed: %s", exc)
        return None


def _spurious_add_count_from_verification(
    verification_result: dict[str, Any] | None,
) -> int:
    """Phase Z.4: integer count of merged items the post-hoc verifier
    judged ``unsupported`` or ``contradicted``.

    This is the count behind the verification summary's
    ``spurious_add_rate`` (post_hoc_verifier._compute_summary:
    ``spurious_add_rate = (unsupported + contradicted) / total``). It is
    surfaced on orchestration_result so a fabricated-claim run is visible
    as a hard count without a downstream consumer re-deriving the rate.
    It is read from the EXISTING verification summary — it does not
    re-measure anything.

    Returns 0 when ``verification_result`` is None (Phase V disabled —
    no verifier ran, so no spurious add was *detected*; 0 is the honest
    value, not a silent skip) or when the summary is missing/malformed.
    The synthetic regression test proves the value goes > 0 when the
    verifier marks an item unsupported, so this is never always-zero.
    """
    if not isinstance(verification_result, dict):
        return 0
    summary = verification_result.get("summary")
    if not isinstance(summary, dict):
        return 0
    total = 0
    for key in ("unsupported_count", "contradicted_count"):
        value = summary.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            total += value
    return total


# -- Phase W (integration wiring) helpers -----------------------------------
#
# These helpers wire the Phase T/V modules (versioned glossary,
# chunk-position attention, V.3 few-shot, generalization checker,
# orchestration counters) into the live runner. They are intentionally
# small and pure: the runner composes them at call sites so the
# wiring is auditable in one place. The integration smoke test
# (``tests/integration/test_extraction_runner_wiring.py``) calls
# ``build_extraction_prompt`` directly to assert block ordering
# without having to invoke an extractor.


_PHASE_W_GLOSSARY_NOT_PRELOADED_LOG_EMITTED: bool = False


def _resolve_versioned_glossary_root(sdl_root: Path | None) -> Path | None:
    """Locate the versioned-glossary artifact directory.

    The versioned glossary (``spectrum_glossary_v1.json``) ships as an
    artifact under ``<sdl_root>/glossary/`` -- NOT under the legacy
    per-term ``<store_root>/glossary/`` location that
    ``_resolve_glossary_root`` returns for ``GlossaryManager``. The
    two locations are independent. An explicit ``SDL_VERSIONED_GLOSSARY``
    env var (or its alias ``SDL_GLOSSARY_V1``) wins when set so smoke
    tests can point at a synthetic glossary without polluting the
    legacy path.
    """
    for env_name in ("SDL_VERSIONED_GLOSSARY", "SDL_GLOSSARY_V1"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return Path(value)
    if sdl_root is None:
        return None
    return sdl_root / "glossary"


def _resolve_versioned_glossary_terms(
    sdl_root: Path | None,
) -> list[dict[str, Any]]:
    """Load the versioned glossary terms list. Returns ``[]`` if absent.

    Backward-compatible wrapper around
    :func:`_resolve_versioned_glossary_artifact`. The function never
    raises; a missing or malformed artifact returns an empty list and a
    single debug-level log line per process. The runner's
    ``glossary_terms_injected`` field will then be ``[]`` on every
    chunk -- still a list shape, never None -- so downstream consumers
    do not have to special-case the unloaded path.
    """
    artifact = _resolve_versioned_glossary_artifact(sdl_root)
    if not isinstance(artifact, dict):
        return []
    terms = artifact.get("terms")
    if not isinstance(terms, list):
        return []
    return [t for t in terms if isinstance(t, dict)]


def _resolve_versioned_glossary_artifact(
    sdl_root: Path | None,
) -> dict[str, Any] | None:
    """Load the versioned glossary artifact (or None when absent).

    Honours ``GLOSSARY_VERSION=latest|<N>`` per Phase P3-A T-3. The
    function reads the env once per call so a test can override the
    pinned version without reimporting the module.
    """
    global _PHASE_W_GLOSSARY_NOT_PRELOADED_LOG_EMITTED
    if sdl_root is None:
        return None
    glossary_root = _resolve_versioned_glossary_root(sdl_root)
    if glossary_root is None or not glossary_root.is_dir():
        if not _PHASE_W_GLOSSARY_NOT_PRELOADED_LOG_EMITTED:
            _LOG.debug(
                "glossary_not_preloaded: versioned glossary root missing "
                "(sdl_root=%s); extraction continues with empty injection",
                sdl_root,
            )
            _PHASE_W_GLOSSARY_NOT_PRELOADED_LOG_EMITTED = True
        return None
    # Phase P3-A T-3: resolve the pinned version path. When a specific
    # version is requested but not present on disk, the resolver falls
    # back to the latest and logs a warning so the operator notices.
    pinned_path = _resolve_pinned_glossary_path(glossary_root)
    if pinned_path is not None and pinned_path.is_file():
        try:
            return json.loads(pinned_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.warning(
                "versioned_glossary_pinned_load_failed: path=%s err=%s",
                pinned_path, exc,
            )
            # Fall through to the legacy loader so a corrupted pinned
            # file does not break the run.
    return load_versioned_glossary(glossary_root)


def _per_chunk_glossary_records(
    chunks: Sequence[dict[str, Any]],
    glossary_terms: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build per-chunk records of glossary term injections + position.

    Returns a list (one entry per chunk) of dicts with keys
    ``chunk_id``, ``chunk_position`` and ``glossary_terms_injected``.
    ``glossary_terms_injected`` is always a list (never None) so the
    field shape is stable regardless of whether the glossary was
    loaded. Term entries carry ``term_id`` (stable UUID) so historical
    comparison is not broken by term-text edits.
    """
    out: list[dict[str, Any]] = []
    for c in chunks:
        if not isinstance(c, dict):
            continue
        text = c.get("text") or ""
        matched = find_matching_terms(text, glossary_terms)
        out.append({
            "chunk_id": str(c.get("chunk_id") or c.get("id") or ""),
            "chunk_position": c.get("chunk_position") or "",
            "glossary_terms_injected": [
                str(t.get("term_id")) for t in matched if t.get("term_id")
            ],
        })
    return out


def _phase_w_block_for_group(
    group_chunks: Sequence[dict[str, Any]],
    glossary_terms: Sequence[dict[str, Any]],
    few_shot_block_str: str,
    legacy_glossary_block: str,
) -> str:
    """Compose the Phase W prompt blocks for one extractor group.

    Block order (canonical):
      1. TERMINOLOGY FOR THIS SECTION (V.2, union of matched terms across the group)
      2. ATTENTION DIRECTION (V.4, when any chunk in the group is `middle`)
      3. FEW-SHOT EXAMPLES (V.3, when one or more verified examples were loaded)
      4. Legacy GlossaryManager block (when versioned glossary is absent and the
         caller still has a non-empty legacy block; mutually exclusive with V.2
         to avoid duplicate ``TERMINOLOGY FOR THIS SECTION`` headers)

    The result is passed as the ``glossary_block`` argument to the
    extractors so no extractor signature changes. The legacy block is
    a fallback only -- when V.2 produces a non-empty terminology
    block, the legacy GlossaryManager output is suppressed.
    """
    union_terms: list[dict[str, Any]] = []
    seen_term_ids: set[str] = set()
    any_middle = False
    for c in group_chunks:
        if not isinstance(c, dict):
            continue
        if c.get("chunk_position") == POSITION_MIDDLE:
            any_middle = True
        matched = find_matching_terms(c.get("text") or "", glossary_terms)
        for t in matched:
            tid = str(t.get("term_id") or "")
            if not tid or tid in seen_term_ids:
                continue
            seen_term_ids.add(tid)
            union_terms.append(t)

    terminology_block = build_terminology_block(union_terms)
    attention_block = (
        attention_block_for_position(POSITION_MIDDLE) if any_middle else ""
    )

    parts: list[str] = []
    if terminology_block:
        parts.append(terminology_block)
    elif legacy_glossary_block:
        # Phase W: when the versioned glossary contributed zero terms,
        # fall back to the legacy GlossaryManager block so we do not
        # regress on transcripts that still rely on the per-term
        # files in `<glossary_root>/<slug>.json`.
        parts.append(legacy_glossary_block)
    if attention_block:
        parts.append(attention_block)
    if few_shot_block_str:
        parts.append(few_shot_block_str)
    return "\n\n".join(parts)


def build_extraction_prompt(
    chunk_text: str,
    extraction_type: str = "decision",
    *,
    terminology_block: str = "",
    attention_block: str = "",
    few_shot_block: str = "",
    glossary_block: str = "",
) -> str:
    """Compose the canonical Phase W extraction-prompt block order.

    The runner's group-level prompt is built INSIDE each extractor
    (``DecisionExtractor._build_prompt`` and friends). This helper
    mirrors the same block order on a SINGLE chunk so the W.6
    integration smoke test can inspect prompt content without
    invoking the LLM. Block order:

      1. Role / extraction-type instruction (always present)
      2. REGULATORY TAXONOMY BLOCK (Phase T.1, always present)
      3. TERMINOLOGY FOR THIS SECTION (Phase V.2, when non-empty)
      4. ATTENTION DIRECTION (Phase V.4, when non-empty)
      5. FEW-SHOT EXAMPLES (Phase V.3, when non-empty)
      6. Legacy glossary block (when non-empty -- typically suppressed
         because the versioned glossary block (3) replaces it)
      7. CHUNK content (always present)

    A missing block is omitted entirely (no header, no separator).
    Tests can therefore assert ``"ATTENTION DIRECTION" not in
    opening_prompt`` when the attention block is empty.
    """
    parts: list[str] = [
        f"Extract {extraction_type.upper()} items from the following chunk.",
        REGULATORY_TAXONOMY_BLOCK,
    ]
    if terminology_block:
        parts.append(terminology_block)
    if attention_block:
        parts.append(attention_block)
    if few_shot_block:
        parts.append(few_shot_block)
    if glossary_block:
        parts.append(glossary_block)
    parts.append(f"CHUNK:\n{chunk_text}")
    return "\n\n".join(parts)


def _glossary_injection_summary_from_records(
    records: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Compute the ``glossary_injection_summary`` shape.

    Scans the per-chunk records produced by
    ``_per_chunk_glossary_records``. Records missing
    ``glossary_terms_injected`` entirely contribute to
    ``stale_records_count`` -- this only happens when a caller passes
    a partially-built record list, never in the live runner path.
    """
    chunks_with_matches = 0
    chunks_without_field = 0
    all_term_ids: list[str] = []
    for r in records or []:
        if not isinstance(r, dict):
            continue
        injected = r.get("glossary_terms_injected")
        if injected is None:
            chunks_without_field += 1
            continue
        if len(injected) > 0:
            chunks_with_matches += 1
            all_term_ids.extend(str(t) for t in injected)
    from collections import Counter
    most = [tid for tid, _ in Counter(all_term_ids).most_common(5)]
    total = len(records or [])
    no_match = total - chunks_with_matches - chunks_without_field
    return {
        "chunks_with_matches": chunks_with_matches,
        "chunks_with_no_matches": max(0, no_match),
        "total_term_injections": len(all_term_ids),
        "most_injected_terms": most,
        "stale_records_count": chunks_without_field,
    }


def _run_overgeneralization_scan(
    decisions: Sequence[dict[str, Any]],
    claims: Sequence[dict[str, Any]],
    chunks_by_id: dict[str, dict[str, Any]],
    *,
    pipeline_run_id: str,
) -> list[HealthFinding]:
    """Run the V.6 generalization checker on decisions + claims.

    The checker requires ``source_text`` to be the chunk text the item
    was extracted from (NOT the full transcript -- otherwise any chunk
    that names a band would taint every item). We attach the chunk
    text per-item under the ``__source_text`` key before delegating
    to ``generalization_checker.scan_items``.

    Returns a flat list of ``HealthFinding`` objects; an empty list
    when ``GENERALIZATION_CHECK_ENABLED=false`` or no item triggered.
    """

    def _attach_source(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            sources = item.get("source_turn_ids") or []
            chunk_text_parts: list[str] = []
            for sid in sources:
                src = chunks_by_id.get(str(sid))
                if isinstance(src, dict):
                    text = src.get("text") or ""
                    if text:
                        chunk_text_parts.append(text)
            enriched = dict(item)
            enriched["__source_text"] = "\n".join(chunk_text_parts)
            out.append(enriched)
        return out

    decisions_enriched = _attach_source(decisions)
    claims_enriched = _attach_source(claims)

    findings: list[HealthFinding] = []
    findings.extend(
        _scan_overgeneralization(
            decisions_enriched,
            source_text_key="__source_text",
            extracted_text_key="decision_text",
            pipeline_run_id=pipeline_run_id,
        )
    )
    findings.extend(
        _scan_overgeneralization(
            claims_enriched,
            source_text_key="__source_text",
            extracted_text_key="claim_text",
            pipeline_run_id=pipeline_run_id,
        )
    )
    return findings


def run_typed_extraction(
    source_id: str,
    *,
    data_lake: str | None = None,
    force: bool = False,
    api_callers: dict[str, Callable[[str], dict[str, Any]]] | None = None,
    async_classifier_caller: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    glossary_manager: GlossaryManager | None = None,
    max_chunks: int | None = None,
    use_classification_cache: bool = True,
    max_concurrent_classifier_batches: int | None = None,
) -> dict[str, Any]:
    """Run the typed-extraction pipeline for one source_id.

    Returns ``{"status": "success"|"skipped"|"failure", ...}``.
    Never raises.

    ``max_chunks`` limits classification + extraction to the first N chunks
    of ``chunks.jsonl``. Used by the PR smoke test to bound API calls.
    """
    if not source_id:
        return {"status": "failure", "reason": "source_id_required"}

    store_root = _resolve_store_root(data_lake)
    if store_root is None:
        return {"status": "failure", "reason": "data_lake_not_found"}

    chunks_path = _find_chunks_path(store_root, source_id)
    if chunks_path is None:
        return {
            "status": "failure",
            "reason": f"chunks_jsonl_not_found:source_id={source_id}",
        }

    source_artifact_id = _find_source_artifact_id(store_root, source_id)
    if not source_artifact_id:
        # No source_record artifact_id found; fabricate a deterministic id
        # so the output path is still stable. Track this in the reason so
        # downstream tools can flag it.
        source_artifact_id = str(uuid.UUID(bytes=(source_id + "x" * 16).encode("utf-8")[:16]))

    sdl_root = _resolve_sdl_root(data_lake)
    if sdl_root is None:
        return {"status": "failure", "reason": "sdl_root_not_found"}

    out_path = _meeting_extraction_path(sdl_root, source_artifact_id)
    if out_path.exists() and not force:
        return {
            "status": "skipped",
            "reason": "meeting_extraction_exists",
            "path": str(out_path),
        }

    chunks = _load_chunks(chunks_path)
    if not chunks:
        return {
            "status": "failure",
            "reason": f"chunks_jsonl_empty:source_id={source_id}",
        }

    if max_chunks is not None and max_chunks >= 0 and len(chunks) > max_chunks:
        _LOG.info(
            "smoke_test_mode: classifying first %d of %d chunks",
            max_chunks,
            len(chunks),
        )
        print(
            f"smoke_test_mode: classifying first {max_chunks} of {len(chunks)} chunks",
            flush=True,
        )
        chunks = chunks[:max_chunks]

    # Phase O.2: install the chunk lookup table once. Every failure
    # artifact emitted during this run will resolve chunk_id -> chunk
    # text in O(1) without re-reading chunks.jsonl. The table is
    # cleared at the end of the run so subsequent runs cannot see
    # stale chunk data.
    install_chunk_lookup(chunks)

    available_turn_ids: set[str] = set()
    for c in chunks:
        cid = c.get("chunk_id") or c.get("id")
        if isinstance(cid, str) and cid:
            available_turn_ids.add(cid)

    # Phase P3-A T-1: chunk metadata contract gate. Default mode is
    # graceful-degradation -- the report is logged and continues. Set
    # STRICT_CHUNK_METADATA=true to promote to halt so a CI run cannot
    # silently process chunks missing metadata. Findings always flow
    # into ``phase_w_findings`` so an operator sees them in the
    # post-run report regardless of mode.
    chunk_metadata_report = _validate_chunk_metadata(chunks)
    if chunk_metadata_report.has_violations():
        _LOG.warning(_format_chunk_metadata_report(chunk_metadata_report))
        if chunk_metadata_report.strict_mode:
            return {
                "status": "failure",
                "reason": (
                    "chunk_metadata_contract_violation:strict_mode="
                    f"{len(chunk_metadata_report.findings)}_violations"
                ),
                "chunk_metadata_findings": chunk_metadata_report.as_strings()[:25],
            }

    # Phase W: agenda detection + chunk annotation.
    # The classifier reads ``chunk["agenda_item_id"]`` when Phase W is
    # on. Per Attack 12 (RT1) this runs synchronously and writes all
    # agenda_item artifacts BEFORE annotating chunks so a downstream
    # reference cannot dangle. Flag-off path is a no-op (chunks
    # unchanged).
    pipeline_run_id = str(uuid.uuid4())
    data_lake_root: Path | None = None
    if store_root is not None:
        # data_lake_path = store_root parent (``store/`` lives under it).
        data_lake_root = store_root.parent
    phase_w_metrics: dict[str, Any]
    if data_lake_root is None or sdl_root is None:
        phase_w_metrics = {
            "agenda_detection_attempted": False,
            "agenda_detection_succeeded": False,
            "agenda_items_detected_count": 0,
            "detection_method": "disabled",
            "detection_duration_seconds": 0.0,
            "detector_model_used": "",
        }
    else:
        try:
            phase_w_metrics = apply_phase_w_if_enabled(
                chunks,
                source_id=source_id,
                data_lake_path=data_lake_root,
                sdl_root=sdl_root,
                pipeline_run_id=pipeline_run_id,
                api_caller=(api_callers or {}).get("agenda"),
            )
        except AgendaReferenceError as exc:
            _LOG.error("phase_w_pre_flight_failed: %s", exc)
            return {
                "status": "failure",
                "reason": f"agenda_reference_error:{exc}",
            }

    # Glossary
    if glossary_manager is None:
        glossary_root = _resolve_glossary_root(sdl_root)
        glossary_manager = GlossaryManager(
            str(glossary_root) if glossary_root else None
        )

    api_callers = _resolve_api_callers(api_callers)
    agenda_resolver = None
    if data_lake_root is not None and sdl_root is not None:
        agenda_resolver = make_phase_w_agenda_resolver(
            data_lake_root, sdl_root, source_id,
        )
    classifier = ChunkClassifier(
        api_caller=api_callers.get("classifier"),
        agenda_resolver=agenda_resolver,
    )
    decision_x = DecisionExtractor(api_caller=api_callers.get("decision"))
    claim_x = ClaimExtractor(api_caller=api_callers.get("claim"))
    action_x = ActionItemExtractor(api_caller=api_callers.get("action_item"))

    cache: ClassificationCache | None = None
    if use_classification_cache and sdl_root is not None:
        try:
            cache = ClassificationCache(str(sdl_root))
            cache.load(source_id)
        except Exception as exc:  # pragma: no cover -- never raise out
            _LOG.warning(
                "typed_extraction_classification_cache_init_failed: %s",
                exc,
            )
            cache = None

    async_caller = _resolve_async_classifier_caller(async_classifier_caller)

    # X-0 part C: ``MAX_CONCURRENT_HAIKU_CALLS`` is the per-process
    # concurrency cap. Explicit kwarg still wins so smoke tests can
    # crank it down further, but the default is the constant -- never
    # a hardcoded literal.
    if max_concurrent_classifier_batches is None:
        max_concurrent_classifier_batches = MAX_CONCURRENT_HAIKU_CALLS

    # X-0 part D: track every chunk submitted vs blocked. Counter
    # is updated on every non-success exit path (rate limit, empty
    # response, parse error, zero items). At the end of the run the
    # totals are serialised into the orchestration_result artifact.
    counters = ChunkCounters()
    counters.record_attempt(len(chunks))

    extraction_run_id = "tex-" + uuid.uuid4().hex[:16]

    # Phase P3-A T-2: EXTRACTION_MODE=single_pass rollback. In
    # single-pass mode the classifier is skipped entirely (no router
    # call, no off_topic filtering); every chunk is fed to every
    # extractor. We still synthesise a classifications list so the
    # downstream merge path (which counts off_topic / regulatory-verb
    # fallback) stays single-shape. Marker classification ``decision``
    # is used because every extractor will see the chunk regardless.
    _early_extraction_mode = _resolve_extraction_mode()
    _skip_classifier = _early_extraction_mode == EXTRACTION_MODE_SINGLE_PASS
    classifications: list[dict[str, Any]] = []
    if _skip_classifier:
        _LOG.info(
            "extraction_mode_single_pass: skipping classifier (rollback path)"
        )
        for chunk in chunks:
            cid = ""
            if isinstance(chunk, dict):
                cid = str(chunk.get("chunk_id") or chunk.get("id") or "")
            classifications.append({
                "classification_id": str(uuid.uuid4()),
                "chunk_id": cid,
                "source_id": source_id,
                "classification": "decision",
                "regulatory_verb_fallback_applied": False,
                "confidence": None,
                "artifact_type": "chunk_classification",
                "schema_version": ChunkClassifier.SCHEMA_VERSION,
                "created_at": _now_iso(),
                "provenance": {
                    "produced_by": "ChunkClassifier",
                    "model": "single_pass_rollback",
                },
            })
    try:
        if _skip_classifier:
            # No classifier call to make; the synthesised list above is
            # the single source of truth.
            pass
        else:
            classifications = asyncio.run(
                classifier.batch_classify_async(
                    chunks,
                    source_id,
                    max_concurrent=max_concurrent_classifier_batches,
                    async_caller=async_caller,
                    cache=cache,
                )
            )
    except RuntimeError as exc:
        # Already inside an event loop (e.g. Jupyter or PipelineOrchestrator
        # being driven from another async context). Run on a fresh loop in
        # a worker thread instead.
        _LOG.info(
            "typed_extraction_async_loop_in_use: %s -- using thread-bound loop",
            exc,
        )
        import threading

        result_box: dict[str, Any] = {"value": [], "error": None}

        def _runner() -> None:
            loop = asyncio.new_event_loop()
            try:
                result_box["value"] = loop.run_until_complete(
                    classifier.batch_classify_async(
                        chunks,
                        source_id,
                        max_concurrent=max_concurrent_classifier_batches,
                        async_caller=async_caller,
                        cache=cache,
                    )
                )
            except BaseException as inner_exc:  # noqa: BLE001
                result_box["error"] = inner_exc
            finally:
                loop.close()

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join()
        if result_box["error"] is not None:
            inner = result_box["error"]
            if _is_rate_limit_error(inner):
                _emit_rate_limit_for_all(
                    chunks,
                    counters=counters,
                    source_id=source_id,
                    component="chunk_classifier",
                    detail=str(inner),
                    extraction_run_id=extraction_run_id,
                    sdl_root=sdl_root,
                )
                _write_orchestration_result(
                    counters,
                    run_id=extraction_run_id,
                    source_id=source_id,
                    sdl_root=sdl_root,
                )
                return {
                    "status": "failure",
                    "reason": "api_rate_limit_exhausted:classifier",
                    **counters.as_dict(),
                    "stage_status": counters.stage_status(),
                }
            classifications = []
        else:
            classifications = result_box.get("value", []) or []  # type: ignore[assignment]
    except Exception as exc:  # noqa: BLE001 - never escape
        if _is_rate_limit_error(exc):
            _emit_rate_limit_for_all(
                chunks,
                counters=counters,
                source_id=source_id,
                component="chunk_classifier",
                detail=str(exc),
                extraction_run_id=extraction_run_id,
                sdl_root=sdl_root,
            )
            _write_orchestration_result(
                counters,
                run_id=extraction_run_id,
                source_id=source_id,
                sdl_root=sdl_root,
            )
            return {
                "status": "failure",
                "reason": "api_rate_limit_exhausted:classifier",
                **counters.as_dict(),
                "stage_status": counters.stage_status(),
            }
        raise

    # Any chunks the classifier could not classify in time (because of
    # an upstream rate limit / empty response that the runner caught at
    # the API layer) come back as "off_topic" by default. The runner
    # cannot tell those apart from real off_topic chunks here, so the
    # counter is bumped from the failure-artifact path inside the API
    # callers themselves. Successful classifications count as
    # ``chunks_succeeded`` so the orchestrator's "✓" actually means
    # every chunk produced *some* outcome.
    counters.record_success(len(classifications))

    if cache is not None:
        cache.save(source_id)

    bucket: dict[str, list[dict[str, Any]]] = {
        "decision": [], "claim": [], "action_item": [], "off_topic": [],
    }
    if _skip_classifier:
        # Phase P3-A T-2 rollback: in single_pass mode every chunk is
        # routed to every extractor. The synthetic classifications
        # carry classification="decision" so the buckets above would
        # otherwise route claim/action work past the claim/action
        # extractors. Override by duplicating each chunk into each
        # bucket.
        for chunk in chunks:
            bucket["decision"].append(chunk)
            bucket["claim"].append(chunk)
            bucket["action_item"].append(chunk)
    else:
        for chunk, cls in zip(chunks, classifications):
            bucket[cls["classification"]].append(chunk)

    # Phase W (integration wiring): load the versioned glossary once
    # per run + the Phase V.3 few-shot examples once per run. Both
    # are pure functions returning lists; a missing or malformed
    # artifact returns an empty list and the run continues with no
    # injection. Per-chunk records are built across ALL chunks
    # (including off_topic) so the orchestration summary reflects
    # actual glossary coverage, not just extractor-routed coverage.
    glossary_artifact = _resolve_versioned_glossary_artifact(sdl_root)
    versioned_glossary_terms: list[dict[str, Any]] = []
    glossary_version: int | None = None
    if isinstance(glossary_artifact, dict):
        terms = glossary_artifact.get("terms")
        if isinstance(terms, list):
            versioned_glossary_terms = [t for t in terms if isinstance(t, dict)]
        raw_version = glossary_artifact.get("glossary_version")
        if isinstance(raw_version, int):
            glossary_version = raw_version
        elif isinstance(raw_version, str):
            try:
                glossary_version = int(raw_version)
            except ValueError:
                glossary_version = None
    chunk_extraction_records = _per_chunk_glossary_records(
        chunks, versioned_glossary_terms,
    )

    few_shot_result = load_few_shot_examples(sdl_root)
    few_shot_block_str = build_few_shot_block(few_shot_result.examples)
    run_findings: list[HealthFinding] = []
    if few_shot_result.finding_code:
        try:
            run_findings.append(
                HealthFinding(
                    finding_code=few_shot_result.finding_code,
                    severity=few_shot_result.severity or "info",
                    context={
                        "artifact_present": few_shot_result.artifact_present,
                    },
                    remediation=few_shot_result.remediation,
                    pipeline_run_id=extraction_run_id,
                )
            )
        except ValueError as exc:  # pragma: no cover -- defensive
            _LOG.warning("phase_w_few_shot_finding_invalid: %s", exc)

    # Glossary context block is rebuilt per group. The Phase W
    # composite block combines (1) the V.2 versioned-glossary
    # terminology block, (2) the V.4 attention-direction block when
    # any chunk in the group is `middle`, (3) the V.3 few-shot
    # examples block. The legacy ``GlossaryManager`` block is kept
    # as a fallback for when the versioned glossary is unavailable;
    # the two never both contribute (they share the
    # ``TERMINOLOGY FOR THIS SECTION`` header).
    def _legacy_block_for(group: Sequence[dict[str, Any]]) -> str:
        text = " ".join((c.get("text") or "") for c in group)
        terms = glossary_manager.retrieve_for_chunk(text)
        return glossary_manager.format_for_prompt(terms)

    def _block_for(
        group: Sequence[dict[str, Any]],
        *,
        extraction_type: str,
    ) -> str:
        legacy = _legacy_block_for(group)
        # Phase V.3 few-shot examples ship a `decision_examples_v1`
        # artifact only. Claims and action items receive no examples
        # until separate artifacts are authored. Passing "" here keeps
        # the block omitted entirely (no header, no separator).
        few_shot = few_shot_block_str if extraction_type == "decision" else ""
        return _phase_w_block_for_group(
            group, versioned_glossary_terms, few_shot, legacy,
        )

    decisions = decision_x.extract(
        bucket["decision"],
        _block_for(bucket["decision"], extraction_type="decision"),
        available_turn_ids,
    )
    claims = claim_x.extract(
        bucket["claim"],
        _block_for(bucket["claim"], extraction_type="claim"),
        available_turn_ids,
    )
    actions = action_x.extract(
        bucket["action_item"],
        _block_for(bucket["action_item"], extraction_type="action_item"),
        available_turn_ids,
    )

    # Carry per-extractor run metadata (few-shot status, low-confidence
    # counts) into the merged artifact so the run is self-describing.
    run_metadata = [
        decision_x.last_run_metadata,
        claim_x.last_run_metadata,
        action_x.last_run_metadata,
    ]

    # X-1: minimum items_count gate. An extractor that returned zero
    # items is a successful API call but a substantively empty result.
    # Emit ``typed_extraction_empty_result`` so the orchestrator
    # counter records it as ``other`` (blocked), but DO continue and
    # write the merged meeting_extraction artifact -- a transcript can
    # legitimately have zero decisions if it was mostly off-topic.
    # The block_reason bump signals "this run did not produce useful
    # extraction content" without erasing the routing/coverage data
    # the merged artifact still carries.
    from ._failure_artifacts import emit_empty_result
    if not decisions and not claims and not actions:
        emit_empty_result(
            counters,
            chunk_id="",
            source_id=source_id,
            component="typed_extraction_runner",
            detail="zero items after extraction",
            extraction_run_id=extraction_run_id,
            sdl_root=sdl_root,
        )

    # Phase P3-A T-2: resolve extraction mode and capture routing
    # breakdown. The two-stage path is the live wiring; single_pass is
    # the documented rollback that skips classification and writes no
    # chunk_classifications artifact. Re-use the value the runner
    # resolved before kicking off classification so the env is read
    # exactly once per run.
    extraction_mode = _early_extraction_mode
    extraction_path_breakdown = {
        "decision": len(bucket["decision"]),
        "claim": len(bucket["claim"]),
        "action_item": len(bucket["action_item"]),
        "off_topic": len(bucket["off_topic"]),
    }
    classified_total = sum(extraction_path_breakdown.values())
    off_topic_rate = (
        extraction_path_breakdown["off_topic"] / classified_total
        if classified_total > 0
        else 0.0
    )

    # Phase P3-A T-1: orphan / diversity over the combined item list.
    # Per-type reports are computed separately so the rollup
    # (`by_type`) is also surfaced for diagnostic reading; the
    # top-level orphan_rate and diversity_rate come from the combined
    # call so a regression on one type cannot be masked by another.
    decision_report = _compute_source_turn_report(
        decisions, available_turn_ids, item_type="decision",
    )
    claim_report = _compute_source_turn_report(
        claims, available_turn_ids, item_type="claim",
    )
    action_report = _compute_source_turn_report(
        actions, available_turn_ids, item_type="action_item",
    )
    combined_report = _compute_source_turn_report(
        list(decisions) + list(claims) + list(actions),
        available_turn_ids,
        item_type="combined",
    )
    source_turn_orphan_rate = combined_report.orphan_rate
    source_turn_diversity_rate = combined_report.diversity_rate
    source_turn_summary = _aggregate_source_turn_reports(
        [decision_report, claim_report, action_report]
    )
    source_turn_summary["diversity_rate"] = source_turn_diversity_rate
    source_turn_summary["distinct_turns_cited"] = (
        combined_report.distinct_turns_cited
    )
    source_turn_summary["available_turn_count"] = (
        combined_report.available_turn_count
    )

    # Phase P3-A T-3: population rates for the extended-schema fields.
    # Emit a single warn finding listing every field below threshold
    # so a fully-degraded run does not produce one finding per field.
    population_rates = _compute_population_rates(decisions, claims)
    below_threshold = _below_pop_threshold_fields(population_rates)

    artifact = ExtractionMerger().merge(
        source_artifact_id=source_artifact_id,
        extraction_run_id=extraction_run_id,
        classifications=classifications,
        decisions=decisions,
        claims=claims,
        action_items=actions,
        run_metadata=run_metadata,
        p3a_fields={
            "extraction_mode": extraction_mode,
            "glossary_version": glossary_version,
            "off_topic_rate": off_topic_rate,
            "extraction_path_breakdown": extraction_path_breakdown,
            "source_turn_orphan_rate": source_turn_orphan_rate,
            "source_turn_diversity_rate": source_turn_diversity_rate,
            "stakeholders_populated_rate": population_rates.stakeholders_populated_rate,
            "rationale_populated_rate": population_rates.rationale_populated_rate,
            "claim_type_populated_rate": population_rates.claim_type_populated_rate,
        },
    )

    # Phase V: if the post-hoc verification flag is on, run the verifier,
    # annotate each item with verification_status, write a
    # source_verification_result artifact, and fail-closed on incomplete
    # coverage. When disabled, this returns None and the legacy v1.1.0
    # write path runs unchanged.
    #
    # The flag check needs SOME data_lake path -- so we resolve from the
    # kwarg OR the DATA_LAKE_PATH env var so callers that drive the
    # runner via env (PipelineOrchestrator, smoke tests, CI) cannot
    # silently bypass Phase V (RT1 Sev-1 fix).
    verification_result = None
    resolved_data_lake = data_lake or os.environ.get("DATA_LAKE_PATH") or ""
    if resolved_data_lake:
        try:
            chunks_by_id = {
                c.get("chunk_id") or c.get("id"): c
                for c in chunks
                if (c.get("chunk_id") or c.get("id"))
            }
            verification_result = apply_phase_v_if_enabled(
                artifact,
                chunks_by_id,
                data_lake_path=resolved_data_lake,
                sdl_root=sdl_root,
                pipeline_run_id=extraction_run_id,
                api_caller=(api_callers or {}).get("verifier"),
            )
        except VerificationIncompleteError as exc:
            _LOG.error("phase_v_verification_incomplete: %s", exc)
            return {
                "status": "failure",
                "reason": f"verification_incomplete:{exc}",
            }
        except Exception as exc:  # pragma: no cover -- never escape
            _LOG.exception("phase_v_unexpected_error: %s", exc)
            return {
                "status": "failure",
                "reason": f"verification_error:{type(exc).__name__}:{exc}",
            }

    # Phase W (integration wiring): run the V.6 generalization checker
    # on decisions + claims using the EXTRACTING CHUNK text as the
    # source -- never the full transcript, otherwise a band reference
    # anywhere in the transcript would taint every item. The checker
    # is gated by ``GENERALIZATION_CHECK_ENABLED`` (default on); when
    # disabled the call returns an empty list and the counter is 0.
    chunks_by_id_for_scan: dict[str, dict[str, Any]] = {}
    for c in chunks:
        cid = c.get("chunk_id") or c.get("id")
        if isinstance(cid, str) and cid:
            chunks_by_id_for_scan[cid] = c
    overgen_findings = _run_overgeneralization_scan(
        artifact.get("decisions") or [],
        artifact.get("claims") or [],
        chunks_by_id_for_scan,
        pipeline_run_id=extraction_run_id,
    )
    run_findings.extend(overgen_findings)

    # Phase P3-A T-1: surface the chunk-metadata gate findings into
    # the unified run_findings stream so they land in the run output
    # next to the overgeneralization / few-shot findings.
    if chunk_metadata_report.has_violations():
        try:
            run_findings.append(
                HealthFinding(
                    finding_code="chunk_metadata_contract_violation",
                    severity="warn",
                    context={
                        "violations": len(chunk_metadata_report.findings),
                        "chunks_scanned": chunk_metadata_report.chunks_scanned,
                        "per_field": chunk_metadata_report.per_field_violation_counts(),
                        "strict_mode": chunk_metadata_report.strict_mode,
                        "sample": chunk_metadata_report.as_strings()[:5],
                    },
                    remediation=(
                        "Re-run the chunker; if the chunker writes the field "
                        "but it was None, investigate the speaker-label "
                        "detector or AGENDA_DETECTION_ENABLED state."
                    ),
                    pipeline_run_id=extraction_run_id,
                )
            )
        except ValueError as exc:  # pragma: no cover -- defensive
            _LOG.warning("chunk_metadata_finding_invalid: %s", exc)

    # Phase P3-A T-1: surface the source-turn orphan and diversity
    # findings so a regression on grounding is visible in the run
    # output. orphan_rate > 0 emits warn; diversity_rate < 0.1 emits
    # info (over-citing a tiny chunk cluster).
    if source_turn_orphan_rate > 0:
        try:
            run_findings.append(
                HealthFinding(
                    finding_code="source_turn_orphan_detected",
                    severity="warn",
                    context={
                        "orphan_rate": source_turn_orphan_rate,
                        "by_type": source_turn_summary["by_type"],
                    },
                    remediation=(
                        "Inspect the orphaned items' source_turn_ids; "
                        "rerun extraction with force=true after fixing the "
                        "prompt or chunker drift."
                    ),
                    pipeline_run_id=extraction_run_id,
                )
            )
        except ValueError as exc:  # pragma: no cover
            _LOG.warning("source_turn_orphan_finding_invalid: %s", exc)
    if (
        combined_report.available_turn_count > 0
        and source_turn_diversity_rate < 0.1
        and (len(decisions) + len(claims) + len(actions)) > 0
    ):
        try:
            run_findings.append(
                HealthFinding(
                    finding_code="source_turn_low_diversity",
                    severity="info",
                    context={
                        "diversity_rate": source_turn_diversity_rate,
                        "distinct_turns_cited": combined_report.distinct_turns_cited,
                        "available_turn_count": combined_report.available_turn_count,
                    },
                    remediation=(
                        "The model is over-citing a tiny chunk cluster; "
                        "review whether prompt routing is collapsing too many "
                        "items onto the same chunk_id."
                    ),
                    pipeline_run_id=extraction_run_id,
                )
            )
        except ValueError as exc:  # pragma: no cover
            _LOG.warning("source_turn_low_diversity_finding_invalid: %s", exc)

    # Phase P3-A T-3: population rate finding. Fail-OPEN: warn but
    # never halt. One finding per run that lists all under-threshold
    # fields so the operator sees the full picture in a single line.
    if below_threshold:
        try:
            run_findings.append(
                HealthFinding(
                    finding_code="low_field_population_rate",
                    severity="warn",
                    context={
                        "threshold": _POPULATION_RATE_WARN_THRESHOLD,
                        "below_threshold": below_threshold,
                        "decisions_total": population_rates.decisions_total,
                        "claims_total": population_rates.claims_total,
                    },
                    remediation=(
                        "Tune the extraction prompt to require the named "
                        "field on every extracted item; re-run extraction "
                        "with force=true."
                    ),
                    pipeline_run_id=extraction_run_id,
                )
            )
        except ValueError as exc:  # pragma: no cover
            _LOG.warning("low_field_population_rate_finding_invalid: %s", exc)

    # Phase P3-A T-2: write the chunk_classifications aggregate
    # artifact. Skipped in single_pass mode -- the absence of the
    # file on disk is the rollback signal.
    chunk_classifications_path: Path | None = None
    if extraction_mode == EXTRACTION_MODE_TWO_STAGE and sdl_root is not None:
        cc_artifact = _build_chunk_classifications_artifact(
            source_artifact_id=source_artifact_id,
            source_id=source_id,
            extraction_run_id=extraction_run_id,
            extraction_mode=extraction_mode,
            classifications=classifications,
            extraction_path_breakdown=extraction_path_breakdown,
            off_topic_rate=off_topic_rate,
            router_model=_resolve_extraction_model_id(),
        )
        chunk_classifications_path = _write_chunk_classifications_artifact(
            cc_artifact, sdl_root, source_artifact_id,
        )

    # Phase W (integration wiring): orchestration counters. The
    # binding-tuple count is derived from the merged artifact (the
    # extractor sets ``binding_tuple`` to a dict when the V.5 flag is
    # on and to None when off), so the count is 0 by design unless
    # ``BINDING_TUPLE_ENABLED=true``.
    binding_tuple_call_count = sum(
        1
        for d in (artifact.get("decisions") or [])
        if isinstance(d, dict) and isinstance(d.get("binding_tuple"), dict)
    )
    glossary_injection_summary = _glossary_injection_summary_from_records(
        chunk_extraction_records,
    )
    phase_w_extras = {
        "glossary_injection_summary": glossary_injection_summary,
        "binding_tuple_call_count": binding_tuple_call_count,
        "scope_overgeneralization_count": len(overgen_findings),
        # Phase Z.4: always an int (never None) so _write_orchestration_result
        # does not skip it via the None-filter — the metric is present on
        # every completed run, 0 when Phase V is off or all items verified.
        # verification_result is computed above (apply_phase_v_if_enabled);
        # both _write_orchestration_result call sites receive this dict.
        "spurious_add_count": _spurious_add_count_from_verification(
            verification_result
        ),
    }

    try:
        ExtractionMerger.write_to(artifact, out_path)
    except OSError as exc:
        # Even on write failure, persist the orchestration_result so the
        # operator can see the chunk-level counters from the failed run.
        _write_orchestration_result(
            counters, run_id=extraction_run_id, source_id=source_id,
            sdl_root=sdl_root, phase_w_extras=phase_w_extras,
        )
        return {
            "status": "failure",
            "reason": f"write_error:{exc}",
            **counters.as_dict(),
            "stage_status": counters.stage_status(),
        }

    # X-0 part D: orchestration_result artifact is mandatory on every
    # run -- this is where the "✓ / partial / failed" stage rollup is
    # serialised. Written AFTER the meeting_extraction so the artifact
    # always reflects the final, post-extraction counter state.
    orchestration_path = _write_orchestration_result(
        counters,
        run_id=extraction_run_id,
        source_id=source_id,
        sdl_root=sdl_root,
        phase_w_extras=phase_w_extras,
    )

    # X-3: calibration_warning. The histogram is computed from
    # succeeded-chunk items only (decisions / claims / action_items
    # are all merged-artifact products of succeeded chunks). Blocked
    # chunks NEVER contribute to the denominator.
    calibration_path: Path | None = None
    from ._calibration import (
        calibration_from_succeeded,
    )
    calibration = calibration_from_succeeded(
        artifact.get("decisions") or [],
        artifact.get("claims") or [],
        artifact.get("action_items") or [],
        counters=counters,
        run_id=extraction_run_id,
    )
    if calibration is not None:
        try:
            from ..validation import ArtifactValidationError, validate_artifact
            try:
                validate_artifact(calibration, "calibration_warning")
            except ArtifactValidationError as exc:
                _LOG.warning(
                    "calibration_warning_schema_violation: %s", exc,
                )
        except ImportError:
            pass
        if sdl_root is not None:
            try:
                target_dir = sdl_root / "calibration"
                target_dir.mkdir(parents=True, exist_ok=True)
                calibration_path = (
                    target_dir / f"{extraction_run_id}_calibration_warning.json"
                )
                calibration_path.write_text(
                    json.dumps(calibration, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                _LOG.warning(
                    "calibration_warning_write_failed: %s", exc,
                )

    return {
        "status": "success",
        "source_id": source_id,
        "source_artifact_id": source_artifact_id,
        "path": str(out_path),
        "decisions": len(artifact["decisions"]),
        "claims": len(artifact["claims"]),
        "action_items": len(artifact["action_items"]),
        "total_chunks_classified": artifact["total_chunks_classified"],
        "off_topic_count": artifact["off_topic_count"],
        "regulatory_verb_fallback_count": artifact["regulatory_verb_fallback_count"],
        "routing_quality_warning": artifact["routing_quality_warning"],
        "requires_human_dedup_count": artifact["requires_human_dedup_count"],
        "extraction_run_id": extraction_run_id,
        "few_shot_injected": artifact["few_shot_injected"],
        "few_shot_example_count": artifact["few_shot_example_count"],
        "low_confidence_item_count": artifact["low_confidence_item_count"],
        "phase_w": phase_w_metrics,
        # Phase W (integration wiring) -- additive, never overwrites
        # the existing ``phase_w`` agenda-detection metrics above. The
        # integration smoke test asserts on these fields:
        "chunk_extraction_records": chunk_extraction_records,
        "glossary_injection_summary": glossary_injection_summary,
        "binding_tuple_call_count": binding_tuple_call_count,
        "scope_overgeneralization_count": len(overgen_findings),
        "phase_w_findings": [
            {
                "finding_code": f.finding_code,
                "severity": f.severity,
                "context": dict(f.context),
                "remediation": f.remediation,
            }
            for f in run_findings
        ],
        **counters.as_dict(),
        "stage_status": counters.stage_status(),
        "orchestration_result_path": (
            str(orchestration_path) if orchestration_path else ""
        ),
        "calibration_warning_path": (
            str(calibration_path) if calibration_path else ""
        ),
        # Phase P3-A: rollup fields surfaced into the runner result so
        # the smoke test / CLI can read them without re-loading the
        # written artifact.
        "extraction_mode": extraction_mode,
        "glossary_version": glossary_version,
        "off_topic_rate": off_topic_rate,
        "extraction_path_breakdown": extraction_path_breakdown,
        "source_turn_orphan_rate": source_turn_orphan_rate,
        "source_turn_diversity_rate": source_turn_diversity_rate,
        "source_turn_summary": source_turn_summary,
        "stakeholders_populated_rate": population_rates.stakeholders_populated_rate,
        "rationale_populated_rate": population_rates.rationale_populated_rate,
        "claim_type_populated_rate": population_rates.claim_type_populated_rate,
        "chunk_classifications_path": (
            str(chunk_classifications_path) if chunk_classifications_path else ""
        ),
        "chunk_metadata_violations_count": len(
            chunk_metadata_report.findings
        ),
    }


def find_meeting_extraction(
    source_artifact_id: str,
    data_lake: str | None = None,
) -> Path | None:
    """Return the path to ``<source_artifact_id>_meeting_extraction.json``
    if it exists, else None. Used by EvalAligner integration to decide
    whether to use typed-extraction output as alignment input.
    """
    sdl_root = _resolve_sdl_root(data_lake)
    if sdl_root is None:
        return None
    p = _meeting_extraction_path(sdl_root, source_artifact_id)
    return p if p.is_file() else None
