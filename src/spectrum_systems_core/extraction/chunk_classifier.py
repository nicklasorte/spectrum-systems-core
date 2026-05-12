"""ChunkClassifier: route speaker-turn chunks to typed extractors.

Phase M3.0. Classifies each chunk as one of:
    decision | claim | action_item | off_topic

The classifier uses an injectable LLM caller (Haiku 4.5 in production)
plus a deterministic regulatory-verb fallback that re-classifies any
``off_topic`` chunk containing a known regulatory verb to ``decision``.
This catches classifier errors on regulatory language --- the verbs are
the strongest signal of a real decision and must never be lost.

Design rules:
- Never raises. Any error path returns ``off_topic`` with
  ``regulatory_verb_fallback_applied`` flag set per the verb check.
- The regulatory verb check is **case-insensitive** with whole-word
  matching so "Approved." matches but "preapproved" does not.
- ``api_caller`` is injectable. The default does no I/O and returns
  ``off_topic`` so tests + offline runs are safe.
- Confidence is reported only when the caller returns one. We do not
  fabricate confidence values.

Phase Perf adds two batch entry points used by ``run_typed_extraction``:

- ``batch_classify(chunks, source_id)``: send the prompts for many chunks
  in one Haiku request. Same-quality classification (the prompt asks for
  one classification per chunk in the same order). On any parse or API
  error the batch falls back per-chunk via ``classify(chunk, source_id)``.
- ``batch_classify_async(chunks, source_id, max_concurrent=3)``: fan out
  multiple batches concurrently with an asyncio Semaphore. Used to drive
  the per-transcript throughput up another 4x while staying within Haiku
  rate limits.

Quality is preserved end-to-end: regulatory-verb fallback is applied to
each parsed result, classification artifacts are produced through the
same ``_make_classification_artifact`` helper, and confidence is left
``None`` (the batch endpoint does not request a confidence score per
chunk to keep the response token count bounded).
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import math
import re
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

_LOG = logging.getLogger(__name__)


_MODEL_ID = "claude-haiku-4-5-20251001"
_SCHEMA_VERSION = "1.0.0"
_VALID_CLASSIFICATIONS: Set[str] = {"decision", "claim", "action_item", "off_topic"}

# BATCH_SIZE rationale: each chunk contributes ~50-150 prompt tokens (we
# truncate the chunk text at 500 chars per chunk -- well above the
# observed average TIG chunk length of ~150-250 chars -- plus a short
# header). Fifteen chunks per batch keeps the request well under 2k
# tokens including the rules block, which keeps round-trip latency
# similar to a single-chunk call but amortizes the per-call overhead
# across 15 chunks.
_BATCH_SIZE = 15

# Per-chunk text cap inside the batch prompt. Average TIG chunk is
# ~150-250 chars; 500 covers the long tail without inflating the prompt.
_BATCH_CHUNK_TEXT_CAP = 500

# Output token cap for a batch response. Each line is ~80 chars
# (chunk_id ~40 chars + literals). Fifteen chunks * ~80 chars / ~4 = ~300
# tokens. 400 leaves headroom while preventing runaway generations.
_BATCH_MAX_OUTPUT_TOKENS = 400

# Default concurrency for batch_classify_async. Conservative because a
# parallel matrix run can have up to 13 transcripts hitting Haiku at
# once: 13 jobs x 3 concurrent = 39 simultaneous calls, comfortably
# under the default 60 req/min Haiku tier.
_DEFAULT_MAX_CONCURRENT = 3

_BATCH_LINE_RE = re.compile(
    r"^\s*chunk_id\s*:\s*(?P<cid>[^|]+?)\s*\|\s*classification\s*:\s*(?P<cls>\S+)",
    re.IGNORECASE,
)

# Regulatory verbs that, when present in a chunk, force re-classification
# of off_topic -> decision. Case-insensitive. Whole-word match. Phrases
# with spaces (e.g. "action required") are matched as substrings with
# word boundaries on the outside.
_REGULATORY_VERBS: Set[str] = {
    "approved",
    "rejected",
    "deferred",
    "noted",
    "considered",
    "action required",
    "action_required",
    "agreed",
    "consensus",
}

# Pre-compile a single regex that fires on any verb. Word boundaries
# around the whole phrase prevent "preapproved" or "considerable" from
# triggering a false positive.
_VERB_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(v) for v in sorted(_REGULATORY_VERBS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _default_api_caller(prompt: str) -> Dict[str, Any]:  # noqa: ARG001
    """Offline default: classify as off_topic. Never raises."""
    return {"classification": "off_topic", "confidence": None}


class ChunkClassifier:
    """Classify a chunk and emit a chunk_classification artifact."""

    MODEL_ID: str = _MODEL_ID
    SCHEMA_VERSION: str = _SCHEMA_VERSION
    REGULATORY_VERBS: Set[str] = _REGULATORY_VERBS
    BATCH_SIZE: int = _BATCH_SIZE
    BATCH_CHUNK_TEXT_CAP: int = _BATCH_CHUNK_TEXT_CAP
    DEFAULT_MAX_CONCURRENT: int = _DEFAULT_MAX_CONCURRENT

    def __init__(
        self,
        api_caller: Optional[Callable[[str], Dict[str, Any]]] = None,
        model: Optional[str] = None,
        *,
        agenda_resolver: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
    ) -> None:
        self._api_caller = api_caller or _default_api_caller
        self._model = model or self.MODEL_ID
        # Phase W.4: optional resolver returns the agenda label to
        # inject into the classifier prompt for ``chunk``. Returns
        # None for "no agenda context to inject" (flag off, undetected
        # agenda, missing field, etc). The resolver is responsible
        # for all gating logic so the classifier stays a pure
        # text-in/JSON-out classifier.
        self._agenda_resolver = agenda_resolver

    def _agenda_prompt_line(self, chunk: Dict[str, Any]) -> str:
        """Return ``"Current agenda item: ...\\n\\n"`` or ``""``.

        Centralised so both the per-chunk and the batch prompt paths
        share the same gating semantics.
        """
        if self._agenda_resolver is None:
            return ""
        try:
            label = self._agenda_resolver(chunk)
        except Exception as exc:  # noqa: BLE001 - never raise out
            _LOG.warning(
                "chunk_classifier_agenda_resolver_error: %s: %s",
                type(exc).__name__, exc,
            )
            return ""
        if not isinstance(label, str) or not label.strip():
            return ""
        return f"Current agenda item: {label.strip()}\n\n"

    def _build_prompt(self, chunk: Dict[str, Any]) -> str:
        text = (chunk or {}).get("text", "")
        return (
            f"{self._agenda_prompt_line(chunk if isinstance(chunk, dict) else {})}"
            "Classify the following meeting speaker-turn into exactly one of: "
            "decision, claim, action_item, off_topic. Return JSON "
            '{"classification": "<one>", "confidence": <0..1 or null>}. '
            "Use 'decision' only when the group reaches or records an "
            "explicit outcome (approved/rejected/deferred/noted/considered). "
            "Use 'claim' for factual or technical assertions. Use "
            "'action_item' for tasks assigned to a named owner. Otherwise "
            f"use 'off_topic'.\n\n---\n{text}\n---"
        )

    def _regulatory_verb_fallback(
        self,
        chunk_text: str,
        classification: str,
    ) -> str:
        """Return 'decision' iff off_topic + regulatory verb present.

        Case-insensitive whole-word match.
        """
        if classification != "off_topic":
            return classification
        if not isinstance(chunk_text, str):
            return classification
        if _VERB_PATTERN.search(chunk_text):
            return "decision"
        return classification

    @staticmethod
    def _normalize_classification(value: Any) -> str:
        if isinstance(value, str) and value in _VALID_CLASSIFICATIONS:
            return value
        return "off_topic"

    @staticmethod
    def _normalize_confidence(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        if f < 0.0 or f > 1.0:
            return None
        return f

    def _make_classification_artifact(
        self,
        chunk: Dict[str, Any],
        raw_classification: str,
        source_id: str,
        confidence: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Build a chunk_classification artifact from a raw classification.

        Applies the regulatory-verb fallback and sets
        ``regulatory_verb_fallback_applied`` accordingly. Shared by both
        the per-chunk ``classify`` path and the batch paths so the
        envelope is identical regardless of how the classification was
        obtained.
        """
        chunk_id = ""
        chunk_text = ""
        if isinstance(chunk, dict):
            chunk_id = str(chunk.get("chunk_id") or chunk.get("id") or "")
            chunk_text = chunk.get("text") or ""

        raw_classification = self._normalize_classification(raw_classification)
        final = self._regulatory_verb_fallback(chunk_text, raw_classification)
        fallback_applied = (
            raw_classification == "off_topic" and final == "decision"
        )
        confidence = self._normalize_confidence(confidence)

        return {
            "classification_id": str(uuid.uuid4()),
            "chunk_id": chunk_id,
            "source_id": source_id or "",
            "classification": final,
            "regulatory_verb_fallback_applied": fallback_applied,
            "confidence": confidence,
            "artifact_type": "chunk_classification",
            "schema_version": self.SCHEMA_VERSION,
            "created_at": _now_iso(),
            "provenance": {
                "produced_by": "ChunkClassifier",
                "model": self._model,
            },
        }

    def classify(self, chunk: Dict[str, Any], source_id: str) -> Dict[str, Any]:
        """Classify ``chunk`` and return a chunk_classification artifact.

        Never raises. Caller may pass any dict; only ``chunk_id`` and
        ``text`` are read.
        """
        chunk_id = ""
        if isinstance(chunk, dict):
            chunk_id = str(chunk.get("chunk_id") or chunk.get("id") or "")

        prompt = self._build_prompt(chunk if isinstance(chunk, dict) else {})

        raw_classification = "off_topic"
        confidence: Optional[float] = None
        try:
            resp = self._api_caller(prompt)
            if isinstance(resp, dict):
                raw_classification = self._normalize_classification(
                    resp.get("classification")
                )
                confidence = self._normalize_confidence(resp.get("confidence"))
        except Exception as exc:  # never raise -- default to off_topic
            _LOG.warning(
                "chunk_classifier_api_error: chunk_id=%s %s: %s",
                chunk_id or "?", type(exc).__name__, exc,
            )
            raw_classification = "off_topic"
            confidence = None

        return self._make_classification_artifact(
            chunk if isinstance(chunk, dict) else {},
            raw_classification,
            source_id,
            confidence=confidence,
        )

    # ------------------------------------------------------------------
    # Phase Perf -- batch + async classification.
    # ------------------------------------------------------------------

    def _build_batch_prompt(self, chunks: Sequence[Dict[str, Any]]) -> str:
        """Build the batch classification prompt (one round-trip per batch)."""
        lines: List[str] = [
            f"Classify each of the following {len(chunks)} meeting "
            "speaker-turn chunks.",
            "For each chunk output exactly one line in this format:",
            "  chunk_id: <id> | classification: <decision|claim|action_item|off_topic>",
            "",
            "Rules (same as the per-chunk classifier):",
            "- decision: the group reached or recorded an explicit outcome "
            "(approved/rejected/deferred/noted/considered).",
            "- claim: a factual or technical assertion.",
            "- action_item: a task assigned to a named owner.",
            "- otherwise: off_topic.",
            "",
            "Return only the lines -- no preamble, no closing remarks. "
            "Output the same number of lines as input chunks, in the same "
            "order, one per chunk_id.",
            "",
        ]
        for i, chunk in enumerate(chunks, 1):
            cid = ""
            text = ""
            if isinstance(chunk, dict):
                cid = str(chunk.get("chunk_id") or chunk.get("id") or "")
                text = chunk.get("text") or ""
            lines.append(f"Chunk {i} (chunk_id: {cid}):")
            # Phase W.4: per-chunk agenda context (resolver-gated).
            agenda_line = self._agenda_prompt_line(
                chunk if isinstance(chunk, dict) else {}
            )
            if agenda_line:
                # Strip the trailing blank line; we already insert one
                # below.
                lines.append(agenda_line.rstrip())
            lines.append(text[: self.BATCH_CHUNK_TEXT_CAP])
            lines.append("")
        return "\n".join(lines)

    def _parse_batch_response(
        self,
        response_text: str,
        chunks: Sequence[Dict[str, Any]],
    ) -> Optional[Dict[str, str]]:
        """Parse a batch response into a {chunk_id: classification} dict.

        Tolerates malformed lines, blank lines, narrative pre/post text,
        and missing chunks (the caller fills the gap with ``off_topic``).
        Never raises.

        Returns ``None`` when the response is so corrupted that the
        caller should fall back per-chunk:
        - any duplicate ``chunk_id`` line (the second occurrence would
          silently overwrite the first; we cannot tell which is correct,
          so re-classifying per chunk is the safe move),
        - any line whose ``chunk_id`` was NOT in the request batch
          (hallucinated chunk_id; signals the model has lost track of
          the input).
        """
        if not isinstance(response_text, str):
            return {}

        requested_ids: Set[str] = set()
        for c in chunks:
            if isinstance(c, dict):
                cid = str(c.get("chunk_id") or c.get("id") or "")
                if cid:
                    requested_ids.add(cid)

        parsed: Dict[str, str] = {}
        seen_cids: Set[str] = set()
        for line in response_text.splitlines():
            m = _BATCH_LINE_RE.match(line)
            if m is None:
                continue
            cid = m.group("cid").strip()
            cls = m.group("cls").strip().lower().rstrip(".,;:")
            if cls not in _VALID_CLASSIFICATIONS:
                cls = "off_topic"
            if not cid:
                continue
            if cid not in requested_ids:
                _LOG.warning(
                    "chunk_classifier_batch_hallucinated_chunk_id: %r "
                    "(falling back per-chunk for batch of %d)",
                    cid, len(chunks),
                )
                return None
            if cid in seen_cids:
                _LOG.warning(
                    "chunk_classifier_batch_duplicate_chunk_id: %r "
                    "(falling back per-chunk for batch of %d)",
                    cid, len(chunks),
                )
                return None
            seen_cids.add(cid)
            parsed[cid] = cls
        return parsed

    def _build_batch_artifacts(
        self,
        chunks: Sequence[Dict[str, Any]],
        parsed: Dict[str, str],
        source_id: str,
    ) -> List[Dict[str, Any]]:
        """Map parsed classifications back to artifacts in input order.

        Missing chunks (LLM dropped a line) get ``off_topic``, which then
        flows through the regulatory-verb fallback so an "approved" line
        cannot be lost just because the response was short.
        """
        results: List[Dict[str, Any]] = []
        for chunk in chunks:
            cid = ""
            if isinstance(chunk, dict):
                cid = str(chunk.get("chunk_id") or chunk.get("id") or "")
            raw = parsed.get(cid, "off_topic")
            results.append(
                self._make_classification_artifact(
                    chunk if isinstance(chunk, dict) else {},
                    raw,
                    source_id,
                    confidence=None,
                )
            )
        return results

    def _per_chunk_fallback(
        self,
        chunks: Sequence[Dict[str, Any]],
        source_id: str,
    ) -> List[Dict[str, Any]]:
        """Fall back to per-chunk classification when a batch fails."""
        return [self.classify(chunk, source_id) for chunk in chunks]

    def _classify_batch_sync(
        self,
        chunks: Sequence[Dict[str, Any]],
        source_id: str,
    ) -> List[Dict[str, Any]]:
        """One synchronous batch classification call."""
        if not chunks:
            return []
        prompt = self._build_batch_prompt(chunks)
        try:
            resp = self._api_caller(prompt)
        except Exception as exc:  # never raise -- per-chunk fallback
            _LOG.warning(
                "chunk_classifier_batch_api_error: %s: %s "
                "(falling back per-chunk for %d chunks)",
                type(exc).__name__, exc, len(chunks),
            )
            return self._per_chunk_fallback(chunks, source_id)

        text = ""
        if isinstance(resp, dict):
            t = resp.get("text") or resp.get("response") or resp.get("content")
            if isinstance(t, str):
                text = t
            else:
                # Some callers return the full {classification: ..} dict
                # because they were built for the per-chunk path. We have
                # no way to extract per-chunk results from that, so fall
                # back per-chunk to preserve quality.
                _LOG.warning(
                    "chunk_classifier_batch_unexpected_response: keys=%s "
                    "(falling back per-chunk for %d chunks)",
                    sorted(resp.keys()), len(chunks),
                )
                return self._per_chunk_fallback(chunks, source_id)
        elif isinstance(resp, str):
            text = resp
        else:
            _LOG.warning(
                "chunk_classifier_batch_unexpected_response_type: %s "
                "(falling back per-chunk for %d chunks)",
                type(resp).__name__, len(chunks),
            )
            return self._per_chunk_fallback(chunks, source_id)

        parsed = self._parse_batch_response(text, chunks)
        if parsed is None:
            # _parse_batch_response already logged the specific reason
            # (duplicate / hallucinated chunk_id).
            return self._per_chunk_fallback(chunks, source_id)
        if not parsed:
            _LOG.warning(
                "chunk_classifier_batch_parse_empty: %d chunks unclassified, "
                "falling back per-chunk",
                len(chunks),
            )
            return self._per_chunk_fallback(chunks, source_id)
        return self._build_batch_artifacts(chunks, parsed, source_id)

    def batch_classify(
        self,
        chunks: Sequence[Dict[str, Any]],
        source_id: str,
        cache: Optional["ClassificationCache"] = None,
    ) -> List[Dict[str, Any]]:
        """Classify ``chunks`` in batches of ``BATCH_SIZE``.

        Returns one artifact per input chunk, in input order. Never raises.

        Quality-preserving: each chunk's classification flows through the
        same regulatory-verb fallback and artifact envelope as the
        per-chunk path. On any batch error the affected batch falls back
        to per-chunk classification.

        Pass ``cache`` (a ``ClassificationCache``) to skip API calls for
        chunks whose text was previously classified. Cache hits never
        block: a cache miss falls through to the batch path. The cache is
        loaded/saved by the caller (see ``run_typed_extraction``).
        """
        if not chunks:
            return []

        # Cache-aware path: separate cached vs uncached chunks, classify
        # only the misses, then re-merge in input order.
        chunks = list(chunks)
        if cache is not None:
            cached_classifications: Dict[int, str] = {}
            uncached_indices: List[int] = []
            for idx, chunk in enumerate(chunks):
                hit = cache.get(chunk)
                if hit is not None:
                    cached_classifications[idx] = hit
                else:
                    uncached_indices.append(idx)
            uncached_chunks = [chunks[i] for i in uncached_indices]
            new_artifacts = self._batch_classify_no_cache(
                uncached_chunks, source_id
            )
            for chunk, art in zip(uncached_chunks, new_artifacts):
                cache.set(chunk, art["classification"])
            results: List[Dict[str, Any]] = [None] * len(chunks)  # type: ignore[list-item]
            for idx, chunk in enumerate(chunks):
                if idx in cached_classifications:
                    results[idx] = self._make_classification_artifact(
                        chunk, cached_classifications[idx], source_id,
                        confidence=None,
                    )
            for slot, art in zip(uncached_indices, new_artifacts):
                results[slot] = art
            return results

        return self._batch_classify_no_cache(chunks, source_id)

    def _batch_classify_no_cache(
        self,
        chunks: Sequence[Dict[str, Any]],
        source_id: str,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for i in range(0, len(chunks), self.BATCH_SIZE):
            batch = chunks[i : i + self.BATCH_SIZE]
            results.extend(self._classify_batch_sync(batch, source_id))
        return results

    async def batch_classify_async(
        self,
        chunks: Sequence[Dict[str, Any]],
        source_id: str,
        max_concurrent: Optional[int] = None,
        async_caller: Optional[Callable[[str], Any]] = None,
        cache: Optional["ClassificationCache"] = None,
    ) -> List[Dict[str, Any]]:
        """Async version of ``batch_classify`` -- fan multiple batches out.

        Each batch round-trip happens inside an ``asyncio.Semaphore`` whose
        capacity is ``max_concurrent`` (defaults to ``DEFAULT_MAX_CONCURRENT``,
        which is conservative because the matrix workflow already has up
        to 13 transcript jobs in flight).

        ``async_caller`` is an awaitable equivalent of the sync api_caller:
        ``async def(prompt) -> {"text": str} | str``. When None we wrap the
        injected sync caller in ``asyncio.to_thread`` so existing test
        callers still work. asyncio.gather preserves order.

        Always falls back per-chunk on any batch error so quality is
        identical to the sync path.
        """
        chunks = list(chunks)
        if not chunks:
            return []

        cap = self.DEFAULT_MAX_CONCURRENT if max_concurrent is None else max(1, int(max_concurrent))

        # Resolve cache hits up front so we never even queue an API call
        # for a chunk we already have a classification for.
        cached_indices: Dict[int, str] = {}
        worklist_indices: List[int] = []
        if cache is not None:
            for idx, chunk in enumerate(chunks):
                hit = cache.get(chunk)
                if hit is not None:
                    cached_indices[idx] = hit
                else:
                    worklist_indices.append(idx)
        else:
            worklist_indices = list(range(len(chunks)))

        worklist = [chunks[i] for i in worklist_indices]

        # Build the list of batches preserving the original chunk index.
        batches: List[List[Dict[str, Any]]] = []
        batch_index_offsets: List[int] = []
        for i in range(0, len(worklist), self.BATCH_SIZE):
            batches.append(worklist[i : i + self.BATCH_SIZE])
            batch_index_offsets.append(i)

        if not batches:
            results: List[Dict[str, Any]] = []
            for idx, chunk in enumerate(chunks):
                results.append(
                    self._make_classification_artifact(
                        chunk, cached_indices[idx], source_id, confidence=None,
                    )
                )
            return results

        semaphore = asyncio.Semaphore(cap)

        if async_caller is None:
            sync_caller = self._api_caller

            async def _aclassify(batch: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
                async with semaphore:
                    try:
                        return await asyncio.to_thread(
                            self._classify_batch_sync, batch, source_id,
                        )
                    except Exception as exc:  # never raise
                        _LOG.warning(
                            "chunk_classifier_async_batch_error: %s: %s "
                            "(falling back per-chunk for %d chunks)",
                            type(exc).__name__, exc, len(batch),
                        )
                        return self._per_chunk_fallback(batch, source_id)

            # silence "unused" lint when the sync caller is the default
            del sync_caller
        else:

            async def _aclassify(batch: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
                async with semaphore:
                    prompt = self._build_batch_prompt(batch)
                    try:
                        resp = await async_caller(prompt)
                    except Exception as exc:
                        _LOG.warning(
                            "chunk_classifier_async_batch_api_error: %s: %s "
                            "(falling back per-chunk for %d chunks)",
                            type(exc).__name__, exc, len(batch),
                        )
                        return self._per_chunk_fallback(batch, source_id)
                    text = ""
                    if isinstance(resp, dict):
                        t = resp.get("text") or resp.get("response") or resp.get("content")
                        if isinstance(t, str):
                            text = t
                        else:
                            _LOG.warning(
                                "chunk_classifier_async_batch_unexpected_response: "
                                "keys=%s falling back per-chunk for %d chunks",
                                sorted(resp.keys()), len(batch),
                            )
                            return self._per_chunk_fallback(batch, source_id)
                    elif isinstance(resp, str):
                        text = resp
                    else:
                        _LOG.warning(
                            "chunk_classifier_async_batch_unexpected_response_type: "
                            "%s falling back per-chunk for %d chunks",
                            type(resp).__name__, len(batch),
                        )
                        return self._per_chunk_fallback(batch, source_id)
                    parsed = self._parse_batch_response(text, batch)
                    if parsed is None:
                        return self._per_chunk_fallback(batch, source_id)
                    if not parsed:
                        _LOG.warning(
                            "chunk_classifier_async_batch_parse_empty: "
                            "falling back per-chunk for %d chunks",
                            len(batch),
                        )
                        return self._per_chunk_fallback(batch, source_id)
                    return self._build_batch_artifacts(batch, parsed, source_id)

        gathered = await asyncio.gather(*(_aclassify(b) for b in batches))

        # Stitch new + cached results back into input order.
        new_artifacts: List[Optional[Dict[str, Any]]] = [None] * len(worklist)
        for offset, batch_result in zip(batch_index_offsets, gathered):
            for j, art in enumerate(batch_result):
                new_artifacts[offset + j] = art

        # Update cache for the freshly classified chunks.
        if cache is not None:
            for chunk, art in zip(worklist, new_artifacts):
                if art is not None:
                    cache.set(chunk, art["classification"])

        results = [None] * len(chunks)  # type: ignore[list-item]
        for slot, art in zip(worklist_indices, new_artifacts):
            if art is None:  # pragma: no cover -- should not happen
                art = self._make_classification_artifact(
                    chunks[slot], "off_topic", source_id, confidence=None,
                )
            results[slot] = art
        for idx, raw in cached_indices.items():
            results[idx] = self._make_classification_artifact(
                chunks[idx], raw, source_id, confidence=None,
            )
        return results

    def num_batches_for(self, n_chunks: int) -> int:
        """Return ``ceil(n_chunks / BATCH_SIZE)``. Used by tests + callers."""
        if n_chunks <= 0:
            return 0
        return math.ceil(n_chunks / self.BATCH_SIZE)
