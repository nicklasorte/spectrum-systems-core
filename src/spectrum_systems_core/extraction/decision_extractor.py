"""DecisionExtractor: extract decision items from classified chunks.

Phase M3.1 + Phase Q (extraction quality pass). One of three typed
extractors that feed ``ExtractionMerger`` -> ``meeting_extraction``
artifact.

Output items use a controlled vocabulary for ``decision_type`` and
require non-empty ``source_turn_ids`` so every decision is traceable
back to specific speaker turns. Items with missing source_turn_ids
are marked ``source_turn_validation = "missing"`` and kept in the
output (so the merger can flag them for human review) rather than
silently dropped.

Phase Q additions:
- OMIT_INSTRUCTION_BLOCK placed before chunk content per Wan et al.
  positional-bias finding.
- Few-shot examples loaded via ``evals.m4.few_shot.FewShotLoader`` and
  injected after the glossary block, before chunk content. Version-gated:
  on mismatch the loader returns ``(None, "version_mismatch")`` and the
  prompt proceeds without examples (degraded mode, not failure).
- ``confidence`` (0.0-1.0) required on every emitted item. Items below
  ``CONFIDENCE_THRESHOLD`` are flagged ``items_requiring_review=True``
  with ``review_reason="low_confidence"`` but kept so HITL can inspect.

Never raises. Returns ``[]`` on any LLM error path. Per-run metadata is
exposed on ``self.last_run_metadata`` for the runner/merger to record.
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

_DECISION_TYPES: Set[str] = {
    "approved", "rejected", "deferred", "noted", "considered",
    "action_required", "open_question", "to_be_determined",
}


def _default_api_caller(prompt: str) -> Dict[str, Any]:  # noqa: ARG001
    return {"items": []}


def _empty_metadata() -> Dict[str, Any]:
    return {
        "few_shot_injected": False,
        "few_shot_version": None,
        "few_shot_example_count": 0,
        # Set to True once _build_prompt has actually rendered a prompt that
        # contains the OMIT block; this avoids the field being a decorative
        # claim that drifts from prompt reality.
        "omit_instruction_present": False,
        "low_confidence_item_count": 0,
    }


class DecisionExtractor:
    MODEL_ID: str = _MODEL_ID
    EXAMPLE_TYPE: str = "decision"
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

    def _load_few_shot_block(self) -> tuple[str, Dict[str, Any]]:
        """Return ``(rendered_block, metadata)``. Empty block on any failure."""
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
        # Count BEFORE formatting -- format_examples_for_prompt returns a
        # header+footer block even when zero examples match the type filter,
        # which would otherwise misreport few_shot_injected=True with
        # example_count=0.
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
        # Order: role -> OMIT -> glossary -> few-shot -> chunks -> schema -> confidence.
        parts: List[str] = []
        parts.append(
            "Extract DECISION items from the following meeting chunks."
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
            "Return JSON {\"items\": [{\"decision_text\": <str>, "
            "\"decision_type\": <one of approved/rejected/deferred/noted/"
            "considered/action_required/open_question/to_be_determined>, "
            "\"stakeholders\": [<str>...], \"rationale\": <str or null>, "
            "\"source_turn_ids\": [<chunk_id>...], "
            "\"confidence\": <number 0.0-1.0>}]}. "
            "Every item MUST cite at least one source_turn_id. Use the "
            "controlled vocabulary exactly."
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
        # Verify the rendered prompt actually carries the OMIT block. This
        # makes omit_instruction_present a fact about the prompt, not a
        # decorative claim.
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
                "confidence": normalize_confidence(raw.get("confidence")),
            })

        low_count = apply_confidence_threshold(out, self.CONFIDENCE_THRESHOLD)
        self.last_run_metadata["low_confidence_item_count"] = low_count
        return out
