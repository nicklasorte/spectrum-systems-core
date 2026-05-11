"""ActionItemExtractor: extract assigned tasks from classified chunks.

Phase M3.1. Never raises. Returns ``[]`` on any LLM error path.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Set


_MODEL_ID = "claude-haiku-4-5-20251001"


def _default_api_caller(prompt: str) -> Dict[str, Any]:  # noqa: ARG001
    return {"items": []}


class ActionItemExtractor:
    MODEL_ID: str = _MODEL_ID

    def __init__(
        self,
        api_caller: Optional[Callable[[str], Dict[str, Any]]] = None,
        model: Optional[str] = None,
    ) -> None:
        self._api_caller = api_caller or _default_api_caller
        self._model = model or self.MODEL_ID

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
            "Extract ACTION ITEMS from the following meeting chunks. An "
            "action item assigns a task to a named owner. Return JSON "
            "{\"items\": [{\"action\": <str>, \"owner\": <person or org>, "
            "\"due\": <ISO date or null>, \"source_turn_ids\": [<id>...]}]}. "
            "Every item MUST cite at least one source_turn_id and have a "
            "named owner.\n\n"
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
            action = raw.get("action")
            if not isinstance(action, str) or not action.strip():
                continue
            owner = raw.get("owner")
            if not isinstance(owner, str) or not owner.strip():
                continue
            due = raw.get("due")
            if due is not None and not isinstance(due, str):
                due = None
            ids, validation = self._validate_turns(
                raw.get("source_turn_ids"), available_turn_ids,
            )
            out.append({
                "action": action.strip(),
                "owner": owner.strip(),
                "due": due,
                "source_turn_ids": ids,
                "source_turn_validation": validation,
            })
        return out
