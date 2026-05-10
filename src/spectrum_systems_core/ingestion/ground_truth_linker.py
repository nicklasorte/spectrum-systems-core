"""GroundTruthLinker: pair transcript source_records with minutes_records.

Phase L.2. Reads transcript ``source_record`` artifacts (source_family
``meetings``) from ``store/processed/meetings/<source_id>/source_record.json``
and from flat ``$SDL_ROOT/<artifact_id>.json`` files. Reads minutes
artifacts from ``$SDL_ROOT/minutes/<minutes_id>.json``.

Matching:
  1. Exact ``meeting_date`` match in a 1-vs-1 cardinality → ``ground_truth_pair``
     with ``match_confidence='high'`` and ``status='confirmed'``.
  2. Within ±1 calendar day in a 1-vs-1 cardinality (after exact pass
     leaves both sides unmatched) → ``ground_truth_pair`` with
     ``match_confidence='medium'`` and ``status='pending_review'``.
  3. Anything else → recorded in ``linking_report`` as unmatched. Never
     auto-paired.

Hard guards:
  * ``meeting_date`` of ``None`` on either side never matches anything,
    including another ``None``.
  * Any date with more than one record on EITHER side routes ALL records
    on that date to unmatched (``duplicate_date_collision``). The linker
    refuses to pick.
  * Within the fuzzy ±1-day pass, any record with more than one candidate
    is routed to unmatched (``ambiguous_fuzzy_match``); never paired
    against an arbitrary peer.

linking_report is ALWAYS written, including when zero pairs are produced.
Never raises; always returns a dict.
"""
from __future__ import annotations

import datetime
import json
import os
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import jsonschema

from ._paths import contracts_root
from .date_utils import (
    extract_meeting_date as _extract_meeting_date,
    family_tokens as _family_tokens,
)

PAIR_SCHEMA_VERSION = "1.0.0"
REPORT_SCHEMA_VERSION = "1.0.0"
PRODUCED_BY = "GroundTruthLinker"


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _resolve_sdl_root(data_lake_path: str) -> Optional[Path]:
    env = os.environ.get("SDL_ROOT", "").strip()
    if env:
        p = Path(env)
        if not p.exists():
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError:
                return None
        return p
    if not data_lake_path:
        return None
    base = Path(data_lake_path)
    if not base.exists():
        return None
    return base / "store" / "artifacts"


