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

Phase R.0: After chunks are produced, sub-threshold chunks (text shorter
than ``MIN_CHUNK_CHARS``) are merged into a neighbour before chunks.jsonl
is written. This eliminates the 2-68 char chunks that caused pipeline #23
empty-response failures upstream of guard_empty_response (Phase X). The
merge pass is bypass-able via ``CHUNK_MERGE_ENABLED=false`` env var.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import jsonschema

from ..ingestion._paths import schema_path
from ._paths import find_processed_dir


_LOG = logging.getLogger(__name__)


CHUNK_SIZE = 8
OVERLAP = 1

# Phase R.0. Roughly 30-50 tokens per the research synthesis -- well below
# the 400-512 token recommendation but sufficient to eliminate the 2-68
# char chunks observed on the 7-ghz-downlink-tig-meeting-kickoff
# transcript (pipeline #23). Set conservatively so the merge pass only
# absorbs trivially short turns.
MIN_CHUNK_CHARS: int = 150

# Env var override knobs. Tests use MIN_CHUNK_CHARS_ENV to crank the
# threshold; operators use CHUNK_MERGE_ENABLED to flag-off the entire
# merge pass.
MIN_CHUNK_CHARS_ENV: str = "MIN_CHUNK_CHARS"
CHUNK_MERGE_ENABLED_ENV: str = "CHUNK_MERGE_ENABLED"
_DISABLED_VALUES: frozenset = frozenset({"false", "0", "no", "off"})


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _resolve_min_chunk_chars() -> int:
    """Read MIN_CHUNK_CHARS from env or fall back to the constant.

    A non-integer / negative env var falls back to the module default
    rather than disabling the merge pass silently.
    """
    raw = os.environ.get(MIN_CHUNK_CHARS_ENV, "").strip()
    if not raw:
        return MIN_CHUNK_CHARS
    try:
        value = int(raw)
    except ValueError:
        _LOG.warning(
            "chunk_merge_min_chars_invalid: %s=%r -> falling back to %d",
            MIN_CHUNK_CHARS_ENV, raw, MIN_CHUNK_CHARS,
        )
        return MIN_CHUNK_CHARS
    if value < 0:
        _LOG.warning(
            "chunk_merge_min_chars_negative: %s=%d -> falling back to %d",
            MIN_CHUNK_CHARS_ENV, value, MIN_CHUNK_CHARS,
        )
        return MIN_CHUNK_CHARS
    return value


def _merge_enabled() -> bool:
    raw = os.environ.get(CHUNK_MERGE_ENABLED_ENV, "").strip().lower()
    if raw in _DISABLED_VALUES:
        return False
    return True


def _agenda_boundary(prev_chunk: Dict[str, Any], next_chunk: Dict[str, Any]) -> bool:
    """Return True when ``prev`` and ``next`` cross an agenda boundary.

    Phase R.0 rule 4: never merge across a strong agenda-boundary signal.
    Until Phase W's agenda detector is wired into the chunker (it runs
    later in the typed_extraction stage), the only available signal is a
    differing ``agenda_item_id`` set on the chunk. When the field is
    absent on either side, no boundary is asserted.
    """
    a = prev_chunk.get("agenda_item_id")
    b = next_chunk.get("agenda_item_id")
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    if not a or not b:
        return False
    return a != b


