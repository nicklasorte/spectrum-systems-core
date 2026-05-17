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
from typing import Any

import jsonschema

from ..glossary.chunk_position import assign_chunk_positions
from ..ingestion._paths import schema_path
from ._paths import find_processed_dir
from .heuristic_agenda_detector import (
    agenda_items_to_artifact_list,
    assign_agenda_item_ids,
    detect_agenda_items,
)
from .heuristic_agenda_detector import (
    detection_enabled as _agenda_detection_enabled,
)

_LOG = logging.getLogger(__name__)


CHUNK_SIZE = 8
OVERLAP = 1

# Phase R.0. Roughly 30-50 tokens per the research synthesis -- well below
# the 400-512 token recommendation but sufficient to eliminate the 2-68
# char chunks observed on the 7-ghz-downlink-tig-meeting-kickoff
# transcript (pipeline #23). Set conservatively so the merge pass only
# absorbs trivially short turns.
MIN_CHUNK_CHARS: int = 150

# Phase T.4: upper bound applied after the merge pass. Chroma context-rot
# research shows extraction quality degrades past ~2,500 tokens of
# context. Our merged chunks are bounded in character count, not token
# count, so we set the budget conservatively. Operators tune via
# ``MAX_CHUNK_CHARS``; set to a very large value (e.g. 999_999) to
# disable the split pass without reverting code.
MAX_CHUNK_CHARS: int = 2500

# Env var override knobs. Tests use MIN_CHUNK_CHARS_ENV to crank the
# threshold; operators use CHUNK_MERGE_ENABLED to flag-off the entire
# merge pass.
MIN_CHUNK_CHARS_ENV: str = "MIN_CHUNK_CHARS"
MAX_CHUNK_CHARS_ENV: str = "MAX_CHUNK_CHARS"
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


def _resolve_max_chunk_chars() -> int:
    """Return the configured upper-bound or the module default."""
    raw = os.environ.get(MAX_CHUNK_CHARS_ENV, "").strip()
    if not raw:
        return MAX_CHUNK_CHARS
    try:
        value = int(raw)
    except ValueError:
        _LOG.warning(
            "chunk_split_max_chars_invalid: %s=%r -> falling back to %d",
            MAX_CHUNK_CHARS_ENV, raw, MAX_CHUNK_CHARS,
        )
        return MAX_CHUNK_CHARS
    if value <= 0:
        _LOG.warning(
            "chunk_split_max_chars_non_positive: %s=%d -> falling back to %d",
            MAX_CHUNK_CHARS_ENV, value, MAX_CHUNK_CHARS,
        )
        return MAX_CHUNK_CHARS
    return value


def _agenda_boundary(prev_chunk: dict[str, Any], next_chunk: dict[str, Any]) -> bool:
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
    chunks: list[dict[str, Any]],
    min_chars: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
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
    working: list[dict[str, Any]] = [dict(c) for c in chunks]
    pairs: list[dict[str, str]] = []

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
        partner: int | None = None
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


