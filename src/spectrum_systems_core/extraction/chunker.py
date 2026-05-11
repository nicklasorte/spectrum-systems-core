"""Chunker: text_units.jsonl -> chunks.jsonl.

Standard library only. No LLM calls. Deterministic. Fail-closed.

Two chunking modes:

1. Speaker-turn mode (transcripts): one chunk per speaker turn. A new chunk
   starts at every line matching a speaker-label pattern; the chunk text is
   the consecutive content lines until the next label. Empty turns
   (label with no content) are skipped. Triggered when ``source_family ==
   "meetings"`` or the source_id contains ``"transcript"``
   (case-insensitive).

2. Character-count mode (everything else, plus transcripts with no
   detected speaker labels): the previous behaviour — overlapping chunks
   of ``CHUNK_SIZE`` text units with ``OVERLAP`` shared boundary unit.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import jsonschema

from ..ingestion._paths import schema_path
from ._paths import find_processed_dir


CHUNK_SIZE = 8
OVERLAP = 1

# Speaker-label pattern: a single line where a speaker name is separated
# from a trailing ``HH:MM`` (or ``H:MM``) timestamp by either a tab or
# three-or-more spaces.
#
# Speaker portion: starts with a letter or ``+`` (phone), contains no
# colons (which excludes content like ``"Action: Bob   4:00"``), no tabs,
# no newlines.
#
# Separator: a tab (any amount of surrounding whitespace) OR three+
# spaces. A 2-space gap is NOT enough — it fires on too much normal
# content like ``"See you at  3:00"``.
#
# Covers task patterns A–D:
#   A: "Firstname Lastname   HH:MM"
#   B: "Firstname Lastname - Role/Org   HH:MM"
#   C: "+1*******XX   HH:MM"
#   D: "Firstname Lastname (Org)   HH:MM"
_SPEAKER_LABEL_RE = re.compile(
    r"^(?P<speaker>[A-Za-z+][^\t\n:]*?)"
    r"(?:[ ]*\t[ \t]*|[ ]{3,})"
    r"(?P<timestamp>\d{1,2}:\d{2})\s*$"
)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _failure(reason: str) -> Dict[str, Any]:
    return {"status": "failure", "chunks": [], "reason": reason}


def _is_transcript(source_family: str, source_id: str) -> bool:
    if source_family == "meetings":
        return True
    return "transcript" in source_id.lower()


class Chunker:
    """Split text_units.jsonl into chunks for story extraction."""

    def chunk(self, source_id: str, repo_root: str) -> Dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        processed_dir, source_family = find_processed_dir(
            repo_root_path, source_id
        )
        if processed_dir is None or source_family is None:
            return _failure("text_units_not_found")
        units_path = processed_dir / "text_units.jsonl"
        if not units_path.is_file():
            return _failure("text_units_not_found")

        units: List[Dict[str, Any]] = []
        try:
            with units_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        units.append(json.loads(line))
                    except json.JSONDecodeError as exc:
                        return _failure(
                            f"text_unit_malformed: invalid json: {exc}"
                        )
        except OSError as exc:
            return _failure(f"text_units_not_found: {exc}")

        if not units:
            return _failure("text_units_empty")

        for u in units:
            if not isinstance(u, dict):
                return _failure("text_unit_malformed: non-object line")
            for key in ("unit_id", "text", "unit_type", "ordinal", "locator"):
                if key not in u:
                    return _failure(f"text_unit_malformed: missing {key}")

        units = sorted(units, key=lambda x: int(x["ordinal"]))

        try:
            chunk_schema = json.loads(
                schema_path("chunk").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError) as exc:
            return _failure(f"chunk_schema_violation: schema unreadable: {exc}")
        validator = jsonschema.Draft202012Validator(chunk_schema)

        chunks: Optional[List[Dict[str, Any]]] = None
        if _is_transcript(source_family, source_id):
            chunks, fallback_reason = self._chunk_by_speaker_turns(
                units, source_id, source_family
            )
            if chunks is None:
                print(
                    f"[chunker] {fallback_reason}: falling back to "
                    f"character chunking for {source_id}"
                )
        if chunks is None:
            chunks = self._chunk_by_character_count(
                units, source_id, source_family
            )

        for chunk in chunks:
            try:
                validator.validate(chunk)
            except jsonschema.ValidationError as exc:
                return _failure(f"chunk_schema_violation: {exc.message}")

        out_dir = processed_dir / "stories"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / "chunks.jsonl"
            with out_path.open("w", encoding="utf-8") as fh:
                for chunk in chunks:
                    fh.write(
                        json.dumps(chunk, sort_keys=True, separators=(",", ":"))
                        + "\n"
                    )
        except OSError as exc:
            return _failure(f"write_error: {exc}")

        return {"status": "success", "chunks": chunks, "reason": ""}

    # -- character-count mode --------------------------------------------

    def _chunk_by_character_count(
        self,
        units: List[Dict[str, Any]],
        source_id: str,
        source_family: str,
    ) -> List[Dict[str, Any]]:
        chunks: List[Dict[str, Any]] = []
        chunk_index = 0
        i = 0
        n = len(units)
        step = CHUNK_SIZE - OVERLAP
        while i < n:
            chunk_units = units[i : i + CHUNK_SIZE]
            # FINDING-C-006: chunks share their boundary unit. The first
            # unit of chunk N is the last unit of chunk N-1.
            overlap_unit_id = chunk_units[0]["unit_id"] if i > 0 else None
            chunk_text = "\n".join(u["text"] for u in chunk_units)
            page_numbers: List[int] = []
            seen_pages: set[int] = set()
            for u in chunk_units:
                locator = u.get("locator", {}) or {}
                page = locator.get("page_number")
                if isinstance(page, int) and page not in seen_pages:
                    seen_pages.add(page)
                    page_numbers.append(page)
            page_numbers.sort()
            chunk = {
                "chunk_id": str(uuid.uuid4()),
                "source_id": source_id,
                "source_family": source_family,
                "chunk_index": chunk_index,
                "unit_ids": [u["unit_id"] for u in chunk_units],
                "text": chunk_text,
                "text_hash": "sha256:" + _sha256_hex(chunk_text.encode("utf-8")),
                "unit_count": len(chunk_units),
                "overlap_unit_id": overlap_unit_id,
                "page_numbers": page_numbers,
                "char_count": len(chunk_text),
            }
            chunks.append(chunk)
            chunk_index += 1
            if len(chunk_units) < CHUNK_SIZE:
                break
            i += step
        return chunks

    # -- speaker-turn mode -----------------------------------------------

    def _chunk_by_speaker_turns(
        self,
        units: List[Dict[str, Any]],
        source_id: str,
        source_family: str,
    ) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
        """Return ``(chunks, None)`` on success, or ``(None, reason)`` for
        the two fallback cases:

        - ``"no_speaker_turns_detected"`` — zero label lines found.
        - ``"all_speaker_turns_empty"`` — labels found but every turn was
          empty (label with no content lines before the next label).

        Operates at the line level so multi-line text units (which can
        occur when the source has speaker labels and content in a single
        paragraph) are handled correctly.
        """
        # Flatten units into a sequence of (unit_id, line_text, page_number)
        # preserving order. A unit's text may contain multiple lines.
        records: List[Tuple[str, str, Optional[int]]] = []
        for u in units:
            text = u.get("text", "")
            locator = u.get("locator", {}) or {}
            page = locator.get("page_number")
            page_int = page if isinstance(page, int) else None
            for line in text.split("\n"):
                records.append((u["unit_id"], line, page_int))

        # Probe for at least one label before doing any work.
        if not any(_SPEAKER_LABEL_RE.match(line) for _, line, _ in records):
            return None, "no_speaker_turns_detected"

        chunks: List[Dict[str, Any]] = []
        chunk_index = 0

        cur_speaker: Optional[str] = None
        cur_timestamp: Optional[str] = None
        cur_unit_ids: List[str] = []
        cur_lines: List[str] = []
        cur_pages: List[int] = []
        seen_pages: set[int] = set()

        def emit_turn() -> int:
            nonlocal chunk_index
            if cur_speaker is None:
                return 0
            # Skip "empty turn" — label with no non-blank content lines.
            if not any(line.strip() for line in cur_lines):
                return 0
            text = "\n".join(cur_lines)
            chunk = {
                "chunk_id": str(uuid.uuid4()),
                "source_id": source_id,
                "source_family": source_family,
                "chunk_index": chunk_index,
                "unit_ids": list(cur_unit_ids),
                "text": text,
                "text_hash": "sha256:" + _sha256_hex(text.encode("utf-8")),
                "unit_count": len(cur_lines),
                "overlap_unit_id": None,
                "page_numbers": sorted(cur_pages),
                "char_count": len(text),
                "speaker": cur_speaker,
                "timestamp": cur_timestamp,
            }
            chunks.append(chunk)
            chunk_index += 1
            return 1

        for unit_id, line, page_number in records:
            match = _SPEAKER_LABEL_RE.match(line)
            if match:
                emit_turn()
                cur_speaker = match.group("speaker").strip()
                cur_timestamp = match.group("timestamp")
                cur_unit_ids = [unit_id]
                cur_lines = []
                cur_pages = []
                seen_pages = set()
                if page_number is not None and page_number not in seen_pages:
                    seen_pages.add(page_number)
                    cur_pages.append(page_number)
                continue

            if cur_speaker is None:
                # Lines before the first speaker label have no owning turn.
                continue

            if unit_id not in cur_unit_ids:
                cur_unit_ids.append(unit_id)
            if page_number is not None and page_number not in seen_pages:
                seen_pages.add(page_number)
                cur_pages.append(page_number)
            cur_lines.append(line)

        emit_turn()

        # Labels were found but every turn was empty (label-only). Fall
        # back to character chunking so the orchestrator's Stage 2
        # artifact-existence check still has a non-empty chunks.jsonl
        # to read.
        if not chunks:
            return None, "all_speaker_turns_empty"
        return chunks, None