class GroundTruthLinker:
    """Match transcript source_records to minutes_records by meeting_date."""

    def link(self, data_lake_path: str) -> Dict[str, Any]:
        try:
            return self._link(data_lake_path)
        except Exception as exc:  # defensive: never raise
            return _failure_result(f"unexpected_error:{exc}")

    # -- internals ---------------------------------------------------------

    def _link(self, data_lake_path: str) -> Dict[str, Any]:
        sdl_root = _resolve_sdl_root(data_lake_path)
        if sdl_root is None:
            return _failure_result(
                "sdl_root_unresolved:set SDL_ROOT or pass a valid data_lake_path"
            )

        # Build the existing-pairs index BEFORE any matching or writing so a
        # mid-run write cannot make a freshly-written pair look pre-existing
        # to itself. Non-recursive: retired/ and reports/ subdirs are excluded.
        existing_pairs = _load_existing_pairs(sdl_root)

        transcripts, filtered_records = _load_transcripts(
            data_lake_path, sdl_root
        )
        for f in filtered_records:
            print(
                "[ground_truth_linker] Filtered from transcript candidates "
                f"(contains 'minutes'): {f['title'] or f['raw_path']}"
            )
        minutes = _load_minutes(sdl_root)

        unmatched_t: List[Dict[str, Any]] = []
        unmatched_m: List[Dict[str, Any]] = []

        # 1. Anything with no meeting_date on either side is unmatched up-front.
        t_with_date: List[Dict[str, Any]] = []
        for t in transcripts:
            if t["meeting_date"] is None:
                # _normalize_transcript distinguishes "no candidate strings
                # to extract from" (no_meeting_date) from "candidate strings
                # were present but matched no date pattern"
                # (no_date_extractable). Default safely to no_meeting_date.
                reason = t.get("no_date_reason") or "no_meeting_date"
                unmatched_t.append(_unmatched_t(t, reason))
            else:
                t_with_date.append(t)
        m_with_date: List[Dict[str, Any]] = []
        for m in minutes:
            if m["meeting_date"] is None:
                unmatched_m.append(_unmatched_m(m, "no_meeting_date"))
            else:
                m_with_date.append(m)

        # 2. Bucket by date for the exact-match pass.
        t_by_date: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        m_by_date: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for t in t_with_date:
            t_by_date[t["meeting_date"]].append(t)
        for m in m_with_date:
            m_by_date[m["meeting_date"]].append(m)

        paired_t_ids: set = set()
        paired_m_ids: set = set()
        pairs_high: List[Dict[str, Any]] = []
        pairs_medium: List[Dict[str, Any]] = []

        all_dates_with_either = set(t_by_date.keys()) | set(m_by_date.keys())
        leftover_t: List[Dict[str, Any]] = []
        leftover_m: List[Dict[str, Any]] = []
        for d in sorted(all_dates_with_either):
            ts = t_by_date.get(d, [])
            ms = m_by_date.get(d, [])
            # Same-day collision: use family-token greedy matching to
            # disambiguate. This is the only place we attempt to choose
            # between candidates; outside collisions we still rely on
            # 1T/1M cardinality alone.
            if len(ts) > 1 or len(ms) > 1:
                resolved_high, resolved_medium, leftover = (
                    _resolve_same_day_collision(ts, ms)
                )
                for t, m in resolved_high:
                    pair = _build_pair(t, m, confidence="high")
                    pairs_high.append(pair)
                    paired_t_ids.add(t["source_artifact_id"])
                    paired_m_ids.add(m["minutes_artifact_id"])
                for t, m in resolved_medium:
                    pair = _build_pair(t, m, confidence="medium")
                    pairs_medium.append(pair)
                    paired_t_ids.add(t["source_artifact_id"])
                    paired_m_ids.add(m["minutes_artifact_id"])
                # Anything still unmatched after the collision pass is
                # routed to ``duplicate_date_collision`` — preserving the
                # contract's existing reason code so consumers (and the
                # report schema enum) keep working.
                for t in leftover["transcripts"]:
                    unmatched_t.append(
                        _unmatched_t(t, "duplicate_date_collision")
                    )
                    paired_t_ids.add(t["source_artifact_id"])
                for m in leftover["minutes"]:
                    unmatched_m.append(
                        _unmatched_m(m, "duplicate_date_collision")
                    )
                    paired_m_ids.add(m["minutes_artifact_id"])
                continue
            if len(ts) == 1 and len(ms) == 1:
                pair = _build_pair(ts[0], ms[0], confidence="high")
                pairs_high.append(pair)
                paired_t_ids.add(ts[0]["source_artifact_id"])
                paired_m_ids.add(ms[0]["minutes_artifact_id"])
            else:
                if ts:
                    leftover_t.extend(ts)
                if ms:
                    leftover_m.extend(ms)

        # 3. Fuzzy ±1-day pass on the leftovers. Each record may match
        #    multiple peers within ±1 day; if so, route to unmatched
        #    (``ambiguous_fuzzy_match``) — never pair arbitrarily.
        # Sort leftovers deterministically for stable test outcomes.
        leftover_t_sorted = sorted(
            leftover_t, key=lambda x: (x["meeting_date"], x["source_artifact_id"])
        )
        leftover_m_sorted = sorted(
            leftover_m, key=lambda x: (x["meeting_date"], x["minutes_artifact_id"])
        )

        # Build candidate sets in both directions.
        candidates_for_t: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        candidates_for_m: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for t in leftover_t_sorted:
            td = datetime.date.fromisoformat(t["meeting_date"])
            for m in leftover_m_sorted:
                md = datetime.date.fromisoformat(m["meeting_date"])
                if abs((td - md).days) <= 1 and td != md:
                    candidates_for_t[t["source_artifact_id"]].append(m)
                    candidates_for_m[m["minutes_artifact_id"]].append(t)

        ambiguous_t_ids: set = set()
        ambiguous_m_ids: set = set()
        for t in leftover_t_sorted:
            cands = candidates_for_t.get(t["source_artifact_id"], [])
            if len(cands) > 1:
                ambiguous_t_ids.add(t["source_artifact_id"])
        for m in leftover_m_sorted:
            cands = candidates_for_m.get(m["minutes_artifact_id"], [])
            if len(cands) > 1:
                ambiguous_m_ids.add(m["minutes_artifact_id"])

        for t in leftover_t_sorted:
            if t["source_artifact_id"] in paired_t_ids:
                continue
            cands = candidates_for_t.get(t["source_artifact_id"], [])
            if not cands:
                unmatched_t.append(_unmatched_t(t, "no_candidate"))
                paired_t_ids.add(t["source_artifact_id"])
                continue
            if t["source_artifact_id"] in ambiguous_t_ids:
                unmatched_t.append(_unmatched_t(t, "ambiguous_fuzzy_match"))
                paired_t_ids.add(t["source_artifact_id"])
                # Propagate ambiguity to every candidate minutes record that
                # lists ONLY this transcript. If a candidate M has its own
                # ambiguity (multiple t-candidates), the m-loop's
                # ambiguous_m_ids branch handles it. Otherwise M's only
                # candidate just got blocked — the cause is fuzzy ambiguity,
                # not absence of candidates, so record the reason that
                # matches the cause.
                for cand_m in cands:
                    m_aid = cand_m["minutes_artifact_id"]
                    if m_aid in paired_m_ids:
                        continue
                    if m_aid in ambiguous_m_ids:
                        continue
                    unmatched_m.append(
                        _unmatched_m(cand_m, "ambiguous_fuzzy_match")
                    )
                    paired_m_ids.add(m_aid)
                continue
            # Exactly one candidate from t's side; verify the candidate
            # also has exactly one candidate (this t) before pairing.
            m = cands[0]
            if m["minutes_artifact_id"] in paired_m_ids:
                # The minutes record was already routed elsewhere
                # (collision/ambiguity).
                unmatched_t.append(_unmatched_t(t, "no_candidate"))
                paired_t_ids.add(t["source_artifact_id"])
                continue
            if m["minutes_artifact_id"] in ambiguous_m_ids:
                unmatched_t.append(_unmatched_t(t, "ambiguous_fuzzy_match"))
                paired_t_ids.add(t["source_artifact_id"])
                # The minutes side is handled in the minutes loop below.
                continue
            pair = _build_pair(t, m, confidence="medium")
            pairs_medium.append(pair)
            paired_t_ids.add(t["source_artifact_id"])
            paired_m_ids.add(m["minutes_artifact_id"])

        for m in leftover_m_sorted:
            if m["minutes_artifact_id"] in paired_m_ids:
                continue
            cands = candidates_for_m.get(m["minutes_artifact_id"], [])
            if not cands:
                unmatched_m.append(_unmatched_m(m, "no_candidate"))
                paired_m_ids.add(m["minutes_artifact_id"])
                continue
            if m["minutes_artifact_id"] in ambiguous_m_ids:
                unmatched_m.append(_unmatched_m(m, "ambiguous_fuzzy_match"))
                paired_m_ids.add(m["minutes_artifact_id"])
                continue
            # Counterpart was paired or routed; nothing left for this m.
            unmatched_m.append(_unmatched_m(m, "no_candidate"))
            paired_m_ids.add(m["minutes_artifact_id"])

        # 4. Partition every identified pair into "new" vs "already exists".
        #    An existing pair (keyed by (source_artifact_id,
        #    minutes_artifact_id)) is never overwritten — the prior artifact
        #    stays on disk untouched and we tally it under the relevant
        #    already_* bucket.
        pairs_high_new, already_confirmed_h, already_pending_h = (
            _partition_against_existing(pairs_high, existing_pairs)
        )
        pairs_medium_new, already_confirmed_m, already_pending_m = (
            _partition_against_existing(pairs_medium, existing_pairs)
        )
        pairs_already_confirmed = already_confirmed_h + already_confirmed_m
        pairs_already_pending = already_pending_h + already_pending_m

        # 5. Validate every NEW pair, then write everything atomically-ish.
        try:
            pair_schema = _load_schema("ground_truth_pair")
            report_schema = _load_schema("linking_report")
        except (FileNotFoundError, OSError) as exc:
            return _failure_result(f"schema_unreadable:{exc}")

        pair_validator = jsonschema.Draft202012Validator(pair_schema)
        for pair in (*pairs_high_new, *pairs_medium_new):
            try:
                pair_validator.validate(pair)
            except jsonschema.ValidationError as exc:
                return _failure_result(
                    f"pair_schema_violation:{exc.message}"
                )

        # 6. Build the linking_report. Pair counts in the report reflect the
        #    NEW pairs written this run; the idempotency breakdown fields
        #    carry the already-existing counts.
        run_id = str(uuid.uuid4())
        pairs_new_total = len(pairs_high_new) + len(pairs_medium_new)
        report = {
            "run_id": run_id,
            "created_at": _now_iso(),
            "data_lake_path": str(data_lake_path) if data_lake_path else "",
            "total_transcripts": len(transcripts),
            "total_minutes": len(minutes),
            "high_confidence_pairs": len(pairs_high_new),
            "medium_confidence_pairs": len(pairs_medium_new),
            "pairs_new": pairs_new_total,
            "pairs_already_confirmed": pairs_already_confirmed,
            "pairs_already_pending": pairs_already_pending,
            "unmatched_transcripts": _stable_sorted_unmatched_t(unmatched_t),
            "unmatched_minutes": _stable_sorted_unmatched_m(unmatched_m),
            "schema_version": REPORT_SCHEMA_VERSION,
        }
        try:
            jsonschema.Draft202012Validator(report_schema).validate(report)
        except jsonschema.ValidationError as exc:
            return _failure_result(f"report_schema_violation:{exc.message}")

        # 7. Write NEW pairs and report. Existing pairs are never overwritten:
        #    we already filtered them out in step 4. All-or-nothing: any write
        #    error short-circuits to a failure result without leaving partial
        #    pair writes silently un-reported.
        try:
            pairs_dir = sdl_root / "ground_truth"
            reports_dir = pairs_dir / "reports"
            pairs_dir.mkdir(parents=True, exist_ok=True)
            reports_dir.mkdir(parents=True, exist_ok=True)
            for pair in (*pairs_high_new, *pairs_medium_new):
                target = pairs_dir / f"{pair['pair_id']}.json"
                target.write_text(
                    json.dumps(pair, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            report_path = reports_dir / f"{run_id}_linking_report.json"
            report_path.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            return _failure_result(f"write_error:{exc}")

        partial = bool(unmatched_t or unmatched_m)
        status = "partial" if partial else "success"
        if not pairs_high_new and not pairs_medium_new and partial:
            status = "partial"

        return {
            "status": status,
            "pairs_produced": pairs_new_total,
            "pairs_pending_review": len(pairs_medium_new),
            "pairs_new": pairs_new_total,
            "pairs_already_confirmed": pairs_already_confirmed,
            "pairs_already_pending": pairs_already_pending,
            "unmatched_transcripts": report["unmatched_transcripts"],
            "unmatched_minutes": report["unmatched_minutes"],
            "filtered_from_transcript_source_records": filtered_records,
            "linking_report_path": str(report_path),
            "reason": "",
        }


# -- record loaders ----------------------------------------------------------


def _load_transcripts(
    data_lake_path: str, sdl_root: Path
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Collect transcripts (source_family == 'meetings') from disk.

    Reads BOTH ``store/processed/meetings/<sid>/source_record.json`` and
    flat ``$SDL_ROOT/<artifact_id>.json``. De-duplicates by
    ``payload.source_id``; the ``processed/`` location wins (it is the
    canonical written form by SourceLoader and is more likely to carry
    the up-to-date metadata).

    Returns ``(transcripts, filtered)``. ``filtered`` carries any
    source_record whose ``payload.title`` or ``payload.raw_path``
    contains the substring "minutes" (case-insensitive). These are
    almost certainly minutes documents that were promoted as
    transcripts before PipelineOrchestrator's filename filter shipped;
    pairing them as transcripts would silently produce a wrong pair.
    They are NEVER candidates for transcript matching.
    """
    by_source_id: Dict[str, Dict[str, Any]] = {}

    # processed/ dir.
    if data_lake_path:
        processed_root = Path(data_lake_path) / "store" / "processed" / "meetings"
        if processed_root.is_dir():
            for sid_dir in sorted(processed_root.iterdir()):
                if not sid_dir.is_dir():
                    continue
                rec_path = sid_dir / "source_record.json"
                rec = _read_source_record(rec_path)
                if rec is None:
                    continue
                source_id = _payload_get(rec, "source_id")
                if not source_id:
                    continue
                by_source_id[source_id] = rec

    # SDL_ROOT flat files (meeting source_records only). Skip subdirs
    # (where minutes/ground_truth live).
    if sdl_root.is_dir():
        for path in sorted(sdl_root.glob("*.json")):
            if not path.is_file():
                continue
            rec = _read_source_record(path)
            if rec is None:
                continue
            if _payload_get(rec, "source_family") != "meetings":
                continue
            source_id = _payload_get(rec, "source_id")
            if not source_id:
                continue
            by_source_id.setdefault(source_id, rec)

    transcripts: List[Dict[str, Any]] = []
    filtered: List[Dict[str, Any]] = []
    for source_id, rec in sorted(by_source_id.items()):
        title = _payload_get(rec, "title") or ""
        raw_path = _payload_get(rec, "raw_path") or ""
        if "minutes" in title.lower() or "minutes" in raw_path.lower():
            artifact_id = rec.get("artifact_id", "")
            filtered.append(
                {
                    "source_artifact_id": (
                        artifact_id if isinstance(artifact_id, str) else ""
                    ),
                    "source_id": source_id,
                    "title": title,
                    "raw_path": raw_path,
                    "reason": "filename_contains_minutes_keyword",
                }
            )
            continue
        transcripts.append(_normalize_transcript(rec, source_id))
    return transcripts, filtered


def _read_source_record(path: Path) -> Optional[Dict[str, Any]]:
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(rec, dict):
        return None
    if rec.get("artifact_kind") != "source_record":
        return None
    return rec


def _payload_get(rec: Dict[str, Any], key: str) -> Optional[str]:
    payload = rec.get("payload") if isinstance(rec, dict) else None
    if not isinstance(payload, dict):
        return None
    val = payload.get(key)
    if isinstance(val, str) and val:
        return val
    return None


def _normalize_transcript(rec: Dict[str, Any], source_id: str) -> Dict[str, Any]:
    payload = rec.get("payload", {}) or {}
    title = payload.get("title") if isinstance(payload.get("title"), str) else None
    artifact_id = rec.get("artifact_id", "")
    meeting_date = _extract_transcript_date(payload)
    no_date_reason: Optional[str] = None
    if meeting_date is None:
        # Distinguish "regex never matched any of the candidate strings"
        # (no_date_extractable) from "no candidate strings present at all"
        # (no_meeting_date) so the linker can pick the right unmatched
        # reason later.
        if any(_filename_candidates(payload)):
            no_date_reason = "no_date_extractable"
        else:
            no_date_reason = "no_meeting_date"
    return {
        "source_id": source_id,
        "source_artifact_id": artifact_id if isinstance(artifact_id, str) else "",
        "meeting_date": meeting_date,
        "meeting_name": title,
        "no_date_reason": no_date_reason,
    }


def _filename_candidates(payload: Dict[str, Any]) -> List[str]:
    """Return the ordered candidate strings that may carry the meeting date.

    Order: ``payload.title`` → basename of ``payload.raw_path`` → basename
    of ``payload.processed_path``. Empty / non-string values are dropped.
    """
    out: List[str] = []
    title = payload.get("title")
    if isinstance(title, str) and title.strip():
        out.append(title)
    for key in ("raw_path", "processed_path"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            base = os.path.basename(v.rstrip("/\\"))
            if base:
                out.append(base)
    return out


def _extract_transcript_date(payload: Dict[str, Any]) -> Optional[str]:
    """Extract YYYY-MM-DD from the transcript source_record payload.

    Tries each of ``title``, basename(``raw_path``), basename(``processed_path``)
    in turn (after stripping the file extension) using the shared
    ``date_utils.extract_meeting_date``. Returns ``None`` if no candidate
    yields a date — does NOT fall back to ``payload.metadata.date``,
    which the orchestrator seeds to a Unix-epoch sentinel
    (``"1970-01-01"``) for raw drops without explicit metadata. Falling
    back to that sentinel would make every dateless transcript collide
    on the epoch.
    """
    for candidate in _filename_candidates(payload):
        stem = Path(candidate).stem or candidate
        d = _extract_meeting_date(stem)
        if d is not None:
            return d
        # Some titles include the extension or no extension at all; try
        # the raw candidate too in case ``Path.stem`` over-trims (e.g.
        # the title "Meeting 2026.01.22" would lose ".22" via stem).
        d = _extract_meeting_date(candidate)
        if d is not None:
            return d
    return None


def _load_minutes(sdl_root: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    minutes_dir = sdl_root / "minutes"
    if not minutes_dir.is_dir():
        return out
    seen: set = set()
    for path in sorted(minutes_dir.glob("*.json")):
        if not path.is_file():
            continue
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("provenance", {}).get("produced_by") != "MinutesProcessor":
            continue
        minutes_id = rec.get("minutes_id", "")
        if not isinstance(minutes_id, str) or not minutes_id:
            continue
        if minutes_id in seen:
            continue
        seen.add(minutes_id)
        meeting_date = _normalize_date(rec.get("meeting_date"))
        meeting_name = rec.get("meeting_name") or None
        out.append(
            {
                "minutes_id": minutes_id,
                "minutes_artifact_id": minutes_id,
                "meeting_date": meeting_date,
                "meeting_name": meeting_name if isinstance(meeting_name, str) else None,
            }
        )
    return out


def _normalize_date(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    # Accept YYYY-MM-DD directly.
    try:
        return datetime.date.fromisoformat(s).isoformat()
    except ValueError:
        pass
    # Accept a few common fallback formats.
    for fmt in ("%Y/%m/%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# -- helpers -----------------------------------------------------------------


def _build_pair(
    transcript: Dict[str, Any],
    minutes: Dict[str, Any],
    *,
    confidence: str,
) -> Dict[str, Any]:
    # The pair takes its meeting_date from the transcript side (it's the
    # canonical eval anchor). meeting_name prefers the transcript's title;
    # falls back to the minutes-derived name.
    meeting_date = transcript["meeting_date"]
    meeting_name = (
        transcript.get("meeting_name") or minutes.get("meeting_name") or "untitled"
    )
    status = "confirmed" if confidence == "high" else "pending_review"
    return {
        "pair_id": str(uuid.uuid4()),
        "source_artifact_id": transcript["source_artifact_id"],
        "minutes_artifact_id": minutes["minutes_artifact_id"],
        "meeting_date": meeting_date,
        "meeting_name": meeting_name,
        "match_confidence": confidence,
        "status": status,
        "created_at": _now_iso(),
        "confirmed_at": None,
        "confirmed_by": None,
        "schema_version": PAIR_SCHEMA_VERSION,
        "provenance": {"produced_by": PRODUCED_BY},
    }


def _unmatched_t(t: Dict[str, Any], reason: str) -> Dict[str, Any]:
    return {
        "source_id": t.get("source_id", "") or "",
        "source_artifact_id": t.get("source_artifact_id", "") or "",
        "meeting_date": t.get("meeting_date"),
        "meeting_name": t.get("meeting_name"),
        "reason": reason,
    }


def _unmatched_m(m: Dict[str, Any], reason: str) -> Dict[str, Any]:
    return {
        "minutes_id": m.get("minutes_id", "") or "",
        "minutes_artifact_id": m.get("minutes_artifact_id", "") or "",
        "meeting_date": m.get("meeting_date"),
        "meeting_name": m.get("meeting_name"),
        "reason": reason,
    }


def _stable_sorted_unmatched_t(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        items,
        key=lambda x: (
            x.get("meeting_date") or "",
            x.get("source_id") or "",
            x.get("source_artifact_id") or "",
        ),
    )


def _stable_sorted_unmatched_m(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        items,
        key=lambda x: (
            x.get("meeting_date") or "",
            x.get("minutes_id") or "",
            x.get("minutes_artifact_id") or "",
        ),
    )


def _load_existing_pairs(
    sdl_root: Path,
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Index existing ground_truth_pair artifacts by (source, minutes) ids.

    Reads only ``$SDL_ROOT/ground_truth/*.json`` — non-recursive, so the
    ``retired/`` and ``reports/`` subdirectories are excluded. A retired
    pair must therefore NOT block a new pair for the same
    ``(source_artifact_id, minutes_artifact_id)``.

    Malformed files (unreadable / not JSON / missing the required
    identifying fields) are silently skipped so a single bad file cannot
    cause the linker to fail closed and refuse to write any pairs.
    """
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    pairs_dir = sdl_root / "ground_truth"
    if not pairs_dir.is_dir():
        return out
    for path in sorted(pairs_dir.glob("*.json")):
        if not path.is_file():
            continue
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rec, dict):
            continue
        produced_by = (
            rec.get("provenance", {}).get("produced_by")
            if isinstance(rec.get("provenance"), dict)
            else None
        )
        if produced_by != PRODUCED_BY:
            continue
        src = rec.get("source_artifact_id")
        mid = rec.get("minutes_artifact_id")
        if not isinstance(src, str) or not src:
            continue
        if not isinstance(mid, str) or not mid:
            continue
        # First write wins on duplicate keys (sorted iteration ensures
        # this is deterministic). With idempotency in place going forward
        # there should never be duplicates here for new data, but the
        # bug we are fixing left some on disk.
        out.setdefault((src, mid), rec)
    return out


def _partition_against_existing(
    pairs: List[Dict[str, Any]],
    existing: Dict[Tuple[str, str], Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int, int]:
    """Split ``pairs`` into (new, already_confirmed_count, already_pending_count).

    A pair whose (source_artifact_id, minutes_artifact_id) key is already
    in ``existing`` is dropped from the new-write list. Its prior status
    determines the bucket it's counted in. The existing artifact on disk
    is left untouched.
    """
    new_pairs: List[Dict[str, Any]] = []
    already_confirmed = 0
    already_pending = 0
    for pair in pairs:
        key = (pair["source_artifact_id"], pair["minutes_artifact_id"])
        prior = existing.get(key)
        if prior is None:
            new_pairs.append(pair)
            continue
        status = prior.get("status")
        if status == "confirmed":
            already_confirmed += 1
        elif status == "pending_review":
            already_pending += 1
        else:
            # Unknown / unexpected status on an existing pair — count it
            # under pending so we still report it as "skipped", but never
            # overwrite it.
            already_pending += 1
    return new_pairs, already_confirmed, already_pending


def _load_schema(name: str) -> Dict[str, Any]:
    schema_file = (
        contracts_root() / "schemas" / "ingestion" / f"{name}.schema.json"
    )
    return json.loads(schema_file.read_text(encoding="utf-8"))


def _failure_result(reason: str) -> Dict[str, Any]:
    return {
        "status": "failure",
        "pairs_produced": 0,
        "pairs_pending_review": 0,
        "pairs_new": 0,
        "pairs_already_confirmed": 0,
        "pairs_already_pending": 0,
        "unmatched_transcripts": [],
        "unmatched_minutes": [],
        "filtered_from_transcript_source_records": [],
        "linking_report_path": "",
        "reason": reason,
    }


def _resolve_same_day_collision(
    transcripts: List[Dict[str, Any]],
    minutes: List[Dict[str, Any]],
) -> Tuple[
    List[Tuple[Dict[str, Any], Dict[str, Any]]],
    List[Tuple[Dict[str, Any], Dict[str, Any]]],
    Dict[str, List[Dict[str, Any]]],
]:
    """Greedy family-token best-match for same-day collisions.

    Returns ``(high, medium, leftover)`` where ``leftover['transcripts']``
    and ``leftover['minutes']`` are the records the linker could not
    safely pair. Behaviour:

    * For every ``(transcript, minutes)`` candidate, compute the
      family-token overlap ``len(t_tokens & m_tokens)``.
    * Sort candidates by ``(-overlap, source_artifact_id,
      minutes_artifact_id)`` — overlap descending, then alphabetical on
      artifact ids for a deterministic tie-break.
    * Greedy: walk the sorted list and assign each pair whose endpoints
      are both still free. ``overlap >= 1`` becomes a high-confidence
      pair.
    * After the high-confidence pass, if exactly one transcript and one
      minutes remain unmatched (overlap was zero), pair them as
      medium-confidence (process of elimination — a known collision
      with no other candidates left).
    * Anything still unmatched is returned in ``leftover`` and routed to
      ``duplicate_date_collision`` by the caller. "Wrong pair is worse
      than unmatched."
    """
    if not transcripts or not minutes:
        return [], [], {"transcripts": list(transcripts), "minutes": list(minutes)}

    # Pre-compute family tokens once per record.
    t_tokens = [
        _family_tokens(t.get("meeting_name") or "") for t in transcripts
    ]
    m_tokens = [
        _family_tokens(m.get("meeting_name") or "") for m in minutes
    ]

    candidates: List[Tuple[int, str, str, int, int]] = []
    for ti, t in enumerate(transcripts):
        for mi, m in enumerate(minutes):
            overlap = len(t_tokens[ti] & m_tokens[mi])
            candidates.append(
                (
                    overlap,
                    t.get("source_artifact_id", "") or "",
                    m.get("minutes_artifact_id", "") or "",
                    ti,
                    mi,
                )
            )
    # Sort: overlap DESC, then artifact_id ASC for deterministic tie-break.
    candidates.sort(key=lambda x: (-x[0], x[1], x[2]))

    matched_t: set = set()
    matched_m: set = set()
    high: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for overlap, _t_aid, _m_aid, ti, mi in candidates:
        if overlap < 1:
            break
        if ti in matched_t or mi in matched_m:
            continue
        high.append((transcripts[ti], minutes[mi]))
        matched_t.add(ti)
        matched_m.add(mi)

    leftover_t_idx = [i for i in range(len(transcripts)) if i not in matched_t]
    leftover_m_idx = [i for i in range(len(minutes)) if i not in matched_m]

    medium: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    if len(leftover_t_idx) == 1 and len(leftover_m_idx) == 1:
        ti = leftover_t_idx[0]
        mi = leftover_m_idx[0]
        medium.append((transcripts[ti], minutes[mi]))
        matched_t.add(ti)
        matched_m.add(mi)
        leftover_t_idx = []
        leftover_m_idx = []

    leftover = {
        "transcripts": [transcripts[i] for i in leftover_t_idx],
        "minutes": [minutes[i] for i in leftover_m_idx],
    }
    return high, medium, leftover
