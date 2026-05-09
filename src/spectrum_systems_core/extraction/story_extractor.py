"""StoryExtractor: chunks.jsonl -> candidates.jsonl via the Anthropic API.

Model: claude-haiku-4-5-20251001 at temperature=0 (FINDING-C-007).
Returns a structured JSON candidate per chunk; failures are recorded as
blocked candidates so they remain debuggable in candidates.jsonl rather
than crashing the pipeline (FINDING-C-002 / debuggability principle).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import jsonschema

from ..ingestion._paths import schema_path
from ._paths import find_processed_dir


_COMPONENT_NAME = "story_extractor"
_COMPONENT_VERSION = "1.0.0"
EXTRACTION_MODEL = "claude-haiku-4-5-20251001"
EXTRACTION_TEMPERATURE = 0
MAX_TOKENS = 1000

PROMPT_TEMPLATE = """You are a story extraction assistant. Your job is to find
potential stories in source text that could be used in speeches, reports, or
strategic narratives.

Analyze the following text chunk and extract ONE story candidate if a strong
candidate exists. If no strong candidate exists, return null for story_found.

Source ID: {source_id}
Chunk index: {chunk_index}
Page numbers: {page_numbers}

Text:
{chunk_text}

Return ONLY valid JSON matching this exact structure. No preamble, no markdown.

{{
  "story_found": true or false,
  "source_excerpt": "verbatim quote from the text above, minimum 10 chars, or null",
  "story_summary": "one sentence summary of the story, or null",
  "possible_theme": "one phrase theme label, or null",
  "tier_guess": "tier_1 or tier_2 or tier_3, or null",
  "why_it_might_work": "one sentence on why this story resonates, or null",
  "risk_flags": ["list of risk strings, can be empty array"]
}}

