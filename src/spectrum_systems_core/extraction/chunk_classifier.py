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
"""
from __future__ import annotations

import datetime
import re
import uuid
from typing import Any, Callable, Dict, Optional, Set


_MODEL_ID = "claude-haiku-4-5-20251001"
_SCHEMA_VERSION = "1.0.0"
_VALID_CLASSIFICATIONS: Set[str] = {"decision", "claim", "action_item", "off_topic"}

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

    def __init__(
        self,
        api_caller: Optional[Callable[[str], Dict[str, Any]]] = None,
        model: Optional[str] = None,
    ) -> None:
        self._api_caller = api_caller or _default_api_caller
        self._model = model or self.MODEL_ID

    def _build_prompt(self, chunk: Dict[str, Any]) -> str:
        text = (chunk or {}).get("text", "")
        return (
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

    def classify(self, chunk: Dict[str, Any], source_id: str) -> Dict[str, Any]:
        """Classify ``chunk`` and return a chunk_classification artifact.

        Never raises. Caller may pass any dict; only ``chunk_id`` and
        ``text`` are read.
        """
        chunk_id = ""
        chunk_text = ""
        if isinstance(chunk, dict):
            chunk_id = str(chunk.get("chunk_id") or chunk.get("id") or "")
            chunk_text = chunk.get("text") or ""

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
        except Exception:  # never raise -- default to off_topic
            raw_classification = "off_topic"
            confidence = None

        final = self._regulatory_verb_fallback(chunk_text, raw_classification)
        fallback_applied = (raw_classification == "off_topic") and (final == "decision")

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