def split_oversized_chunks(
    chunks: list[dict[str, Any]],
    max_chars: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Phase T.4. Split any chunk exceeding ``max_chars`` at the nearest
    speaker-turn boundary below the limit. Returns
    ``(output_chunks, split_log)``.

    Speaker-turn boundaries are detected via the merger's ``"\\n"``
    separator: ``_merge_pair`` joins unit text with a single newline,
    so each newline in a chunk's text is a unit-boundary (which is also
    a speaker-boundary in transcript chunks). If no newline falls
    within ``max_chars``, the chunk is split at ``max_chars`` exactly
    and the entry in ``split_log`` carries
    ``chunk_split_mid_turn: true``.

    Each produced chunk inherits a fresh ``chunk_id`` so a downstream
    citation cannot ambiguously point at "the merged chunk" vs "the
    survivor of the split". The original chunk_id is preserved on the
    log entry only.

    Split chunks below ``MIN_CHUNK_CHARS`` are not re-merged here -- a
    caller wanting that invariant should run ``merge_short_chunks``
    after the split pass. The chunker already does this when it
    invokes both passes.
    """
    if max_chars is None:
        max_chars = _resolve_max_chunk_chars()
    if max_chars <= 0 or not chunks:
        out = [dict(c) for c in chunks]
        for i, c in enumerate(out):
            c["chunk_index"] = i
        return out, []

    working: list[dict[str, Any]] = []
    split_log: list[dict[str, Any]] = []

    for chunk in chunks:
        text = chunk.get("text") or ""
        cc = chunk.get("char_count")
        if not isinstance(cc, int):
            cc = len(text)
        if cc <= max_chars:
            working.append(dict(chunk))
            continue

        pieces = _split_text_at_boundary(text, max_chars)
        produced_ids: list[str] = []
        mid_turn_seen = False
        for piece_text, was_mid_turn in pieces:
            produced = dict(chunk)
            produced["chunk_id"] = str(uuid.uuid4())
            produced["text"] = piece_text
            produced["text_hash"] = (
                "sha256:" + _sha256_hex(piece_text.encode("utf-8"))
            )
            produced["char_count"] = len(piece_text)
            # unit_count is best-effort: we know the piece contains at
            # least one unit, and possibly more. Without re-running the
            # unit assembler we cannot say exactly how many, so we
            # report 1. The unit_ids list inherits from the original
            # chunk because the split crosses unit boundaries; this is
            # acknowledged in the split_log entry.
            produced["unit_count"] = max(1, piece_text.count("\n") + 1)
            working.append(produced)
            produced_ids.append(produced["chunk_id"])
            mid_turn_seen = mid_turn_seen or was_mid_turn

        split_log.append({
            "original_chunk_id": str(chunk.get("chunk_id") or ""),
            "produced_chunk_ids": produced_ids,
            "split_reason": "exceeded_max_chars",
            "chunk_split_mid_turn": bool(mid_turn_seen),
            "original_char_count": int(cc),
            "max_chars": int(max_chars),
        })

    for i, c in enumerate(working):
        c["chunk_index"] = i
    return working, split_log


def _split_text_at_boundary(
    text: str,
    max_chars: int,
) -> list[tuple[str, bool]]:
    """Split ``text`` into pieces each at or below ``max_chars`` chars.

    Boundary preference: the rightmost ``"\\n"`` strictly inside
    ``[1, max_chars]``. If no such boundary exists, the cut is made at
    ``max_chars`` exactly and the returned tuple's second slot is
    ``True`` (mid-turn split). Empty input returns ``[("", False)]`` so
    the caller's downstream ``unit_count`` heuristic still works.
    """
    if not text:
        return [("", False)]
    pieces: list[tuple[str, bool]] = []
    remaining = text
    while len(remaining) > max_chars:
        # Search for the rightmost newline in [1, max_chars].
        cut = remaining.rfind("\n", 1, max_chars + 1)
        if cut == -1:
            pieces.append((remaining[:max_chars], True))
            remaining = remaining[max_chars:]
        else:
            pieces.append((remaining[:cut], False))
            # Skip the newline character itself so the next piece does
            # not start with an empty line.
            remaining = remaining[cut + 1:]
    if remaining:
        pieces.append((remaining, False))
    return pieces


def _first_short(
    chunks: list[dict[str, Any]],
    min_chars: int,
) -> int | None:
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
    prev: dict[str, Any], nxt: dict[str, Any],
) -> dict[str, Any]:
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
    union_units: list[str] = []
    for uid in list(prev.get("unit_ids") or []) + list(nxt.get("unit_ids") or []):
        if uid in seen:
            continue
        seen.add(uid)
        union_units.append(uid)

    # Page numbers: union, sorted ascending (matches existing chunker).
    pages_seen: set = set()
    page_union: list[int] = []
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


def _failure(reason: str) -> dict[str, Any]:
    return {"status": "failure", "chunks": [], "reason": reason}


def _is_transcript(source_family: str, source_id: str) -> bool:
    if source_family == "meetings":
        return True
    return "transcript" in source_id.lower()


def _reconstruct_source_text(units: list[dict[str, Any]]) -> str:
    """Rebuild the raw transcript text from ordered ``text_units``.

    Phase X2.1 wiring: the heuristic agenda detector inspects raw
    transcript lines for agenda-style headers and uses a speaker-turn
    lookahead to validate all-caps candidates. text_units already
    preserve line boundaries (units chunk on paragraph breaks), so a
    newline-joined concatenation is a faithful reconstruction for
    detector purposes. Returns ``""`` when no units carry text.
    """
    parts: list[str] = []
    for u in units:
        text = u.get("text", "")
        if not isinstance(text, str):
            continue
        parts.append(text)
    return "\n".join(parts)


def _update_source_record_chunking_strategy(
    source_record_path: Path,
    chunking_strategy: str,
    *,
    agenda_items: list[dict[str, Any]] | None = None,
) -> None:
    """Write ``payload.chunking_strategy`` (and optionally
    ``payload.agenda_items``) into the on-disk source_record.

    Phase M.4 sets ``chunking_strategy``. Phase X2 follow-up adds the
    optional ``agenda_items`` field so downstream readers can map
    chunk ``agenda_item_id`` values back to a human-readable title.
    Read-modify-write; non-fatal on every failure: the chunker's
    primary output (chunks.jsonl) has already been written by the
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
    dirty = False
    if payload.get("chunking_strategy") != chunking_strategy:
        payload["chunking_strategy"] = chunking_strategy
        dirty = True
    if agenda_items is not None and payload.get("agenda_items") != agenda_items:
        payload["agenda_items"] = agenda_items
        dirty = True
    if not dirty:
        return
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

    def chunk(self, source_id: str, repo_root: str) -> dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        processed_dir, source_family = find_processed_dir(
            repo_root_path, source_id
        )
        if processed_dir is None or source_family is None:
            return _failure("text_units_not_found")
        units_path = processed_dir / "text_units.jsonl"
        if not units_path.is_file():
            return _failure("text_units_not_found")

        units: list[dict[str, Any]] = []
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

        chunks: list[dict[str, Any]] | None = None
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
        merge_pairs: list[dict[str, str]] = []
        min_chunk_chars_used = _resolve_min_chunk_chars()
        if _merge_enabled():
            chunks, merge_pairs = merge_short_chunks(
                chunks, min_chars=min_chunk_chars_used,
            )
        merged_chunk_count = len(chunks)

        # Phase T.4: cap any merged chunk that grew past MAX_CHUNK_CHARS.
        # The split pass runs AFTER merge so an extreme worst case is
        # two short turns merging into a 1500-char chunk and then a
        # second pass running the split. We then re-run the merge pass
        # so trailing split pieces below MIN_CHUNK_CHARS get absorbed.
        max_chunk_chars_used = _resolve_max_chunk_chars()
        chunks, split_log = split_oversized_chunks(
            chunks, max_chars=max_chunk_chars_used,
        )
        post_split_count = len(chunks)
        if _merge_enabled() and split_log:
            # Second merge pass is fail-OPEN: any failure leaves the
            # split survivors in place. The merge pass is idempotent
            # so re-running on a clean list is a no-op.
            chunks, _post_split_merge_pairs = merge_short_chunks(
                chunks, min_chars=min_chunk_chars_used,
            )

        # Phase W (integration wiring): chunk_position MUST be computed
        # AFTER all merge AND split passes are complete because the
        # position is proportional to the final chunk count. Do not
        # reorder: a future engineer running assign_chunk_positions
        # before the merge/split passes would label positions on a
        # different chunk list than what ships in chunks.jsonl, so
        # downstream attention-direction wiring would target the wrong
        # chunks.
        chunks = assign_chunk_positions(chunks)

        # Phase X2.1 wiring: assign agenda_item_id to every chunk AFTER
        # chunk_position so the position field is already on the chunk
        # when the assignment runs. Order is mandatory:
        #   merge_short_chunks -> split_oversized_chunks ->
        #   merge_short_chunks (re-merge) -> assign_chunk_positions ->
        #   assign_agenda_item_ids
        # The detector returns [] when AGENDA_DETECTION_ENABLED=false;
        # in that mode we preserve pre-X2 behaviour and leave
        # agenda_item_id absent on the chunk (the field is optional in
        # the chunk schema). When enabled, agenda_item_id is ALWAYS a
        # non-empty string (per CLAUDE.md amendment).
        agenda_artifact_list: list[dict[str, Any]] | None = None
        if _agenda_detection_enabled():
            source_text = _reconstruct_source_text(units)
            agenda_items = detect_agenda_items(source_text)
            chunks = assign_agenda_item_ids(chunks, agenda_items)
            agenda_artifact_list = agenda_items_to_artifact_list(agenda_items)

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

        # Phase T.4: chunk_split_summary artifact. Always written so a
        # run with zero splits is distinguishable from a run that
        # skipped the split pass.
        split_summary_path = self._write_chunk_split_summary(
            processed_dir=processed_dir,
            source_id=source_id,
            original_chunk_count=merged_chunk_count,
            split_chunk_count=post_split_count,
            split_log=split_log,
            max_chunk_chars=max_chunk_chars_used,
        )

        # Phase M.4: persist the chunking_strategy back into the
        # source_record so downstream readers (EvalAligner, eval_summary
        # grouping) can attribute coverage/precision to the strategy used.
        # Failure to update the source_record is non-fatal: chunks.jsonl is
        # the canonical output of this step.
        _update_source_record_chunking_strategy(
            processed_dir / "source_record.json",
            chunking_strategy,
            agenda_items=agenda_artifact_list,
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
            "chunks_split": len(split_log),
            "post_split_count": post_split_count,
            "split_log": split_log,
            "chunk_merge_summary_path": (
                str(merge_summary_path) if merge_summary_path else ""
            ),
            "chunk_split_summary_path": (
                str(split_summary_path) if split_summary_path else ""
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
        merge_pairs: list[dict[str, str]],
        min_chunk_chars: int,
    ) -> Path | None:
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

    # -- chunk_split_summary artifact (Phase T.4) ------------------------

    def _write_chunk_split_summary(
        self,
        *,
        processed_dir: Path,
        source_id: str,
        original_chunk_count: int,
        split_chunk_count: int,
        split_log: list[dict[str, Any]],
        max_chunk_chars: int,
    ) -> Path | None:
        """Persist the chunk_split_summary artifact.

        Co-located with chunks.jsonl and chunk_merge_summary.json so an
        operator triaging a run sees the merge → split → final-chunks
        progression in one directory. Write failure is non-fatal; the
        chunks themselves are the canonical output.
        """
        artifact = {
            "artifact_type": "chunk_split_summary",
            "schema_version": "1.0.0",
            "source_id": source_id,
            "max_chunk_chars": int(max_chunk_chars),
            "original_chunk_count": int(original_chunk_count),
            "split_chunk_count": int(split_chunk_count),
            "chunks_split": int(len(split_log)),
            "split_log": list(split_log),
            "created_at": _now_iso(),
        }
        try:
            from ..validation import (
                ArtifactValidationError,
                validate_artifact,
            )
            try:
                validate_artifact(artifact, "chunk_split_summary")
            except ArtifactValidationError as exc:
                _LOG.warning(
                    "chunk_split_summary_schema_violation: %s", exc,
                )
        except ImportError:
            pass

        try:
            out_dir = processed_dir / "stories"
            out_dir.mkdir(parents=True, exist_ok=True)
            target = out_dir / "chunk_split_summary.json"
            target.write_text(
                json.dumps(artifact, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return target
        except OSError as exc:
            _LOG.warning(
                "chunk_split_summary_write_failed: %s", exc,
            )
            return None

    # -- character-count mode --------------------------------------------

    def _chunk_by_character_count(
        self,
        units: list[dict[str, Any]],
        source_id: str,
        source_family: str,
    ) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
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
            page_numbers: list[int] = []
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
        units: list[dict[str, Any]],
        source_id: str,
        source_family: str,
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
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
        records: list[tuple[str, str, int | None]] = []
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

        chunks: list[dict[str, Any]] = []
        chunk_index = 0

        cur_speaker: str | None = None
        cur_timestamp: str | None = None
        cur_unit_ids: list[str] = []
        cur_lines: list[str] = []
        cur_pages: list[int] = []
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