Rules:
- source_excerpt must be copied VERBATIM from the text above. Do not paraphrase.
- If no story exists in this chunk, return story_found: false and null for all other fields.
- tier_1 = has a clear human moment, stakes, and narrative tension.
- tier_2 = interesting but needs development.
- tier_3 = background/context only.
"""


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _execution_fingerprint(chunk_id: str, source_excerpt: str) -> str:
    seed = f"{chunk_id}|{source_excerpt}|{_COMPONENT_NAME}:{_COMPONENT_VERSION}"
    return "sha256:" + _sha256_hex(seed.encode("utf-8"))


def _failure(reason: str) -> Dict[str, Any]:
    return {"status": "failure", "candidates": [], "reason": reason}


def _make_blocked_skeleton(
    chunk: Dict[str, Any],
    *,
    source_id: str,
    source_family: str,
    block_reason: str,
) -> Dict[str, Any]:
    """Skeleton blocked candidate that does NOT need to satisfy the schema.

    Blocked candidates are written to candidates.jsonl for debuggability but
    bypass schema validation. They preserve chunk_id, source, and the block
    reason so a human can locate the original chunk in chunks.jsonl.
    """
    return {
        "story_id": str(uuid.uuid4()),
        "source_id": source_id,
        "source_family": source_family,
        "chunk_id": chunk["chunk_id"],
        "unit_ids": list(chunk.get("unit_ids", [])),
        "page_numbers": list(chunk.get("page_numbers", [])),
        "extraction_model": EXTRACTION_MODEL,
        "extraction_temperature": EXTRACTION_TEMPERATURE,
        "grounded": False,
        "grounded_unit_ids": [],
        "status": "blocked",
        "superseded_by": None,
        "created_at": _now_iso(),
        "block_reason": block_reason,
    }


class StoryExtractor:
    """Extract structured story candidates from chunks via the Anthropic API."""

    def __init__(self, api_caller: Optional[Callable[[str], str]] = None):
        # Tests inject api_caller; production uses the real Anthropic client.
        self._api_caller = api_caller

    def extract_from_source(
        self, source_id: str, repo_root: str
    ) -> Dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        processed_dir, source_family = find_processed_dir(
            repo_root_path, source_id
        )
        if processed_dir is None or source_family is None:
            return _failure("chunks_not_found")
        chunks_path = processed_dir / "stories" / "chunks.jsonl"
        if not chunks_path.is_file():
            return _failure("chunks_not_found")

        chunks: List[Dict[str, Any]] = []
        try:
            with chunks_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    chunks.append(json.loads(line))
        except (OSError, json.JSONDecodeError) as exc:
            return _failure(f"chunks_unreadable: {exc}")
        if not chunks:
            return _failure("chunks_empty")

        if self._api_caller is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                return _failure("api_key_missing")
            try:
                self._api_caller = self._build_default_api_caller()
            except ImportError as exc:
                return _failure(f"anthropic_sdk_missing: {exc}")

        try:
            schema = json.loads(
                schema_path("story_candidate").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError) as exc:
            return _failure(f"schema_unreadable: {exc}")
        validator = jsonschema.Draft202012Validator(schema)

        all_records: List[Dict[str, Any]] = []
        ok_candidates: List[Dict[str, Any]] = []

        for chunk in chunks:
            prompt = PROMPT_TEMPLATE.format(
                source_id=source_id,
                chunk_index=chunk.get("chunk_index"),
                page_numbers=chunk.get("page_numbers", []),
                chunk_text=chunk.get("text", ""),
            )
            try:
                response_text = self._api_caller(prompt)
            except Exception as exc:  # broad: API can raise many error types
                all_records.append(
                    _make_blocked_skeleton(
                        chunk,
                        source_id=source_id,
                        source_family=source_family,
                        block_reason=f"api_error: {type(exc).__name__}: {exc}",
                    )
                )
                continue

            try:
                parsed = json.loads(response_text)
            except (TypeError, json.JSONDecodeError) as exc:
                all_records.append(
                    _make_blocked_skeleton(
                        chunk,
                        source_id=source_id,
                        source_family=source_family,
                        block_reason=f"json_parse_error: {exc}",
                    )
                )
                continue

            if not isinstance(parsed, dict):
                all_records.append(
                    _make_blocked_skeleton(
                        chunk,
                        source_id=source_id,
                        source_family=source_family,
                        block_reason="json_parse_error: response was not a JSON object",
                    )
                )
                continue

            if not parsed.get("story_found"):
                # No story in this chunk — skip silently. Not an error.
                continue

            candidate = self._assemble_candidate(
                parsed, chunk, source_id=source_id, source_family=source_family
            )

            try:
                validator.validate(candidate)
            except jsonschema.ValidationError as exc:
                blocked = _make_blocked_skeleton(
                    chunk,
                    source_id=source_id,
                    source_family=source_family,
                    block_reason=f"schema_violation: {exc.message}",
                )
                # Preserve raw fields so a human can debug what the model said.
                blocked["raw_extraction"] = parsed
                all_records.append(blocked)
                continue

            all_records.append(candidate)
            ok_candidates.append(candidate)

        # Always overwrite (FINDING from RT2: appending produces duplicates).
        out_path = processed_dir / "stories" / "candidates.jsonl"
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", encoding="utf-8") as fh:
                for record in all_records:
                    fh.write(
                        json.dumps(record, sort_keys=True, separators=(",", ":"))
                        + "\n"
                    )
        except OSError as exc:
            return _failure(f"write_error: {exc}")

        return {
            "status": "success",
            "candidates": ok_candidates,
            "all_records": all_records,
            "reason": "",
        }

    def _assemble_candidate(
        self,
        parsed: Dict[str, Any],
        chunk: Dict[str, Any],
        *,
        source_id: str,
        source_family: str,
    ) -> Dict[str, Any]:
        source_excerpt = parsed.get("source_excerpt") or ""
        return {
            "story_id": str(uuid.uuid4()),
            "source_id": source_id,
            "source_family": source_family,
            "chunk_id": chunk["chunk_id"],
            "unit_ids": list(chunk.get("unit_ids", [])),
            "page_numbers": list(chunk.get("page_numbers", [])),
            "source_excerpt": source_excerpt,
            "story_summary": parsed.get("story_summary") or "",
            "possible_theme": parsed.get("possible_theme") or "",
            "tier_guess": parsed.get("tier_guess") or "tier_3",
            "why_it_might_work": parsed.get("why_it_might_work") or "",
            "risk_flags": list(parsed.get("risk_flags") or []),
            "storyworthy_score": {
                "five_second_moment": 0,
                "stakes": 0,
                "central_question": 0,
                "vulnerability": 0,
                "narrative_compression": 0,
                "total": 0,
            },
            "storyworthy_verdict": "reject",
            "extraction_model": EXTRACTION_MODEL,
            "extraction_temperature": EXTRACTION_TEMPERATURE,
            "grounded": False,
            "grounded_unit_ids": [],
            "status": "candidate",
            "superseded_by": None,
            "created_at": _now_iso(),
            "provenance": {
                "produced_by": {
                    "component": _COMPONENT_NAME,
                    "version": _COMPONENT_VERSION,
                },
                "input_artifact_ids": [chunk["chunk_id"]],
                "execution_fingerprint_hash": _execution_fingerprint(
                    chunk["chunk_id"], source_excerpt
                ),
            },
        }

    def _build_default_api_caller(self) -> Callable[[str], str]:
        import anthropic  # imported lazily so tests can run without it

        client = anthropic.Anthropic()

        def _call(prompt: str) -> str:
            message = client.messages.create(
                model=EXTRACTION_MODEL,
                max_tokens=MAX_TOKENS,
                temperature=EXTRACTION_TEMPERATURE,
                messages=[{"role": "user", "content": prompt}],
            )
            # Anthropic SDK returns a list of content blocks; join text blocks.
            parts: List[str] = []
            for block in message.content:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    parts.append(text)
            return "\n".join(parts)

        return _call
