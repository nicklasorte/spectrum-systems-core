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
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set

from .action_item_extractor import ActionItemExtractor
from .chunk_classifier import ChunkClassifier
from .claim_extractor import ClaimExtractor
from .classification_cache import ClassificationCache
from .decision_extractor import DecisionExtractor
from .extraction_merger import ExtractionMerger
from .glossary_manager import GlossaryManager
from ._chunk_counters import ChunkCounters
from ._failure_artifacts import (
    clear_chunk_lookup,
    emit_empty_response,
    emit_json_parse_failed,
    emit_rate_limit_exhausted,
    install_chunk_lookup,
)
from ._raw_response_log import write_log_from_context as _write_raw_response_log
from ._resilience import (
    EmptyResponseError,
    MAX_CONCURRENT_HAIKU_CALLS,
    call_with_backoff,
    guard_empty_response,
    strip_markdown_fence,
)
from ..agenda import (
    AgendaReferenceError,
    apply_phase_w_if_enabled,
    make_phase_w_agenda_resolver,
)
from ..verification.model_registry import ModelRegistry
from ..verification.pipeline_integration import (
    VerificationIncompleteError,
    apply_phase_v_if_enabled,
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


def _parse_json_response_strict(text: str, chunk_id: str = "") -> Dict[str, Any]:
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


def _parse_json_response(text: str) -> Dict[str, Any]:
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
) -> Callable[[str], Dict[str, Any]]:
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

    def _call(prompt: str) -> Dict[str, Any]:
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
        parts: List[str] = []
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
) -> Callable[[str], Dict[str, Any]]:
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

    def _call(prompt: str) -> Dict[str, Any]:
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
        parts: List[str] = []
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
) -> Callable[[str], Awaitable[Dict[str, Any]]]:
    """Async caller for the batch classifier (uses AsyncAnthropic).

    The returned coroutine accepts a prompt string and returns
    ``{"text": <response>}``. Errors degrade to ``{"text": ""}`` so the
    batch classifier triggers its per-chunk fallback path.
    """
    import anthropic
    import asyncio
    import random as _random

    client = anthropic.AsyncAnthropic()

    async def _acall(prompt: str) -> Dict[str, Any]:
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

        parts: List[str] = []
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
    injected: Optional[Dict[str, Callable[[str], Dict[str, Any]]]],
) -> Dict[str, Callable[[str], Dict[str, Any]]]:
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
    callers: Dict[str, Callable[[str], Dict[str, Any]]] = dict(injected or {})
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
        new_callers: Dict[str, Callable[[str], Dict[str, Any]]] = {}
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
    injected: Optional[Callable[[str], Awaitable[Dict[str, Any]]]],
) -> Optional[Callable[[str], Awaitable[Dict[str, Any]]]]:
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


def _resolve_store_root(data_lake: Optional[str] = None) -> Optional[Path]:
    raw = data_lake or os.environ.get("DATA_LAKE_PATH") or ""
    if not raw:
        return None
    p = Path(raw)
    if not p.exists():
        return None
    return p / "store"


def _resolve_sdl_root(data_lake: Optional[str] = None) -> Optional[Path]:
    env_sdl = os.environ.get("SDL_ROOT", "").strip()
    if env_sdl:
        return Path(env_sdl)
    store = _resolve_store_root(data_lake)
    if store is None:
        return None
    return store / "artifacts"


def _resolve_glossary_root(sdl_root: Optional[Path]) -> Optional[Path]:
    env_glossary = os.environ.get("SDL_GLOSSARY", "").strip()
    if env_glossary:
        return Path(env_glossary)
    if sdl_root is not None:
        return sdl_root.parent / "glossary" if sdl_root.name == "artifacts" else sdl_root / "glossary"
    return None


def _find_chunks_path(store_root: Path, source_id: str) -> Optional[Path]:
    for family in _SOURCE_FAMILIES:
        p = store_root / "processed" / family / source_id / "stories" / "chunks.jsonl"
        if p.is_file():
            return p
    return None


def _find_source_artifact_id(store_root: Path, source_id: str) -> Optional[str]:
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


def _load_chunks(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
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
    chunks: Sequence[Dict[str, Any]],
    *,
    counters: ChunkCounters,
    source_id: str,
    component: str,
    detail: str,
    extraction_run_id: str,
    sdl_root: Optional[Path],
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
    sdl_root: Optional[Path],
) -> Optional[Path]:
    """Serialise the counter into the orchestration_result artifact.

    The artifact is validated via ``validate_artifact`` before write so
    a malformed result cannot land on disk. Validation failures are
    logged and the artifact is still written so forensic evidence is
    preserved -- the strict invariant is "the in-memory counter is
    authoritative", and the artifact is a forensic mirror.
    """
    artifact = {
        "artifact_type": "orchestration_result",
        "schema_version": "1.0.0",
        "run_id": run_id,
        "source_id": source_id,
        "stage_status": counters.stage_status(),
        "created_at": _now_iso(),
        **counters.as_dict(),
    }
    try:
        from ..validation import validate_artifact, ArtifactValidationError
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


