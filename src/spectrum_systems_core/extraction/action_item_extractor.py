"""ActionItemExtractor: extract assigned tasks from classified chunks.

Phase M3.1 + Phase Q. See ``decision_extractor`` docstring for the Phase
Q additions (OMIT block, few-shot injection, confidence + threshold).

Never raises. Returns ``[]`` on any LLM error path.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from ..evals.m4.few_shot import (
    format_examples_for_prompt,
    load_few_shot_examples,
)
from ._prompt_blocks import (
    CONFIDENCE_SCORING_BLOCK,
    CONFIDENCE_THRESHOLD,
    OMIT_INSTRUCTION_BLOCK,
    PROMPT_SCHEMA_VERSION,
    apply_confidence_threshold,
    normalize_confidence,
)


_MODEL_ID = "claude-haiku-4-5-20251001"


def _default_api_caller(prompt: str) -> Dict[str, Any]:  # noqa: ARG001
    return {"items": []}


def _empty_metadata() -> Dict[str, Any]:
    return {
        "few_shot_injected": False,
        "few_shot_version": None,
        "few_shot_example_count": 0,
        "omit_instruction_present": False,
        "low_confidence_item_count": 0,
    }


class ActionItemExtractor:
    MODEL_ID: str = _MODEL_ID
    EXAMPLE_TYPE: str = "action_item"
    PROMPT_SCHEMA_VERSION: str = PROMPT_SCHEMA_VERSION
    CONFIDENCE_THRESHOLD: float = CONFIDENCE_THRESHOLD

    def __init__(
        self,
        api_caller: Optional[Callable[[str], Dict[str, Any]]] = None,
        model: Optional[str] = None,
        few_shot_path: Optional[str] = None,
        data_lake_path: Optional[str] = None,
    ) -> None:
        self._api_caller = api_caller or _default_api_caller
        self._model = model or self.MODEL_ID
        self._few_shot_path = few_shot_path
        self._data_lake_path = data_lake_path
        self.last_run_metadata: Dict[str, Any] = _empty_metadata()

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

    def _load_few_shot_block(self) -> tuple[str, Dict[str, Any]]:
        meta: Dict[str, Any] = {
            "few_shot_injected": False,
            "few_shot_version": None,
            "few_shot_example_count": 0,
        }
        try:
            artifact, status = load_few_shot_examples(
                prompt_schema_version=self.PROMPT_SCHEMA_VERSION,
                data_lake_path=self._data_lake_path,
                seed_path=self._few_shot_path,
            )
        except Exception:
            return "", meta
        if status != "ok" or not isinstance(artifact, dict):
            return "", meta
        examples = artifact.get("examples") or []
        count = sum(
            1 for e in examples
            if isinstance(e, dict) and e.get("example_type") == self.EXAMPLE_TYPE
        )
        if count == 0:
            return "", meta
        block = format_examples_for_prompt(artifact, example_type=self.EXAMPLE_TYPE)
        if not block:
            return "", meta
        meta["few_shot_injected"] = True
        meta["few_shot_version"] = artifact.get("prompt_schema_version")
        meta["few_shot_example_count"] = count
        return block, meta

    def _build_prompt(
        self,
        chunks: Sequence[Dict[str, Any]],
        glossary_block: str,
        few_shot_block: str,
    ) -> str:
        parts: List[str] = []
        parts.append(
            "Extract ACTION ITEMS from the following meeting chunks. An "
            "action item assigns a task to a named owner."
        )
        parts.append(OMIT_INSTRUCTION_BLOCK)
        if glossary_block:
            parts.append(glossary_block)
        if few_shot_block:
            parts.append(few_shot_block)
        body_lines: List[str] = ["MEETING CHUNKS:"]
        for c in chunks:
            cid = c.get("chunk_id") or c.get("id") or ""
            speaker = c.get("speaker") or ""
            text = c.get("text") or ""
            body_lines.append(f"[{cid}] {speaker}: {text}")
        parts.append("\n".join(body_lines))
        parts.append(
            "OUTPUT SCHEMA:\n"
            "Return JSON {\"items\": [{\"action\": <str>, "
            "\"owner\": <person or org>, \"due\": <ISO date or null>, "
            "\"source_turn_ids\": [<id>...], "
            "\"confidence\": <number 0.0-1.0>}]}. "
            "Every item MUST cite at least one source_turn_id and have a "
            "named owner."
        )
        parts.append(CONFIDENCE_SCORING_BLOCK)
        return "\n\n".join(parts)

    def extract(
        self,
        chunks: Sequence[Dict[str, Any]],
        glossary_block: str = "",
        available_turn_ids: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        self.last_run_metadata = _empty_metadata()
        if not chunks:
            return []

        few_shot_block, few_shot_meta = self._load_few_shot_block()
        self.last_run_metadata.update(few_shot_meta)

        built_prompt = self._build_prompt(chunks, glossary_block, few_shot_block)
        self.last_run_metadata["omit_instruction_present"] = (
            OMIT_INSTRUCTION_BLOCK in built_prompt
        )

        try:
            resp = self._api_caller(built_prompt)
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
                "confidence": normalize_confidence(raw.get("confidence")),
            })

        low_count = apply_confidence_threshold(out, self.CONFIDENCE_THRESHOLD)
        self.last_run_metadata["low_confidence_item_count"] = low_count
        return out
