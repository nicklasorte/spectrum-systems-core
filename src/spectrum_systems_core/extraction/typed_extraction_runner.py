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


def _parse_json_response(text: str) -> Dict[str, Any]:
    """Parse a model JSON response into a dict.

    Tolerates markdown code fences (```json ... ```) and a leading or
    trailing narrative line by extracting the outermost ``{ ... }`` span.
    Returns ``{}`` on any failure so each component's existing defensive
    paths produce empty results instead of raising.
    """
    if not isinstance(text, str) or not text.strip():
        return {}
    candidates: List[str] = [text]
    stripped = text.strip()
    if stripped.startswith("```"):
        # Drop opening fence (possibly ```json) and trailing fence.
        body = stripped[3:]
        if body.startswith("json"):
            body = body[4:]
        body = body.lstrip("\n").rstrip()
        if body.endswith("```"):
            body = body[:-3].rstrip()
        if body:
            candidates.append(body)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if 0 <= start < end:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    _LOG.warning(
        "typed_extraction_llm_json_parse_failed: head=%r", text[:200]
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
        try:
            message = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # SDK can raise many error types
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
        return _parse_json_response("\n".join(parts))

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
        try:
            message = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
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
        return {"text": "\n".join(parts)}

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

    client = anthropic.AsyncAnthropic()

    async def _acall(prompt: str) -> Dict[str, Any]:
        try:
            message = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            _LOG.warning(
                "typed_extraction_async_batch_classifier_call_failed: %s: %s",
                type(exc).__name__, exc,
            )
            return {"text": ""}
        parts: List[str] = []
        for block in getattr(message, "content", []) or []:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return {"text": "\n".join(parts)}

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
        for key in missing:
            if key == "classifier":
                new_callers[key] = _build_anthropic_batch_classifier_caller(
                    ChunkClassifier.MODEL_ID
                )
            else:
                new_callers[key] = _build_anthropic_caller(
                    ChunkClassifier.MODEL_ID
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
            ChunkClassifier.MODEL_ID
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

    available_turn_ids: Set[str] = set()
    for c in chunks:
        cid = c.get("chunk_id") or c.get("id")
        if isinstance(cid, str) and cid:
            available_turn_ids.add(cid)

    # Glossary
    if glossary_manager is None:
        glossary_root = _resolve_glossary_root(sdl_root)
        glossary_manager = GlossaryManager(
            str(glossary_root) if glossary_root else None
        )

    api_callers = _resolve_api_callers(api_callers)
    classifier = ChunkClassifier(api_caller=api_callers.get("classifier"))
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

        result_box: Dict[str, Any] = {}

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
            finally:
                loop.close()

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join()
        classifications = result_box.get("value", [])  # type: ignore[assignment]

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

    extraction_run_id = "tex-" + uuid.uuid4().hex[:16]
    artifact = ExtractionMerger().merge(
        source_artifact_id=source_artifact_id,
        extraction_run_id=extraction_run_id,
        classifications=classifications,
        decisions=decisions,
        claims=claims,
        action_items=actions,
        run_metadata=run_metadata,
    )

    try:
        ExtractionMerger.write_to(artifact, out_path)
    except OSError as exc:
        return {
            "status": "failure",
            "reason": f"write_error:{exc}",
        }

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