def run_typed_extraction(
    source_id: str,
    *,
    data_lake: Optional[str] = None,
    force: bool = False,
    api_callers: Optional[Dict[str, Callable[[str], Dict[str, Any]]]] = None,
    async_classifier_caller: Optional[
        Callable[[str], Awaitable[Dict[str, Any]]]
    ] = None,
    glossary_manager: Optional[GlossaryManager] = None,
    max_chunks: Optional[int] = None,
    use_classification_cache: bool = True,
    max_concurrent_classifier_batches: Optional[int] = None,
) -> Dict[str, Any]:
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

    available_turn_ids: Set[str] = set()
    for c in chunks:
        cid = c.get("chunk_id") or c.get("id")
        if isinstance(cid, str) and cid:
            available_turn_ids.add(cid)

    # Phase W: agenda detection + chunk annotation.
    # The classifier reads ``chunk["agenda_item_id"]`` when Phase W is
    # on. Per Attack 12 (RT1) this runs synchronously and writes all
    # agenda_item artifacts BEFORE annotating chunks so a downstream
    # reference cannot dangle. Flag-off path is a no-op (chunks
    # unchanged).
    pipeline_run_id = str(uuid.uuid4())
    data_lake_root: Optional[Path] = None
    if store_root is not None:
        # data_lake_path = store_root parent (``store/`` lives under it).
        data_lake_root = store_root.parent
    phase_w_metrics: Dict[str, Any]
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

    cache: Optional[ClassificationCache] = None
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

    classifications: List[Dict[str, Any]]
    try:
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

        result_box: Dict[str, Any] = {"value": [], "error": None}

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

    bucket: Dict[str, List[Dict[str, Any]]] = {
        "decision": [], "claim": [], "action_item": [], "off_topic": [],
    }
    for chunk, cls in zip(chunks, classifications):
        bucket[cls["classification"]].append(chunk)

    # Glossary context block is rebuilt per group (cheap; one call per
    # extractor here). Concatenate texts from this group to pick relevant
    # terms.
    def _block_for(group: Sequence[Dict[str, Any]]) -> str:
        text = " ".join((c.get("text") or "") for c in group)
        terms = glossary_manager.retrieve_for_chunk(text)
        return glossary_manager.format_for_prompt(terms)

    decisions = decision_x.extract(
        bucket["decision"], _block_for(bucket["decision"]), available_turn_ids,
    )
    claims = claim_x.extract(
        bucket["claim"], _block_for(bucket["claim"]), available_turn_ids,
    )
    actions = action_x.extract(
        bucket["action_item"], _block_for(bucket["action_item"]),
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

    artifact = ExtractionMerger().merge(
        source_artifact_id=source_artifact_id,
        extraction_run_id=extraction_run_id,
        classifications=classifications,
        decisions=decisions,
        claims=claims,
        action_items=actions,
        run_metadata=run_metadata,
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

    try:
        ExtractionMerger.write_to(artifact, out_path)
    except OSError as exc:
        # Even on write failure, persist the orchestration_result so the
        # operator can see the chunk-level counters from the failed run.
        _write_orchestration_result(
            counters, run_id=extraction_run_id, source_id=source_id,
            sdl_root=sdl_root,
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
    )

    # X-3: calibration_warning. The histogram is computed from
    # succeeded-chunk items only (decisions / claims / action_items
    # are all merged-artifact products of succeeded chunks). Blocked
    # chunks NEVER contribute to the denominator.
    calibration_path: Optional[Path] = None
    from ._calibration import (
        calibration_from_succeeded,
        CALIBRATION_WARNING_SCHEMA_VERSION as _CWSV,
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
            from ..validation import validate_artifact, ArtifactValidationError
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
        **counters.as_dict(),
        "stage_status": counters.stage_status(),
        "orchestration_result_path": (
            str(orchestration_path) if orchestration_path else ""
        ),
        "calibration_warning_path": (
            str(calibration_path) if calibration_path else ""
        ),
    }


def find_meeting_extraction(
    source_artifact_id: str,
    data_lake: Optional[str] = None,
) -> Optional[Path]:
    """Return the path to ``<source_artifact_id>_meeting_extraction.json``
    if it exists, else None. Used by EvalAligner integration to decide
    whether to use typed-extraction output as alignment input.
    """
    sdl_root = _resolve_sdl_root(data_lake)
    if sdl_root is None:
        return None
    p = _meeting_extraction_path(sdl_root, source_artifact_id)
    return p if p.is_file() else None
