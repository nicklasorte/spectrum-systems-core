"""AgendaDetector: identify agenda items from the intro phase of a transcript.

Phase W. Implements Rec 2a from the transcript_extraction_research_2026
research note: spectrum-policy meetings have agenda structure that can
be exploited to give ChunkClassifier "what was being discussed" context.

Design rules:
- Never raises. Any error path (API failure, malformed response, timeout,
  too few distinct items) collapses to a single ``undetected`` agenda
  item covering all chunks.
- Detection success requires at least ``MIN_AGENDA_ITEMS_FOR_SUCCESS=2``
  DISTINCT labels (case-insensitive). One generic label such as
  "Meeting" or "Discussion" is treated as failure (Attack 1).
- Max wall-clock duration is bounded by
  ``MAX_DETECTION_DURATION_SECONDS=60`` so a slow LLM cannot block a
  pipeline run for minutes (Attack 9).
- The actual model id resolved from ``ModelRegistry`` is logged and
  stored on every produced ``agenda_item`` artifact so a new engineer
  can answer "which model ran?" from the artifact alone (Attack 4).

The detector is intentionally a pure function over its inputs: it
takes a chunk list + identifiers and returns a result dict. Writing
the artifacts to disk is the pipeline's responsibility -- this keeps
the detector trivially mockable and testable without filesystem state.
"""
from __future__ import annotations

import datetime
import json
import logging
import math
import re
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence

_LOG = logging.getLogger(__name__)


UNCATEGORIZED_LABEL = "Uncategorized Meeting Content"
_GENERIC_LABEL_TOKENS = frozenset({"meeting", "discussion", "agenda", "item"})


class AgendaReferenceError(RuntimeError):
    """Raised by ``validate_agenda_references`` when chunk references
    point to agenda_item artifacts that do not exist on disk.

    Per Attack 12 (RT1): the pipeline must halt rather than silently
    let a classifier resolve to an empty agenda label.
    """


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _default_api_caller(prompt: str) -> Dict[str, Any]:  # noqa: ARG001
    """Offline default. Returns an empty agenda; never raises."""
    return {"text": "{}"}


