"""DecisionExtractor: extract decision items from classified chunks.

Phase M3.1. One of three typed extractors that feed
``ExtractionMerger`` -> ``meeting_extraction`` artifact.

Output items use a controlled vocabulary for ``decision_type`` and
require non-empty ``source_turn_ids`` so every decision is traceable
back to specific speaker turns. Items with missing source_turn_ids
are marked ``source_turn_validation = "missing"`` and kept in the
output (so the merger can flag them for human review) rather than
silently dropped.

Never raises. Returns ``[]`` on any LLM error path.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Set


_MODEL_ID = "claude-haiku-4-5-20251001"

_DECISION_TYPES: Set[str] = {
    "approved", "rejected", "deferred", "noted", "considered",
    "action_required", "open_question", "to_be_determined",
}


def _default_api_caller(prompt: str) -> Dict[str, Any]:  # noqa: ARG001
    return {"items": []}


class DecisionExtractor:
    MODEL_ID: str = _MODEL_ID

    def __init__(
        self,
        api_caller: Optional[Callable[[str], Dict[str, Any]]] = None,
        model: Optional[str] = None,
    ) -> None:
        self._api_caller = api_caller or _default_api_caller
        self._model = model or self.MODEL_ID

    @staticmethod
    def _normalize_type(value: Any) -> str:
        if isinstance(value, str) and value in _DECISION_TYPES:
            return value
        return "noted"

    @staticmethod
    def _validate_turns(
        source_turn_ids: Any,
        available_turn_ids: Optional[Set[str]],
    ) -> tuple:
        if not isinstance(source_turn_ids, list) or not source_turn_ids:
            return [], "missing"
        ids = [str(x) for x in source_turn_ids if isinstance(x, (str, int))]
        if not ids:
            return [], "missing"
        if available_turn_ids is not None:
            unknown = [i for i in ids if i not in available_turn_ids]
            if unknown:
                return ids, "invalid"
        return ids, "verified"

    def _build_prompt(
        self, chunks: Sequence[Dict[str, Any]], glossary_block: str,
    ) -> str:
        head = (
            "Extract DECISION items from the following meeting chunks. "
            "Return JSON {\"items\": [{\"decision_text\": <str>, "
            "\"decision_type\": <one of approved/rejected/deferred/noted/"
            "considered/action_required/open_question/to_be_determined>, "
            "\"stakeholders\": [<str>...], \"rationale\": <str or null>, "
            "\"source_turn_ids\": [<chunk_id>...]}]}. Every item MUST cite "
            "at least one source_turn_id. Use the controlled vocabulary "
            "exactly.\n\n"
        )
        if glossary_block:
            head += glossary_block + "\n\n"
        body_lines = []
        for c in chunks:
            cid = c.get("chunk_id") or c.get("id") or ""
            speaker = c.get("speaker") or ""
            text = c.get("text") or ""
            body_lines.append(f"[{cid}] {speaker}: {text}")
        return head + "\n".join(body_lines)

    def extract(
        self,
        chunks: Sequence[Dict[str, Any]],
        glossary_block: str = "",
        available_turn_ids: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        if not chunks:
            return []
        try:
            resp = self._api_caller(self._build_prompt(chunks, glossary_block))
        except Exception:
            return []
        if not isinstance(resp, dict):
            return []
        items = resp.get("items")
        if not isinstance(items, list):
            return []

        out: List[Dict[str, Any]] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            decision_text = raw.get("decision_text")
            if not isinstance(decision_text, str) or not decision_text.strip():
                continue
            ids, validation = self._validate_turns(
                raw.get("source_turn_ids"), available_turn_ids,
            )
            stakeholders = raw.get("stakeholders") or []
            if not isinstance(stakeholders, list):
                stakeholders = []
            else:
                stakeholders = [s for s in stakeholders if isinstance(s, str)]
            rationale = raw.get("rationale")
            if rationale is not None and not isinstance(rationale, str):
                rationale = None
            out.append({
                "decision_text": decision_text.strip(),
                "decision_type": self._normalize_type(raw.get("decision_type")),
                "stakeholders": stakeholders,
                "rationale": rationale,
                "source_turn_ids": ids,
                "source_turn_validation": validation,
            })
        return out