def merge_short_chunks(
    chunks: List[Dict[str, Any]],
    min_chars: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Merge any chunk below ``min_chars`` into its nearest neighbour.

    Phase R.0. Rules:

    1. Prefer merging with the preceding chunk (same speaker context).
    2. If no preceding chunk exists, merge with the following chunk.
    3. After merge, re-check: the merged chunk may now be below threshold
       if both components were short. Repeat until stable.
    4. Never merge across an agenda-boundary signal (differing
       ``agenda_item_id``).
    5. The merged chunk's speaker is the speaker of the FIRST component.
    6. The merged chunk's unit_ids is the UNION of both components'
       unit_ids (set semantics; ordering preserved from prev then next).
    7. The merged chunk's text is the concatenation with a single
       ``"\\n"`` separator.
    8. ``chunk_index`` is rewritten sequentially over the survivors.

    Returns ``(merged_chunks, merge_pairs)``. ``merge_pairs`` records
    every absorption that happened, in the order it happened.
    """
    if min_chars is None:
        min_chars = _resolve_min_chunk_chars()
    if min_chars <= 0 or not chunks:
        # Re-index defensively so callers can rely on contiguous indices.
        out = [dict(c) for c in chunks]
        for i, c in enumerate(out):
            c["chunk_index"] = i
        return out, []

    # Work on shallow copies so the caller's chunk dicts are not mutated.
    working: List[Dict[str, Any]] = [dict(c) for c in chunks]
    pairs: List[Dict[str, str]] = []

    # Iterate to a fixed point. Each pass merges every short chunk into
    # whichever neighbour the rules dictate; the resulting chunk may
    # still be short (both components below threshold) -- the outer loop
    # catches that on the next pass.
    while True:
        idx = _first_short(working, min_chars)
        if idx is None:
            break
        # Choose merge partner.
        prev_idx = idx - 1
        next_idx = idx + 1
        partner: Optional[int] = None
        absorbed = working[idx]
        if prev_idx >= 0 and not _agenda_boundary(working[prev_idx], absorbed):
            partner = prev_idx
        elif next_idx < len(working) and not _agenda_boundary(absorbed, working[next_idx]):
            partner = next_idx
        else:
            # Cannot merge in either direction (agenda boundaries on both
            # sides or singleton chunk). Leave as-is; the orchestrator's
            # downstream gates (Phase X guard_empty_response) will catch
            # the empty response if the model returns nothing.
            _LOG.info(
                "chunk_merge_blocked_by_agenda_boundary: chunk_id=%s",
                absorbed.get("chunk_id"),
            )
            break

        if partner < idx:
            merged = _merge_pair(working[partner], absorbed)
            pairs.append({
                "absorbed_chunk_id": str(absorbed.get("chunk_id", "")),
                "into_chunk_id": str(working[partner].get("chunk_id", "")),
                "reason": "below_min_chars",
            })
            working[partner] = merged
            working.pop(idx)
        else:  # partner == idx + 1
            merged = _merge_pair(absorbed, working[partner])
            # When merging forward, the survivor keeps the FIRST
            # component's chunk_id (per rule 5: speaker/identity comes
            # from the first component).
            pairs.append({
                "absorbed_chunk_id": str(working[partner].get("chunk_id", "")),
                "into_chunk_id": str(absorbed.get("chunk_id", "")),
                "reason": "below_min_chars",
            })
            working[idx] = merged
            working.pop(partner)

    # Re-index after all merges so the survivor's chunk_index is
    # contiguous from 0.
    for i, c in enumerate(working):
        c["chunk_index"] = i
    return working, pairs


def _first_short(
    chunks: List[Dict[str, Any]],
    min_chars: int,
) -> Optional[int]:
    """Return the index of the first below-threshold chunk, or None."""
    for i, c in enumerate(chunks):
        # char_count is the schema field; fall back to len(text) when
        # the field is absent (defensive: callers may pass partial dicts
        # in tests).
        cc = c.get("char_count")
        if not isinstance(cc, int):
            cc = len(c.get("text") or "")
        if cc < min_chars:
            return i
    return None


def _merge_pair(
    prev: Dict[str, Any], nxt: Dict[str, Any],
) -> Dict[str, Any]:
    """Combine two adjacent chunks into a single chunk.

    The survivor inherits ``prev``'s identity (chunk_id, speaker,
    timestamp, source_id, source_family, page_numbers ordering) and
    accumulates ``nxt``'s content.
    """
    merged = dict(prev)
    prev_text = prev.get("text") or ""
    next_text = nxt.get("text") or ""
    merged_text = prev_text + "\n" + next_text if prev_text and next_text else (prev_text or next_text)

    # Union of unit_ids preserving order from prev then nxt; dedupe to
    # honour the "set" semantics in rule 6.
    seen: set = set()
    union_units: List[str] = []
    for uid in list(prev.get("unit_ids") or []) + list(nxt.get("unit_ids") or []):
        if uid in seen:
            continue
        seen.add(uid)
        union_units.append(uid)

    # Page numbers: union, sorted ascending (matches existing chunker).
    pages_seen: set = set()
    page_union: List[int] = []
    for p in list(prev.get("page_numbers") or []) + list(nxt.get("page_numbers") or []):
        if isinstance(p, int) and p not in pages_seen:
            pages_seen.add(p)
            page_union.append(p)
    page_union.sort()

    merged["text"] = merged_text
    merged["text_hash"] = "sha256:" + _sha256_hex(merged_text.encode("utf-8"))
    merged["unit_ids"] = union_units
    merged["unit_count"] = len(union_units)
    merged["char_count"] = len(merged_text)
    merged["page_numbers"] = page_union
    # Speaker / timestamp come from prev per rule 5; nothing to do
    # because ``merged = dict(prev)`` already carries those keys.

    return merged

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


def _update_source_record_chunking_strategy(
    source_record_path: Path, chunking_strategy: str
) -> None:
    """Write ``payload.chunking_strategy`` into the on-disk source_record.

    Phase M.4. Read-modify-write the source_record JSON to record which
    chunking strategy this run produced. Non-fatal on every failure: the
    chunker's primary output (chunks.jsonl) has already been written by the
    caller, so any IO error here is logged and swallowed rather than
    failing the whole chunking step.
    """
    if not source_record_path.is_file():
        return
    try:
        rec = json.loads(source_record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"[chunker] could not update chunking_strategy on "
            f"{source_record_path}: {exc}"
        )
        return
    if not isinstance(rec, dict):
        return
    payload = rec.get("payload")
    if not isinstance(payload, dict):
        return
    if payload.get("chunking_strategy") == chunking_strategy:
        return
    payload["chunking_strategy"] = chunking_strategy
    try:
        source_record_path.write_text(
            json.dumps(rec, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        print(
            f"[chunker] could not write chunking_strategy update to "
            f"{source_record_path}: {exc}"
        )


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
        chunking_strategy: str = "character_count_fallback"
        if _is_transcript(source_family, source_id):
            chunks, fallback_reason = self._chunk_by_speaker_turns(
                units, source_id, source_family
            )
            if chunks is None:
                print(
                    f"[chunker] {fallback_reason}: falling back to "
                    f"character chunking for {source_id}"
                )
            else:
                chunking_strategy = "speaker_turn"
        if chunks is None:
            chunks = self._chunk_by_character_count(
                units, source_id, source_family
            )

        # Phase R.0: merge sub-threshold chunks BEFORE the schema check
        # and BEFORE chunks.jsonl is written. This is the only point in
        # the pipeline where the chunks have not yet flowed downstream,
        # so it is the only place the merge can have an effect (RT1
        # finding: doing the merge after write is a no-op).
        original_chunk_count = len(chunks)
        merge_pairs: List[Dict[str, str]] = []
        min_chunk_chars_used = _resolve_min_chunk_chars()
        if _merge_enabled():
            chunks, merge_pairs = merge_short_chunks(
                chunks, min_chars=min_chunk_chars_used,
            )
        merged_chunk_count = len(chunks)

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

        # Phase R.0: chunk_merge_summary artifact. Even when the merge
        # pass is disabled or no chunks were absorbed, we write the
        # artifact so the run is self-describing (the operator can see
        # the merge ran and found nothing, vs. the merge was disabled).
        merge_summary_path = self._write_chunk_merge_summary(
            processed_dir=processed_dir,
            source_id=source_id,
            original_chunk_count=original_chunk_count,
            merged_chunk_count=merged_chunk_count,
            merge_pairs=merge_pairs,
            min_chunk_chars=min_chunk_chars_used,
        )

        # Phase M.4: persist the chunking_strategy back into the
        # source_record so downstream readers (EvalAligner, eval_summary
        # grouping) can attribute coverage/precision to the strategy used.
        # Failure to update the source_record is non-fatal: chunks.jsonl is
        # the canonical output of this step.
        _update_source_record_chunking_strategy(
            processed_dir / "source_record.json", chunking_strategy
        )

        return {
            "status": "success",
            "chunks": chunks,
            "chunking_strategy": chunking_strategy,
            "reason": "",
            "original_chunk_count": original_chunk_count,
            "merged_chunk_count": merged_chunk_count,
            "chunks_merged": original_chunk_count - merged_chunk_count,
            "merge_pairs": merge_pairs,
            "chunk_merge_summary_path": (
                str(merge_summary_path) if merge_summary_path else ""
            ),
        }

    # -- chunk_merge_summary artifact (Phase R.0) ------------------------

    def _write_chunk_merge_summary(
        self,
        *,
        processed_dir: Path,
        source_id: str,
        original_chunk_count: int,
        merged_chunk_count: int,
        merge_pairs: List[Dict[str, str]],
        min_chunk_chars: int,
    ) -> Optional[Path]:
        """Persist the chunk_merge_summary artifact alongside chunks.jsonl.

        Writing it next to ``chunks.jsonl`` keeps the merge bookkeeping
        co-located with the chunks the operator inspects when triaging a
        run. Failure to write is non-fatal: chunks.jsonl is the canonical
        output, the summary is a forensic mirror.
        """
        artifact = {
            "artifact_type": "chunk_merge_summary",
            "schema_version": "1.0.0",
            "source_id": source_id,
            "min_chunk_chars": int(min_chunk_chars),
            "original_chunk_count": int(original_chunk_count),
            "merged_chunk_count": int(merged_chunk_count),
            "chunks_merged": int(original_chunk_count - merged_chunk_count),
            "merge_pairs": list(merge_pairs),
            "created_at": _now_iso(),
        }
        # Validate via the central gate so a malformed summary cannot
        # silently ship. Validation failures are logged but never raise
        # -- the chunks themselves are the durable signal.
        try:
            from ..validation import (
                ArtifactValidationError,
                validate_artifact,
            )
            try:
                validate_artifact(artifact, "chunk_merge_summary")
            except ArtifactValidationError as exc:
                _LOG.warning(
                    "chunk_merge_summary_schema_violation: %s", exc,
                )
        except ImportError:
            # Validation module unavailable; continue.
            pass

        try:
            out_dir = processed_dir / "stories"
            out_dir.mkdir(parents=True, exist_ok=True)
            target = out_dir / "chunk_merge_summary.json"
            target.write_text(
                json.dumps(artifact, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return target
        except OSError as exc:
            _LOG.warning(
                "chunk_merge_summary_write_failed: %s", exc,
            )
            return None

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
