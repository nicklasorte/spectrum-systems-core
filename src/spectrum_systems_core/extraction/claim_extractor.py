"""ClaimExtractor: extract atomic factual claims from classified chunks.

Phase M3.1. Distinct from ``paper/claim_extractor.py``, which produces
technical_claim artifacts for the paper module. This extractor feeds the
typed-extraction pipeline (decision / claim / action_item) and produces
``claim`` items inside a ``meeting_extraction`` artifact.

Never raises. Returns ``[]`` on any LLM error path.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Set


_MODEL_ID = "claude-haiku-4-5-20251001"
_CLAIM_TYPES: Set[str] = {"technical", "procedural", "regulatory", "opinion"}


def _default_api_caller(prompt: str) -> Dict[str, Any]:  # noqa: ARG001
    return {"items": []}


class ClaimExtractor:
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
        if isinstance(value, str) and value in _CLAIM_TYPES:
            return value
        return "opinion"

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
            "Extract atomic CLAIM items from the following meeting chunks. "
            "A claim is a single factual or technical assertion. Return JSON "
            "{\"items\": [{\"claim_text\": <atomic statement>, "
            "\"claim_type\": <technical|procedural|regulatory|opinion>, "
            "\"speaker\": <str>, \"source_turn_ids\": [<chunk_id>...]}]}. "
            "Every item MUST cite at least one source_turn_id.\n\n"
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
            claim_text = raw.get("claim_text")
            if not isinstance(claim_text, str) or not claim_text.strip():
                continue
            ids, validation = self._validate_turns(
                raw.get("source_turn_ids"), available_turn_ids,
            )
            speaker = raw.get("speaker")
            if not isinstance(speaker, str):
                speaker = ""
            out.append({
                "claim_text": claim_text.strip(),
                "claim_type": self._normalize_type(raw.get("claim_type")),
                "speaker": speaker,
                "source_turn_ids": ids,
                "source_turn_validation": validation,
            })
        return out