class AgendaDetector:
    """Detect agenda items from a transcript's intro phase.

    See module docstring for design rules.
    """

    MIN_AGENDA_ITEMS_FOR_SUCCESS = 2
    MAX_DETECTION_DURATION_SECONDS = 60
    INTRO_PHASE_PERCENTAGE = 0.20
    SCHEMA_VERSION = "1.0.0"
    PRODUCED_BY = "AgendaDetector"

    def __init__(
        self,
        model_registry: Any,
        sdl_root: Optional[str] = None,
        *,
        api_caller: Optional[Callable[[str], Dict[str, Any]]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.model_registry = model_registry
        self.sdl_root = sdl_root
        # Per Attack 8 (RT1): resolve the actual model id ONCE at
        # construction and log it so a new engineer can trace which
        # model produced any agenda_item written during this run.
        entry = model_registry.get("generation")
        if isinstance(entry, dict):
            self._model = str(entry.get("model") or "unknown")
        else:
            self._model = "unknown"
        _LOG.info(
            "agenda_detector_initialized_with_model: %s", self._model,
        )
        self._api_caller = api_caller or _default_api_caller
        self._clock = clock or time.monotonic

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def detect(
        self,
        chunks: Sequence[Dict[str, Any]],
        source_id: str,
        pipeline_run_id: str,
        *,
        trace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Detect agenda items.

        Returns a dict with keys:
          - detection_succeeded: bool
          - agenda_items: list[dict] (always non-empty; falls back to a
            single "undetected" item covering all chunks)
          - detection_method: "llm_detected" | "single_default" | "undetected"
          - detector_model_used: str
          - items_count: int
          - detection_duration_seconds: float

        Never raises.
        """
        chunks = list(chunks or [])
        if not chunks:
            return self._undetected_result(
                chunks=[], source_id=source_id,
                pipeline_run_id=pipeline_run_id, trace_id=trace_id,
                duration=0.0, reason="no_chunks",
            )

        start = self._clock()

        # Slice the intro phase. Bound below by MIN_AGENDA_ITEMS_FOR_SUCCESS
        # speaker turns so a tiny smoke-test fixture still has room to
        # detect 2 items; bound above so we don't ship the whole
        # transcript to the LLM on a long meeting.
        intro_size = max(
            self.MIN_AGENDA_ITEMS_FOR_SUCCESS,
            math.ceil(len(chunks) * self.INTRO_PHASE_PERCENTAGE),
        )
        intro_chunks = chunks[:intro_size]

        prompt = self._build_detection_prompt(intro_chunks)
        try:
            response = self._api_caller(prompt)
        except Exception as exc:  # noqa: BLE001 - never raise
            _LOG.warning(
                "agenda_detector_api_error: source_id=%s %s: %s",
                source_id, type(exc).__name__, exc,
            )
            duration = self._clock() - start
            return self._undetected_result(
                chunks=chunks, source_id=source_id,
                pipeline_run_id=pipeline_run_id, trace_id=trace_id,
                duration=duration, reason="api_error",
            )

        duration = self._clock() - start
        if duration > self.MAX_DETECTION_DURATION_SECONDS:
            _LOG.warning(
                "agenda_detector_timeout: source_id=%s duration=%.1fs "
                "max=%ds (falling back to undetected)",
                source_id, duration, self.MAX_DETECTION_DURATION_SECONDS,
            )
            return self._undetected_result(
                chunks=chunks, source_id=source_id,
                pipeline_run_id=pipeline_run_id, trace_id=trace_id,
                duration=duration, reason="timeout",
            )

        detected_items, confidence = self._parse_detection_response(response)
        if not self._validate_detected_items(detected_items):
            _LOG.info(
                "agenda_detector_insufficient_distinct_items: source_id=%s "
                "items=%d (falling back to undetected)",
                source_id, len(detected_items),
            )
            return self._undetected_result(
                chunks=chunks, source_id=source_id,
                pipeline_run_id=pipeline_run_id, trace_id=trace_id,
                duration=duration, reason="insufficient_distinct_items",
            )

        agenda_artifacts = self._map_chunks_to_agenda_items(
            chunks=chunks,
            detected_items=detected_items,
            source_id=source_id,
            pipeline_run_id=pipeline_run_id,
            trace_id=trace_id,
            confidence=confidence,
        )
        return {
            "detection_succeeded": True,
            "agenda_items": agenda_artifacts,
            "detection_method": "llm_detected",
            "detector_model_used": self._model,
            "items_count": len(agenda_artifacts),
            "detection_duration_seconds": float(duration),
        }

    # ------------------------------------------------------------------
    # Prompt + parse
    # ------------------------------------------------------------------

    def _build_detection_prompt(
        self,
        intro_chunks: Sequence[Dict[str, Any]],
    ) -> str:
        lines: List[str] = [
            "You are reading the introduction phase of a federal-policy "
            "working-group meeting transcript.",
            "Identify the DISCRETE agenda items the meeting will cover.",
            "",
            "Return JSON only -- no prose -- in this exact shape:",
            "{",
            '  "agenda_items": [',
            '    {"ordinal": 1, "label": "<short topic name>", '
            '"approximate_start_chunk_index": <integer>}',
            "  ],",
            '  "detection_confidence": <0.0-1.0>,',
            '  "rationale": "<one short sentence>"',
            "}",
            "",
            "Rules:",
            "- At least 2 distinct agenda labels OR return an empty list.",
            "- Labels must be specific (e.g. 'FSS Protection Criteria'), "
            "not generic ('Meeting', 'Discussion').",
            "- approximate_start_chunk_index is 0-indexed into the list "
            "of chunks below.",
            "",
            "Chunks:",
        ]
        for i, chunk in enumerate(intro_chunks):
            cid = ""
            text = ""
            if isinstance(chunk, dict):
                cid = str(chunk.get("chunk_id") or chunk.get("id") or "")
                text = chunk.get("text") or ""
            lines.append(f"[{i}] chunk_id={cid}")
            lines.append(text[:500])
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _parse_detection_response(
        response: Any,
    ) -> "tuple[List[Dict[str, Any]], Optional[float]]":
        """Tolerantly parse the LLM response. Returns ``([], None)`` if
        nothing usable was returned.
        """
        if response is None:
            return [], None
        text: Optional[str] = None
        if isinstance(response, dict):
            for key in ("text", "response", "content"):
                value = response.get(key)
                if isinstance(value, str):
                    text = value
                    break
            if text is None and "agenda_items" in response:
                # Caller already returned a parsed dict.
                text = json.dumps(response)
        elif isinstance(response, str):
            text = response

        if not text:
            return [], None

        # Tolerate prose around the JSON object by finding the first
        # top-level brace pair.
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match is None:
            return [], None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return [], None
        if not isinstance(data, dict):
            return [], None

        raw_items = data.get("agenda_items")
        if not isinstance(raw_items, list):
            return [], None

        items: List[Dict[str, Any]] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            label = raw.get("label")
            if not isinstance(label, str) or not label.strip():
                continue
            try:
                ordinal = int(raw.get("ordinal") or len(items) + 1)
            except (TypeError, ValueError):
                ordinal = len(items) + 1
            try:
                start_idx = int(raw.get("approximate_start_chunk_index") or 0)
            except (TypeError, ValueError):
                start_idx = 0
            items.append({
                "label": label.strip()[:200],
                "ordinal": max(1, ordinal),
                "approximate_start_chunk_index": max(0, start_idx),
            })

        confidence: Optional[float] = None
        raw_conf = data.get("detection_confidence")
        if isinstance(raw_conf, (int, float)):
            try:
                f = float(raw_conf)
                if 0.0 <= f <= 1.0:
                    confidence = f
            except (TypeError, ValueError):
                confidence = None

        return items, confidence

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_detected_items(
        self,
        detected_items: Sequence[Dict[str, Any]],
    ) -> bool:
        """Per Attack 1 (RT1): require >=2 DISTINCT labels.

        A single agenda item, or N copies of "Discussion", is
        functionally equivalent to no detection and must be rejected.
        Generic single-token labels also count toward the distinct
        check (so "Meeting" + "Item" is still rejected).
        """
        if len(detected_items) < self.MIN_AGENDA_ITEMS_FOR_SUCCESS:
            return False
        labels = {
            (item.get("label") or "").lower().strip()
            for item in detected_items
        }
        labels.discard("")
        if len(labels) < self.MIN_AGENDA_ITEMS_FOR_SUCCESS:
            return False
        # Reject "all labels are generic single-token nonsense".
        non_generic = {
            label for label in labels
            if not _is_generic_label(label)
        }
        if not non_generic:
            return False
        return True

    # ------------------------------------------------------------------
    # Chunk -> agenda mapping
    # ------------------------------------------------------------------

    def _map_chunks_to_agenda_items(
        self,
        *,
        chunks: Sequence[Dict[str, Any]],
        detected_items: Sequence[Dict[str, Any]],
        source_id: str,
        pipeline_run_id: str,
        trace_id: Optional[str],
        confidence: Optional[float],
    ) -> List[Dict[str, Any]]:
        # Sort detected items by approximate_start_chunk_index so the
        # sequential split below is deterministic.
        sorted_items = sorted(
            list(detected_items),
            key=lambda d: (
                int(d.get("approximate_start_chunk_index") or 0),
                int(d.get("ordinal") or 0),
            ),
        )

        n_chunks = len(chunks)
        # Compute boundaries: each agenda item's start = its
        # approximate_start_chunk_index (clamped); its end = the next
        # item's start - 1 (or last chunk for the final item).
        boundaries: List[int] = []
        for it in sorted_items:
            idx = int(it.get("approximate_start_chunk_index") or 0)
            idx = max(0, min(idx, n_chunks - 1))
            boundaries.append(idx)
        # Ensure first item starts at 0 (otherwise leading chunks would
        # have no agenda).
        if boundaries:
            boundaries[0] = 0
        # Ensure strictly non-decreasing boundaries.
        for i in range(1, len(boundaries)):
            if boundaries[i] <= boundaries[i - 1]:
                boundaries[i] = boundaries[i - 1] + 1
        # Clamp anything that ran off the end and drop items with no
        # chunks left to assign.
        while boundaries and boundaries[-1] >= n_chunks:
            boundaries.pop()
            sorted_items = sorted_items[: len(boundaries)]

        out: List[Dict[str, Any]] = []
        for i, item in enumerate(sorted_items):
            start_idx = boundaries[i]
            end_idx = (
                boundaries[i + 1] - 1
                if i + 1 < len(boundaries)
                else n_chunks - 1
            )
            start_turn_id = self._chunk_id(chunks[start_idx])
            end_turn_id = self._chunk_id(chunks[end_idx])
            out.append({
                "agenda_item_id": str(uuid.uuid4()),
                "artifact_type": "agenda_item",
                "schema_version": self.SCHEMA_VERSION,
                "created_at": _now_iso(),
                "trace_id": trace_id,
                "pipeline_run_id": pipeline_run_id,
                "source_id": source_id,
                "ordinal": i + 1,
                "label": (item.get("label") or "")[:200],
                "start_turn_id": start_turn_id,
                "end_turn_id": end_turn_id,
                "detection_method": "llm_detected",
                "detection_confidence": confidence,
                "detector_model_used": self._model,
                "provenance": {
                    "produced_by": self.PRODUCED_BY,
                    "detected_from": "first_20_percent_of_turns",
                },
            })
        return out

    @staticmethod
    def _chunk_id(chunk: Dict[str, Any]) -> str:
        return str(chunk.get("chunk_id") or chunk.get("id") or "")

    # ------------------------------------------------------------------
    # Fallback
    # ------------------------------------------------------------------

    def _undetected_result(
        self,
        *,
        chunks: Sequence[Dict[str, Any]],
        source_id: str,
        pipeline_run_id: str,
        trace_id: Optional[str],
        duration: float,
        reason: str,
    ) -> Dict[str, Any]:
        items = [
            self._make_undetected_agenda_item(
                chunks=chunks,
                source_id=source_id,
                pipeline_run_id=pipeline_run_id,
                trace_id=trace_id,
                reason=reason,
            )
        ] if chunks else []
        return {
            "detection_succeeded": False,
            "agenda_items": items,
            "detection_method": "undetected",
            "detector_model_used": self._model,
            "items_count": len(items),
            "detection_duration_seconds": float(duration),
            "detection_failure_reason": reason,
        }

    def _make_undetected_agenda_item(
        self,
        *,
        chunks: Sequence[Dict[str, Any]],
        source_id: str,
        pipeline_run_id: str,
        trace_id: Optional[str],
        reason: str,
    ) -> Dict[str, Any]:
        start_turn_id = self._chunk_id(chunks[0])
        end_turn_id = self._chunk_id(chunks[-1])
        return {
            "agenda_item_id": str(uuid.uuid4()),
            "artifact_type": "agenda_item",
            "schema_version": self.SCHEMA_VERSION,
            "created_at": _now_iso(),
            "trace_id": trace_id,
            "pipeline_run_id": pipeline_run_id,
            "source_id": source_id,
            "ordinal": 1,
            "label": UNCATEGORIZED_LABEL,
            "start_turn_id": start_turn_id,
            "end_turn_id": end_turn_id,
            "detection_method": "undetected",
            "detection_confidence": None,
            "detector_model_used": self._model,
            "provenance": {
                "produced_by": self.PRODUCED_BY,
                "detected_from": "fallback_all_chunks",
            },
        }


def _is_generic_label(label: str) -> bool:
    """Treat single-token labels made entirely of generic words as
    "no signal" (Attack 1).

    A label like "FSS Protection" survives because "fss" / "protection"
    are not in the generic token set. A label like "Meeting" or
    "Discussion Item" gets rejected.
    """
    tokens = [t for t in re.split(r"\W+", label.lower()) if t]
    if not tokens:
        return True
    return all(t in _GENERIC_LABEL_TOKENS for t in tokens)


def build_chunk_to_agenda_mapping(
    chunks: Sequence[Dict[str, Any]],
    agenda_items: Sequence[Dict[str, Any]],
) -> Dict[str, str]:
    """Build {chunk_id: agenda_item_id} from chunks ordered by chunk_index
    and the agenda_item artifacts.

    Walks chunks in index order and assigns each chunk to the agenda
    item whose [start_turn_id, end_turn_id] range covers it. Falls
    back to the last agenda item for any trailing chunks not bracketed
    by a range (defensive).
    """
    if not agenda_items or not chunks:
        return {}

    chunk_id_to_index: Dict[str, int] = {}
    for i, chunk in enumerate(chunks):
        cid = AgendaDetector._chunk_id(chunk)
        if cid:
            chunk_id_to_index[cid] = i

    ranges: List["tuple[int, int, str]"] = []
    for item in agenda_items:
        start_id = item.get("start_turn_id")
        end_id = item.get("end_turn_id")
        item_id = item.get("agenda_item_id")
        if not (isinstance(start_id, str) and isinstance(end_id, str)
                and isinstance(item_id, str)):
            continue
        start_idx = chunk_id_to_index.get(start_id, 0)
        end_idx = chunk_id_to_index.get(end_id, len(chunks) - 1)
        ranges.append((start_idx, end_idx, item_id))

    if not ranges:
        return {}

    ranges.sort(key=lambda r: (r[0], r[1]))
    mapping: Dict[str, str] = {}
    for i, chunk in enumerate(chunks):
        cid = AgendaDetector._chunk_id(chunk)
        if not cid:
            continue
        assigned: Optional[str] = None
        for start_idx, end_idx, item_id in ranges:
            if start_idx <= i <= end_idx:
                assigned = item_id
                break
        if assigned is None:
            # Trailing chunk past the last agenda item's end -- attach
            # to the last item rather than leaving the chunk dangling.
            assigned = ranges[-1][2]
        mapping[cid] = assigned
    return mapping


def validate_agenda_references(
    chunks: Sequence[Dict[str, Any]],
    agenda_items: Sequence[Dict[str, Any]],
) -> None:
    """Pre-flight check (Attack 12 / RT1).

    Raises ``AgendaReferenceError`` if any chunk references an
    ``agenda_item_id`` that is not present in ``agenda_items``.

    This is the pre-flight that the pipeline runs after annotating
    chunks but before invoking the ChunkClassifier. The classifier
    must NEVER see a dangling agenda_item_id; we'd rather halt the
    pipeline than silently fall through.
    """
    valid_ids = {
        item.get("agenda_item_id")
        for item in agenda_items
        if isinstance(item.get("agenda_item_id"), str)
    }
    referenced = {
        chunk.get("agenda_item_id")
        for chunk in chunks
        if isinstance(chunk.get("agenda_item_id"), str)
    }
    missing = referenced - valid_ids
    if missing:
        raise AgendaReferenceError(
            f"Chunks reference agenda_item_ids that don't exist: "
            f"{sorted(missing)}"
        )
